"""Selection at the real create boundary and the durable startup hold."""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.server.host_load_queue import HostLoadQueue, start_allowed
from omnigent.server.session_choice import CHOICE_LABEL, SLOT_LABEL, ChoiceState, holds_slot
from omnigent.server.session_choice_runtime import choice_record
from omnigent.server.user_preferences_store import SqlAlchemyUserPreferencesStore
from omnigent.stores.host_store import HostStore
from tests.server.helpers import create_test_agent

HOST = "1" * 32
HEADERS = {"X-Omnigent-Create-Origin": "agent", "X-Omnigent-Session-Choice": "1"}


@pytest.fixture
async def choice_env(client, app, db_uri, monkeypatch):
    agents = [await create_test_agent(client, name=f"choice-{index}") for index in range(2)]
    store = app.state.conversation_store
    parent = store.create_conversation(
        agent_id=agents[0]["id"], host_id=HOST, workspace="/opt/work/project"
    )
    prefs = SqlAlchemyUserPreferencesStore(db_uri)
    app.state.user_preferences_store = prefs
    app.state.host_store = HostStore(db_uri)
    app.state.host_store.upsert_on_connect(HOST, "sample-host", RESERVED_USER_LOCAL)
    app.state.agent_quota_service.preferences = prefs
    from omnigent.server.routes import sessions

    monkeypatch.setattr(sessions, "_get_runner_client", AsyncMock(return_value=None))
    links = [
        {"agent": agent["id"], "model": "test-model", "meter": f"pool-{index}"}
        for index, agent in enumerate(agents)
    ]
    settings = {
        "enabled": True,
        "workTypes": [{"id": "work", "label": "Work"}],
        "presets": [
            {"id": f"p{index}", "name": f"Choice {index}", "links": [link]}
            for index, link in enumerate(links)
        ],
        "hosts": {
            HOST: {
                "work": [
                    [{"preset": "p0", "links": [links[0]], "weight": 1}],
                    [{"preset": "p1", "links": [links[1]], "weight": 1}],
                ]
            }
        },
        "limits": {"pool-0": {"maxRunning": 1}},
    }
    prefs.replace_session_choice(
        RESERVED_USER_LOCAL, settings, prefs.session_choice(RESERVED_USER_LOCAL)["revision"]
    )
    return SimpleNamespace(
        app=app,
        client=client,
        store=store,
        prefs=prefs,
        parent=parent,
        agents=agents,
        settings=settings,
    )


async def create(env, **changes):
    return await env.client.post(
        "/v1/sessions",
        headers=HEADERS,
        json={"parent_session_id": env.parent.id, "work_type": "work", **changes},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["codex-native", "codex"])
