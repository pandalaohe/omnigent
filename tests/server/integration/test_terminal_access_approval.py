"""Admin consent cards drive the real runner switch only after browser approval."""

import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio

from omnigent.errors import OmnigentError
from omnigent.host.frames import CAP_TERMINAL_ELEVATED, HostHelloFrame
from omnigent.inner.datamodel import TerminalEnvSpec
from omnigent.runner import create_runner_app
from omnigent.runner.tool_dispatch import _execute_terminal_tool
from omnigent.runtime import pending_elicitations, session_stream
from omnigent.server.accounts_config import AccountsConfig
from omnigent.server.auth import LEVEL_EDIT
from omnigent.server.oidc import mint_session_cookie
from omnigent.server.routes._sessions import orchestration
from omnigent.server.schemas import ElicitationRequestParams, SessionEventInput
from omnigent.server.terminal_agent_access import (
    _requests,
    _resolution_locks,
    end_terminal_access_requests,
    require_terminal_card_approval,
    terminal_access_card_fields,
)
from omnigent.spec import AgentSpec
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.terminals import TerminalRegistry
from tests._helpers.polling import poll_until_async
from tests.runner.helpers import NullServerClient, make_test_terminal_instance
from tests.server.helpers import create_test_agent


@pytest_asyncio.fixture
async def terminal_access(auth_app, auth_client, db_uri, tmp_path, monkeypatch):
    owner = "owner@example.test"
    agent = await create_test_agent(auth_client, user=owner)
    sid = agent["_session_id"]
    store = SqlAlchemyConversationStore(db_uri)
    host_id = "0123456789abcdef0123456789abcdef"
    store.set_host_id(sid, host_id, workspace="/opt/work/project")
    hosts = HostStore(db_uri)
    hosts.upsert_on_connect(host_id, "consent-test", owner)
    auth_app.state.host_store = hosts
    monkeypatch.setenv("OMNIGENT_WS_ALLOWED_ORIGINS", "http://test")
    registry = auth_app.state.host_registry
    conn = registry.register(
        host_id,
        AsyncMock(),
        HostHelloFrame("test", 1, "consent-test", capabilities=[CAP_TERMINAL_ELEVATED]),
        owner,
    )
    monkeypatch.setattr(
        "omnigent.server.routes._sessions.common.get_server_host_registry", lambda: registry
    )
    terminals = TerminalRegistry()
    instance = make_test_terminal_instance("bash", "admin", tmp_path)
    instance.elevated = True
    terminals._by_conversation[sid] = {("bash", "admin"): instance}
    terminals._instance_locks[(sid, "bash", "admin")] = threading.Lock()
    runner = create_runner_app(terminal_registry=terminals, server_client=NullServerClient())
    spec = AgentSpec(spec_version=1, terminals={"bash": TerminalEnvSpec()})
    monkeypatch.setattr(orchestration, "_load_agent_spec_for_session", lambda *_: spec)
    monkeypatch.setattr(orchestration, "_resolve_harness", lambda *_, **__: None)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(runner), base_url="http://runner"
    ) as runner_client:
        monkeypatch.setattr(
            "omnigent.server.routes.sessions._get_runner_client_for_resource_access",
            AsyncMock(return_value=runner_client),
        )
        config = AccountsConfig(
            b"browser-consent-test-key-32-bytes", 8, "http://test", None, 3600, 300
        )
        provider = auth_app.state.mcp_registry_auth
        monkeypatch.setattr(provider, "_source", "accounts")
        monkeypatch.setattr(provider, "_accounts_config", config)
        browser = mint_session_cookie(
            owner, config.cookie_secret, 8, "accounts", browser_session=True
        )
        auth_client.cookies.set(config.session_cookie_name, browser)
        _requests.clear()
        _resolution_locks.clear()
        try:
            yield SimpleNamespace(
                sid=sid,
                store=store,
                agents=SqlAlchemyAgentStore(db_uri),
                hosts=hosts,
                owner=owner,
                conn=conn,
                instance=instance,
                terminals=terminals,
                spec=spec,
                client=auth_client,
                provider=provider,
                config=config,
                browser=browser,
                runner_client=runner_client,
                args={
                    "terminal": "bash",
                    "session": "admin",
                    "instance_id": instance.instance_id,
                    "reason": "Install the requested package",
                },
                path=f"/v1/sessions/{sid}/resources/terminals/terminal_bash_admin",
            )
        finally:
            await end_terminal_access_requests(sid, "terminal_bash_admin", None, store)
            _requests.clear()
            _resolution_locks.clear()


