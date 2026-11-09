"""Quota enforcement shares live rules with admission and the usage-lines API."""

import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from omnigent.db.db_models import workspace_scope
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.server.session_choice import FloorOff, parse_session_choice
from omnigent.server.session_choice_runtime import (
    enforce_stop_lines,
    refuse_if_stopped,
    session_products,
    stop_targets,
    stopped_line,
    tile_of_session,
)
from omnigent.server.user_preferences_store import SqlAlchemyUserPreferencesStore
from tests.server.test_agent_quota import usage_report
from tests.server.test_session_choice import document


@pytest.fixture
def stop_env(app, db_uri):
    now = time.time()
    prefs = SqlAlchemyUserPreferencesStore(db_uri)
    app.state.user_preferences_store = prefs
    service = app.state.agent_quota_service
    service.preferences = prefs
    value = document()
    value["limits"] = {
        "account-a": {"reserve": {"mode": "total", "window": "weekly", "stopPct": 5}}
    }
    prefs.replace_session_choice(
        RESERVED_USER_LOCAL, value, prefs.session_choice(RESERVED_USER_LOCAL)["revision"]
    )
    service.report_account(
        RESERVED_USER_LOCAL,
        "account-a",
        usage_report(
            account_product="codex",
            observed_at=now,
            windows=[
                {
                    "id": "weekly",
                    "kind": "weekly",
                    "percent_used": 96,
                    "window_minutes": 10080,
                    "resets_at": now + 86400,
                }
            ],
        ),
        now=now,
    )
    store = app.state.conversation_store
    conv = store.create_conversation(harness_override="codex-native")
    store.set_runner_id(conv.id, "a" * 32)
    store.touch_runner_liveness(["a" * 32], int(now))
    store.set_session_live_status(conv.id, "running")
    return SimpleNamespace(
        app=app,
        prefs=prefs,
        service=service,
        store=store,
        conv=store.get_conversation(conv.id),
        now=now,
        value=value,
    )


def test_live_refusal_release_and_unknown_fail_open(stop_env):
    env = stop_env
    assert stopped_line(env.app, env.conv).state == "stop"
    with pytest.raises(OmnigentError) as error:
        refuse_if_stopped(env.app, env.conv)
    assert error.value.code == ErrorCode.QUOTA_STOPPED
    env.prefs.set_choice_floor_off(RESERVED_USER_LOCAL, "account-a", FloorOff(until="manual"))
    assert stopped_line(env.app, env.conv) is None
    env.prefs.set_choice_floor_off(RESERVED_USER_LOCAL, "account-a", None)
    env.service.pool_readings = lambda *args, **kwargs: {}
    assert stopped_line(env.app, env.conv) is None


def test_targets_exclude_idle_stale_mirrors_and_ambiguous_product(stop_env):
    env = stop_env
    settings = parse_session_choice(env.value)
    from omnigent.server.agent_quota import QuotaContext

    readings = env.service.pool_readings(QuotaContext(RESERVED_USER_LOCAL, None))
    assert tile_of_session(env.conv, settings, readings, {}) == "account-a"
    reading = readings["account-a"]
    ambiguous = {**readings, "other": reading.model_copy(update={"id": "other"})}
    assert tile_of_session(env.conv, settings, ambiguous, {}) is None
    cases = [
        replace(env.conv, id="idle", live_status="idle"),
        replace(env.conv, id="stale", runner_last_seen=int(env.now - 301)),
        replace(env.conv, id="missing", runner_last_seen=None),
        replace(
            env.conv, id="mirror", labels={"omnigent.wrapper": "claude-code-native-ui-subagent"}
        ),
        replace(env.conv, id="child", parent_conversation_id=env.conv.id),
    ]
    # A workspace listing includes children without direct grants; ownership follows parents.
    store = SimpleNamespace(
        list_conversations=lambda **kw: SimpleNamespace(
            data=[env.conv, *cases], has_more=False, last_id=None
        )
    )
    app = SimpleNamespace(state=SimpleNamespace(conversation_store=store, permission_store=None))
    assert stop_targets(
        app, RESERVED_USER_LOCAL, settings, readings, {}, {"account-a"}, env.now
    ) == {"account-a": (env.conv.id, "child")}


@pytest.mark.asyncio
async def test_loop_repeats_one_note_and_honors_release_during_round(stop_env, monkeypatch):
    env = stop_env
    post = AsyncMock(return_value={"queued": False})
    monkeypatch.setattr(env.app.state, "quota_stop_post_event", post)
    await enforce_stop_lines(env.app)
    await enforce_stop_lines(env.app)
    assert post.await_count == 2
    notes = env.store.list_items(env.conv.id).data
    assert len(notes) == 1
    assert "Stopped: account-a" in notes[0].data.content[0]["text"]
    original = env.store.list_conversations

    def release(**kwargs):
        page = original(**kwargs)
        env.prefs.set_choice_floor_off(RESERVED_USER_LOCAL, "account-a", FloorOff(until="manual"))
        return page

    monkeypatch.setattr(env.store, "list_conversations", release)
    await enforce_stop_lines(env.app)
    assert post.await_count == 2