@pytest.mark.parametrize("offered", [True, False, "failed"])
async def test_create_checks_current_host_catalog_before_skipping_model(
    choice_env, db_uri, monkeypatch, harness, offered
):
    from omnigent.host.frames import CAP_CACHED_MODEL_OPTIONS
    from omnigent.server.session_choice import parse_session_choice
    from omnigent.server.session_choice_runtime import context_request
    from omnigent.stores.host_model_catalog_cache_store import HostModelCatalogCacheStore

    env = choice_env
    official = "gpt-example"
    custom = "provider-example/gpt-example"
    for index, model in enumerate((custom, official)):
        env.settings["hosts"][HOST]["work"][index][0]["links"][0]["model"] = model
    env.prefs.replace_session_choice(
        RESERVED_USER_LOCAL,
        env.settings,
        env.prefs.session_choice(RESERVED_USER_LOCAL)["revision"],
    )
    catalogs = HostModelCatalogCacheStore(db_uri)
    catalogs.upsert(HOST, harness, [{"id": official}], 1)
    env.app.state.host_model_catalog_cache_store = catalogs
    connection = SimpleNamespace(
        hello=SimpleNamespace(
            configured_harnesses={harness: True}, capabilities=[CAP_CACHED_MODEL_OPTIONS]
        )
    )
    monkeypatch.setattr(env.app.state.host_registry, "get", lambda host: connection)
    monkeypatch.setattr(
        "omnigent.server.routes._sessions.helpers._resolve_harness_impl", lambda *a, **kw: harness
    )
    available = offered is True
    rows = [{"id": official}, *([{"id": custom}] if available else [])]
    lookup = AsyncMock(return_value={"status": "ok", "models": rows})
    if offered == "failed":
        lookup.side_effect = RuntimeError("host disconnected")
    monkeypatch.setattr("omnigent.server.routes.calling_defaults._proxy_model_options", lookup)

    response = await create(env)
    assert response.status_code == 201, response.text
    assert response.json()["agent_id"] == env.agents[0 if available else 1]["id"]
    assert response.json()["model_override"] == (custom if available else official)
    assert catalogs.list([HOST])[0]["models"] == rows
    lookup.assert_awaited_once()
    if offered != "failed":
        await context_request(
            env.app, RESERVED_USER_LOCAL, HOST, parse_session_choice(env.settings), env.parent
        )
        lookup.assert_awaited_once()
    if available:
        from fastapi import HTTPException

        catalogs.upsert(HOST, harness, [{"id": official}], 1)
        lookup.reset_mock()
        with pytest.raises(HTTPException) as denied:
            await context_request(
                env.app,
                RESERVED_USER_LOCAL,
                HOST,
                parse_session_choice(env.settings),
                env.parent,
                user_id="other@example.test",
            )
        assert denied.value.status_code == 403
        lookup.assert_not_awaited()
        connection.hello.capabilities = []
        legacy = await context_request(
            env.app, RESERVED_USER_LOCAL, HOST, parse_session_choice(env.settings), env.parent
        )
        assert legacy.models[env.agents[0]["id"]] == frozenset({official})
        lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_creates_select_before_project_defaults_and_admit_in_parallel(choice_env):
    env = choice_env
    one, two = await asyncio.gather(create(env), create(env))
    assert one.status_code == two.status_code == 201, (one.text, two.text)
    choices = [one.json(), two.json()]
    assert {item["session_choice"]["pool"] for item in choices} == {"pool-0", "pool-1"}
    first = next(item for item in choices if item["session_choice"]["pool"] == "pool-0")
    conv = env.store.get_conversation(first["id"])
    assert holds_slot(conv, time.time())
    assert choice_record(conv)["pool"] == "pool-0"
    assert all(len(value) <= 256 for value in conv.labels.values())
    snapshot = await env.client.get(f"/v1/sessions/{conv.id}")
    assert snapshot.json()["session_choice"]["pool"] == "pool-0"
    env.store.set_session_live_status(conv.id, "idle")
    assert SLOT_LABEL not in env.store.get_conversation(conv.id).labels


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["named_no_reason", "choice_reason", "user_named", "legacy"])
async def test_named_precedence_and_old_runner(choice_env, mode):
    env = choice_env
    body = {"agent_id": env.agents[1]["id"]}
    if mode == "choice_reason":
        body["choice_reason"] = "The task needs this agent"
    elif mode == "user_named":
        body["user_named"] = True
    if mode == "legacy":
        response = await env.client.post(
            "/v1/sessions", json={"parent_session_id": env.parent.id, **body}
        )
    else:
        response = await create(env, **body)
    assert response.status_code == 201, response.text
    result = response.json()
    expected = env.agents[0 if mode == "named_no_reason" else 1]["id"]
    assert result["agent_id"] == expected
    assert bool(result.get("session_choice")) == (mode != "legacy")


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["missing", "foreign"])
async def test_create_skips_an_unbindable_agent_before_choosing_next(choice_env, invalid):
    env = choice_env
    bad_id = "f" * 32
    if invalid == "foreign":
        env.app.state.agent_store.create_user_agent(
            bad_id, "foreign", "test:///bundle", "other@example.test"
        )
    env.settings["hosts"][HOST]["work"][0][0]["links"].insert(0, {"agent": bad_id})
    env.prefs.replace_session_choice(
        RESERVED_USER_LOCAL,
        env.settings,
        env.prefs.session_choice(RESERVED_USER_LOCAL)["revision"],
    )
    response = await create(env)
    assert response.status_code == 201, response.text
    assert response.json()["agent_id"] == env.agents[0]["id"]
    assert response.json()["session_choice"]["skipped"][0]["why"] == "unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("default_model", [False, True])
