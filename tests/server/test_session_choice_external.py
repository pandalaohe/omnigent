"""External admission delegates stops and contains failed-child exclusions to one host."""

import json
import time
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.server.session_choice import (
    CHOICE_LABEL,
    ChoiceRequest,
    ChoiceState,
    choice_document,
    parse_session_choice,
)
from omnigent.server.session_choice_external import ExternalAdmission, refusal_until
from omnigent.server.session_choice_runtime import (
    context_request,
    enforce_stop_lines,
    observe_external_admission,
    refuse_if_stopped,
    release_choice,
    running_pools,
    session_products,
    stopped_line,
    tile_of_session,
)
from tests.server.helpers import create_test_agent
from tests.server.test_agent_quota import usage_report
from tests.server.test_quota_stop_lines import stop_env as stop_env
from tests.server.test_session_choice import document
from tests.server.test_session_choice_create import (
    HOST,
    create,
)
from tests.server.test_session_choice_create import (
    choice_env as choice_env,
)
from tests.server.test_session_choice_create import (
    quota_runner as quota_runner,
)

RULE = {
    "host": HOST,
    "agent": "agent-a",
    "admittedByRouter": True,
    "refusalPrefix": "router-refused",
    "exhaustionCode": "all_closed",
    "routeTiles": ["route-a"],
}
HEADER = "[router-refused:all_closed clears_at=-]"


def save_external(env, **changes):
    rule = {**RULE, "agent": env.agents[0]["id"], **changes}
    env.settings["externalAdmission"] = [rule]
    env.prefs.replace_session_choice(
        RESERVED_USER_LOCAL,
        env.settings,
        env.prefs.session_choice(RESERVED_USER_LOCAL)["revision"],
    )
    return rule


# Native forwarder passes turn.error.message through to terminal output.
# These status/stream envelopes follow forwarder/test_turn_errors.py and Codex's HTTP diagnostic.
@pytest.fixture(
    params=[
        "plain",
        "stream",
        "http-json",
        "stream-http-json",
        "http-plain",
        "stderr-http-json",
        "json-message",
        "json-error",
        "stream-json",
    ]
)
def wrapped_refusal(request):
    if request.param == "plain":
        return HEADER
    if request.param == "stream":
        return f"stream error: {HEADER}"
    if request.param == "http-plain":
        return f"unexpected status 403 Forbidden: {HEADER}, url: https://router.example.test/v1/responses"
    if request.param == "json-message":
        return json.dumps({"message": HEADER})
    body = json.dumps({"error": {"message": HEADER, "type": "inference_hook_denied"}})
    if request.param == "json-error":
        return body
    if request.param == "stream-json":
        return f"stream error: {body}"
    message = (
        f"unexpected status 400 Bad Request: {body}, url: https://router.example.test/v1/responses"
    )
    if request.param == "stderr-http-json":
        return f"ERROR: {message}"
    return f"stream error: {message}" if request.param == "stream-http-json" else message


def test_external_configuration_roundtrip_and_default_off():
    baseline = document()
    assert "externalAdmission" not in choice_document(parse_session_choice(baseline))
    baseline["externalAdmission"] = [{**RULE, "admittedByRouter": False}]
    saved = choice_document(parse_session_choice(baseline))
    rule = saved["externalAdmission"][0]
    assert rule["admittedByRouter"] is False
    assert rule["defaultExclusionSeconds"] == 600
    assert parse_session_choice(saved).external_admission[0].refusal_prefix == "router-refused"
    for invalid in (
        [RULE, RULE],
        [{**RULE, "host": "*"}],
        [{**RULE, "defaultExclusionSeconds": 0}],
        [{**RULE, "refusalPrefix": "unsafe:token"}],
    ):
        with pytest.raises(ValidationError):
            parse_session_choice({**baseline, "externalAdmission": invalid})


def test_refusal_body_shapes_and_expiry(wrapped_refusal):
    rule = ExternalAdmission.model_validate(RULE)
    assert refusal_until(wrapped_refusal, rule, 1000) == 1600
    assert (
        refusal_until(
            wrapped_refusal, rule.model_copy(update={"default_exclusion_seconds": 42}), 1000
        )
        == 1042
    )
    assert (
        refusal_until("[router-refused:all_closed clears_at=2026-11-17T00:00:00Z]", rule, 1000)
        == 1794873600
    )


