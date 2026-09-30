"""User-scoped, cross-device preferences API tests."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.requests import HTTPConnection

from omnigent.db.db_models import SqlPreference, workspace_scope
from omnigent.db.utils import get_or_create_engine
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.accounts_store import SqlAlchemyAccountStore
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
    CollabSettings,
    SqlAlchemyUserPreferencesStore,
    UserPreferencesUserNotFoundError,
    UserPreferencesValidationError,
    clamp_keep_warm,
    read_approval_timeout,
    read_collab_settings,
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

    with pytest.raises(UserPreferencesValidationError, match="unsupported preferences namespace"):
        store.patch_namespace("alice@example.com", "not_allowed", {})


def test_store_patches_of_sibling_namespaces_merge_across_instances(db_uri: str) -> None:
    """Two stores patching different namespaces: the second envelope has both."""
    first = SqlAlchemyUserPreferencesStore(db_uri)
    second = SqlAlchemyUserPreferencesStore(db_uri)

    first.patch_namespace("alice@example.com", "usage_context", {"visible": True})
    merged = second.patch_namespace("alice@example.com", "context_indicator", "compact")

    expected = {
        "version": 1,
        "settings": {
            "usage_context": {"visible": True},
            "context_indicator": "compact",
        },
    }
    assert merged == expected
    assert first.get("alice@example.com") == expected


@pytest.mark.parametrize("create_if_missing", [True, False], ids=["external", "accounts"])
def test_concurrent_patches_of_sibling_namespaces_serialize_per_user(
    db_uri: str, monkeypatch: pytest.MonkeyPatch, create_if_missing: bool
) -> None:
    """A racing patch waits on the per-user lock, so it merges the committed one."""
    from omnigent.server import user_preferences_store as store_module

    first = SqlAlchemyUserPreferencesStore(db_uri)
    if first._engine.dialect.name == "sqlite":
        pytest.skip("SQLite already serializes writers with its database-wide write lock")
    second = SqlAlchemyUserPreferencesStore(db_uri)
    if not create_if_missing:
        SqlAlchemyAccountStore(db_uri).create_user_with_password(
            "alice@example.com", "test-password-hash"
        )

    entered = [threading.Event(), threading.Event()]
    release = threading.Event()
    read_settings = store_module._read_settings

    def gated_read_settings(session: Session, user_id: str):
        result = read_settings(session, user_id)
        writer = int(threading.current_thread().name.rsplit("_", 1)[1])
        entered[writer].set()
        assert writer != 0 or release.wait(10)
        return result

    monkeypatch.setattr(store_module, "_read_settings", gated_read_settings)

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="writer") as pool:
        first_patch = pool.submit(
            first.patch_namespace,
            "alice@example.com",
            "usage_context",
            {"visible": True},
            create_if_missing=create_if_missing,
        )
        # Writer 0 pauses holding its write transaction open.
        assert entered[0].wait(10)
        second_patch = pool.submit(
            second.patch_namespace,
            "alice@example.com",
            "context_indicator",
            "compact",
            create_if_missing=create_if_missing,
        )
        # Writer 1 must block on the account lock (accounts mode) or the
        # version-row lock before it reads, so it cannot assemble an envelope
        # missing the still-open patch.
        assert not entered[1].wait(0.5)
        release.set()
        assert first_patch.result(timeout=10) == {
            "version": 1,
            "settings": {"usage_context": {"visible": True}},
        }
        assert second_patch.result(timeout=10) == {
            "version": 1,
            "settings": {
                "usage_context": {"visible": True},
                "context_indicator": "compact",
            },
        }


def test_store_round_trips_namespaces_through_settings_rows(db_uri: str) -> None:
    """Each namespace is one settings.<namespace> row; reads reassemble them."""
    store = SqlAlchemyUserPreferencesStore(db_uri)
    envelope = {
        "version": 1,
        "settings": {
            "keyboard_shortcuts": {"enabled": True},
            "context_indicator": "compact",
        },
    }
    assert store.initialize("alice@example.com", envelope) == envelope

    with Session(get_or_create_engine(db_uri)) as session:
        rows = {
            row.key: row.value
            for row in session.scalars(
                select(SqlPreference).where(SqlPreference.user_id == "alice@example.com")
            )
        }

    assert rows == {
        "settings.version": "1",
        "settings.keyboard_shortcuts": '{"enabled":true}',
        "settings.context_indicator": '"compact"',
    }
    assert store.get("alice@example.com") == envelope


def test_store_patch_touches_only_its_namespace_row(db_uri: str) -> None:
    """A namespace patch leaves sibling settings rows and project order alone."""
    store = SqlAlchemyUserPreferencesStore(db_uri)
    store.initialize(
        "alice@example.com",
        {"version": 1, "settings": {"usage_context": {"visible": True}}},
    )
    with Session(get_or_create_engine(db_uri)) as session:
        session.add(
            SqlPreference(
                workspace_id=0,
                user_id="alice@example.com",
                key="project_order",
                value='{"sort_mode":"alphabetical"}',
            )
        )
        session.commit()

    store.patch_namespace("alice@example.com", "agent_badges", {"enabled": False})

    with Session(get_or_create_engine(db_uri)) as session:
        rows = {
            row.key: row.value
            for row in session.scalars(
                select(SqlPreference).where(SqlPreference.user_id == "alice@example.com")
            )
        }
    assert rows == {
        "settings.version": "1",
        "settings.usage_context": '{"visible":true}',
        "settings.agent_badges": '{"enabled":false}',
        "project_order": '{"sort_mode":"alphabetical"}',
    }


def test_store_size_limit_spans_namespaces(db_uri: str) -> None:
    """The 64 KiB cap covers the merged envelope, not one namespace row."""
    store = SqlAlchemyUserPreferencesStore(db_uri)
    padding = "x" * (40 * 1024)
    store.patch_namespace("alice@example.com", "usage_context", padding)

    with pytest.raises(UserPreferencesValidationError, match="64 KiB"):
        store.patch_namespace("alice@example.com", "keyboard_shortcuts", padding)

    assert store.get("alice@example.com") == {
        "version": 1,
        "settings": {"usage_context": padding},
    }


def test_store_scopes_rows_to_the_current_workspace(db_uri: str) -> None:
    """The same user id in another workspace never shares preferences."""
    store = SqlAlchemyUserPreferencesStore(db_uri)
    with workspace_scope(101):
        store.patch_namespace("alice@example.com", "usage_context", {"visible": True})
    with workspace_scope(102):
        assert store.get("alice@example.com") is None
        store.patch_namespace("alice@example.com", "usage_context", {"visible": False})
    with workspace_scope(101):
        assert store.get("alice@example.com") == {
            "version": 1,
            "settings": {"usage_context": {"visible": True}},
        }


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

        patched = await client.patch(
            "/v1/me/preferences/agent_pins",
            headers=headers,
            json={"value": {"ids": ["ag_polly", "ag_debby"]}},
        )
        assert patched.status_code == 200
        assert patched.json()["settings"]["agent_pins"] == {"ids": ["ag_polly", "ag_debby"]}

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


def test_read_approval_timeout_reads_and_defaults_async_approvals(db_uri: str) -> None:
    """The async-approval switch: explicit False reads back; any gap is True."""
    store = SqlAlchemyUserPreferencesStore(db_uri)
    default = ApprovalTimeout(timeout_s=3000.0, stop_turn=True)
    assert default.async_approvals is True

    store.patch_namespace(
        "off@example.com",
        "approval_timeout",
        {"timeoutMinutes": 10, "stopTurn": False, "asyncApprovals": False},
    )
    assert read_approval_timeout(store, "off@example.com") == ApprovalTimeout(
        timeout_s=600.0,
        stop_turn=False,
        async_approvals=False,
    )

    store.patch_namespace(
        "on@example.com",
        "approval_timeout",
        {"asyncApprovals": True},
    )
    on = read_approval_timeout(store, "on@example.com")
    assert on.async_approvals is True
    assert on == default

    store.patch_namespace(
        "garbage@example.com",
        "approval_timeout",
        {"asyncApprovals": "yes"},
    )
    garbage = read_approval_timeout(store, "garbage@example.com")
    assert garbage.async_approvals is True
    assert garbage == default


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


def test_read_collab_settings_defaults_on_missing_store_owner_or_namespace(
    db_uri: str,
) -> None:
    """Every gap resolves to the fail-safe collaboration defaults."""
    store = SqlAlchemyUserPreferencesStore(db_uri)
    default = CollabSettings()
    assert read_collab_settings(None, "alice@example.com") == default
    assert read_collab_settings(store, None) == default
    empty = read_collab_settings(store, "alice@example.com")
    assert empty == default
    assert empty.open_rate_count == 10
    assert empty.open_rate_window_s == 60
    assert empty.keep_warm_enabled is False

    store.patch_namespace("alice@example.com", "agent_badges", {"enabled": False})
    assert read_collab_settings(store, "alice@example.com") == default


def test_read_collab_settings_reads_all_fifteen_fields(db_uri: str) -> None:
    """All fifteen camelCase fields load; unknown keys are ignored."""
    store = SqlAlchemyUserPreferencesStore(db_uri)
    store.patch_namespace(
        "all@example.com",
        "session_collab",
        {
            "enabled": False,
            "openRateCount": 9,
            "openRateWindowSeconds": 90,
            "relayDepthMax": 12,
            "pairRateCount": 3,
            "pairRateWindowSeconds": 30,
            "senderRateCount": 120,
            "senderRateWindowSeconds": 1200,
            "duplicateWindowSeconds": 300,
            "undeliveredTtlSeconds": 7200,
            "flowTimerEnabled": False,
            "childKeepWarmEnabled": True,
            "childKeepWarmClaudeIntervalSeconds": 3000,
            "childKeepWarmCodexIntervalSeconds": 1200,
            "childKeepWarmMaxSeconds": 14400,
            "unexpectedKey": {"nested": True},
        },
    )
    assert read_collab_settings(store, "all@example.com") == CollabSettings(
        enabled=False,
        open_rate_count=9,
        open_rate_window_s=90,
        relay_depth_max=12,
        pair_rate_count=3,
        pair_rate_window_s=30,
        sender_rate_count=120,
        sender_rate_window_s=1200,
        duplicate_window_s=300,
        undelivered_ttl_s=7200,
        flow_timer_enabled=False,
        keep_warm_enabled=True,
        keep_warm_claude_interval_s=3000,
        keep_warm_codex_interval_s=1200,
        keep_warm_max_s=14400,
    )


@pytest.mark.parametrize(
    ("stored_key", "invalid", "attribute"),
    [
        ("enabled", 1, "enabled"),
        ("enabled", "yes", "enabled"),
        ("openRateCount", True, "open_rate_count"),
        ("openRateCount", 0, "open_rate_count"),
        ("openRateCount", -1, "open_rate_count"),
        ("openRateCount", "5", "open_rate_count"),
        ("openRateCount", 5.5, "open_rate_count"),
        ("childKeepWarmEnabled", "yes", "keep_warm_enabled"),
        ("childKeepWarmEnabled", 1, "keep_warm_enabled"),
        ("childKeepWarmClaudeIntervalSeconds", 0, "keep_warm_claude_interval_s"),
        ("childKeepWarmCodexIntervalSeconds", True, "keep_warm_codex_interval_s"),
        ("childKeepWarmMaxSeconds", "28800", "keep_warm_max_s"),
    ],
)
def test_read_collab_settings_falls_back_per_field(
    db_uri: str, stored_key: str, invalid: object, attribute: str
) -> None:
    """An invalid field takes its own default while valid siblings survive."""
    store = SqlAlchemyUserPreferencesStore(db_uri)
    store.patch_namespace(
        "field@example.com",
        "session_collab",
        {stored_key: invalid, "relayDepthMax": 12},
    )
    settings = read_collab_settings(store, "field@example.com")
    assert settings.relay_depth_max == 12
    assert getattr(settings, attribute) == getattr(CollabSettings(), attribute)


def test_clamp_keep_warm_returns_defaults_inside_the_bounds() -> None:
    """The defaults are already inside every bound and pass through unchanged."""
    assert clamp_keep_warm(CollabSettings()) == (3300, 1500, 28800)


def test_clamp_keep_warm_clamps_stored_values_to_the_bounds(db_uri: str) -> None:
    """A hand-written out-of-range window is clamped on read, not rejected."""
    store = SqlAlchemyUserPreferencesStore(db_uri)
    store.patch_namespace(
        "clamp@example.com",
        "session_collab",
        {
            "childKeepWarmClaudeIntervalSeconds": 99999,
            "childKeepWarmCodexIntervalSeconds": 1,
            "childKeepWarmMaxSeconds": 999999,
        },
    )
    settings = read_collab_settings(store, "clamp@example.com")
    assert settings.keep_warm_claude_interval_s == 99999
    assert clamp_keep_warm(settings) == (3540, 300, 172800)

    store.patch_namespace(
        "low@example.com",
        "session_collab",
        {
            "childKeepWarmClaudeIntervalSeconds": 1,
            "childKeepWarmCodexIntervalSeconds": 99999,
            "childKeepWarmMaxSeconds": 1,
        },
    )
    assert clamp_keep_warm(read_collab_settings(store, "low@example.com")) == (
        300,
        1740,
        3600,
    )


def test_read_collab_settings_tolerates_bad_rows_and_shapes() -> None:
    """A corrupt row or malformed namespace value never fails a caller."""
    default = CollabSettings()

    class _RaisingStore:
        def get(self, user_id: str) -> None:
            raise UserPreferencesValidationError("stored preferences are invalid JSON")

    class _ShapeStore:
        def __init__(self, value: object) -> None:
            self._value = value

        def get(self, user_id: str) -> object:
            return self._value

    assert read_collab_settings(_RaisingStore(), "alice@example.com") == default
    assert read_collab_settings(_ShapeStore([]), "alice@example.com") == default
    assert (
        read_collab_settings(
            _ShapeStore({"settings": {"session_collab": "compact"}}),
            "alice@example.com",
        )
        == default
    )


def test_read_collab_settings_defaults_on_sqlalchemy_error() -> None:
    """A database failure resolves to defaults instead of raising."""

    class _RaisingStore:
        def get(self, user_id: str) -> None:
            raise SQLAlchemyError("database unavailable")

    assert read_collab_settings(_RaisingStore(), "alice@example.com") == CollabSettings()


def test_store_accepts_session_collab_patch_and_reads_it_back(db_uri: str) -> None:
    """The namespace is allowlisted; a partial patch keeps field defaults."""
    store = SqlAlchemyUserPreferencesStore(db_uri)
    patched = store.patch_namespace(
        "collab@example.com",
        "session_collab",
        {"enabled": False, "openRateCount": 10},
    )
    assert patched["settings"]["session_collab"] == {"enabled": False, "openRateCount": 10}
    assert read_collab_settings(store, "collab@example.com") == CollabSettings(
        enabled=False, open_rate_count=10
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
async def test_preferences_api_accepts_the_calling_defaults_namespaces(
    db_uri: str,
    runtime_init: None,
    tmp_path: Path,
) -> None:
    """Both calling-defaults namespaces are allowlisted and round-trip."""
    app = _preferences_app(db_uri, tmp_path)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"x-test-user": "calling@example.com"}
        master = {"HDS": {"codex": {"model": "gpt-6-sol", "effort": "high"}}}
        patched = await client.patch(
            "/v1/me/preferences/calling_defaults",
            headers=headers,
            json={"value": master},
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["settings"]["calling_defaults"] == master

        last = {"enabled": True, "p:proj": {"HDS": {"last_agent_id": "codex-sdk"}}}
        patched = await client.patch(
            "/v1/me/preferences/calling_last",
            headers=headers,
            json={"value": last},
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["settings"]["calling_last"] == last


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


@pytest.mark.asyncio
async def test_preferences_api_accepts_the_host_colors_namespace(
    db_uri: str,
    runtime_init: None,
    tmp_path: Path,
) -> None:
    """The host_colors namespace is allowlisted and merges one host per patch."""
    app = _preferences_app(db_uri, tmp_path)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"x-test-user": "hostcolors@example.com"}
        first_host = {"51dc949aba31e24ca8f047d6fba31a0d": "teal"}
        patched = await client.patch(
            "/v1/me/preferences/host_colors",
            headers=headers,
            json={"value": first_host},
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["settings"]["host_colors"] == first_host

        # One-key patch: a second host colour merges without clobbering the
        # first, which is what keeps concurrent per-host picks safe.
        second_host = {"9b2ec6de30f5e014c7056afe505510c3": "amber"}
        patched = await client.patch(
            "/v1/me/preferences/host_colors",
            headers=headers,
            json={"value": second_host},
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["settings"]["host_colors"] == {**first_host, **second_host}
