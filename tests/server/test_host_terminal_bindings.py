"""Host terminal admission uses current persisted host/account authority."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock

import jwt
import pytest
from asgiref.testing import ApplicationCommunicator
from fastapi import FastAPI
from sqlalchemy import update

from omnigent.db.account_authority import account_authority_scope
from omnigent.db.db_models import SqlHost, workspace_scope
from omnigent.errors import OmnigentError
from omnigent.host.frames import (
    HostHelloFrame,
    HostLaunchRunnerFrame,
    HostRunnerStatusFrame,
    decode_host_frame,
)
from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id
from omnigent.runner.transports.ws_tunnel.frames import HelloFrame, encode_frame
from omnigent.runner.transports.ws_tunnel.registry import TunnelRegistry
from omnigent.server.accounts_config import AccountsConfig
from omnigent.server.accounts_store import SqlAlchemyAccountStore
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.host_registry import HostRegistry
from omnigent.server.host_terminals import HOST_TERMINAL_TOKEN_PREFIX, HostTerminalService
from omnigent.server.oidc import OIDCConfig, mint_session_token
from omnigent.server.routes.runner_tunnel import create_runner_tunnel_router
from omnigent.stores.host_store import Host, HostStore
from tests.server.integration.test_runner_tunnel_route import _websocket_scope

_HOST_ID = "11aa11aa11aa11aa11aa11aa11aa11aa"
_KEY = b"synthetic-host-terminal-signing-key-for-tests"


@dataclass
class State:
    service: HostTerminalService
    provider: UnifiedAuthProvider | None
    accounts: SqlAlchemyAccountStore
    host: Host


@pytest.fixture(params=["accounts", "oidc", "header", "none"])
def state(request: pytest.FixtureRequest, db_uri: str) -> State:
    accounts = SqlAlchemyAccountStore(db_uri)
    owner = accounts.create_user_with_password("terminal-owner", "synthetic-hash", is_admin=True)
    store = HostStore(db_uri)
    identity = "external-oidc-owner" if request.param == "oidc" else owner.id
    generation = None if request.param == "oidc" else owner.account_generation
    with account_authority_scope(identity, generation):
        host = store.upsert_on_connect(_HOST_ID, "test-host", identity)
    provider = None
    if request.param == "accounts":
        provider = UnifiedAuthProvider(
            "accounts",
            accounts_config=AccountsConfig(_KEY, 8, "http://testserver", None, 3600, 60),
        )
    elif request.param == "header":
        provider = UnifiedAuthProvider("header", local_single_user=False)
    elif request.param == "oidc":
        provider = UnifiedAuthProvider(
            "oidc",
            oidc_config=OIDCConfig(
                issuer="https://issuer.test",
                client_id="test-client",
                client_secret="synthetic",
                redirect_uri="http://testserver/callback",
                cookie_secret=_KEY,
                scopes="openid email",
                session_ttl_hours=8,
                logout_redirect_uri=None,
                allowed_domains=None,
                provider_type="oidc",
                authorization_endpoint="https://issuer.test/authorize",
                token_endpoint="https://issuer.test/token",
                jwks_uri=None,
                userinfo_endpoint=None,
                allow_invites=False,
            ),
        )
    service = HostTerminalService(
        HostRegistry(),
        store,
        TunnelRegistry(),
        auth_provider=provider,
        account_store=accounts if request.param == "accounts" else None,
    )
    return State(service, provider, accounts, host)


def test_admission_and_server_restart_boundary(state: State) -> None:
    token, runner_id = state.service.mint(state.host)
    claim = state.service.admit(token)
    assert claim.host_id == state.host.host_id
    assert state.service.hosts.terminal_runner(state.host.host_id) == runner_id
    replacement = HostTerminalService(
        HostRegistry(),
        state.service.host_store,
        TunnelRegistry(),
        auth_provider=state.provider,
        account_store=state.accounts
        if state.provider and state.provider._source == "accounts"
        else None,
    )
    if state.provider and state.provider.host_terminal_signing_secret is not None:
        assert replacement.admit(token) == claim
    else:
        with pytest.raises(ValueError, match="record missing"):
            replacement.admit(token)


@pytest.mark.parametrize("change", ["owner", "generation", "deleted", "workspace"])
def test_original_token_cannot_outlive_current_authority(state: State, change: str) -> None:
    token, _ = state.service.mint(state.host)
    if change == "workspace":
        with workspace_scope(7), pytest.raises(ValueError):
            state.service.admit(token)
        return
    values = (
        {"user_id": "replacement-owner"}
        if change == "owner"
        else {"account_generation": "replacement-generation"}
        if change == "generation"
        else {"deleted_at": 1}
    )
    with state.service.host_store._session("test_change_host_authority") as session:
        session.execute(
            update(SqlHost).where(SqlHost.host_id == state.host.host_id).values(**values)
        )
    with pytest.raises(ValueError):
        state.service.admit(token)
    assert state.service.hosts.terminal_runner(state.host.host_id) is None


def test_one_verified_runner_per_host(state: State) -> None:
    first, runner_id = state.service.mint(state.host)
    second, _ = state.service.mint(state.host)
    state.service.admit(first)
    with pytest.raises(ValueError, match="already has"):
        state.service.admit(second)
    assert state.service.hosts.terminal_runner(state.host.host_id) == runner_id


@pytest.mark.asyncio
async def test_forged_purpose_refused_before_generic_auth_in_every_mode(state: State) -> None:
    token, _ = state.service.mint(state.host)
    bad = HOST_TERMINAL_TOKEN_PREFIX + "forged-or-unknown-launch-record"
    if state.provider and state.provider.host_terminal_signing_secret is not None:
        payload = jwt.decode(
            token[len(HOST_TERMINAL_TOKEN_PREFIX) :], options={"verify_signature": False}
        )
        bad = HOST_TERMINAL_TOKEN_PREFIX + jwt.encode(
            payload, b"synthetic-forged-host-terminal-signing-key", algorithm="HS256"
        )
    app = FastAPI()
    app.include_router(
        create_runner_tunnel_router(
            state.service.tunnels,
            auth_provider=state.provider,
            host_terminals=state.service,
            allowed_tunnel_tokens=frozenset({"ordinary-allowlisted-runner"}),
        ),
        prefix="/v1",
    )
    rid = token_bound_runner_id(bad)
    headers = [
        (RUNNER_TUNNEL_TOKEN_HEADER.lower().encode(), bad.encode()),
        (b"x-forwarded-email", state.host.user_id.encode()),
    ]
    if state.provider and state.provider.host_terminal_signing_secret is not None:
        headers.append(
            (
                b"authorization",
                (
                    "Bearer "
                    + mint_session_token(
                        state.host.user_id,
                        _KEY,
                        3600,
                        state.provider._source,
                        account_generation=state.host.account_generation,
                    )
                ).encode(),
            )
        )
    connection = ApplicationCommunicator(
        app,
        _websocket_scope(f"/v1/runners/{rid}/tunnel", headers=headers, client_host="203.0.113.7"),
    )
    await connection.send_input({"type": "websocket.connect"})
    assert (await connection.receive_output(timeout=2))["type"] == "websocket.accept"
    refused = await connection.receive_output(timeout=2)
    assert refused == {
        "type": "websocket.close",
        "code": 4004,
        "reason": "invalid terminal binding",
    }
    await connection.wait(timeout=2)
    assert state.service.tunnels.get(rid) is None
    assert state.service.hosts.terminal_runner(state.host.host_id) is None


@pytest.mark.asyncio
async def test_valid_purpose_tunnel_has_no_conversation_or_agent_callback(state: State) -> None:
    token, rid = state.service.mint(state.host)
    callbacks = []

    async def session_hook(*_args):
        callbacks.append("session initialized")

    app = FastAPI()
    app.include_router(
        create_runner_tunnel_router(
            state.service.tunnels,
            auth_provider=state.provider,
            host_terminals=state.service,
            on_runner_connect=session_hook,
            on_runner_disconnect=session_hook,
            allowed_tunnel_tokens=frozenset({"ordinary-allowlisted-runner"}),
        ),
        prefix="/v1",
    )
    connection = ApplicationCommunicator(
        app,
        _websocket_scope(
            f"/v1/runners/{rid}/tunnel",
            client_host="203.0.113.7",
            headers=[(RUNNER_TUNNEL_TOKEN_HEADER.lower().encode(), token.encode())],
        ),
    )
    await connection.send_input({"type": "websocket.connect"})
    assert (await connection.receive_output(timeout=2))["type"] == "websocket.accept"
    await connection.send_input(
        {
            "type": "websocket.receive",
            "text": encode_frame(
                HelloFrame(runner_version="test", frame_protocol_version=1, harnesses=[], envs=[])
            ),
        }
    )
    assert await state.service.tunnels.wait_for_runner(rid, timeout_s=2) is not None
    assert state.service.tunnels.runner_owner(rid) == state.host.user_id
    await connection.send_input({"type": "websocket.disconnect", "code": 1000})
    await connection.wait(timeout=2)
    await asyncio.sleep(0)
    assert callbacks == []


@pytest.mark.parametrize("state", ["accounts"], indirect=True)
@pytest.mark.parametrize(
    "claim,value",
    [
        ("purpose", "runner"),
        ("workspace_id", True),
        ("workspace_id", "0"),
        ("aud", "other-audience"),
        ("iss", "other-issuer"),
        ("workspace", "/opt/work/other"),
    ],
)
def test_signed_tuple_rejects_wrong_types_and_purpose(state, claim, value):
    token, _ = state.service.mint(state.host)
    payload = jwt.decode(
        token[len(HOST_TERMINAL_TOKEN_PREFIX) :], options={"verify_signature": False}
    )
    payload[claim] = value
    altered = HOST_TERMINAL_TOKEN_PREFIX + jwt.encode(payload, _KEY, algorithm="HS256")
    with pytest.raises(ValueError):
        state.service.admit(altered)


@pytest.mark.parametrize("state", ["accounts"], indirect=True)
def test_current_account_generation_is_checked_even_if_host_row_is_stale(state):
    from omnigent.db.db_models import SqlUser

    token, _ = state.service.mint(state.host)
    with state.accounts._session("test_revoke_account") as session:
        session.execute(
            update(SqlUser)
            .where(SqlUser.id == state.host.user_id)
            .values(account_generation="new-generation")
        )
    assert (
        state.service.host_store.get_host(state.host.host_id).account_generation
        == state.host.account_generation
    )
    with pytest.raises(ValueError, match="account authority"):
        state.service.admit(token)


@pytest.mark.parametrize("state", ["accounts"], indirect=True)
@pytest.mark.asyncio
async def test_generic_bearer_keeps_ordinary_runner_path_without_terminal_authority(state):
    ordinary = "synthetic-ordinary-runner-binding"
    rid = token_bound_runner_id(ordinary)
    bearer = mint_session_token(
        state.host.user_id,
        _KEY,
        3600,
        "accounts",
        account_generation=state.host.account_generation,
    )
    app = FastAPI()
    app.include_router(
        create_runner_tunnel_router(
            state.service.tunnels, auth_provider=state.provider, host_terminals=state.service
        ),
        prefix="/v1",
    )
    connection = ApplicationCommunicator(
        app,
        _websocket_scope(
            f"/v1/runners/{rid}/tunnel",
            client_host="203.0.113.7",
            headers=[
                (RUNNER_TUNNEL_TOKEN_HEADER.lower().encode(), ordinary.encode()),
                (b"authorization", f"Bearer {bearer}".encode()),
            ],
        ),
    )
    await connection.send_input({"type": "websocket.connect"})
    assert (await connection.receive_output(timeout=2))["type"] == "websocket.accept"
    await connection.send_input(
        {"type": "websocket.receive", "text": encode_frame(HelloFrame("test", 1, [], []))}
    )
    assert await state.service.tunnels.wait_for_runner(rid, timeout_s=2) is not None
    assert state.service.tunnels.runner_owner(rid) == state.host.user_id
    assert state.service.hosts.terminal_runner(state.host.host_id) is None
    with pytest.raises(OmnigentError):
        state.service.validate_runner(state.host.host_id, rid)
    await connection.send_input({"type": "websocket.disconnect", "code": 1000})
    await connection.wait(timeout=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["alive", "unknown", None, "dead"])
async def test_offline_identity_is_replaced_only_after_daemon_confirms_dead(
    state, status, monkeypatch
):
    token, old = state.service.mint(state.host)
    state.service.admit(token)
    conn = state.service.hosts.register(
        state.host.host_id,
        SimpleNamespace(),
        HostHelloFrame("test", 1, "test"),
        state.host.user_id,
    )
    state.service.reconnect_timeout_s = state.service.launch_timeout_s = 0.01
    launches = []

    def reply(connection, raw):
        frame = decode_host_frame(raw)
        if isinstance(frame, HostRunnerStatusFrame):
            if status is not None:
                connection.pending_runner_status[frame.request_id].set_result({"status": status})
        elif isinstance(frame, HostLaunchRunnerFrame):
            launches.append(frame)
            rid = token_bound_runner_id(frame.binding_token)
            claim = state.service.admit(frame.binding_token)
            state.service.tunnels.register(
                rid,
                SimpleNamespace(send_text=AsyncMock()),
                HelloFrame("test", 1, [], []),
                owner=claim.owner,
            )
            connection.pending_launches[frame.request_id].set_result(
                {"status": "launched", "runner_id": rid}
            )

    monkeypatch.setattr(state.service.hosts, "send_text", reply)
    if status == "dead":
        replacement = await state.service.runner(state.host, conn, launch=True)
        assert replacement != old and len(launches) == 1
    else:
        with pytest.raises(OmnigentError, match="reconnecting"):
            await state.service.runner(state.host, conn, launch=True)
        assert launches == []
        assert state.service.hosts.terminal_runner(state.host.host_id) == old


@pytest.mark.asyncio
async def test_lost_launch_result_preserves_identity_until_redial(state, monkeypatch):
    conn = state.service.hosts.register(
        state.host.host_id,
        SimpleNamespace(),
        HostHelloFrame("test", 1, "test"),
        state.host.user_id,
    )
    state.service.reconnect_timeout_s = state.service.launch_timeout_s = 0.01
    launched = []

    def lose_result(connection, raw):
        frame = decode_host_frame(raw)
        assert isinstance(frame, HostLaunchRunnerFrame)
        launched.append(frame)
        connection.pending_launches[frame.request_id].set_exception(
            ConnectionError("host disconnected")
        )

    monkeypatch.setattr(state.service.hosts, "send_text", lose_result)
    with pytest.raises(OmnigentError, match="reconnecting"):
        await state.service.runner(state.host, conn, launch=True)
    frame = launched[0]
    rid = token_bound_runner_id(frame.binding_token)
    assert conn.hello.terminal_runner_id == rid
    claim = state.service.admit(frame.binding_token)
    state.service.tunnels.register(
        rid,
        SimpleNamespace(send_text=AsyncMock()),
        HelloFrame("test", 1, [], []),
        owner=claim.owner,
    )
    assert await state.service.runner(state.host, conn, launch=True) == rid
    assert len(launched) == 1
