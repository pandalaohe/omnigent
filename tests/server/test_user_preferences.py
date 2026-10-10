"""User-scoped, cross-device preferences API tests."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.requests import HTTPConnection

from omnigent.db.db_models import SqlPreference, current_workspace_id, workspace_scope
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
    AgentKeepWarm,
    ApprovalTimeout,
    CollabSettings,
    KeepWarmSettings,
    ResolvedAgentKeepWarm,
    SqlAlchemyUserPreferencesStore,
    UserPreferencesUserNotFoundError,
    UserPreferencesValidationError,
    clamp_host_offline_archive_s,
    clamp_keep_warm,
    keep_warm_for_agent,
    migrate_legacy_keep_warm,
    read_approval_timeout,
    read_collab_settings,
    read_keep_warm_settings,
    read_worktree_archive_mode,
    read_worktree_path_template,
    touch_runner_log_warning_dismissals,
    validate_preferences_envelope,
)
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_store import HostStore


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


def test_worktree_archive_preference_validates_and_migrates_once(db_uri: str) -> None:
    store = SqlAlchemyUserPreferencesStore(db_uri)
    assert read_worktree_archive_mode(store, "alice@example.com") == "never"
    assert store.migrate_worktree_archive("alice@example.com", True) == {"mode": "delete_safe"}
    assert store.migrate_worktree_archive("alice@example.com", False) == {"mode": "delete_safe"}
    assert read_worktree_archive_mode(store, "alice@example.com") == "delete_safe"
    assert read_worktree_archive_mode(store, "bob@example.com") == "never"
    with pytest.raises(UserPreferencesValidationError):
        store.patch_namespace("alice@example.com", "worktree_archive", {"mode": "force"})
    with pytest.raises(UserPreferencesValidationError):
        store.patch_namespace("alice@example.com", "worktree_archive", {"extra": True})
    store.patch_namespace("alice@example.com", "worktree_archive", {"mode": "never"})
    assert store.migrate_worktree_archive("alice@example.com", True) == {"mode": "never"}
    with workspace_scope(101):
        assert store.migrate_worktree_archive("alice@example.com", True) == {"mode": "delete_safe"}
    assert read_worktree_archive_mode(store, "alice@example.com") == "never"


@pytest.mark.asyncio
async def test_worktree_archive_preference_api_is_scoped_and_first_value_wins(
    db_uri: str, tmp_path: Path
) -> None:
    app = _preferences_app(db_uri, tmp_path)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            "/v1/me/preferences/worktree_archive/migrate",
            headers={"x-test-user": "alice@example.com"},
            json={"delete_safe": True},
        )
        assert first.status_code == 200
        assert first.json() == {"mode": "delete_safe"}
        second = await client.post(
            "/v1/me/preferences/worktree_archive/migrate",
            headers={"x-test-user": "alice@example.com"},
            json={"delete_safe": False},
        )
        assert second.json() == {"mode": "delete_safe"}
        other = await client.post(
            "/v1/me/preferences/worktree_archive/migrate",
            headers={"x-test-user": "bob@example.com"},
            json={"delete_safe": False},
        )
        assert other.json() == {"mode": "never"}
        invalid = await client.patch(
            "/v1/me/preferences/worktree_archive",
            headers={"x-test-user": "alice@example.com"},
            json={"value": {"mode": "force"}},
        )
        assert invalid.status_code == 422


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


def test_store_patch_namespace_round_trips_sound_alerts(db_uri: str) -> None:
    store = SqlAlchemyUserPreferencesStore(db_uri)
    value = {
        "levels": {
            "done": {"enabled": True, "sound": "chime"},
            "error": {"enabled": False, "sound": "alert"},
            "needs_response": {"enabled": True, "sound": "ping"},
        },
        "quietHours": {"enabled": True, "start": "23:00", "end": "08:00"},
        "primaryDeviceId": None,
        "mutedSessionIds": ["conv_a", "conv_b"],
    }

    merged = store.patch_namespace("alice@example.com", "sound_alerts", value)

    assert merged["settings"]["sound_alerts"] == value
    assert store.get("alice@example.com") == merged


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


def test_read_keep_warm_settings_defaults_on_missing_store_owner_or_namespace(
    db_uri: str,
) -> None:
    """Every gap resolves to the fail-safe keep-warm defaults with present=False."""
    store = SqlAlchemyUserPreferencesStore(db_uri)
    default = KeepWarmSettings(
        agents={}, host_offline_archive_s=14400, migrated_from_legacy_at=None, present=False
    )
    assert read_keep_warm_settings(None, "alice@example.com") == default
    assert read_keep_warm_settings(store, None) == default
    assert read_keep_warm_settings(store, "alice@example.com") == default

    store.patch_namespace("alice@example.com", "session_collab", {"childKeepWarmEnabled": True})
    assert read_keep_warm_settings(store, "alice@example.com") == default


def test_read_keep_warm_settings_parses_rows_and_defaults_invalid_fields(db_uri: str) -> None:
    """Per-row fields validate independently; a non-object row is skipped."""
    store = SqlAlchemyUserPreferencesStore(db_uri)
    store.patch_namespace(
        "kw@example.com",
        "keep_warm",
        {
            "agents": {
                "agent-a": {
                    "main": True,
                    "child": False,
                    "intervalSeconds": 900,
                    "maxSeconds": 7200,
                },
                "agent-b": {"main": "yes", "child": 1, "intervalSeconds": 0, "maxSeconds": -3},
                "agent-c": {"child": True},
                "agent-d": "not-a-row",
            },
            "hostOfflineArchiveSeconds": 0,
            "migratedFromLegacyAt": 123,
            "unexpectedKey": {"ignored": True},
        },
    )
    settings = read_keep_warm_settings(store, "kw@example.com")
    assert settings.present is True
    assert settings.host_offline_archive_s == 0
    assert settings.migrated_from_legacy_at == 123
    assert settings.agents == {
        "agent-a": AgentKeepWarm(main=True, child=False, interval_s=900, max_s=7200),
        "agent-b": AgentKeepWarm(main=False, child=False, interval_s=None, max_s=14400),
        "agent-c": AgentKeepWarm(main=False, child=True, interval_s=None, max_s=14400),
    }

    # The host-offline delay clamps to the shared keep-warm bounds on read.
    store.patch_namespace("clamped@example.com", "keep_warm", {"hostOfflineArchiveSeconds": 100})
    assert read_keep_warm_settings(store, "clamped@example.com").host_offline_archive_s == 3600


def test_read_keep_warm_settings_parses_cold_after_seconds(db_uri: str) -> None:
    """coldAfterSeconds keeps 0, clamps to 60..172800, else reads as None."""
    store = SqlAlchemyUserPreferencesStore(db_uri)
    store.patch_namespace(
        "cold@example.com",
        "keep_warm",
        {
            "agents": {
                "never": {"coldAfterSeconds": 0},
                "short": {"coldAfterSeconds": 30},
                "exact": {"coldAfterSeconds": 600},
                "huge": {"coldAfterSeconds": 999999},
                "negative": {"coldAfterSeconds": -1},
                "string": {"coldAfterSeconds": "5"},
                "float": {"coldAfterSeconds": 1.5},
                "bool": {"coldAfterSeconds": True},
                "missing": {},
            }
        },
    )
    settings = read_keep_warm_settings(store, "cold@example.com")
    assert settings.agents["never"].cold_after_s == 0
    assert settings.agents["short"].cold_after_s == 60
    assert settings.agents["exact"].cold_after_s == 600
    assert settings.agents["huge"].cold_after_s == 172800
    for agent_id in ("negative", "string", "float", "bool", "missing"):
        assert settings.agents[agent_id].cold_after_s is None


def test_read_keep_warm_settings_tolerates_bad_rows_and_shapes() -> None:
    """A corrupt row or malformed namespace value never fails a caller."""
    default = KeepWarmSettings(
        agents={}, host_offline_archive_s=14400, migrated_from_legacy_at=None, present=False
    )

    class _RaisingStore:
        def get(self, user_id: str) -> None:
            raise UserPreferencesValidationError("stored preferences are invalid JSON")

    class _SQLStore:
        def get(self, user_id: str) -> None:
            raise SQLAlchemyError("database unavailable")

    class _ShapeStore:
        def __init__(self, value: object) -> None:
            self._value = value

        def get(self, user_id: str) -> object:
            return self._value

    assert read_keep_warm_settings(_RaisingStore(), "alice@example.com") == default
    assert read_keep_warm_settings(_SQLStore(), "alice@example.com") == default
    assert read_keep_warm_settings(_ShapeStore([]), "alice@example.com") == default

    # A non-object namespace value reads as defaults, but the namespace exists.
    garbage = read_keep_warm_settings(
        _ShapeStore({"settings": {"keep_warm": "compact"}}), "alice@example.com"
    )
    assert garbage == KeepWarmSettings(
        agents={}, host_offline_archive_s=14400, migrated_from_legacy_at=None, present=True
    )

    # An invalid host-offline value takes the default while the rows survive.
    partial = read_keep_warm_settings(
        _ShapeStore(
            {
                "settings": {
                    "keep_warm": {
                        "agents": {"agent-a": {"main": True, "child": True}},
                        "hostOfflineArchiveSeconds": True,
                    }
                }
            }
        ),
        "alice@example.com",
    )
    assert partial.host_offline_archive_s == 14400
    assert partial.agents == {
        "agent-a": AgentKeepWarm(main=True, child=True, interval_s=None, max_s=14400)
    }


def test_keep_warm_for_agent_resolves_family_defaults_and_clamps(db_uri: str) -> None:
    """Absent agents are off; stored values clamp to the family bounds."""
    store = SqlAlchemyUserPreferencesStore(db_uri)
    store.patch_namespace(
        "resolve@example.com",
        "keep_warm",
        {
            "agents": {
                "defaulted": {"main": True, "child": True},
                "tight-claude": {
                    "main": True,
                    "child": False,
                    "intervalSeconds": 10,
                    "maxSeconds": 999999,
                },
                "wide-codex": {
                    "main": False,
                    "child": True,
                    "intervalSeconds": 5000,
                    "maxSeconds": 100,
                },
            }
        },
    )
    settings = read_keep_warm_settings(store, "resolve@example.com")
    assert keep_warm_for_agent(settings, None, "claude") is None
    assert keep_warm_for_agent(settings, "absent", "claude") is None
    assert keep_warm_for_agent(settings, "defaulted", "claude") == ResolvedAgentKeepWarm(
        main=True, child=True, interval_s=3300, max_s=14400
    )
    assert keep_warm_for_agent(settings, "defaulted", "codex") == ResolvedAgentKeepWarm(
        main=True, child=True, interval_s=1500, max_s=14400
    )
    assert keep_warm_for_agent(settings, "tight-claude", "claude") == ResolvedAgentKeepWarm(
        main=True, child=False, interval_s=300, max_s=172800
    )
    assert keep_warm_for_agent(settings, "wide-codex", "codex") == ResolvedAgentKeepWarm(
        main=False, child=True, interval_s=1740, max_s=3600
    )


def test_clamp_host_offline_archive_s_keeps_zero_and_clamps() -> None:
    """0 disables auto-archive and stays 0; other values take the shared bounds."""
    assert clamp_host_offline_archive_s(0) == 0
    assert clamp_host_offline_archive_s(100) == 3600
    assert clamp_host_offline_archive_s(14400) == 14400
    assert clamp_host_offline_archive_s(999999) == 172800


def test_migrate_legacy_keep_warm_writes_rows_for_the_given_agents(db_uri: str) -> None:
    """The migration writes one row per given agent and stamps the write."""
    store = SqlAlchemyUserPreferencesStore(db_uri)
    store.patch_namespace(
        "mig@example.com",
        "session_collab",
        {
            "childKeepWarmEnabled": True,
            "childKeepWarmClaudeIntervalSeconds": 3000,
            "childKeepWarmCodexIntervalSeconds": 1200,
        },
    )
    wrote = migrate_legacy_keep_warm(
        store,
        "mig@example.com",
        [("agent-claude", "claude"), ("agent-codex", "codex")],
        now=111,
    )
    assert wrote is True
    envelope = store.get("mig@example.com")
    assert envelope is not None
    assert envelope["settings"]["keep_warm"] == {
        "agents": {
            "agent-claude": {
                "main": False,
                "child": True,
                "intervalSeconds": 3000,
                "maxSeconds": 14400,
            },
            "agent-codex": {
                "main": False,
                "child": True,
                "intervalSeconds": 1200,
                "maxSeconds": 14400,
            },
        },
        "hostOfflineArchiveSeconds": 14400,
        "migratedFromLegacyAt": 111,
    }
    # The legacy keys are never changed or deleted.
    collab = read_collab_settings(store, "mig@example.com")
    assert collab.keep_warm_enabled is True
    assert (collab.keep_warm_claude_interval_s, collab.keep_warm_codex_interval_s) == (3000, 1200)
    # A second run is a no-op: the namespace now exists.
    assert (
        migrate_legacy_keep_warm(store, "mig@example.com", [("other", "claude")], now=222) is False
    )
    envelope = store.get("mig@example.com")
    assert envelope is not None
    assert envelope["settings"]["keep_warm"]["migratedFromLegacyAt"] == 111


def test_migrate_legacy_keep_warm_noops_when_namespace_exists_or_legacy_off(
    db_uri: str,
) -> None:
    """No namespace and no legacy switch means nothing is ever written."""
    store = SqlAlchemyUserPreferencesStore(db_uri)
    assert migrate_legacy_keep_warm(None, "none@example.com", [("a", "claude")], now=1) is False
    assert migrate_legacy_keep_warm(store, None, [("a", "claude")], now=1) is False

    # Legacy switch off (the default): nothing written.
    assert migrate_legacy_keep_warm(store, "off@example.com", [("a", "claude")], now=1) is False
    assert read_keep_warm_settings(store, "off@example.com").present is False

    # Legacy switch on but collaboration disabled: the legacy sweeper required
    # both flags, so nothing is written.
    store.patch_namespace(
        "disabled@example.com",
        "session_collab",
        {"enabled": False, "childKeepWarmEnabled": True},
    )
    assert (
        migrate_legacy_keep_warm(store, "disabled@example.com", [("a", "claude")], now=1) is False
    )
    assert read_keep_warm_settings(store, "disabled@example.com").present is False

    # Namespace already present: no-op even with the legacy switch on.
    store.patch_namespace("on@example.com", "session_collab", {"childKeepWarmEnabled": True})
    existing = {
        "agents": {"a": {"main": True, "child": False, "intervalSeconds": 600, "maxSeconds": 7200}}
    }
    store.patch_namespace("on@example.com", "keep_warm", existing)
    assert migrate_legacy_keep_warm(store, "on@example.com", [("b", "codex")], now=2) is False
    envelope = store.get("on@example.com")
    assert envelope is not None
    assert envelope["settings"]["keep_warm"] == existing


def test_migrate_legacy_keep_warm_leaves_later_agents_off(db_uri: str) -> None:
    """Scenario 27: an agent created after the migration is absent = off."""
    store = SqlAlchemyUserPreferencesStore(db_uri)
    store.patch_namespace("late@example.com", "session_collab", {"childKeepWarmEnabled": True})
    assert migrate_legacy_keep_warm(store, "late@example.com", [("agent-old", "claude")], now=7)
    settings = read_keep_warm_settings(store, "late@example.com")
    assert keep_warm_for_agent(settings, "agent-new", "claude") is None
    assert keep_warm_for_agent(settings, "agent-old", "claude") == ResolvedAgentKeepWarm(
        main=False, child=True, interval_s=3300, max_s=14400
    )


def test_migrate_legacy_keep_warm_returns_false_on_store_error() -> None:
    """A store failure logs and returns False instead of raising."""

    class _FailingStore:
        def get(self, user_id: str) -> object:
            return {
                "version": 1,
                "settings": {"session_collab": {"childKeepWarmEnabled": True}},
            }

        def patch_namespace(self, *args: object, **kwargs: object) -> None:
            raise SQLAlchemyError("database unavailable")

    assert (
        migrate_legacy_keep_warm(_FailingStore(), "err@example.com", [("a", "claude")], now=1)
        is False
    )


def test_migrate_legacy_keep_warm_returns_false_when_the_read_fails() -> None:
    """A transient read failure is not an absent namespace: nothing is written."""

    class _FlakyStore:
        def __init__(self) -> None:
            self.get_calls = 0
            self.patch_calls: list[object] = []

        def get(self, user_id: str) -> object:
            self.get_calls += 1
            if self.get_calls == 1:
                raise SQLAlchemyError("database unavailable")
            return {
                "version": 1,
                "settings": {"session_collab": {"childKeepWarmEnabled": True}},
            }

        def patch_namespace(self, *args: object, **kwargs: object) -> None:
            self.patch_calls.append(args)

    store = _FlakyStore()
    assert migrate_legacy_keep_warm(store, "err@example.com", [("a", "claude")], now=1) is False
    assert store.get_calls == 1
    assert store.patch_calls == []


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


@pytest.mark.asyncio
async def test_preferences_api_merges_stale_device_dismissals_and_prunes_old_ones(
    db_uri: str,
    runtime_init: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dismissals merge per detection instant; entries older than 30 days age out."""
    import omnigent.server.user_preferences_store as store_module

    clock = SimpleNamespace(now=1_800_000_000.0)
    monkeypatch.setattr(store_module, "time", SimpleNamespace(time=lambda: clock.now))

    app = _preferences_app(db_uri, tmp_path)
    transport = httpx.ASGITransport(app=app)
    flag_a = "2026-10-07T06:22:16+00:00"
    flag_b = "2026-10-07T07:22:16+00:00"
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"x-test-user": "runaway@example.com"}
        # Device A dismisses one detection instant.
        patched = await client.patch(
            "/v1/me/preferences/runner_log_warnings",
            headers=headers,
            json={"value": {flag_a: int(clock.now * 1000)}},
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["settings"]["runner_log_warnings"] == {flag_a: int(clock.now * 1000)}

        # Device B holds a stale snapshot with only its own instant; the
        # per-key merge keeps A's dismissal as well.
        patched = await client.patch(
            "/v1/me/preferences/runner_log_warnings",
            headers=headers,
            json={"value": {flag_b: int(clock.now * 1000) + 60_000}},
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["settings"]["runner_log_warnings"] == {
            flag_a: int(clock.now * 1000),
            flag_b: int(clock.now * 1000) + 60_000,
        }

        # 31 days later the first entry is past retention: the next write
        # drops it while the fresh dismissal stays.
        clock.now += 31 * 24 * 60 * 60
        patched = await client.patch(
            "/v1/me/preferences/runner_log_warnings",
            headers=headers,
            json={"value": {flag_b: int(clock.now * 1000)}},
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["settings"]["runner_log_warnings"] == {flag_b: int(clock.now * 1000)}


@pytest.mark.asyncio
async def test_preferences_api_never_moves_a_runaway_dismissal_touch_backwards(
    db_uri: str,
    runtime_init: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale device's older dismissed-at cannot lower the stored touch."""
    import omnigent.server.user_preferences_store as store_module

    clock = SimpleNamespace(now=1_800_000_000.0)
    monkeypatch.setattr(store_module, "time", SimpleNamespace(time=lambda: clock.now))

    app = _preferences_app(db_uri, tmp_path)
    transport = httpx.ASGITransport(app=app)
    flag = "2026-10-07T06:22:16+00:00"
    stored_touch = int(clock.now * 1000)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"x-test-user": "runaway-stale@example.com"}
        patched = await client.patch(
            "/v1/me/preferences/runner_log_warnings",
            headers=headers,
            json={"value": {flag: stored_touch}},
        )
        assert patched.status_code == 200, patched.text

        # A second device still holds the previous day's snapshot of the key.
        patched = await client.patch(
            "/v1/me/preferences/runner_log_warnings",
            headers=headers,
            json={"value": {flag: stored_touch - 24 * 60 * 60 * 1000}},
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["settings"]["runner_log_warnings"] == {flag: stored_touch}


@pytest.mark.asyncio
async def test_preferences_api_keeps_a_retouched_runaway_dismissal_and_prunes_untouched_ones(
    db_uri: str,
    runtime_init: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Daily re-touches keep an entry past 30 days; untouched ones still drop."""
    import omnigent.server.user_preferences_store as store_module

    clock = SimpleNamespace(now=1_800_000_000.0)
    monkeypatch.setattr(store_module, "time", SimpleNamespace(time=lambda: clock.now))

    app = _preferences_app(db_uri, tmp_path)
    transport = httpx.ASGITransport(app=app)
    day_s = 24 * 60 * 60
    flag_retouched = "2026-09-01T06:22:16+00:00"
    flag_untouched = "2026-09-02T06:22:16+00:00"
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"x-test-user": "runaway-retouch@example.com"}
        # Both episodes were dismissed 40 days ago.
        clock.now -= 40 * day_s
        patched = await client.patch(
            "/v1/me/preferences/runner_log_warnings",
            headers=headers,
            json={
                "value": {
                    flag_retouched: int(clock.now * 1000),
                    flag_untouched: int(clock.now * 1000),
                }
            },
        )
        assert patched.status_code == 200, patched.text

        # One was re-touched yesterday while its detection stayed confirmed.
        clock.now += 39 * day_s
        retouched_at = int(clock.now * 1000)
        patched = await client.patch(
            "/v1/me/preferences/runner_log_warnings",
            headers=headers,
            json={"value": {flag_retouched: retouched_at}},
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["settings"]["runner_log_warnings"] == {flag_retouched: retouched_at}

        # The next write drops the entry no client touched for over 30 days
        # while the re-touched entry survives.
        clock.now += day_s
        patched = await client.patch(
            "/v1/me/preferences/runner_log_warnings",
            headers=headers,
            json={"value": {}},
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["settings"]["runner_log_warnings"] == {flag_retouched: retouched_at}


def test_touch_runner_log_warning_dismissals_refreshes_only_stale_dismissed_flags(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The server touch refreshes every stale dismissal and never adds a flag.

    Any user holding the dismissal is refreshed, e.g. a viewer of a shared
    session who never owned it; accounts mode skips users without an account.
    """
    import omnigent.server.user_preferences_store as store_module

    clock = SimpleNamespace(now=1_800_000_000.0)
    monkeypatch.setattr(store_module, "time", SimpleNamespace(time=lambda: clock.now))
    store = SqlAlchemyUserPreferencesStore(db_uri)
    flag = "2026-10-07T06:22:16+00:00"
    other_flag = "2026-10-06T06:22:16+00:00"
    now_ms = clock.now * 1000.0
    day_ms = 24 * 60 * 60 * 1000

    accounts = SqlAlchemyAccountStore(db_uri)
    for user in ("owner@example.com", "viewer@example.com"):
        accounts.create_user_with_password(user, "test-password-hash")
    store.patch_namespace(
        "owner@example.com",
        "runner_log_warnings",
        {flag: now_ms - 2 * day_ms, other_flag: now_ms - 2 * day_ms},
    )
    store.patch_namespace("viewer@example.com", "runner_log_warnings", {flag: now_ms - 3 * day_ms})
    store.patch_namespace("fresh@example.com", "runner_log_warnings", {flag: now_ms - day_ms / 2})
    store.patch_namespace(
        "undismissed@example.com", "runner_log_warnings", {other_flag: now_ms - 2 * day_ms}
    )
    store.patch_namespace(
        "no-account@example.com", "runner_log_warnings", {flag: now_ms - 2 * day_ms}
    )
    store.patch_namespace("garbage@example.com", "runner_log_warnings", [1, 2])
    store.initialize("empty@example.com", {"version": 1, "settings": {}})
    with workspace_scope(101):
        store.patch_namespace(
            "elsewhere@example.com", "runner_log_warnings", {flag: now_ms - 2 * day_ms}
        )
    # A row that no longer decodes must not hide the other users.
    with Session(get_or_create_engine(db_uri)) as session:
        session.add(
            SqlPreference(
                workspace_id=current_workspace_id(),
                user_id="corrupt@example.com",
                key="settings.runner_log_warnings",
                value="{not json",
            )
        )
        session.commit()

    def dismissals(user: str) -> object:
        return store.get(user)["settings"]["runner_log_warnings"]

    # Accounts mode: only users with a live account row are written.
    assert (
        touch_runner_log_warning_dismissals(store, flag, create_if_missing=False, now_ms=now_ms)
        == 2
    )
    assert dismissals("owner@example.com") == {flag: now_ms, other_flag: now_ms - 2 * day_ms}
    assert dismissals("viewer@example.com") == {flag: now_ms}
    assert dismissals("no-account@example.com") == {flag: now_ms - 2 * day_ms}

    # Auth off: the same refresh reaches a user with no account row.
    assert (
        touch_runner_log_warning_dismissals(store, flag, create_if_missing=True, now_ms=now_ms)
        == 1
    )
    assert dismissals("no-account@example.com") == {flag: now_ms}
    assert dismissals("fresh@example.com") == {flag: now_ms - day_ms / 2}
    assert dismissals("undismissed@example.com") == {other_flag: now_ms - 2 * day_ms}
    assert dismissals("garbage@example.com") == [1, 2]
    assert store.get("empty@example.com") == {"version": 1, "settings": {}}
    with workspace_scope(101):
        assert dismissals("elsewhere@example.com") == {flag: now_ms - 2 * day_ms}


@pytest.mark.parametrize("accounts_mode", [False, True])
@pytest.mark.asyncio
async def test_app_runaway_hook_follows_the_preferences_account_policy(
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    accounts_mode: bool,
) -> None:
    """Outside accounts mode the app refreshes a header identity with no account row."""
    import omnigent.server.routes.host_tunnel as host_tunnel

    hooks: list[Any] = []
    real_router = host_tunnel.create_host_tunnel_router

    def _capture(*args: Any, **kwargs: Any) -> Any:
        hooks.append(kwargs["on_runner_log_runaway"])
        return real_router(*args, **kwargs)

    monkeypatch.setattr(host_tunnel, "create_host_tunnel_router", _capture)
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts-runaway-hook"))
    auth_provider = _AccountsAuthProvider() if accounts_mode else _HeaderAuthProvider()
    store = SqlAlchemyUserPreferencesStore(db_uri)
    create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        auth_provider=auth_provider,
        user_preferences_store=store,
        host_store=HostStore(db_uri),
    )
    if isinstance(auth_provider, _AccountsAuthProvider):
        auth_provider._source = "accounts"
    flag = "2026-10-07T06:22:16+00:00"
    stale = time.time() * 1000.0 - 2 * 24 * 60 * 60 * 1000
    user = "header-user@example.com"
    store.patch_namespace(user, "runner_log_warnings", {flag: stale})

    await hooks[0](flag)

    refreshed = store.get(user)["settings"]["runner_log_warnings"][flag]
    assert (refreshed == stale) is accounts_mode


def test_server_touched_dismissal_survives_a_stale_device_patch(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A device's 40-day-old snapshot cannot age a server touch back out."""
    import omnigent.server.user_preferences_store as store_module

    clock = SimpleNamespace(now=1_800_000_000.0)
    monkeypatch.setattr(store_module, "time", SimpleNamespace(time=lambda: clock.now))
    store = SqlAlchemyUserPreferencesStore(db_uri)
    owner = "stale-device@example.com"
    flag = "2026-09-01T06:22:16+00:00"
    day_s = 24 * 60 * 60

    SqlAlchemyAccountStore(db_uri).create_user_with_password(owner, "test-password-hash")
    clock.now -= 40 * day_s
    stale_touch = int(clock.now * 1000)
    store.patch_namespace(owner, "runner_log_warnings", {flag: stale_touch})

    # The host keeps confirming the detection, so the server touches yesterday.
    clock.now += 39 * day_s
    touched_at = clock.now * 1000.0
    assert (
        touch_runner_log_warning_dismissals(
            store, flag, create_if_missing=False, now_ms=touched_at
        )
        == 1
    )

    # The stale device patches its 40-day-old touch; the per-key merge keeps
    # the newer server touch and the prune spares it.
    clock.now += day_s
    merged = store.patch_namespace(owner, "runner_log_warnings", {flag: stale_touch})
    assert merged["settings"]["runner_log_warnings"] == {flag: touched_at}


@pytest.mark.asyncio
async def test_preferences_api_accepts_the_keep_warm_namespace(
    db_uri: str,
    runtime_init: None,
    tmp_path: Path,
) -> None:
    """The keep_warm namespace is allowlisted by the API and reads back resolved."""
    app = _preferences_app(db_uri, tmp_path)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"x-test-user": "keepwarm@example.com"}
        patched = await client.patch(
            "/v1/me/preferences/keep_warm",
            headers=headers,
            json={
                "value": {
                    "agents": {
                        "agent-a": {
                            "main": True,
                            "child": False,
                            "intervalSeconds": 10,
                            "maxSeconds": 7200,
                        }
                    },
                    "hostOfflineArchiveSeconds": 0,
                }
            },
        )
        assert patched.status_code == 200, patched.text

    settings = read_keep_warm_settings(
        SqlAlchemyUserPreferencesStore(db_uri), "keepwarm@example.com"
    )
    assert settings.host_offline_archive_s == 0
    assert keep_warm_for_agent(settings, "agent-a", "claude") == ResolvedAgentKeepWarm(
        main=True, child=False, interval_s=300, max_s=7200
    )
    assert keep_warm_for_agent(settings, "agent-b", "codex") is None


_ENTRY_TEMPLATE = "{entry}/.worktrees/{repo}/{branch}"


def test_store_accepts_and_clears_the_worktree_location_namespace(db_uri: str) -> None:
    """A valid template stores and reads back; ``null`` value and patch both unset."""
    store = SqlAlchemyUserPreferencesStore(db_uri)
    patched = store.patch_namespace(
        "alice@example.com", "worktree_location", {"pathTemplate": _ENTRY_TEMPLATE}
    )
    assert patched["settings"]["worktree_location"] == {"pathTemplate": _ENTRY_TEMPLATE}
    assert read_worktree_path_template(store, "alice@example.com") == _ENTRY_TEMPLATE

    # An explicit {"pathTemplate": null} keeps the namespace with no template.
    patched = store.patch_namespace(
        "alice@example.com", "worktree_location", {"pathTemplate": None}
    )
    assert patched["settings"]["worktree_location"] == {"pathTemplate": None}
    assert read_worktree_path_template(store, "alice@example.com") is None

    removed = store.patch_namespace("alice@example.com", "worktree_location", None)
    assert "worktree_location" not in removed["settings"]
    assert read_worktree_path_template(store, "alice@example.com") is None


@pytest.mark.parametrize(
    ("value", "match"),
    [
        ("a string", "worktree_location must be an object"),
        ({"template": "x"}, "unsupported worktree_location key: template"),
        ({"pathTemplate": 42}, "must be a string or null"),
        ({"pathTemplate": ""}, "must not be empty"),
        ({"pathTemplate": "wt/{branch}"}, "must contain {repo}"),
        ({"pathTemplate": "{entry}/{repo}"}, "must contain {branch}"),
        ({"pathTemplate": "{unknown}/{repo}/{branch}"}, "unknown token"),
        ({"pathTemplate": "{entry}/wt/{repo}/{branch"}, "stray"),
        ({"pathTemplate": "{entry}/../{repo}/{branch}"}, "must not contain '.' or '..'"),
        ({"pathTemplate": "{entry}/~/x/{repo}/{branch}"}, "only as the whole first segment"),
        ({"pathTemplate": "{entry}/" + "a" * 510 + "/{repo}/{branch}"}, "at most 512"),
        ({"pathTemplate": "{entry}/wt\t/{repo}/{branch}"}, "control characters"),
    ],
)
def test_store_rejects_invalid_worktree_location_values(
    db_uri: str, value: object, match: str
) -> None:
    """Write validation runs the template grammar; a refusal writes no row."""
    store = SqlAlchemyUserPreferencesStore(db_uri)
    with pytest.raises(UserPreferencesValidationError, match=match):
        store.patch_namespace("alice@example.com", "worktree_location", value)
    # The refusal happened before the write transaction: no rows at all.
    assert store.get("alice@example.com") is None


def test_stored_worktree_location_under_an_older_rule_still_reads(db_uri: str) -> None:
    """A stored value a newer rule would reject never breaks the envelope.

    Write validation lives only on the incoming value, so a template stored
    under an older rule survives ``_assemble_envelope``: sibling namespaces
    keep reading and patching, and the reader returns the string unchanged
    (the host re-validates it before rendering).
    """
    store = SqlAlchemyUserPreferencesStore(db_uri)
    with Session(get_or_create_engine(db_uri)) as session:
        session.add(
            SqlPreference(
                workspace_id=0,
                user_id="alice@example.com",
                key="settings.version",
                value="1",
            )
        )
        session.add(
            SqlPreference(
                workspace_id=0,
                user_id="alice@example.com",
                key="settings.worktree_location",
                value='{"pathTemplate":"wt/{branch}"}',
            )
        )
        session.commit()

    envelope = store.get("alice@example.com")
    assert envelope is not None
    assert envelope["settings"]["worktree_location"] == {"pathTemplate": "wt/{branch}"}
    assert read_worktree_path_template(store, "alice@example.com") == "wt/{branch}"

    merged = store.patch_namespace("alice@example.com", "usage_context", {"visible": True})
    assert merged["settings"]["worktree_location"] == {"pathTemplate": "wt/{branch}"}
    assert merged["settings"]["usage_context"] == {"visible": True}


def test_read_worktree_path_template_defaults_on_missing_store_owner_or_namespace(
    db_uri: str,
) -> None:
    """Every gap resolves to ``None`` — the upstream sibling layout."""
    store = SqlAlchemyUserPreferencesStore(db_uri)
    assert read_worktree_path_template(None, "alice@example.com") is None
    assert read_worktree_path_template(store, None) is None
    assert read_worktree_path_template(store, "alice@example.com") is None


def test_read_worktree_path_template_tolerates_bad_rows_and_shapes() -> None:
    """A corrupt row or malformed value never fails a worktree create."""

    class _RaisingStore:
        def get(self, user_id: str) -> None:
            raise UserPreferencesValidationError("stored preferences are invalid JSON")

    class _ValueErrorStore:
        def get(self, user_id: str) -> None:
            raise ValueError("oversized integer in stored preferences")

    class _DatabaseErrorStore:
        def get(self, user_id: str) -> None:
            raise OperationalError("select", {}, Exception("down"))

    class _ShapeStore:
        def __init__(self, value: object) -> None:
            self._value = value

        def get(self, user_id: str) -> object:
            return self._value

    assert read_worktree_path_template(_RaisingStore(), "alice@example.com") is None
    assert read_worktree_path_template(_ValueErrorStore(), "alice@example.com") is None
    assert read_worktree_path_template(_DatabaseErrorStore(), "alice@example.com") is None
    assert read_worktree_path_template(_ShapeStore([]), "alice@example.com") is None
    assert (
        read_worktree_path_template(
            _ShapeStore({"settings": {"worktree_location": "compact"}}),
            "alice@example.com",
        )
        is None
    )
    assert (
        read_worktree_path_template(
            _ShapeStore({"settings": {"worktree_location": {"pathTemplate": None}}}),
            "alice@example.com",
        )
        is None
    )
    assert (
        read_worktree_path_template(
            _ShapeStore({"settings": {"worktree_location": {"pathTemplate": ""}}}),
            "alice@example.com",
        )
        is None
    )
    assert (
        read_worktree_path_template(
            _ShapeStore({"settings": {"worktree_location": {"pathTemplate": 42}}}),
            "alice@example.com",
        )
        is None
    )
    assert (
        read_worktree_path_template(
            _ShapeStore({"settings": {"worktree_location": {"pathTemplate": _ENTRY_TEMPLATE}}}),
            "alice@example.com",
        )
        == _ENTRY_TEMPLATE
    )


@pytest.mark.asyncio
async def test_preferences_api_accepts_the_worktree_location_namespace(
    db_uri: str,
    runtime_init: None,
    tmp_path: Path,
) -> None:
    """The worktree_location namespace is allowlisted by the API and reads back."""
    app = _preferences_app(db_uri, tmp_path)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"x-test-user": "worktree@example.com"}
        patched = await client.patch(
            "/v1/me/preferences/worktree_location",
            headers=headers,
            json={"value": {"pathTemplate": _ENTRY_TEMPLATE}},
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["settings"]["worktree_location"] == {"pathTemplate": _ENTRY_TEMPLATE}

    assert (
        read_worktree_path_template(SqlAlchemyUserPreferencesStore(db_uri), "worktree@example.com")
        == _ENTRY_TEMPLATE
    )


@pytest.mark.asyncio
async def test_preferences_api_rejects_an_invalid_worktree_location_template(
    db_uri: str,
    runtime_init: None,
    tmp_path: Path,
) -> None:
    """A 422 detail carries the template rule verbatim; nothing is stored."""
    app = _preferences_app(db_uri, tmp_path)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"x-test-user": "worktree@example.com"}
        refused = await client.patch(
            "/v1/me/preferences/worktree_location",
            headers=headers,
            json={"value": {"pathTemplate": "wt/{branch}"}},
        )
        assert refused.status_code == 422, refused.text
        assert "must contain {repo}" in refused.json()["detail"]

        refused = await client.patch(
            "/v1/me/preferences/worktree_location",
            headers=headers,
            json={"value": {"other": "{entry}/{repo}/{branch}"}},
        )
        assert refused.status_code == 422, refused.text
        assert "unsupported worktree_location key: other" in refused.json()["detail"]

    assert (
        read_worktree_path_template(SqlAlchemyUserPreferencesStore(db_uri), "worktree@example.com")
        is None
    )


@pytest.mark.asyncio
async def test_preferences_api_clears_the_worktree_location_namespace(
    db_uri: str,
    runtime_init: None,
    tmp_path: Path,
) -> None:
    """PATCH ``null`` removes the namespace; the reader defaults to ``None``."""
    app = _preferences_app(db_uri, tmp_path)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"x-test-user": "worktree@example.com"}
        patched = await client.patch(
            "/v1/me/preferences/worktree_location",
            headers=headers,
            json={"value": {"pathTemplate": _ENTRY_TEMPLATE}},
        )
        assert patched.status_code == 200, patched.text

        cleared = await client.patch(
            "/v1/me/preferences/worktree_location",
            headers=headers,
            json={"value": None},
        )
        assert cleared.status_code == 200, cleared.text
        assert "worktree_location" not in cleared.json()["settings"]

    assert (
        read_worktree_path_template(SqlAlchemyUserPreferencesStore(db_uri), "worktree@example.com")
        is None
    )


@pytest.mark.asyncio
async def test_preferences_api_initialize_validates_worktree_location(
    db_uri: str,
    runtime_init: None,
    tmp_path: Path,
) -> None:
    """PUT applies the same rule: an invalid template 422s and stores nothing."""
    app = _preferences_app(db_uri, tmp_path)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"x-test-user": "worktree@example.com"}
        refused = await client.put(
            "/v1/me/preferences",
            headers=headers,
            json={
                "version": 1,
                "settings": {"worktree_location": {"pathTemplate": "wt/{branch}"}},
            },
        )
        assert refused.status_code == 422, refused.text
        assert "must contain {repo}" in refused.json()["detail"]

        accepted = await client.put(
            "/v1/me/preferences",
            headers=headers,
            json={
                "version": 1,
                "settings": {"worktree_location": {"pathTemplate": _ENTRY_TEMPLATE}},
            },
        )
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["settings"]["worktree_location"] == {
            "pathTemplate": _ENTRY_TEMPLATE
        }


@pytest.mark.asyncio
async def test_preferences_api_accepts_the_sidebar_layout_namespace(
    db_uri: str,
    runtime_init: None,
    tmp_path: Path,
) -> None:
    """The sidebar layout namespace is allowlisted and round-trips whole values."""
    app = _preferences_app(db_uri, tmp_path)
    transport = httpx.ASGITransport(app=app)
    layout = {
        "version": 1,
        "sections": [
            {
                "id": "s1",
                "kind": "projects",
                "name": "Work",
                "maxRows": 10,
                "projectIds": ["p1", "p2"],
            },
            {"id": "s2", "kind": "other_projects", "name": "Projects", "maxRows": None},
            {"id": "s3", "kind": "other_sessions", "name": "Sessions", "maxRows": None},
        ],
    }
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"x-test-user": "sidebar@example.com"}
        patched = await client.patch(
            "/v1/me/preferences/sidebar_layout",
            headers=headers,
            json={"value": layout},
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["settings"]["sidebar_layout"] == layout

        synced_me = await client.get("/v1/me", headers=headers)
        assert synced_me.status_code == 200
        assert synced_me.json()["preferences"]["settings"]["sidebar_layout"] == layout
