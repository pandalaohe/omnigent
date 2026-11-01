"""Restart cards render the stored preview and accept only the host owner."""

import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from omnigent.host.frames import (
    CAP_HOST_RESTART,
    HostHelloFrame,
    HostRestartResultFrame,
    decode_host_frame,
)
from omnigent.policies.types import PolicyResult
from omnigent.runner.host_restart import execute_host_restart
from omnigent.runtime import pending_elicitations
from omnigent.server.accounts_config import AccountsConfig
from omnigent.server.auth import LEVEL_EDIT, UnifiedAuthProvider
from omnigent.server.host_restart_approval import approved_tool_preview
from omnigent.server.oidc import mint_session_cookie
from omnigent.server.routes._sessions import orchestration
from omnigent.server.routes._sessions.orchestration import (
    _handle_mcp_tools_call,
    _publish_and_wait_for_harness_elicitation,
    _register_policy_elicitation,
)
from omnigent.server.routes.hosts import create_hosts_router
from omnigent.server.schemas import ElicitationRequestParams
from omnigent.spec import AgentSpec
from omnigent.spec.types import PolicyAction
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests.server.helpers import create_test_agent


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["url", "event"])
@pytest.mark.parametrize("admin", [False, True])
@pytest.mark.parametrize("gate", ["relay", "park", "mcp"])
@pytest.mark.parametrize("action", ["accept", "decline"])
async def test_restart_card_preview_and_owner_resolution(
    auth_app, auth_client, db_uri, monkeypatch, path, admin, gate, action
):
    owner, collaborator = "alice@example.com", "bob@example.com"
    agent = await create_test_agent(auth_client, user=owner)
    created = await auth_client.post(
        "/v1/sessions", json={"agent_id": agent["id"]}, headers={"X-Forwarded-Email": owner}
    )
    assert created.status_code == 201
    session_id = created.json()["id"]
    store = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.ensure_user(collaborator)
    permissions.grant(collaborator, session_id, LEVEL_EDIT)
    if admin:
        permissions.set_admin(collaborator, True)
    registry = auth_app.state.host_registry
    hosts = HostStore(db_uri)
    host_id = "0123456789abcdef0123456789abcdef"
    hosts.upsert_on_connect(host_id, "restart-test", owner)
    auth_app.include_router(
        create_hosts_router(
            registry,
            hosts,
            store,
            auth_provider=UnifiedAuthProvider(source="header"),
            permission_store=permissions,
        ),
        prefix="/v1",
    )
    conn = registry.register(
        "0123456789abcdef0123456789abcdef",
        AsyncMock(),
        HostHelloFrame(
            "test", 1, "restart-test", capabilities=[CAP_HOST_RESTART], supervised="launchd"
        ),
        owner,
    )
    monkeypatch.setattr(
        "omnigent.server.routes._sessions.common.get_server_host_registry", lambda: registry
    )

    frames = []

    def reply(connection, raw):
        frame = decode_host_frame(raw)
        frames.append(frame)
        connection.pending_restarts[frame.request_id].set_result(
            HostRestartResultFrame(
                frame.request_id,
                True,
                supervisor="launchd",
                running_sessions=["busy"],
                runner_count=2,
            )
        )

    monkeypatch.setattr(registry, "send_text", reply)
    parked = None
    args = {"host": " restart-test ", "dry_run": False}
    initial = None
    if gate == "mcp":
        monkeypatch.setattr(
            orchestration, "_load_agent_spec_for_session", lambda *_: AgentSpec(spec_version=1)
        )
        monkeypatch.setattr(orchestration, "_resolve_harness", lambda *_, **__: None)
        response = await _handle_mcp_tools_call(
            1, session_id, {"name": "sys_host_restart", "arguments": args}, store, None, None
        )
        initial = json.loads(bytes(response.body))
        assert "requestState" in initial["result"], initial
        elicitation_id = json.loads(initial["result"]["requestState"])["elicitation_id"]
    elif gate == "relay":
        elicitation_id = await _register_policy_elicitation(
            session_id,
            PolicyResult(
                action=PolicyAction.ASK,
                reason="Restart?",
                deciding_policies=["__ask_on_host_restart"],
            ),
            "{}",
            store,
            tool_name="sys_host_restart",
            tool_input=args,
        )
    else:
        published = asyncio.Event()
        elicitation_id = "elicit_restart_card"
        parked = asyncio.create_task(
            _publish_and_wait_for_harness_elicitation(
                None,
                session_id=session_id,
                params=ElicitationRequestParams(
                    mode="form", message="Restart?", policy_name="__ask_on_host_restart"
                ),
                timeout_s=5,
                conversation_store=store,
                elicitation_id=elicitation_id,
                tool_name="sys_host_restart",
                tool_input=args,
                published=published,
            )
        )
        await asyncio.wait_for(published.wait(), 5)
    event = pending_elicitations.lookup(elicitation_id, conversation_id=session_id)[1]
    params = event["params"]
    preview = json.loads(params["content_preview"])
    assert preview["running_sessions"] == ["busy"]
    assert preview["supervisor"] == "launchd"
    assert preview["runner_count"] == 2
    assert "busy" in params["message"] and "terminals" in params["message"]
    assert params["host_restart_owner"] == owner
    if path == "url":
        url = f"/v1/sessions/{session_id}/elicitations/{elicitation_id}/resolve"
        body = {"action": action}
    else:
        url = f"/v1/sessions/{session_id}/events"
        body = {"type": "approval", "data": {"elicitation_id": elicitation_id, "action": action}}
    denied = await auth_client.post(url, json=body, headers={"X-Forwarded-Email": collaborator})
    assert denied.status_code == 403, denied.text
    assert not registry.restarts.previews[preview["preview_id"]].tool_approved
    provider = auth_app.state.mcp_registry_auth
    config = AccountsConfig(
        cookie_secret=b"test-browser-approval-key-32-bytes",
        session_ttl_hours=8,
        base_url="http://test",
        init_admin_password=None,
        invite_ttl_seconds=3600,
        magic_ttl_seconds=300,
    )
    monkeypatch.setattr(provider, "_source", "accounts")
    monkeypatch.setattr(provider, "_accounts_config", config)
    auth_client.cookies.set(
        config.session_cookie_name,
        mint_session_cookie(
            collaborator, config.cookie_secret, 8, "accounts", browser_session=True
        ),
    )
    denied = await auth_client.post(url, json=body, headers={"Origin": "http://test"})
    assert denied.status_code == 403, denied.text
    auth_client.cookies.clear()
    runner_token = provider.mint_runner_token(owner, 60)
    assert runner_token is not None
    denied = await auth_client.post(
        url,
        json=body,
        headers={"Authorization": f"Bearer {runner_token}", "Origin": "http://test"},
    )
    assert denied.status_code == 403, denied.text
    assert pending_elicitations.lookup(elicitation_id, conversation_id=session_id) is not None
    assert not registry.restarts.previews[preview["preview_id"]].tool_approved
    browser_token = mint_session_cookie(
        owner, config.cookie_secret, 8, "accounts", browser_session=True
    )
    for token in (runner_token, mint_session_cookie(owner, config.cookie_secret, 8, "accounts")):
        auth_client.cookies.set(config.session_cookie_name, token)
        denied = await auth_client.post(url, json=body, headers={"Origin": "http://test"})
        assert denied.status_code == 403, denied.text
        assert not registry.restarts.previews[preview["preview_id"]].tool_approved
    auth_client.cookies.set(config.session_cookie_name, browser_token)
    for headers in (
        {},
        {"Origin": "https://untrusted.example.test"},
        {"Origin": "omnigent://internal"},
        {"Origin": "null"},
        {"Origin": "http://test", "Authorization": f"Bearer {browser_token}"},
        {"Origin": "http://test", "X-Omnigent-Runner-Tunnel-Token": "test-runner-binding"},
    ):
        denied = await auth_client.post(url, json=body, headers=headers)
        assert denied.status_code == 403, denied.text
        assert pending_elicitations.lookup(elicitation_id, conversation_id=session_id) is not None
        assert not registry.restarts.previews[preview["preview_id"]].tool_approved
    accepted = await auth_client.post(url, json=body, headers={"Origin": "http://test"})
    assert accepted.status_code in (200, 202), accepted.text
    if action == "accept":
        assert approved_tool_preview(registry, conn, owner, session_id, False).payload == preview
    assert all(frame.dry_run for frame in frames)
    if parked is not None:
        assert (await asyncio.wait_for(parked, 5)).action == action
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=auth_app),
        base_url="http://test",
        headers={"X-Forwarded-Email": owner},
    ) as client:
        if initial is not None:
            executed = []

            async def execute(approved_args):
                result = json.loads(await execute_host_restart(approved_args, client, session_id))
                executed.append(result)
                return {"result": {"output": json.dumps(result)}}

            response = await _handle_mcp_tools_call(
                2,
                session_id,
                {
                    "name": "sys_host_restart",
                    "arguments": args,
                    "requestState": initial["result"]["requestState"],
                    "inputResponses": {elicitation_id: {"action": action}},
                },
                store,
                None,
                None,
                execute_tool=execute,
            )
            outcome = json.loads(bytes(response.body))
            if action == "accept":
                assert "error" not in outcome, outcome
                assert len(executed) == 1
                result = executed[0]
            else:
                assert "error" in outcome and not executed
                result = outcome
        else:
            result = json.loads(await execute_host_restart(args, client, session_id))
    if action == "accept":
        assert result["phase"] == "draining"
        assert sum(not frame.dry_run for frame in frames) == 1
    else:
        assert "error" in result
        assert all(frame.dry_run for frame in frames)
    await registry.restarts.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ["mcp", "park"])