@pytest.mark.parametrize(
    "message",
    [
        "diagnostic: " + HEADER,
        "stream error: diagnostic: " + HEADER,
        '{"error":{"message":"quoted: ' + HEADER + '"}}',
        HEADER.replace("all_closed", "model_unavailable"),
        HEADER.replace("all_closed", "warn_hold"),
        HEADER.replace("router-refused", "another-router"),
        HEADER.replace("=-]", "=invalid]"),
        HEADER.replace("=-]", "=2026-11-17T00:00:00]"),
        HEADER.replace("=-]", "=1970-01-01T00:01:00Z]"),
        HEADER[:-1],
    ],
)
def test_non_exhaustion_or_malformed_failures_do_not_exclude(message):
    assert refusal_until(message, ExternalAdmission.model_validate(RULE), 1000) is None


@pytest.mark.asyncio
async def test_failed_child_excludes_only_its_host_and_preserves_real_expiry(
    quota_runner, wrapped_refusal, monkeypatch
):
    from omnigent.harnesses.codex_native.forwarder import _terminal_error_from_turn
    from omnigent.server.session_choice_preview import candidate_preview

    env = quota_runner
    now = int(time.time())
    monkeypatch.setattr("omnigent.server.session_choice_runtime.time.time", lambda: now)
    save_external(env)
    first = (await create(env)).json()
    terminal_error = _terminal_error_from_turn({"turn": {"error": {"message": wrapped_refusal}}})
    assert terminal_error is not None
    event = {
        "type": "external_session_status",
        "data": {
            "status": "failed",
            "response_id": "refused-turn",
            "output": "Provider declined this turn",
            "failure_detail": terminal_error.message,
        },
    }
    response = await env.client.post(f"/v1/sessions/{first['id']}/events", json=event)
    assert response.status_code == 202, response.text
    next_child = await create(env)
    assert next_child.status_code == 201, next_child.text
    choice = next_child.json()["session_choice"]
    assert choice["pool"] == "pool-1"
    assert (choice["skipped"][0]["why"], choice["skipped"][0]["until"]) == ("excluded", now + 600)
    assert env.store.get_conversation(first["id"]).last_task_error is not None
    for user_named in (False, True):
        named = await create(
            env,
            agent_id=env.agents[0]["id"],
            model_override="test-model",
            user_named=user_named,
            choice_reason="specific task",
        )
        assert named.status_code == 409, named.text
        assert named.json()["detail"]["code"] == "agent_excluded"
        assert named.json()["detail"]["reset"] == now + 600
    settings = parse_session_choice(env.settings)
    preview = await candidate_preview(
        env.app, RESERVED_USER_LOCAL, HOST, settings, env.agents[0]["id"], "test-model"
    )
    assert preview["check"]["reason"] == "excluded"
    other_host = "2" * 32
    request = await context_request(env.app, RESERVED_USER_LOCAL, other_host, settings, env.parent)
    assert request.link_exclusions == {}
    await env.client.post(f"/v1/sessions/{first['id']}/events", json=event)
    request = await context_request(env.app, RESERVED_USER_LOCAL, HOST, settings, env.parent)
    assert request.link_exclusions == {env.agents[0]["id"]: now + 600}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source", ["root", "assistant", "other-code", "editor", "successful", "unrelated-turn"]
)
async def test_only_an_authoritative_terminal_child_failure_excludes(quota_runner, source):
    from omnigent.entities import MessageData, NewConversationItem

    env = quota_runner
    save_external(env)
    first = (await create(env)).json()
    session = env.store.get_conversation(first["id"])
    if source == "root":
        session = env.parent
    if source == "assistant":
        env.store.append(
            session.id,
            [
                NewConversationItem(
                    type="message",
                    response_id="refused-turn",
                    data=MessageData(
                        role="assistant",
                        agent="sample",
                        content=[{"type": "output_text", "text": HEADER}],
                    ),
                )
            ],
        )
    if source in {"successful", "unrelated-turn"}:
        await observe_external_admission(
            env.app, session, HEADER, actor=None, from_runner=True, response_id="refused-turn"
        )
    await observe_external_admission(
        env.app,
        session,
        None
        if source in {"assistant", "successful", "unrelated-turn"}
        else HEADER.replace("all_closed", "warn_hold")
        if source == "other-code"
        else HEADER,
        actor="editor@example.test" if source == "editor" else None,
        from_runner=source != "editor",
        response_id="different-turn" if source == "unrelated-turn" else "refused-turn",
        status="idle" if source == "successful" else "failed",
    )
    env.store.set_session_live_status(first["id"], "idle")
    response = await create(env)
    assert response.status_code == 201, response.text
    assert response.json()["session_choice"]["pool"] == "pool-0"


