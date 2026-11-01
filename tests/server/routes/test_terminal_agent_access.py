"""Host-owner and browser provenance on the admin terminal REST surface."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import httpx
import pytest

from omnigent.host.frames import CAP_TERMINAL_ELEVATED, HostHelloFrame
from omnigent.inner.datamodel import TerminalEnvSpec
from omnigent.runtime import set_runner_router
from omnigent.server.accounts_config import AccountsConfig
from omnigent.server.auth import LEVEL_OWNER, UnifiedAuthProvider
from omnigent.server.oidc import mint_session_token
from omnigent.spec.types import AgentSpec
from omnigent.stores.host_store import HostStore
from tests.server.routes.test_session_resources import (
    _ConversationStore,
    _FakeRunnerClient,
    _FakeRunnerRouter,
    _sessions_app,
)
from tests.server.routes.test_session_resources import runner_globals_reset as runner_globals_reset
from tests.server.routes.test_shell_permission_gate import _StubPermissionStore


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["POST", "PATCH"])
@pytest.mark.parametrize(
    "credential",
    [
        "browser",
        "bearer",
        "runner_bearer",
        "runner_cookie",
        "tunnel_header",
        "other_owner",
        "bad_origin",
        "bad_origin_default",
    ],
)
async def test_admin_terminal_requires_host_owner_browser(
    db_uri,
    runner_globals_reset,
    monkeypatch,
    caplog,
    method,
    credential,
):
    del runner_globals_reset
    owner = "owner@example.test"
    cfg = AccountsConfig(
        cookie_secret=b"synthetic-terminal-tests-32-bytes-only",
        session_ttl_hours=8,
        base_url="http://server",
        init_admin_password=None,
        invite_ttl_seconds=60,
        magic_ttl_seconds=60,
    )
    auth = UnifiedAuthProvider("accounts", accounts_config=cfg)
    conversations = _ConversationStore()
    sid = "79b22ebd2309e48fdeb450c65611d51b"
    conv = conversations.get_conversation(sid)
    assert conv is not None
    conv.host_id = uuid.uuid4().hex
    permissions = _StubPermissionStore()
    for user in [owner, "other@example.test"]:
        permissions.add_grant(user, sid, LEVEL_OWNER)
        permissions.add_admin(user)
    app = _sessions_app(conversations, auth_provider=auth, permission_store=permissions)
    hosts = HostStore(db_uri)
    hosts.upsert_on_connect(conv.host_id, "test-host", owner)
    app.state.host_store = hosts
    app.state.test_host_registry.register(
        conv.host_id,
        object(),
        HostHelloFrame(
            version="test",
            frame_protocol_version=1,
            name="test-host",
            capabilities=[CAP_TERMINAL_ELEVATED],
        ),
        owner=owner,
    )
    monkeypatch.setenv("OMNIGENT_WS_ALLOWED_ORIGINS", "http://server")
    monkeypatch.setenv("OMNIGENT_LOCAL_SINGLE_USER", "0")
    if credential == "bad_origin_default":
        monkeypatch.delenv("OMNIGENT_WS_ALLOWED_ORIGINS")
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._load_agent_spec_for_session",
        lambda *_: AgentSpec(spec_version=1, name="test", terminals={"bash": TerminalEnvSpec()}),
    )
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.routes_resources.ensure_runner_connected",
        AsyncMock(return_value=(None, conv)),
    )
    runner = _FakeRunnerClient(
        payload={
            "id": "terminal_bash_admin",
            "metadata": {"elevated": True, "agent_access": True, "instance_id": "instance-one"},
        }
    )
    set_runner_router(_FakeRunnerRouter(runner))
    user = "other@example.test" if credential == "other_owner" else owner
    browser_token = mint_session_token(
        user, cfg.cookie_secret, 60, "accounts", browser_session=True
    )
    runner_token = auth.mint_runner_token(owner, 60)
    headers = {"origin": "http://server", "cookie": f"{cfg.session_cookie_name}={browser_token}"}
    if credential in ("bearer", "runner_bearer"):
        headers.pop("cookie")
        headers["authorization"] = (
            f"Bearer {browser_token if credential == 'bearer' else runner_token}"
        )
    if credential == "runner_cookie":
        headers["cookie"] = f"{cfg.session_cookie_name}={runner_token}"
    if credential == "tunnel_header":
        headers["X-Omnigent-Runner-Tunnel-Token"] = "synthetic-runner-proof"
    if credential in ("bad_origin", "bad_origin_default"):
        headers["origin"] = "http://untrusted.example"
    path = f"/v1/sessions/{sid}/resources/terminals"
    body = {"terminal": "bash", "session_key": "admin", "elevated": True}
    if method == "PATCH":
        path += "/terminal_bash_admin"
        body = {"agent_access": True, "instance_id": "instance-one"}
    with caplog.at_level("INFO"):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://server"
        ) as client:
            result = await client.request(method, path, json=body, headers=headers)
    assert result.status_code == (200 if credential == "browser" else 403), result.text
    assert runner.calls == ([(method, path)] if credential == "browser" else [])
    if method == "PATCH" and credential == "browser":
        assert "instance=instance-one" in caplog.text and "route=W" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,admin,status", [(False, True, 409), (True, False, 403)])
async def test_admin_create_requires_capability_and_admin(
    db_uri, runner_globals_reset, monkeypatch, enabled, admin, status
):
    del runner_globals_reset
    owner = "owner@example.test"
    auth = UnifiedAuthProvider("header", local_single_user=False)
    conversations = _ConversationStore()
    sid = "79b22ebd2309e48fdeb450c65611d51b"
    conv = conversations.get_conversation(sid)
    conv.host_id = uuid.uuid4().hex
    permissions = _StubPermissionStore()
    permissions.add_grant(owner, sid, LEVEL_OWNER)
    if admin:
        permissions.add_admin(owner)
    app = _sessions_app(conversations, auth_provider=auth, permission_store=permissions)
    hosts = HostStore(db_uri)
    hosts.upsert_on_connect(conv.host_id, "test-host", owner)
    app.state.host_store = hosts
    app.state.test_host_registry.register(
        conv.host_id,
        object(),
        HostHelloFrame(
            version="test",
            frame_protocol_version=1,
            name="test-host",
            capabilities=[CAP_TERMINAL_ELEVATED] if enabled else [],
        ),
        owner=owner,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://server"
    ) as client:
        result = await client.post(
            f"/v1/sessions/{sid}/resources/terminals",
            json={"terminal": "bash", "session_key": "admin", "elevated": True},
            headers={"X-Forwarded-Email": owner},
        )
    assert result.status_code == status, result.text
