"""Ownership and browser boundaries on session-linked host terminals."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent._platform import IS_WINDOWS
from omnigent.errors import OmnigentError
from omnigent.runner.host_terminals import create_host_terminal_app
from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id
from omnigent.server.accounts_config import AccountsConfig
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.linked_host_terminals import LinkedHostTerminals
from omnigent.server.oidc import mint_session_token
from omnigent.server.routes.session_host_terminals import create_session_host_terminals_router
from omnigent.terminals.elevation import AGENT_ACCESS_REQUIRED


@pytest.fixture
def linked_app(monkeypatch):
    key = b"synthetic-linked-terminal-browser-key"
    config = AccountsConfig(key, 8, "http://server", None, 3600, 60)
    auth = UnifiedAuthProvider("accounts", accounts_config=config)
    monkeypatch.setenv("OMNIGENT_WS_ALLOWED_ORIGINS", "http://server")
    token = "synthetic-session-runner-binding"
    conv = SimpleNamespace(
        id="session",
        root_conversation_id="session",
        runner_id=token_bound_runner_id(token),
        archived=False,
    )
    store = SimpleNamespace(get_conversation=lambda sid: conv if sid == "session" else None)
    permissions = SimpleNamespace(
        get=lambda user, sid: SimpleNamespace(level=4 if user == "owner" else 2),
        is_admin=lambda user: user == "owner",
        list_for_sessions=lambda ids: {"session": [SimpleNamespace(user_id="owner", level=4)]},
    )
    conn = SimpleNamespace(
        hello=SimpleNamespace(
            name="Remote machine", capabilities=["host_terminal_links_v1", "terminal_elevated_v1"]
        )
    )
    host = SimpleNamespace(host_id="host", user_id="owner")
    terminals = SimpleNamespace(
        host_store=SimpleNamespace(get_host=lambda hid: host),
        hosts=SimpleNamespace(get=lambda hid: conn),
    )
    service = LinkedHostTerminals(terminals, store, auth_provider=auth, permissions=permissions)
    service.resource = AsyncMock(return_value={"instance_id": "one", "agent_tier": "read"})
    service.call = AsyncMock(
        return_value={"terminal_id": "a" * 32, "instance_id": "one", "agent_tier": "read"}
    )
    service.changed = Mock()
    service.list = AsyncMock(
        return_value={"terminals": [{"terminal_id": "a" * 32}], "complete": True}
    )
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def handle_error(request, exc):
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    app.include_router(create_session_host_terminals_router(service), prefix="/v1")

    def headers(user="owner", *, runner=False, browser=True):
        bearer = mint_session_token(
            user, key, 60, "accounts", browser_session=browser and not runner
        )
        return {"Origin": "http://server", "Cookie": f"{config.session_cookie_name}={bearer}"}

    return SimpleNamespace(
        app=app, service=service, conn=conn, headers=headers, token=token, host=host
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "category",
    [
        "origin_untrusted",
        "runner_credential",
        "browser_session_missing",
        "browser_account_mismatch",
        "not_owner",
        "admin_not_allowed",
    ],
)
async def test_linked_terminal_refusal_logs_fixed_category(
    linked_app, monkeypatch, caplog, category
):
    env = linked_app
    headers = env.headers(browser=category != "browser_session_missing")
    if category == "origin_untrusted":
        headers["Origin"] = "https://untrusted.example"
    if category == "runner_credential":
        headers["Authorization"] = "Bearer synthetic-runner-credential"
    if category == "browser_account_mismatch":
        monkeypatch.setattr(env.service.auth, "browser_session_user_id", lambda _: "another-owner")
    if category == "not_owner":
        headers = env.headers("collaborator")
    if category == "admin_not_allowed":
        env.service.permissions.is_admin = lambda _: False
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(env.app), base_url="http://server"
    ) as client:
        with caplog.at_level("INFO"):
            response = await client.post(
                "/v1/sessions/session/host-terminals",
                headers=headers,
                json={"host_id": "host", "elevated": category == "admin_not_allowed"},
            )
    assert response.status_code == 403
    assert f"terminal access refused category={category}" in caplog.text
    assert headers["Cookie"] not in caplog.text
    assert "synthetic-runner-credential" not in caplog.text
    env.service.call.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_cookie_can_authenticate_but_linked_inventory_requires_fresh_login(
    linked_app, caplog
):
    env = linked_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(env.app), base_url="http://server"
    ) as client:
        with caplog.at_level("INFO"):
            denied = await client.get(
                "/v1/sessions/session/host-terminals", headers=env.headers(browser=False)
            )
        allowed = await client.get("/v1/sessions/session/host-terminals", headers=env.headers())
    assert denied.status_code == 403
    assert denied.headers["X-Omnigent-Auth-Recovery"] == "browser-session-required"
    assert "terminal access refused category=browser_session_missing" in caplog.text
    assert allowed.status_code == 200
    env.service.list.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["open", "list", "patch", "delete"])
async def test_runner_credentials_cannot_use_linked_human_routes(linked_app, operation):
    env = linked_app
    path = "/v1/sessions/session/host-terminals"
    method = {"open": "POST", "list": "GET", "patch": "PATCH", "delete": "DELETE"}[operation]
    if operation in ("patch", "delete"):
        path += "/host/" + "a" * 32
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(env.app), base_url="http://server"
    ) as client:
        response = await client.request(
            method,
            path,
            headers=env.headers(runner=True),
            json={"host_id": "host"}
            if operation == "open"
            else {"agent_tier": "read_write", "instance_id": "one"},
        )
    assert response.status_code == 403
    env.service.call.assert_not_awaited()


@pytest.mark.asyncio
async def test_collaborator_cannot_open_or_discover_linked_terminals(linked_app):
    env = linked_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(env.app), base_url="http://server"
    ) as client:
        opened = await client.post(
            "/v1/sessions/session/host-terminals",
            json={"host_id": "host"},
            headers=env.headers("collaborator"),
        )
        listed = await client.get(
            "/v1/sessions/session/host-terminals", headers=env.headers("collaborator")
        )
    assert opened.status_code == 403
    assert listed.json() == {"terminals": [], "complete": True}
    env.service.list.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["owner", "links", "elevated"])
async def test_linked_open_checks_host_ownership_and_capabilities(linked_app, failure):
    env = linked_app
    if failure == "owner":
        env.host.user_id = "another-owner"
    else:
        env.conn.hello.capabilities.remove(
            "host_terminal_links_v1" if failure == "links" else "terminal_elevated_v1"
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(env.app), base_url="http://server"
    ) as client:
        response = await client.post(
            "/v1/sessions/session/host-terminals",
            json={"host_id": "host", "elevated": True},
            headers=env.headers(),
        )
    assert response.status_code == (403 if failure == "owner" else 409)
    env.service.call.assert_not_awaited()


@pytest.mark.asyncio
async def test_agent_relay_requires_exact_session_binding_and_supplies_link_identity(linked_app):
    env = linked_app
    path = "/v1/sessions/session/host-terminals/host/" + "a" * 32 + "/agent/read"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(env.app), base_url="http://server"
    ) as client:
        denied = await client.post(
            path, headers={RUNNER_TUNNEL_TOKEN_HEADER: "other-binding"}, json={}
        )
        allowed = await client.post(
            path,
            headers={RUNNER_TUNNEL_TOKEN_HEADER: env.token},
            json={"session_id": "forged", "scrollback": 10},
        )
    assert denied.status_code == 403
    assert allowed.status_code == 200
    assert env.service.call.await_args.kwargs["body"] == {
        "session_id": "session",
        "scrollback": 10,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("admission", [False, True])
async def test_archive_sweep_closes_archived_and_deleted_sessions_only(linked_app, admission):
    env = linked_app
    env.service.terminals.hosts.online_host_ids = lambda: ["host"]
    env.service.store.get_conversation = lambda sid: (
        None if sid == "deleted" else SimpleNamespace(archived=sid == "archived")
    )

    async def call(host, user, method, path, **kwargs):
        if method == "GET":
            return {
                "terminals": [
                    {"terminal_id": name, "linked_session_id": name}
                    for name in ("archived", "deleted", "active", None)
                ]
            }
        return None

    env.service.call = AsyncMock(side_effect=call)
    await env.service.sweep("host" if admission else None)
    deleted = [c.args[3] for c in env.service.call.await_args_list if c.args[2] == "DELETE"]
    assert deleted == ["/v1/host-terminals/archived", "/v1/host-terminals/deleted"]
    assert env.service.changed.call_count == 2


def test_linked_change_event_has_no_resource_body(linked_app, monkeypatch):
    events = []
    monkeypatch.setattr(
        "omnigent.runtime.session_stream.publish", lambda sid, event: events.append((sid, event))
    )
    LinkedHostTerminals.changed(linked_app.service, "session")
    assert events == [("session", {"type": "session.resource.changed", "session_id": "session"})]


@pytest.mark.asyncio
async def test_linked_routes_forward_to_real_terminal_runner(linked_app, tmp_path, monkeypatch):
    env = linked_app
    runner = create_host_terminal_app(home=tmp_path, shells=["powershell" if IS_WINDOWS else "sh"])
    env.service.call = LinkedHostTerminals.call.__get__(env.service)
    env.service.resource = LinkedHostTerminals.resource.__get__(env.service)
    env.service.terminals.runner = AsyncMock(return_value="terminal-runner")
    env.service.terminals.tunnels = object()
    monkeypatch.setattr(
        "omnigent.runner.transports.ws_tunnel.transport.WSTunnelTransport",
        lambda *args: httpx.ASGITransport(runner),
    )
    with TestClient(runner):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(env.app), base_url="http://server"
        ) as client:
            path = "/v1/sessions/session/host-terminals"
            opened = await client.post(
                path,
                headers=env.headers(),
                json={"host_id": "host", "agent_tier": "read"},
            )
            assert opened.status_code == 201, opened.text
            terminal = opened.json()
            target = path + "/host/" + terminal["terminal_id"]
            agent = {RUNNER_TUNNEL_TOKEN_HEADER: env.token}
            read = await client.post(target + "/agent/read", headers=agent, json={})
            assert read.status_code == 200 and "screen" in read.json()
            denied = await client.post(
                target + "/agent/send", headers=agent, json={"text": "echo denied"}
            )
            assert denied.status_code == 409
            assert denied.json()["error"]["message"] == AGENT_ACCESS_REQUIRED
            granted = await client.patch(
                target,
                headers=env.headers(),
                json={"agent_tier": "read_write", "instance_id": terminal["instance_id"]},
            )
            assert granted.status_code == 200, granted.text
            sent = await client.post(
                target + "/agent/send", headers=agent, json={"text": "echo allowed"}
            )
            assert sent.status_code == 200 and "error" not in sent.json()
            closed = await client.post(target + "/agent/close", headers=agent, json={})
            assert closed.status_code == 200
            assert runner.state.terminal_registry.list_for_conversation("host-terminals") == []