async def _publish(env, gate):
    parked = None
    initial = None
    if gate == "mcp":
        response = await orchestration._handle_mcp_tools_call(
            1,
            env.sid,
            {"name": "sys_terminal_request_access", "arguments": env.args},
            env.store,
            None,
            None,
        )
        initial = json.loads(bytes(response.body))
        assert "requestState" in initial.get("result", {}), initial
        eid = json.loads(initial["result"]["requestState"])["elicitation_id"]
    elif gate == "relay":
        verdict = await orchestration._evaluate_independent_tool_call_policy(
            env.sid,
            env.store.get_conversation(env.sid),
            SessionEventInput(
                type="function_call",
                data={"name": "sys_terminal_request_access", "arguments": json.dumps(env.args)},
            ),
            env.store,
            env.agents,
            None,
        )
        assert verdict["verdict"] == "pending", verdict
        eid = verdict["elicitation_id"]
    else:
        published = asyncio.Event()
        eid = "elicit_terminal_consent"
        parked = asyncio.create_task(
            orchestration._publish_and_wait_for_harness_elicitation(
                None,
                session_id=env.sid,
                params=ElicitationRequestParams(mode="form", message="Access?"),
                timeout_s=10,
                conversation_store=env.store,
                elicitation_id=eid,
                tool_name="mcp__omnigent__sys_terminal_request_access",
                tool_input=env.args,
                published=published,
            )
        )
        await asyncio.wait_for(published.wait(), 5)
    return eid, initial, parked


def _answer(env, eid, path, action):
    if path == "url":
        return f"/v1/sessions/{env.sid}/elicitations/{eid}/resolve", {"action": action}
    return f"/v1/sessions/{env.sid}/events", {
        "type": "approval",
        "data": {"elicitation_id": eid, "action": action},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ["mcp", "relay", "park"])
@pytest.mark.parametrize("path", ["url", "event"])
@pytest.mark.parametrize("action", ["accept", "decline"])
async def test_terminal_card_grants_only_after_human_verdict(
    terminal_access, gate, path, action, caplog
):
    env = terminal_access
    eid, initial, parked = await _publish(env, gate)
    fields = pending_elicitations.lookup(eid, conversation_id=env.sid)[1]["params"]
    assert fields["terminal_access_owner"] == env.owner
    assert fields["terminal_access_terminal_id"] == "terminal_bash_admin"
    assert fields["terminal_access_instance_id"] == env.instance.instance_id
    assert fields["terminal_access_host_id"] == env.conn.host_id
    page = await env.client.get(f"/v1/sessions/{env.sid}/elicitations/{eid}")
    assert page.status_code == 200, page.text
    assert {key: page.json().get(key) for key in fields if key.startswith("terminal_access_")} == {
        key: value for key, value in fields.items() if key.startswith("terminal_access_")
    }
    for text in ("consent-test", env.spec.name or "", env.args["reason"], "agent:"):
        assert text in fields["message"]
    assert not env.instance.agent_access
    with pytest.raises(OmnigentError):
        require_terminal_card_approval(env.sid, eid)

    executed = []

    async def execute(args):
        result = json.loads(
            await _execute_terminal_tool(
                "sys_terminal_request_access",
                args,
                terminal_registry=env.terminals,
                agent_spec=env.spec,
                conversation_id=env.sid,
                task_id="test",
                agent_id="test",
            )
        )
        executed.append(result)
        return {"result": {"output": json.dumps(result)}}

    async def retry(verdict):
        response = await orchestration._handle_mcp_tools_call(
            2,
            env.sid,
            {
                "name": "sys_terminal_request_access",
                "arguments": env.args,
                "requestState": initial["result"]["requestState"],
                "inputResponses": {eid: {"action": verdict}},
            },
            env.store,
            None,
            None,
            execute_tool=execute,
        )
        return json.loads(bytes(response.body))

    if initial is not None:
        forged = await retry("accept")
        assert "error" in forged and not executed
        assert not env.instance.agent_access
    url, body = _answer(env, eid, path, action)
    with caplog.at_level("INFO"):
        result = await env.client.post(url, json=body, headers={"Origin": "http://test"})
    assert result.status_code in (200, 202), result.text
    assert env.instance.agent_access is (action == "accept")
    if action == "accept":
        require_terminal_card_approval(env.sid, eid)
        assert "route=C" in caplog.text and f"instance={env.instance.instance_id}" in caplog.text
    if parked is not None:
        assert (await asyncio.wait_for(parked, 5)).action == action
    if initial is not None:
        outcome = await retry(action)
        if action == "accept":
            assert "error" not in outcome, outcome
            assert executed == [{"status": "granted"}]
        else:
            assert "error" in outcome and not executed
    assert pending_elicitations.lookup(eid, conversation_id=env.sid) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["url", "event"])