@pytest.mark.asyncio
async def test_unreachable_stop_retries_next_round(stop_env, monkeypatch):
    env = stop_env
    other = env.store.create_conversation(harness_override="codex-native", runner_id="e" * 32)
    env.store.touch_runner_liveness(["e" * 32], int(env.now))
    env.store.set_session_live_status(other.id, "running")
    failed = []

    async def send(request, sid, event, **kwargs):
        if sid == env.conv.id and not failed:
            failed.append(sid)
            raise RuntimeError("offline")
        return {"queued": False}

    post = AsyncMock(side_effect=send)
    monkeypatch.setattr(env.app.state, "quota_stop_post_event", post)
    await enforce_stop_lines(env.app)
    assert env.store.list_items(env.conv.id).data == []
    assert len(env.store.list_items(other.id).data) == 1
    await enforce_stop_lines(env.app)
    assert len(env.store.list_items(env.conv.id).data) == 1


def test_persisted_owners_across_workspaces(stop_env):
    env = stop_env
    with workspace_scope(17):
        env.prefs.replace_session_choice(
            RESERVED_USER_LOCAL,
            env.value,
            env.prefs.session_choice(RESERVED_USER_LOCAL)["revision"],
        )
    owners = env.prefs.stop_line_owners()
    assert {row[0] for row in owners} == {0, 17}


@pytest.mark.asyncio
async def test_lines_route_running_counts_and_no_listing_by_default(stop_env, client, monkeypatch):
    env = stop_env
    plain = await client.get("/v1/me/usage/lines")
    assert plain.status_code == 200, plain.text
    body = plain.json()
    assert body["schema"] == 1
    assert body["tiles"]["account-a"]["state"] == "stop"
    assert "running" not in body["tiles"]["account-a"]
    running = await client.get("/v1/me/usage/lines?running=1")
    assert running.json()["tiles"]["account-a"]["running"] == [env.conv.id]


@pytest.mark.asyncio
@pytest.mark.parametrize("event", ["message", "compact"])
async def test_event_refused_before_runner_work(stop_env, client, event):
    response = await client.post(
        f"/v1/sessions/{stop_env.conv.id}/events",
        json={
            "type": event,
            "data": {"role": "user", "content": [{"type": "input_text", "text": "work"}]},
        },
    )
    assert response.status_code == 409, response.text
    assert "quota_stopped" in response.text


@pytest.mark.parametrize("off,expected", [(True, "ASK"), (False, "ALLOW")])
def test_agent_lifting_floor_asks(off, expected):
    from omnigent.policies.builtins.safety import ask_on_session_choice_update

    result = ask_on_session_choice_update(
        {"type": "tool_call", "target": "sys_quota_floor", "data": {"arguments": {"off": off}}}
    )
    assert result["result"] == expected


@pytest.mark.asyncio
async def test_native_request_denied(stop_env, client):
    response = await client.post(
        f"/v1/sessions/{stop_env.conv.id}/policies/evaluate",
        json={"event": {"type": "PHASE_REQUEST", "data": {}}},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"result": "POLICY_ACTION_DENY", "reason": "quota_stopped"}


@pytest.mark.asyncio
async def test_auto_rotation_drops_stopped_notice(db_uri):
    from omnigent.server.auto_rotation import NOTICED_COUNT_LABEL_KEY
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
    from tests.server.test_auto_rotation_notices import due_session, service

    store = SqlAlchemyConversationStore(db_uri)
    conv = due_session(store)
    posts = []
    notices = service(store, None, posts, ["idle"], [])

    async def refused(*args, **kwargs):
        raise OmnigentError("stopped", code=ErrorCode.QUOTA_STOPPED)

    notices._post = refused
    await notices.start(SimpleNamespace())
    try:
        notices.mark_due(conv.id, 1)
        await notices._deliveries[(0, conv.id)]
        assert (0, conv.id) not in notices._due
        assert NOTICED_COUNT_LABEL_KEY not in store.get_conversation(conv.id).labels
    finally:
        await notices.shutdown()


def test_value_only_floor_expires_on_reset():
    from omnigent.server.session_choice import floor_off_active
    from omnigent.usage.contract import UsageAccountView, UsageWindowView

    reading = UsageAccountView(
        id="a",
        kind="account",
        display_name="Account",
        state="ok",
        freshness="fresh",
        windows=[
            UsageWindowView(
                id="weekly", kind="weekly", state="ok", remaining_pct=4, resets_at=2000
            )
        ],
    )
    floor = FloorOff(windows={"weekly": (2000, 96)})
    assert floor_off_active(floor, reading, 1000)
    reset = reading.model_copy(
        update={"windows": [reading.windows[0].model_copy(update={"remaining_pct": 100})]}
    )
    assert not floor_off_active(floor, reset, 1001)


def test_unknown_product_is_never_inferred_from_unknown_account(stop_env):
    from omnigent.usage.contract import UsageAccountView

    conv = replace(stop_env.conv, harness_override=None)
    assert (
        tile_of_session(
            conv,
            parse_session_choice(stop_env.value),
            {
                "unrelated": UsageAccountView(
                    id="unrelated",
                    kind="account",
                    display_name="Unrelated",
                    state="unknown",
                    freshness="none",
                    product=None,
                )
            },
            {},
        )
        is None
    )