@pytest.mark.asyncio
async def test_error_and_failed_status_correlate_without_restarting_exclusion(
    quota_runner, monkeypatch
):
    env = quota_runner
    clock = [int(time.time())]
    monkeypatch.setattr("omnigent.server.session_choice_runtime.time.time", lambda: clock[0])
    save_external(env, defaultExclusionSeconds=45)
    first = (await create(env)).json()
    error = {
        "type": "external_conversation_item",
        "data": {
            "item_type": "error",
            "item_data": {"message": HEADER, "source": "execution", "code": "native_turn_error"},
            "response_id": "refused-turn",
            "source_id": "refusal-error",
        },
    }
    posted = await env.client.post(f"/v1/sessions/{first['id']}/events", json=error)
    assert posted.status_code == 202, posted.text
    settings = parse_session_choice(env.settings)
    request = await context_request(env.app, RESERVED_USER_LOCAL, HOST, settings, env.parent)
    assert request.link_exclusions == {}
    clock[0] += 10
    status = {
        "type": "external_session_status",
        "data": {
            "status": "failed",
            "response_id": "refused-turn",
            "output": "Provider declined this turn",
        },
    }
    posted = await env.client.post(f"/v1/sessions/{first['id']}/events", json=status)
    assert posted.status_code == 202, posted.text
    request = await context_request(env.app, RESERVED_USER_LOCAL, HOST, settings, env.parent)
    assert request.link_exclusions == {env.agents[0]["id"]: clock[0] + 35}
    clock[0] += 10
    await env.client.post(f"/v1/sessions/{first['id']}/events", json=error)
    await env.client.post(f"/v1/sessions/{first['id']}/events", json=status)
    request = await context_request(env.app, RESERVED_USER_LOCAL, HOST, settings, env.parent)
    assert request.link_exclusions == {env.agents[0]["id"]: clock[0] + 25}
    clock[0] += 25
    await env.client.post(f"/v1/sessions/{first['id']}/events", json=error)
    await env.client.post(f"/v1/sessions/{first['id']}/events", json=status)
    request = await context_request(env.app, RESERVED_USER_LOCAL, HOST, settings, env.parent)
    assert request.link_exclusions == {}


@pytest.mark.asyncio
async def test_queued_release_waits_for_explicit_time(quota_runner, monkeypatch):
    env = quota_runner
    clock = [int(time.time())]
    monkeypatch.setattr("omnigent.server.session_choice_runtime.time.time", lambda: clock[0])
    save_external(env)
    first = (await create(env)).json()
    env.settings["hosts"][HOST]["work"] = env.settings["hosts"][HOST]["work"][:1]
    save_external(env)
    held = (await create(env)).json()
    assert held["session_choice"]["outcome"] == "hold"
    until = clock[0] + 90
    timestamp = datetime.fromtimestamp(until, UTC).isoformat()
    event = {
        "type": "external_session_status",
        "data": {
            "status": "failed",
            "response_id": "refused-turn",
            "output": f"[router-refused:all_closed clears_at={timestamp}]",
        },
    }
    posted = await env.client.post(f"/v1/sessions/{first['id']}/events", json=event)
    assert posted.status_code == 202, posted.text
    session = env.store.get_conversation(held["id"])
    assert not await release_choice(env.app, session, HOST)
    clock[0] = until
    assert await release_choice(env.app, session, HOST)


@pytest.mark.asyncio
async def test_tile_state_change_lifts_early_but_failed_reads_and_percent_changes_do_not(
    quota_runner, monkeypatch
):
    env = quota_runner
    now = int(time.time())
    env.settings["limits"]["route-a"] = {
        "reserve": {"mode": "total", "window": "weekly", "stopPct": 5}
    }
    save_external(env)
    service = env.app.state.agent_quota_service

    def report(used):
        service.report_account(
            RESERVED_USER_LOCAL,
            "route-a",
            usage_report(
                account_product="codex",
                observed_at=now,
                windows=[
                    {
                        "id": "weekly",
                        "percent_used": used,
                        "window_minutes": 10080,
                        "resets_at": now + 3600,
                    }
                ],
            ),
            now=now,
        )

    report(50)
    first = (await create(env)).json()
    await observe_external_admission(
        env.app,
        env.store.get_conversation(first["id"]),
        HEADER,
        actor=None,
        from_runner=True,
        response_id="refused-turn",
        status="failed",
    )
    env.store.set_session_live_status(first["id"], "idle")
    settings = parse_session_choice(env.settings)
    for used in (51, 70):
        report(used)
        request = await context_request(env.app, RESERVED_USER_LOCAL, HOST, settings, env.parent)
        assert env.agents[0]["id"] in request.link_exclusions
    with monkeypatch.context() as patch:

        def unavailable(*args, **kwargs):
            raise OSError("temporary quota snapshot failure")

        patch.setattr(service, "pool_readings", unavailable)
        request = await context_request(env.app, RESERVED_USER_LOCAL, HOST, settings, env.parent)
        assert env.agents[0]["id"] in request.link_exclusions
    report(96)
    response = await create(env)
    assert response.status_code == 201, response.text
    assert response.json()["session_choice"]["pool"] == "pool-0"