async def test_terminal_card_rejects_agent_credentials_and_other_admin(
    terminal_access, db_uri, path
):
    env = terminal_access
    eid, _, _ = await _publish(env, "relay")
    url, body = _answer(env, eid, path, "accept")
    permissions = SqlAlchemyPermissionStore(db_uri)
    other = "admin@example.test"
    permissions.ensure_user(other)
    permissions.grant(other, env.sid, LEVEL_EDIT)
    permissions.set_admin(other, True)
    runner_token = env.provider.mint_runner_token(env.owner, 60)
    other_browser = mint_session_cookie(
        other, env.config.cookie_secret, 8, "accounts", browser_session=True
    )
    legacy = mint_session_cookie(env.owner, env.config.cookie_secret, 8, "accounts")
    for token in (runner_token, legacy, other_browser):
        env.client.cookies.set(env.config.session_cookie_name, token)
        result = await env.client.post(url, json=body, headers={"Origin": "http://test"})
        assert result.status_code == 403, result.text
        assert not env.instance.agent_access
    env.client.cookies.set(env.config.session_cookie_name, env.browser)
    for headers in (
        {},
        {"Origin": "null"},
        {"Origin": "https://untrusted.example.test"},
        {"Origin": "http://test", "Authorization": f"Bearer {runner_token}"},
        {"Origin": "http://test", "X-Omnigent-Runner-Tunnel-Token": "test-only"},
    ):
        result = await env.client.post(url, json=body, headers=headers)
        assert result.status_code == 403, result.text
        assert not env.instance.agent_access
        assert pending_elicitations.lookup(eid, conversation_id=env.sid) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target", ["plain", "missing", "stale", "stopped", "granted", "duplicate", "blank"]
)
async def test_invalid_terminal_request_never_publishes_a_card(
    terminal_access, target, monkeypatch
):
    env = terminal_access
    if target == "plain":
        env.instance.elevated = False
    elif target == "missing":
        env.args["terminal"] = "missing"
    elif target == "stale":
        env.args["instance_id"] = "old-instance"
    elif target == "stopped":
        env.instance.running = False
    elif target == "granted":
        env.instance.agent_access = True
    elif target == "duplicate":
        await _publish(env, "relay")
    else:
        env.args["reason"] = " "
    published = []
    monkeypatch.setattr(session_stream, "publish", lambda *args: published.append(args))
    response = await orchestration._handle_mcp_tools_call(
        1,
        env.sid,
        {"name": "sys_terminal_request_access", "arguments": env.args},
        env.store,
        None,
        None,
    )
    assert "error" in json.loads(bytes(response.body))
    assert not published


@pytest.mark.asyncio
@pytest.mark.parametrize("end", ["close", "expire", "switch_on", "switch_off", "old_instance"])
async def test_terminal_request_withdrawal_and_web_switch(terminal_access, end):
    env = terminal_access
    eid, _, parked = await _publish(env, "park")
    if end == "expire":
        _requests[(env.sid, "terminal_bash_admin", env.instance.instance_id)].deadline = 0
        url, body = _answer(env, eid, "url", "accept")
        denied = await env.client.post(url, json=body, headers={"Origin": "http://test"})
        assert denied.status_code == 409 and not env.instance.agent_access
        fields = await terminal_access_card_fields(
            env.sid, "replacement", "sys_terminal_request_access", env.args, env.store, 10
        )
        assert fields["terminal_access_instance_id"] == env.instance.instance_id
    elif end.startswith("switch"):
        response = await env.client.patch(
            env.path,
            json={"instance_id": env.instance.instance_id, "agent_access": end == "switch_on"},
            headers={"Origin": "http://test"},
        )
        assert response.status_code == 200, response.text
        assert env.instance.agent_access is (end == "switch_on")
    else:
        await end_terminal_access_requests(
            env.sid, "terminal_bash_admin", "old-instance", env.store
        )
        assert pending_elicitations.lookup(eid, conversation_id=env.sid) is not None
        if end == "old_instance":
            replacement = make_test_terminal_instance("bash", "admin", env.instance.private_dir)
            replacement.elevated = True
            env.terminals._by_conversation[env.sid][("bash", "admin")] = replacement
            url, body = _answer(env, eid, "event", "accept")
            denied = await env.client.post(url, json=body, headers={"Origin": "http://test"})
            assert denied.status_code == 409 and not replacement.agent_access
            await end_terminal_access_requests(
                env.sid, "terminal_bash_admin", env.instance.instance_id, env.store
            )
        else:
            denied = await env.client.delete(
                env.path,
                headers={
                    "Origin": "http://test",
                    "Authorization": f"Bearer {env.provider.mint_runner_token(env.owner, 60)}",
                },
            )
            assert denied.status_code == 403
            closed = await env.client.delete(env.path, headers={"Origin": "http://test"})
            assert closed.status_code == 200, closed.text
    assert (await asyncio.wait_for(parked, 5)).action == (
        "accept" if end == "switch_on" else "cancel"
    )
    assert pending_elicitations.lookup(eid, conversation_id=env.sid) is None


