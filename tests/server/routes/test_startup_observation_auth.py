"""PROPOSED real JWT/auth/owner-route checks. All tests UNRUN pending isolated bootstrap."""

from __future__ import annotations

import time
from types import SimpleNamespace

import httpx
import jwt
import pytest
from fastapi import APIRouter, FastAPI
from fastapi.responses import JSONResponse

from omnigent.errors import OmnigentError
from omnigent.runner.tool_dispatch import _session_startup_observation_v1_via_rest
from omnigent.server.accounts_config import AccountsConfig
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.oidc import OIDCConfig
from omnigent.server.routes.sessions.routes_core import register_core_routes
from omnigent.server.sharing_settings import PublicSharingMaxLevel, PublicSharingPolicyMiddleware
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore


def _provider(mode):
    # Fixture-only key; never obtained from env/config or printed. No IdP/network.
    key = b"startup-observation-fixture-key-32"
    if mode == "accounts":
        config = AccountsConfig(
            cookie_secret=key,
            session_ttl_hours=1,
            base_url="http://test",
            init_admin_password=None,
            invite_ttl_seconds=60,
            magic_ttl_seconds=60,
        )
        provider = UnifiedAuthProvider(source="accounts", accounts_config=config)
    else:
        config = OIDCConfig(
            issuer="https://idp.invalid",
            client_id="fixture",
            client_secret="fixture-only",
            redirect_uri="http://test/callback",
            cookie_secret=key,
            scopes="openid email",
            session_ttl_hours=1,
            logout_redirect_uri=None,
            allowed_domains=None,
            provider_type="github",
            authorization_endpoint="https://idp.invalid/authorize",
            token_endpoint="https://idp.invalid/token",
            jwks_uri=None,
            userinfo_endpoint="https://idp.invalid/user",
            allow_invites=False,
        )
        provider = UnifiedAuthProvider(source="oidc", oidc_config=config)
    return provider, key


def _route_app(db_uri, monkeypatch, mode, role):
    # Real SQLite-backed permission stores, following test_auth_helpers.py.
    conv_store = SqlAlchemyConversationStore(db_uri)
    perm_store = SqlAlchemyPermissionStore(db_uri)
    agent_store = SqlAlchemyAgentStore(db_uri)
    conv = conv_store.create_conversation(runner_id="1" * 32)
    actor = "observer@test.invalid"
    perm_store.ensure_user(actor, is_admin=role == "admin")
    if role in ("owner", "read", "edit", "manage"):
        perm_store.grant(actor, conv.id, {"owner": 4, "read": 1, "edit": 2, "manage": 3}[role])
    if role == "public":
        # Deliberately excessive raw public grant; actual policy caps it at EDIT.
        perm_store.ensure_user("__public__")
        perm_store.grant("__public__", conv.id, 4)

    counts = {
        "registry": 0,
        "routing": 0,
        "http": 0,
        "snapshot": 0,
        "agent_cache": 0,
        "revocation": 0,
    }

    class Registry:
        def get(self, runner_id):
            counts["registry"] += 1
            return  # No HTTP/harness/provider fixture required for auth reachability.

    class Router:
        def client_for_session_resources(self, *args, **kwargs):
            counts["routing"] += 1
            raise AssertionError("offline auth-boundary fixture must not route HTTP")

    def no_cache(*args, **kwargs):
        counts["agent_cache"] += 1
        raise AssertionError("diagnostic authentication must not load agent/config")

    async def no_snapshot(*args, **kwargs):
        counts["snapshot"] += 1
        raise AssertionError("ordinary detail snapshot is not the diagnostic route")

    from omnigent.server.routes.sessions import routes_core

    monkeypatch.setattr(routes_core, "_get_session_snapshot", no_snapshot)
    provider, key = _provider(mode)

    def revoked(grant_id):
        counts["revocation"] += 1
        return False

    provider.set_grant_revocation_check(revoked)
    app = FastAPI()
    app.state.tunnel_registry = Registry()
    router = APIRouter()
    register_core_routes(
        router,
        conversation_store=conv_store,
        agent_store=agent_store,
        runner_router=Router(),
        auth_provider=provider,
        permission_store=perm_store,
        agent_cache=SimpleNamespace(load=no_cache),
    )
    # Production server/app.py mounts create_sessions_router at /v1; register_core_routes
    # is its real route producer. This focused fixture mounts that producer identically.
    app.include_router(router, prefix="/v1")
    app.add_middleware(PublicSharingPolicyMiddleware, max_level=lambda: PublicSharingMaxLevel.EDIT)

    @app.exception_handler(OmnigentError)
    async def app_error(request, exc):
        # Envelope-only fixture handler: authentication/authorization are NOT replaced.
        return JSONResponse(status_code=exc.http_status, content={"error": {"code": exc.code}})

    token = jwt.encode(
        {
            "sub": actor,
            "exp": int(time.time()) + 300,
            "scope": "sessions",
            "grant_id": "fixture-grant",
        },
        key,
        algorithm="HS256",
    )
    return app, conv.id, token, counts


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["accounts", "oidc"])
@pytest.mark.parametrize(
    "role,expected",
    [
        ("owner", 200),
        ("admin", 200),
        ("read", 403),
        ("edit", 403),
        ("manage", 403),
        ("public", 403),
        ("inaccessible", 404),
    ],
)
async def test_sessions_scope_real_auth_to_registered_observation_route(
    db_uri, monkeypatch, mode, role, expected
):
    app, sid, token, counts = _route_app(db_uri, monkeypatch, mode, role)
    path = f"/v1/sessions/{sid}/startup-observation/v1"
    headers = {"Authorization": "Bearer " + token, "X-Omnigent-Permission-Tiers": "1"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        negotiated = await client.options(path, headers=headers)
        assert negotiated.status_code == expected
        assert counts["registry"] == counts["routing"] == counts["http"] == 0
        if expected == 200:
            assert negotiated.headers["X-Omnigent-Startup-Observation"] == "1"
            assert negotiated.json() == {
                "protocol": "permission_startup_observation",
                "version": 1,
            }
        # Direct GET independently reauthenticates; denied callers cannot bypass OPTIONS.
        read = await client.get(path, headers=headers)
        assert read.status_code == expected
        if expected == 200:
            packet = read.json()["startup_observation"]
            assert packet["reason"] == "bound_runner_not_connected_here"
            assert packet["status"] == "unknown" and packet["admission"] == "not_evaluated"
            assert counts["registry"] == 1
        else:
            assert counts["registry"] == 0
        assert counts["revocation"] >= 2  # Actual scoped JWT checks; no cached auth bypass.
        assert (
            counts["routing"] == counts["http"] == counts["snapshot"] == counts["agent_cache"] == 0
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["accounts", "oidc"])
async def test_sessions_scope_adapter_negotiates_real_owner_route_without_old_path(
    db_uri, monkeypatch, mode
):
    app, sid, token, counts = _route_app(db_uri, monkeypatch, mode, "owner")
    received = []

    @app.middleware("http")
    async def record(request, call_next):
        received.append((request.method, request.url.path))
        return await call_next(request)

    new = f"/v1/sessions/{sid}/startup-observation/v1"
    old = f"/v1/session-startup-observations/v1/{sid}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer " + token},
    ) as client:
        import json

        packet = json.loads(
            await _session_startup_observation_v1_via_rest({"session_id": sid}, "caller", client)
        )
        assert received == [("OPTIONS", new), ("GET", new)]
        assert packet["startup_observation"]["admission"] == "not_evaluated"
        before = dict(counts)
        for method in ("OPTIONS", "GET"):
            response = await client.request(method, old)
            assert response.status_code == 404
        assert counts == before  # Removed path matched no handler, auth, snapshot or runner.
    assert counts["snapshot"] == counts["agent_cache"] == counts["http"] == 0