@pytest.mark.parametrize("target", ["missing", "offline", "duplicate", "unsupervised"])
async def test_invalid_restart_target_never_publishes_card(
    auth_app, auth_client, db_uri, monkeypatch, gate, target
):
    owner = "owner@example.test"
    agent = await create_test_agent(auth_client, user=owner)
    created = await auth_client.post(
        "/v1/sessions", json={"agent_id": agent["id"]}, headers={"X-Forwarded-Email": owner}
    )
    assert created.status_code == 201
    session_id = created.json()["id"]
    store = SqlAlchemyConversationStore(db_uri)
    registry = auth_app.state.host_registry
    monkeypatch.setattr(
        "omnigent.server.routes._sessions.common.get_server_host_registry", lambda: registry
    )
    monkeypatch.setattr(
        orchestration, "_load_agent_spec_for_session", lambda *_: AgentSpec(spec_version=1)
    )
    monkeypatch.setattr(orchestration, "_resolve_harness", lambda *_, **__: None)
    if target != "missing":
        first = registry.register("host-a", AsyncMock(), HostHelloFrame("test", 1, target), owner)
        if target == "offline":
            registry.deregister(first.host_id, conn=first)
        elif target == "duplicate":
            registry.register("host-b", AsyncMock(), HostHelloFrame("test", 1, target), owner)
    published = []
    monkeypatch.setattr(
        orchestration.session_stream, "publish", lambda *args: published.append(args)
    )
    args = {"host": target, "dry_run": False}
    if gate == "mcp":
        response = await _handle_mcp_tools_call(
            1, session_id, {"name": "sys_host_restart", "arguments": args}, store, None, None
        )
        assert "error" in json.loads(bytes(response.body))
    else:
        ready = asyncio.Event()
        result = await _publish_and_wait_for_harness_elicitation(
            None,
            session_id=session_id,
            params=ElicitationRequestParams(mode="form", message="Restart?"),
            timeout_s=5,
            conversation_store=store,
            tool_name="sys_host_restart",
            tool_input=args,
            published=ready,
        )
        assert result.action == "decline" and ready.is_set()
    assert not published
    assert not registry.restarts.previews