@pytest.mark.asyncio
async def test_web_revocation_waits_for_in_flight_card_grant(terminal_access, monkeypatch):
    env = terminal_access
    eid, _, _ = await _publish(env, "relay")
    patch_started, release = asyncio.Event(), asyncio.Event()
    original = env.runner_client.patch

    async def patch(*args, **kwargs):
        if kwargs["json"]["agent_access"]:
            patch_started.set()
            await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(env.runner_client, "patch", patch)
    url, body = _answer(env, eid, "url", "accept")
    approving = asyncio.create_task(
        env.client.post(url, json=body, headers={"Origin": "http://test"})
    )
    await asyncio.wait_for(patch_started.wait(), 5)
    revoking = asyncio.create_task(
        env.client.patch(
            env.path,
            json={"instance_id": env.instance.instance_id, "agent_access": False},
            headers={"Origin": "http://test"},
        )
    )
    release.set()
    accepted, revoked = await asyncio.wait_for(asyncio.gather(approving, revoking), 5)
    assert accepted.status_code == 202, accepted.text
    assert revoked.status_code == 200, revoked.text
    assert not env.instance.agent_access
    with pytest.raises(OmnigentError):
        require_terminal_card_approval(env.sid, eid)


@pytest.mark.asyncio
async def test_old_host_owner_cannot_approve_after_persistent_reassignment(terminal_access):
    env = terminal_access
    eid, _, _ = await _publish(env, "relay")
    env.hosts.upsert_on_connect(
        env.conn.host_id, "consent-test", "new-owner@example.test", allow_host_id_reown=True
    )
    url, body = _answer(env, eid, "url", "accept")
    result = await env.client.post(url, json=body, headers={"Origin": "http://test"})
    assert result.status_code == 403, result.text
    assert not env.instance.agent_access


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["timeout", "invalid_json"])
async def test_unavailable_terminal_never_creates_a_reservation(
    terminal_access, monkeypatch, fault
):
    env = terminal_access
    if fault == "timeout":
        probe = AsyncMock(side_effect=httpx.ReadTimeout("test-only timeout"))
    else:
        probe = AsyncMock(return_value=httpx.Response(200, text="not JSON"))
    monkeypatch.setattr(env.runner_client, "get", probe)
    with pytest.raises(OmnigentError):
        await terminal_access_card_fields(
            env.sid, "card", "sys_terminal_request_access", env.args, env.store, 10
        )
    assert not _requests


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "replacement", [False, True], ids=["stopped-plain", "replaced-with-admin"]
)
async def test_delete_authorizes_and_closes_the_same_registered_instance(
    terminal_access, monkeypatch, replacement
):
    env = terminal_access
    env.instance.elevated = False
    token = env.provider.mint_runner_token(env.owner, 60)
    env.client.cookies.set(env.config.session_cookie_name, token)
    if replacement:
        successor = make_test_terminal_instance("bash", "admin", env.instance.private_dir)
        successor.elevated = True
        original = env.runner_client.delete

        async def replace_before_delete(*args, **kwargs):
            assert kwargs["params"] == {"instance_id": env.instance.instance_id}
            env.terminals._by_conversation[env.sid][("bash", "admin")] = successor
            return await original(*args, **kwargs)

        monkeypatch.setattr(env.runner_client, "delete", replace_before_delete)
    else:
        env.instance.running = False
        env.instance.private_dir.mkdir(parents=True)
        (env.instance.private_dir / "cleanup-marker").write_text("test-only")
        assert (await env.runner_client.get(env.path)).status_code == 404
    response = await env.client.delete(env.path, headers={"Origin": "http://test"})
    assert response.status_code == (409 if replacement else 200), response.text
    if replacement:
        assert env.terminals.get(env.sid, "bash", "admin") is successor
        assert successor.running and not successor.agent_access
    else:
        assert env.terminals.get(env.sid, "bash", "admin") is None
        assert not env.instance.private_dir.exists()


@pytest.mark.asyncio
async def test_real_card_timeout_releases_reservation_without_another_request(
    terminal_access, monkeypatch
):
    env = terminal_access
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._HARNESS_ELICITATION_REPARK_GRACE_S", 0.01
    )
    eid = "elicit_terminal_timeout"
    result = await orchestration._publish_and_wait_for_harness_elicitation(
        None,
        session_id=env.sid,
        params=ElicitationRequestParams(mode="form", message="Access?"),
        timeout_s=0.03,
        conversation_store=env.store,
        elicitation_id=eid,
        tool_name="sys_terminal_request_access",
        tool_input=env.args,
    )
    assert result is None or result.action != "accept"
    assert not env.instance.agent_access
    await poll_until_async(lambda: not _requests, timeout_s=2)
    assert not _requests
    assert pending_elicitations.lookup(eid, conversation_id=env.sid) is None