async def test_named_reason_checks_cap_but_user_can_override(choice_env, default_model):
    env = choice_env
    if default_model:
        model = env.agents[0]["name"]
        env.settings["presets"][0]["links"][0]["model"] = model
        env.settings["hosts"][HOST]["work"][0][0]["links"][0]["model"] = model
        env.prefs.replace_session_choice(
            RESERVED_USER_LOCAL,
            env.settings,
            env.prefs.session_choice(RESERVED_USER_LOCAL)["revision"],
        )
    first = await create(env)
    assert first.status_code == 201, first.text
    named = {
        "agent_id": env.agents[0]["id"],
        "model_override": "test-model",
        "choice_reason": "specific need",
    }
    if default_model:
        named.pop("model_override")
    refused = await create(env, **named)
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["code"] == "agent_busy"
    allowed = await create(env, **{**named, "user_named": True})
    assert allowed.status_code == 201, allowed.text


@pytest.mark.asyncio
async def test_host_hold_rechecks_pool_after_admission_timeout(choice_env, monkeypatch):
    env = choice_env
    now = time.time()
    clock = [now]
    monkeypatch.setattr("omnigent.server.session_choice_runtime.time.time", lambda: clock[0])
    env.app.state.system_status = SimpleNamespace(
        queue_enabled=lambda workspace: True,
        host_load=lambda *args: {
            "level": "new_session_hold",
            "load_per_core": 6,
            "lines": {"heavy": 4, "new_session": 5},
        },
    )
    held_response = await create(env)
    assert held_response.status_code == 201, held_response.text
    held_id = held_response.json()["id"]
    held = env.store.get_conversation(held_id)
    assert held.labels[SLOT_LABEL].startswith("queued@")
    assert not holds_slot(held, now)
    clock[0] += 121
    env.app.state.system_status = None
    other_response = await create(env)
    assert other_response.status_code == 201, other_response.text
    other = other_response.json()
    assert other["session_choice"]["pool"] == "pool-0"
    env.store.set_host_id(other["id"], "2" * 32, workspace="/opt/work/project")
    queue = HostLoadQueue(env.store)
    monkeypatch.setattr(queue, "_start", AsyncMock())
    monkeypatch.setattr(env.app.state.host_load_inputs, "deliver_session", AsyncMock())
    assert not await queue.release(env.app, held_id, HOST)
    queue._start.assert_not_awaited()
    env.store.set_session_live_status(other["id"], "idle")
    assert await queue.release(env.app, held_id, HOST)
    queue._start.assert_awaited_once()
    assert holds_slot(env.store.get_conversation(held_id), clock[0])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reserve", "state", "expected"),
    [
        ({"mode": "total", "window": "7d", "pct": 50}, "ok", True),
        ({"mode": "total", "window": "7d", "pct": 30}, "low", False),
        ({"mode": "daily", "window": "7d", "pct": 11}, "ok", True),
        (None, "tight", False),
        (None, "low", True),
    ],
)
async def test_notice_uses_reserve_else_low_pace(
    choice_env, monkeypatch, reserve, state, expected
):
    import httpx
    from fastapi import FastAPI

    from omnigent.server.routes.session_choice import create_session_choice_router
    from omnigent.usage.contract import UsageAccountView, UsageWindowView

    env = choice_env
    env.settings["limits"]["pool-0"]["reserve"] = reserve
    env.prefs.replace_session_choice(
        RESERVED_USER_LOCAL,
        env.settings,
        env.prefs.session_choice(RESERVED_USER_LOCAL)["revision"],
    )
    reset = time.time() + 4 * 86400
    reading = UsageAccountView(
        id="pool-0",
        kind="account",
        display_name="Example",
        state=state,
        freshness="fresh",
        deciding_window="7d",
        windows=[
            UsageWindowView(
                id="7d",
                kind="weekly",
                label="7d",
                percent_used=60,
                remaining_pct=40,
                window_minutes=10080,
                resets_at=reset,
                state=state,
            )
        ],
    )
    monkeypatch.setattr(
        env.app.state.agent_quota_service, "pool_readings", lambda *a, **k: {"pool-0": reading}
    )
    app = FastAPI()
    app.state = env.app.state
    app.include_router(create_session_choice_router(env.prefs, None), prefix="/v1")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/v1/me/session-choice/notice",
            params={"host_id": HOST, "agent_id": env.agents[0]["id"], "model": "test-model"},
        )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert bool(payload["notice"]) is expected, payload
    if expected:
        assert payload["remaining_pct"] == 40
        assert payload["resets_at"] == reset


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_after_queue", [False, True])
async def test_user_named_host_hold_keeps_priority_at_release(
    choice_env, monkeypatch, stop_after_queue
):
    from omnigent.server.session_choice import state_for
    from omnigent.usage.contract import UsageAccountView, UsageWindowView

    env = choice_env
    first = await create(env)
    assert first.status_code == 201, first.text
    env.app.state.system_status = SimpleNamespace(
        queue_enabled=lambda workspace: True,
        host_load=lambda *args: {
            "level": "new_session_hold",
            "load_per_core": 6,
            "lines": {"heavy": 4, "new_session": 5},
        },
    )
    response = await create(
        env, agent_id=env.agents[0]["id"], user_named=True, model_override="test-model"
    )
    assert response.status_code == 201, response.text
    held_id = response.json()["id"]
    assert choice_record(env.store.get_conversation(held_id))["flags"] == ["user_named"]
    reset = time.time() + 86400
    exhausted = UsageAccountView(
        id="pool-0",
        kind="account",
        display_name="Pool",
        state="low",
        freshness="fresh",
        windows=[
            UsageWindowView(
                id="7d",
                kind="weekly",
                percent_used=100,
                remaining_pct=0,
                resets_at=reset,
                state="low",
            )
        ],
    )
    monkeypatch.setattr(
        env.app.state.agent_quota_service, "pool_readings", lambda *a, **k: {"pool-0": exhausted}
    )
    state_for(env.app).cooldowns[(RESERVED_USER_LOCAL, "pool-0")] = reset
    env.app.state.system_status = None
    queue = HostLoadQueue(env.store)
    monkeypatch.setattr(queue, "_start", AsyncMock())
    monkeypatch.setattr(env.app.state.host_load_inputs, "deliver_session", AsyncMock())
    if stop_after_queue:
        env.settings["limits"]["pool-0"]["reserve"] = {
            "mode": "total",
            "window": "7d",
            "stopPct": 0,
        }
        env.prefs.replace_session_choice(
            RESERVED_USER_LOCAL,
            env.settings,
            env.prefs.session_choice(RESERVED_USER_LOCAL)["revision"],
        )
        assert not await queue.release(env.app, held_id, HOST)
        queue._start.assert_not_awaited()
        env.settings["limits"]["pool-0"].pop("reserve")
        env.prefs.replace_session_choice(
            RESERVED_USER_LOCAL,
            env.settings,
            env.prefs.session_choice(RESERVED_USER_LOCAL)["revision"],
        )
    assert await queue.release(env.app, held_id, HOST)
    queue._start.assert_awaited_once()
    assert holds_slot(env.store.get_conversation(first.json()["id"]), time.time())
    assert holds_slot(env.store.get_conversation(held_id), time.time())


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["quota", "running", "choose"])
async def test_failed_reads_and_selector_keep_projectless_create_usable(
    choice_env, monkeypatch, failure
):
    env = choice_env

    def fail(*args, **kwargs):
        raise RuntimeError("unavailable test input")

    if failure == "quota":
        monkeypatch.setattr(env.app.state.agent_quota_service, "pool_readings", fail)
    elif failure == "running":
        monkeypatch.setattr("omnigent.server.session_choice_runtime.running_pools", fail)
    else:
        monkeypatch.setattr(ChoiceState, "select", fail)
    response = await create(env)
    assert response.status_code == 201, response.text
    assert response.json()["agent_id"] == env.agents[0]["id"]
    if failure == "choose":
        assert "choice_failed" in response.json()["session_choice"]["flags"]