@pytest.mark.asyncio
@pytest.mark.parametrize("schema", [1, 2])
async def test_external_stop_exemption_shared_paths_and_running_attribution(
    stop_env, client, monkeypatch, schema
):
    env = stop_env
    agent = await create_test_agent(
        client,
        name="externally-admitted",
        executor={"type": "omnigent", "config": {"harness": "codex-native"}},
    )
    rule = {**RULE, "agent": agent["id"]}
    env.value.update(externalAdmission=[rule], schemaVersion=schema)
    if schema == 2:
        env.value["limitRules"] = []
    env.store.update_conversation(
        env.conv.id,
        agent_id=agent["id"],
        host_id=HOST,
        labels={CHOICE_LABEL: json.dumps({"pool": "account-a"})},
    )
    env.prefs.replace_session_choice(
        RESERVED_USER_LOCAL, env.value, env.prefs.session_choice(RESERVED_USER_LOCAL)["revision"]
    )
    conv = env.store.get_conversation(env.conv.id)
    settings = parse_session_choice(env.value)
    from omnigent.server.agent_quota import QuotaContext

    readings = env.service.pool_readings(QuotaContext(RESERVED_USER_LOCAL, None))
    assert tile_of_session(conv, settings, readings, {}) is None
    assert stopped_line(env.app, conv) is None
    refuse_if_stopped(env.app, conv)
    post = AsyncMock(return_value={"queued": False})
    monkeypatch.setattr(env.app.state, "quota_stop_post_event", post)
    await enforce_stop_lines(env.app)
    post.assert_not_awaited()
    running = running_pools(
        env.app, RESERVED_USER_LOCAL, settings, session_products(env.app, settings), env.now
    )
    identity = next(row for row in running.identities if row.session_id == conv.id)
    assert identity.pool == "account-a"
    assert running["account-a"] == (conv.id,)
    # Another host and a disabled rule retain the previous enforcing tile.
    assert (
        tile_of_session(replace(conv, host_id="other-host"), settings, readings, {}) == "account-a"
    )
    disabled = parse_session_choice(
        {**env.value, "externalAdmission": [{**rule, "admittedByRouter": False}]}
    )
    assert tile_of_session(conv, disabled, readings, {}) == "account-a"


@pytest.mark.asyncio
async def test_native_hook_and_event_start_use_external_exemption(stop_env, client):
    env = stop_env
    agent = await create_test_agent(
        client,
        name="external-gate",
        executor={"type": "omnigent", "config": {"harness": "codex-native"}},
    )
    env.store.update_conversation(env.conv.id, agent_id=agent["id"], host_id=HOST)
    env.value["externalAdmission"] = [{**RULE, "agent": agent["id"]}]
    env.prefs.replace_session_choice(
        RESERVED_USER_LOCAL, env.value, env.prefs.session_choice(RESERVED_USER_LOCAL)["revision"]
    )
    response = await client.post(
        f"/v1/sessions/{env.conv.id}/policies/evaluate",
        json={"event": {"type": "PHASE_REQUEST", "data": {}}},
    )
    assert response.status_code == 200, response.text
    assert response.json().get("reason") != "quota_stopped"
    for event in ("message", "compact"):
        response = await client.post(
            f"/v1/sessions/{env.conv.id}/events",
            json={
                "type": event,
                "data": {"role": "user", "content": [{"type": "input_text", "text": "work"}]},
            },
        )
        assert "quota_stopped" not in response.text


def test_scope_isolation_models_and_config_removal():
    from omnigent.server.session_choice_external import ExternalRefusal

    settings = parse_session_choice({**document(), "externalAdmission": [RULE]})
    state = ChoiceState()
    request = ChoiceRequest(RESERVED_USER_LOCAL, HOST)
    scope = (request.owner, request.host, RULE["agent"])
    state.external_admission.exclusions[scope] = ExternalRefusal(
        scope, settings.external_admission[0], 1600, None
    )
    assert state.external_admission.active(settings, request, {}, {}, 1000) == {"agent-a": 1600}
    assert (
        state.external_admission.active(
            settings, replace(request, owner="other@example.test"), {}, {}, 1000
        )
        == {}
    )
    assert (
        state.external_admission.active(
            settings, replace(request, host="other-host"), {}, {}, 1000
        )
        == {}
    )
    assert (
        state.external_admission.active(
            parse_session_choice(document()), replace(request, preview=True), {}, {}, 1000
        )
        == {}
    )
    assert state.external_admission.active(settings, request, {}, {}, 1000) == {"agent-a": 1600}
    assert (
        state.external_admission.active(parse_session_choice(document()), request, {}, {}, 1000)
        == {}
    )