@pytest.mark.asyncio
async def test_owner_protocol_missing_and_unauthenticated_requests_never_contact_runner(
    db_uri, monkeypatch
):
    app, sid, token, counts = _route_app(db_uri, monkeypatch, "accounts", "owner")
    path = f"/v1/sessions/{sid}/startup-observation/v1"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for method in ("OPTIONS", "GET"):
            assert (await client.request(method, path)).status_code == 401
            assert (
                await client.request(method, path, headers={"Authorization": "Bearer " + token})
            ).status_code == 426
    assert (
        counts["registry"] == counts["routing"] == counts["snapshot"] == counts["agent_cache"] == 0
    )


@pytest.mark.asyncio
async def test_snapshot_spy_is_observed_by_actual_ordinary_detail_handler(db_uri, monkeypatch):
    """Positive instrumentation control for the diagnostic zero-snapshot assertions."""
    from omnigent.server.routes.sessions import routes_core

    app, sid, token, counts = _route_app(db_uri, monkeypatch, "accounts", "owner")
    ordinary_handler = next(
        route.endpoint
        for route in app.routes
        if getattr(route, "path", None) == "/v1/sessions/{session_id}"
        and "GET" in getattr(route, "methods", ())
    )
    # Verify the route produced by register_core_routes uses this module-global slot.
    assert ordinary_handler.__globals__ is routes_core.__dict__
    assert (
        ordinary_handler.__globals__["_get_session_snapshot"] is routes_core._get_session_snapshot
    )
    headers = {"Authorization": "Bearer " + token, "X-Omnigent-Permission-Tiers": "1"}
    diagnostic = f"/v1/sessions/{sid}/startup-observation/v1"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get(diagnostic, headers=headers)).status_code == 200
        assert counts["snapshot"] == 0
        # Fixture conversation has no agent binding: the actual harness resolver returns
        # None before agent/cache work. The ordinary detail route then calls this spy.
        with pytest.raises(
            AssertionError, match="ordinary detail snapshot is not the diagnostic route"
        ):
            await client.get(f"/v1/sessions/{sid}", headers=headers)
        assert counts["snapshot"] == 1
        # The same installed spy remains active; another diagnostic must not call it.
        assert (await client.get(diagnostic, headers=headers)).status_code == 200
        assert counts["snapshot"] == 1
    assert counts["routing"] == counts["http"] == counts["agent_cache"] == 0


def test_openapi_publishes_both_methods_with_unique_operation_ids(db_uri, monkeypatch):
    """One handler, two published methods, distinct operation ids in the whole schema."""
    app, _sid, _token, _counts = _route_app(db_uri, monkeypatch, "accounts", "owner")
    path = "/v1/sessions/{session_id}/startup-observation/v1"
    schema = app.openapi()
    entry = schema["paths"][path]
    # Both methods stay published; hiding either from the schema would evade drift.
    assert set(entry) == {"get", "options"}
    get_id = entry["get"]["operationId"]
    options_id = entry["options"]["operationId"]
    assert get_id.endswith("_get") and options_id.endswith("_options")
    assert get_id != options_id
    operation_ids = [
        operation["operationId"]
        for item in schema["paths"].values()
        for operation in item.values()
        if isinstance(operation, dict)
    ]
    assert len(operation_ids) == len(set(operation_ids))
    # A single handler backs both registrations, OPTIONS registered before GET.
    routes = [route for route in app.routes if getattr(route, "path", None) == path]
    assert [sorted(route.methods) for route in routes] == [["OPTIONS"], ["GET"]]
    assert routes[0].endpoint is routes[1].endpoint