@pytest.mark.asyncio
async def test_custom_agent_mapping_disambiguates_two_accounts(stop_env, client):
    from omnigent.server.agent_quota import QuotaContext
    from tests.server.helpers import create_test_agent

    env = stop_env
    agent = await create_test_agent(
        client,
        name="custom-code",
        executor={"type": "omnigent", "config": {"harness": "codex-native"}},
    )
    for row in [env.value["presets"][0], env.value["hosts"]["host-a"]["implementation"][0][0]]:
        row["links"][0]["agent"] = agent["id"]
    env.prefs.replace_session_choice(
        RESERVED_USER_LOCAL, env.value, env.prefs.session_choice(RESERVED_USER_LOCAL)["revision"]
    )
    settings = parse_session_choice(env.value)
    readings = env.service.pool_readings(QuotaContext(RESERVED_USER_LOCAL, None))
    readings["other"] = readings["account-a"].model_copy(update={"id": "other"})
    conv = replace(env.conv, model_override="gpt-example")
    assert session_products(env.app, settings)[agent["id"]] == "codex"
    assert (
        tile_of_session(conv, settings, readings, session_products(env.app, settings))
        == "account-a"
    )


def test_owner_inheritance_without_child_grant_and_workspace_isolation(stop_env):
    from omnigent.server.agent_quota import QuotaContext
    from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

    env = stop_env
    permissions = SqlAlchemyPermissionStore(env.store.storage_location)
    permissions.ensure_user("owner@example.test")
    permissions.ensure_user("other@example.test")
    permissions.grant("owner@example.test", env.conv.id, 4)
    child = env.store.create_conversation(
        parent_conversation_id=env.conv.id, harness_override="codex-native", runner_id="b" * 32
    )
    foreign = env.store.create_conversation(harness_override="codex-native", runner_id="c" * 32)
    permissions.grant("other@example.test", foreign.id, 4)
    env.store.touch_runner_liveness(["b" * 32, "c" * 32], int(env.now))
    for sid in [child.id, foreign.id]:
        env.store.set_session_live_status(sid, "running")
    with workspace_scope(17):
        outside = env.store.create_conversation(
            harness_override="codex-native", runner_id="d" * 32
        )
        env.store.touch_runner_liveness(["d" * 32], int(env.now))
        env.store.set_session_live_status(outside.id, "running")
    env.app.state.permission_store = permissions
    readings = env.service.pool_readings(QuotaContext(RESERVED_USER_LOCAL, None))
    targets = stop_targets(
        env.app,
        "owner@example.test",
        parse_session_choice(env.value),
        readings,
        {},
        {"account-a"},
        env.now,
    )
    assert set(targets["account-a"]) == {env.conv.id, child.id}


@pytest.mark.asyncio
async def test_saved_recovery_refusal_is_terminal(stop_env):
    from omnigent.entities import MessageData, NewConversationItem
    from omnigent.entities.conversation import StartupInputOptions
    from omnigent.server.startup_inputs import ManagedStartupInputs
    from omnigent.stores.conversation_store import PENDING_INPUT_RESPONSE_ID

    env = stop_env
    service = ManagedStartupInputs(env.store, None)
    [item] = env.store.append(
        env.conv.id,
        [
            NewConversationItem(
                type="message",
                response_id=PENDING_INPUT_RESPONSE_ID,
                status="in_progress",
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": "recover"}],
                    startup_options=StartupInputOptions(recovery_id="recovery"),
                ),
            )
        ],
    )

    async def refused(*args, **kwargs):
        refuse_if_stopped(env.app, env.conv)

    service.dispatch = refused
    await service._deliver(env.app, env.conv.id, item)
    assert env.store.get_item(env.conv.id, item.id).status == "failed"
    env.prefs.set_choice_floor_off(RESERVED_USER_LOCAL, "account-a", FloorOff(until="manual"))
    assert await service._deliver(env.app, env.conv.id, item) == "not_dispatched"


@pytest.mark.asyncio
async def test_floor_approval_passes_through_real_policy_engine(stop_env):
    from omnigent.policies.types import EvaluationContext
    from omnigent.runtime.policies.builder import any_policies_apply, build_policy_engine
    from omnigent.spec import AgentSpec
    from omnigent.spec.types import Phase

    env = stop_env
    spec = AgentSpec(spec_version=1)
    assert any_policies_apply(
        spec=spec,
        conversation_id=env.conv.id,
        phase=Phase.TOOL_CALL,
        tool_name="sys_quota_floor",
        default_policies=None,
        policy_store=None,
    )
    engine = build_policy_engine(
        spec=spec, conversation_id=env.conv.id, conversation_store=env.store
    )
    for off, action in [(True, "ASK"), (False, "ALLOW")]:
        result = await engine.evaluate(
            EvaluationContext(
                phase=Phase.TOOL_CALL,
                tool_name="sys_quota_floor",
                content={"name": "sys_quota_floor", "arguments": {"off": off}},
            )
        )
        assert result.action.name == action