@pytest.mark.asyncio
async def test_all_full_holds_then_releases_only_bound_pool(choice_env, monkeypatch):
    env = choice_env
    env.settings["hosts"][HOST]["work"] = env.settings["hosts"][HOST]["work"][:1]
    env.prefs.replace_session_choice(
        RESERVED_USER_LOCAL,
        env.settings,
        env.prefs.session_choice(RESERVED_USER_LOCAL)["revision"],
    )
    first = (await create(env)).json()
    held_response = await create(env)
    assert held_response.status_code == 201, held_response.text
    held = held_response.json()
    assert held["startup"]["reason"] == "pool_full"
    row = env.store.get_conversation(held["id"])
    assert not start_allowed(row)
    assert not holds_slot(row, time.time())
    queue = HostLoadQueue(env.store)
    monkeypatch.setattr(queue, "_start", AsyncMock())
    monkeypatch.setattr(env.app.state.host_load_inputs, "deliver_session", AsyncMock())
    assert not await queue.release(env.app, row.id, HOST)
    env.store.set_session_live_status(first["id"], "idle")
    assert await queue.release(env.app, row.id, HOST)
    queue._start.assert_awaited_once()
    assert start_allowed(env.store.get_conversation(row.id))
    assert holds_slot(env.store.get_conversation(row.id), time.time())


