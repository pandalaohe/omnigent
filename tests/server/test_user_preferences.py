"""User-scoped, cross-device preferences API tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from starlette.requests import HTTPConnection

from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import (
    LEVEL_EDIT,
    LEVEL_OWNER,
    RESERVED_USER_LOCAL,
    AuthProvider,
    UnifiedAuthProvider,
)
from omnigent.server.routes.sessions.routes_hooks import _approval_timeout_owner
from omnigent.server.user_preferences_store import (
    ApprovalTimeout,
    SqlAlchemyUserPreferencesStore,
    UserPreferencesUserNotFoundError,
    UserPreferencesValidationError,
    read_approval_timeout,
    validate_preferences_envelope,
)
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore


class _HeaderAuthProvider(AuthProvider):
    """Resolve a test identity from ``X-Test-User``."""

    def get_user_id(self, request: HTTPConnection) -> str | None:
        return request.headers.get("x-test-user")


class _AccountsAuthProvider(UnifiedAuthProvider):
    """Expose test-header identity while exercising accounts-mode guards."""

    def __init__(self) -> None:
        super().__init__(source="header", local_single_user=False)

    def get_user_id(self, request: HTTPConnection) -> str | None:
        return request.headers.get("x-test-user")


def _preferences_app(db_uri: str, tmp_path: Path) -> FastAPI:
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts-preferences"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(
            artifact_store=artifact_store,
            cache_dir=tmp_path / "cache-preferences",
        ),
        auth_provider=_HeaderAuthProvider(),
        user_preferences_store=SqlAlchemyUserPreferencesStore(db_uri),
    )


def _deleted_account_app(db_uri: str, tmp_path: Path) -> FastAPI:
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts-deleted-account"))
    auth_provider = _AccountsAuthProvider()
    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(
            artifact_store=artifact_store,
            cache_dir=tmp_path / "cache-deleted-account",
        ),
        auth_provider=auth_provider,
        user_preferences_store=SqlAlchemyUserPreferencesStore(db_uri),
    )
    # create_app only mounts accounts-only auth routes when accounts mode is
    # active at construction. Switch after construction so this focused test
    # exercises the preferences guard without bootstrapping a real account.
    auth_provider._source = "accounts"
    return app


def test_store_preserves_uninitialized_vs_initialized_defaults(db_uri: str) -> None:
    """NULL and an explicit empty settings envelope have different meaning."""
    store = SqlAlchemyUserPreferencesStore(db_uri)
    assert store.get("alice@example.com") is None

    empty = {"version": 1, "settings": {}}
    assert store.initialize("alice@example.com", empty) == empty
    assert store.get("alice@example.com") == empty

    # First-device migration is idempotent and cannot overwrite an already
    # initialized account with stale localStorage from another device.
    stale = {"version": 1, "settings": {"usage_context": {"visible": False}}}
    assert store.initialize("alice@example.com", stale) == empty


def test_store_merges_one_namespace_and_keeps_users_isolated(db_uri: str) -> None:
    store = SqlAlchemyUserPreferencesStore(db_uri)
    first = store.patch_namespace(
        "alice@example.com",
        "keyboard_shortcuts",
        {"enabled": True, "actions": {"archive": "Alt+W"}},
    )
    assert first["settings"]["keyboard_shortcuts"]["enabled"] is True

    merged = store.patch_namespace(
        "alice@example.com",
        "keyboard_shortcuts",
        {"enabled": False},
    )
    assert merged["settings"]["keyboard_shortcuts"] == {
        "enabled": False,
        "actions": {"archive": "Alt+W"},
    }
    assert store.get("bob@example.com") is None

    removed = store.patch_namespace("alice@example.com", "keyboard_shortcuts", None)
    assert removed == {"version": 1, "settings": {}}

    compact = store.patch_namespace("alice@example.com", "context_indicator", "compact")
    assert compact["settings"]["context_indicator"] == "compact"


def test_store_can_refuse_to_recreate_a_deleted_account(db_uri: str) -> None:
    """Accounts-mode routes fail closed when a JWT outlives its user row."""
    store = SqlAlchemyUserPreferencesStore(db_uri)
    with pytest.raises(UserPreferencesUserNotFoundError):
        store.initialize(
            "deleted@example.com",
            {"version": 1, "settings": {}},
            create_if_missing=False,
        )
    with pytest.raises(UserPreferencesUserNotFoundError):
        store.patch_namespace(
            "deleted@example.com",
            "usage_context",
            {"version": 1},
            create_if_missing=False,
        )
    assert store.get("deleted@example.com") is None


def test_store_rejects_an_unpaired_unicode_surrogate_as_a_validation_error() -> None:
    with pytest.raises(UserPreferencesValidationError, match="valid UTF-8"):
        validate_preferences_envelope({"version": 1, "settings": {"usage_context": "\ud800"}})


@pytest.mark.asyncio
async def test_preferences_api_initializes_merges_and_returns_from_me(
    db_uri: str,
    runtime_init: None,
    tmp_path: Path,
) -> None:
    app = _preferences_app(db_uri, tmp_path)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"x-test-user": "alice@example.com"}

        initial_me = await client.get("/v1/me", headers=headers)
        assert initial_me.status_code == 200
        assert initial_me.headers["cache-control"] == "private, no-store"
        assert initial_me.json() == {
            "user_id": "alice@example.com",
            "is_admin": False,
            "preferences": None,
        }

        initialized = await client.put(
            "/v1/me/preferences",
            headers=headers,
            json={
                "version": 1,
                "settings": {"session_navigation": {"activeHours": 12}},
            },
        )
        assert initialized.status_code == 200

        # Once initialized, PUT is idempotent and cannot replace the first
        # device's value with stale state from a second device.
        repeated = await client.put(
            "/v1/me/preferences",
            headers=headers,
            json={"version": 1, "settings": {}},
        )
        assert repeated.json() == initialized.json()

        patched = await client.patch(
            "/v1/me/preferences/session_navigation",
            headers=headers,
            json={"value": {"showMobileTitle": True}},
        )
        assert patched.status_code == 200
        assert patched.json()["settings"]["session_navigation"] == {
            "activeHours": 12,
            "showMobileTitle": True,
        }

        patched = await client.patch(
            "/v1/me/preferences/agent_badges",
            headers=headers,
            json={
                "value": {
                    "version": 1,
                    "enabled": False,
                    "entries": {
                        "agent-a": {
                            "label": "A",
                            "borderColor": "#123456",
                            "textColor": "#abcdef",
                        }
                    },
                }
            },
        )
        assert patched.status_code == 200
        assert patched.json()["settings"]["agent_badges"]["enabled"] is False
        assert "agent-a" in patched.json()["settings"]["agent_badges"]["entries"]

        synced_me = await client.get("/v1/me", headers=headers)
        assert synced_me.json()["preferences"] == patched.json()


@pytest.mark.asyncio
async def test_preferences_api_isolates_users_and_rejects_invalid_payloads(
    db_uri: str,
    runtime_init: None,
    tmp_path: Path,
) -> None:
    app = _preferences_app(db_uri, tmp_path)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        alice = {"x-test-user": "alice@example.com"}
        bob = {"x-test-user": "bob@example.com"}
        payload = {
            "version": 1,
            "settings": {"mobile_assistant": {"enabled": True}},
        }
        assert (
            await client.put("/v1/me/preferences", headers=alice, json=payload)
        ).status_code == 200

        bob_me = await client.get("/v1/me", headers=bob)
        assert bob_me.status_code == 200
        assert bob_me.json()["preferences"] is None

        unauthorized = await client.patch(
            "/v1/me/preferences/mobile_assistant",
            json={"value": {"enabled": False}},
        )
        assert unauthorized.status_code == 401

        unknown = await client.patch(
            "/v1/me/preferences/not_allowed",
            headers=alice,
            json={"value": {}},
        )
        assert unknown.status_code == 422

        oversized = await client.put(
            "/v1/me/preferences",
            headers=alice,
            json={
                "version": 1,
                "settings": {"usage_context": {"padding": "x" * (64 * 1024)}},
            },
        )
        assert oversized.status_code == 422

        raw_oversized = await client.put(
            "/v1/me/preferences",
            headers={**alice, "content-type": "application/json"},
            content=b'{"version":1,"settings":{"usage_context":{"padding":"'
            + (b"x" * (1024 * 1024))
            + b'"}}}',
        )
        assert raw_oversized.status_code == 413

        extra = await client.put(
            "/v1/me/preferences",
            headers=alice,
            json={"version": 1, "settings": {}, "unexpected": True},
        )
        assert extra.status_code == 422

        invalid_unicode = await client.patch(
            "/v1/me/preferences/usage_context",
            headers={**alice, "content-type": "application/json"},
            content=b'{"value":"\\ud800"}',
        )
        assert invalid_unicode.status_code == 422

        wrong_media_type = await client.put(
            "/v1/me/preferences",
            headers={**alice, "content-type": "text/plain"},
            content=b'{"version":1,"settings":{}}',
        )
        assert wrong_media_type.status_code == 415


@pytest.mark.asyncio
async def test_preferences_api_does_not_recreate_a_deleted_account(
    db_uri: str,
    runtime_init: None,
    tmp_path: Path,
) -> None:
    app = _deleted_account_app(db_uri, tmp_path)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.patch(
            "/v1/me/preferences/usage_context",
            headers={"x-test-user": "deleted@example.com"},
            json={"value": {"visible": True}},
        )

    assert response.status_code == 401
    assert response.json()["detail"] == "Account no longer exists"
    assert SqlAlchemyUserPreferencesStore(db_uri).get("deleted@example.com") is None


@pytest.mark.asyncio
async def test_preferences_api_rate_limits_writes_per_user(
    db_uri: str,
    runtime_init: None,
    tmp_path: Path,
) -> None:
    app = _preferences_app(db_uri, tmp_path)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"x-test-user": "rate-limited@example.com"}
        for index in range(120):
            response = await client.patch(
                "/v1/me/preferences/usage_context",
                headers=headers,
                json={"value": {"counter": index}},
            )
            assert response.status_code == 200

        limited = await client.patch(
            "/v1/me/preferences/usage_context",
            headers=headers,
            json={"value": {"counter": 120}},
        )
        assert limited.status_code == 429
        assert limited.headers["retry-after"] == "60"


def test_read_approval_timeout_defaults_on_missing_store_or_owner(db_uri: str) -> None:
    """S15: every gap resolves to 50 minutes and stop enabled."""
    store = SqlAlchemyUserPreferencesStore(db_uri)
    default = ApprovalTimeout(timeout_s=3000.0, stop_turn=True)
    assert read_approval_timeout(None, "alice@example.com") == default
    assert read_approval_timeout(store, None) == default
    assert read_approval_timeout(store, "alice@example.com") == default


def test_read_approval_timeout_clamps_and_defaults_invalid_fields(db_uri: str) -> None:
    """S15: timeoutMinutes clamps to 1..1380; invalid fields take defaults."""
    store = SqlAlchemyUserPreferencesStore(db_uri)
    default = ApprovalTimeout(timeout_s=3000.0, stop_turn=True)

    store.patch_namespace(
        "ten@example.com", "approval_timeout", {"timeoutMinutes": 10, "stopTurn": False}
    )
    assert read_approval_timeout(store, "ten@example.com") == ApprovalTimeout(
        timeout_s=600.0, stop_turn=False
    )

    store.patch_namespace(
        "low@example.com", "approval_timeout", {"timeoutMinutes": 0, "stopTurn": "yes"}
    )
    assert read_approval_timeout(store, "low@example.com") == ApprovalTimeout(
        timeout_s=60.0, stop_turn=True
    )

    store.patch_namespace("high@example.com", "approval_timeout", {"timeoutMinutes": 5000})
    assert read_approval_timeout(store, "high@example.com") == ApprovalTimeout(
        timeout_s=1380.0 * 60.0, stop_turn=True
    )

    store.patch_namespace(
        "text@example.com", "approval_timeout", {"timeoutMinutes": "x", "stopTurn": True}
    )
    assert read_approval_timeout(store, "text@example.com") == default

    store.patch_namespace("null@example.com", "approval_timeout", None)
    assert read_approval_timeout(store, "null@example.com") == default


def test_read_approval_timeout_tolerates_bad_rows_and_shapes() -> None:
    """S15: a corrupt row or malformed value never fails a hook."""
    default = ApprovalTimeout(timeout_s=3000.0, stop_turn=True)

    class _RaisingStore:
        def get(self, user_id: str) -> None:
            raise UserPreferencesValidationError("stored preferences are invalid JSON")

    class _ShapeStore:
        def __init__(self, value: object) -> None:
            self._value = value

        def get(self, user_id: str) -> object:
            return self._value

    assert read_approval_timeout(_RaisingStore(), "alice@example.com") == default
    assert read_approval_timeout(_ShapeStore([]), "alice@example.com") == default
    assert (
        read_approval_timeout(
            _ShapeStore({"settings": {"approval_timeout": "compact"}}),
            "alice@example.com",
        )
        == default
    )


class _OwnerGrantStore:
    """Minimal permission store returning fixed grants for owner resolution."""

    def __init__(self, grants: list[object]) -> None:
        self._grants = grants

    def list_for_session(
        self, conversation_id: str, limit: int = 1000
    ) -> tuple[list[object], None]:
        return self._grants, None


def test_approval_timeout_owner_resolution() -> None:
    """S16: local mode resolves to the reserved user; accounts need an owner grant."""
    assert (
        _approval_timeout_owner("conv_1", auth_provider=None, permission_store=None)
        == RESERVED_USER_LOCAL
    )

    provider = _HeaderAuthProvider()
    assert _approval_timeout_owner("conv_1", auth_provider=provider, permission_store=None) is None

    owner = SimpleNamespace(level=LEVEL_OWNER, user_id="owner@example.com")
    assert (
        _approval_timeout_owner(
            "conv_1",
            auth_provider=provider,
            permission_store=_OwnerGrantStore([owner]),
        )
        == "owner@example.com"
    )

    member = SimpleNamespace(level=LEVEL_EDIT, user_id="member@example.com")
    assert (
        _approval_timeout_owner(
            "conv_1",
            auth_provider=provider,
            permission_store=_OwnerGrantStore([member]),
        )
        is None
    )


@pytest.mark.asyncio
async def test_preferences_api_accepts_the_approval_timeout_namespace(
    db_uri: str,
    runtime_init: None,
    tmp_path: Path,
) -> None:
    """The new namespace is in the allowlist and survives the API round trip."""
    app = _preferences_app(db_uri, tmp_path)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"x-test-user": "timeout@example.com"}
        patched = await client.patch(
            "/v1/me/preferences/approval_timeout",
            headers=headers,
            json={"value": {"timeoutMinutes": 10, "stopTurn": False}},
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["settings"]["approval_timeout"] == {
            "timeoutMinutes": 10,
            "stopTurn": False,
        }

        assert read_approval_timeout(
            SqlAlchemyUserPreferencesStore(db_uri), "timeout@example.com"
        ) == ApprovalTimeout(timeout_s=600.0, stop_turn=False)