@pytest.mark.asyncio
async def test_parallel_release_keeps_one_admitted_slot(choice_env, monkeypatch):
    env = choice_env
    first = (await create(env)).json()
    row = env.store.get_conversation(first["id"])
    env.store.set_session_live_status(row.id, "idle")
    labels = dict(row.labels)
    labels[SLOT_LABEL] = f"queued@{HOST}@pool-0"
    queued = [
        env.store.create_conversation(
            agent_id=env.agents[0]["id"],
            parent_conversation_id=env.parent.id,
            kind="sub_agent",
            host_id=host,
            workspace="/opt/work/project",
            labels={**labels, SLOT_LABEL: f"queued@{host}@pool-0"},
        )
        for host in (HOST, "2" * 32)
    ]
    queue = HostLoadQueue(env.store)
    monkeypatch.setattr(queue, "_start", AsyncMock())
    monkeypatch.setattr(env.app.state.host_load_inputs, "deliver_session", AsyncMock())
    results = await asyncio.gather(
        *(queue.release(env.app, conv.id, conv.host_id) for conv in queued)
    )
    assert sum(results) == 1
    assert (
        sum(holds_slot(env.store.get_conversation(conv.id), time.time()) for conv in queued) == 1
    )
    another = await create(env)
    assert another.json()["session_choice"]["pool"] == "pool-1"


@pytest.mark.asyncio
async def test_refusal_and_reserved_labels_leave_no_child(choice_env):
    env = choice_env
    env.settings["hosts"][HOST]["work"] = []
    env.prefs.replace_session_choice(
        RESERVED_USER_LOCAL,
        env.settings,
        env.prefs.session_choice(RESERVED_USER_LOCAL)["revision"],
    )
    response = await create(env)
    assert (
        response.status_code == 409 and response.json()["detail"]["code"] == "session_choice_ask"
    )
    response = await create(
        env,
        agent_id=env.agents[0]["id"],
        user_named=True,
        labels={CHOICE_LABEL: json.dumps({"pool": "forged"})},
    )
    assert response.status_code == 400, response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "used,user_named,exempt,code",
    [
        (90, True, False, None),
        (95, True, False, "agent_stopped"),
        (90, False, False, "agent_reserved"),
        (90, False, True, None),
        (95, False, True, "agent_stopped"),
        (100, False, False, "agent_stopped"),
    ],
)
async def test_named_quota_lines(choice_env, used, user_named, exempt, code):
    from omnigent.usage.contract import UsageReport

    env = choice_env
    env.settings["limits"]["pool-0"] = {
        "reserve": {"mode": "total", "window": "7d", "pct": 15, "stopPct": 5},
        "reserveExempt": ["work"] if exempt else [],
    }
    env.prefs.replace_session_choice(
        RESERVED_USER_LOCAL,
        env.settings,
        env.prefs.session_choice(RESERVED_USER_LOCAL)["revision"],
    )
    now = time.time()
    env.app.state.agent_quota_service.report_account(
        RESERVED_USER_LOCAL,
        "pool-0",
        UsageReport.model_validate(
            {
                "observed_at": now,
                "windows": [
                    {
                        "id": "7d",
                        "percent_used": used,
                        "resets_at": now + 86400,
                    }
                ],
            }
        ),
        now=now,
    )
    response = await create(
        env,
        agent_id=env.agents[0]["id"],
        model_override="test-model",
        choice_reason="specific need",
        user_named=user_named,
    )
    assert response.status_code == (409 if code else 201), response.text
    if code:
        assert response.json()["detail"]["code"] == code
