"""Persistence for versioned, user-scoped web preferences.

Each namespace lives in one row of the shared ``preferences`` table under the
``settings.<namespace>`` key, next to a ``settings.version`` marker row that
records the envelope version and marks the envelope initialized. Reads assemble
those rows back into the version-1 envelope. Namespace updates run in a single
database transaction, so concurrent clients cannot observe a partially-written
preference set. The store owns structural and size validation as a
defence-in-depth boundary for non-HTTP callers.
"""

from __future__ import annotations

import json
import logging
import math
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal, TypeAlias

import zstandard
from sqlalchemy import LargeBinary, delete, select, type_coerce
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.sql.dml import Insert

from omnigent.db.account_authority import lock_account
from omnigent.db.compression import decode
from omnigent.db.db_models import SqlPreference, current_workspace_id
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    run_write_transaction,
)

logger = logging.getLogger(__name__)

USER_PREFERENCE_VERSION = 1
USER_PREFERENCES_MAX_BYTES = 64 * 1024
USER_PREFERENCE_NAMESPACES = frozenset(
    {
        "keyboard_shortcuts",
        "mobile_assistant",
        "session_navigation",
        "context_indicator",
        "usage_context",
        "agent_badges",
        "approval_timeout",
        "agent_pins",
        "calling_defaults",
        "calling_last",
        "session_collab",
    }
)

# This store shares the ``preferences`` table with unrelated keys (project
# ordering), so its rows carry the ``settings.`` prefix. The version row is not
# a namespace: its presence marks an initialized envelope, distinguishing
# "never initialized" from an explicit all-defaults envelope.
_SETTINGS_KEY_PREFIX = "settings."
_ENVELOPE_VERSION_KEY = f"{_SETTINGS_KEY_PREFIX}version"

PreferencesEnvelope: TypeAlias = dict[str, Any]


class UserPreferencesValidationError(ValueError):
    """A preferences payload violates the persisted envelope contract."""


class UserPreferencesUserNotFoundError(LookupError):
    """The authenticated account no longer has a backing user row."""


APPROVAL_TIMEOUT_NAMESPACE = "approval_timeout"
APPROVAL_TIMEOUT_DEFAULT_MINUTES = 50
APPROVAL_TIMEOUT_MAX_MINUTES = 1380


@dataclass(frozen=True)
class ApprovalTimeout:
    """
    Resolved approval / question wait setting for one session owner.

    :param timeout_s: Wait budget in seconds, e.g. ``3000.0``.
    :param stop_turn: Whether the deadline stops the turn instead of
        falling back to the harness's native timeout behaviour.
    """

    timeout_s: float
    stop_turn: bool


def read_approval_timeout(
    store: SqlAlchemyUserPreferencesStore | None,
    owner: str | None,
) -> ApprovalTimeout:
    """
    Read one owner's approval-timeout preference, defaulting on any gap.

    The hook path must never fail on a malformed preference row: a
    missing store / owner / namespace, a non-object value, an invalid
    field, or a row that fails store validation all resolve to the
    50-minute, stop-enabled default. ``timeoutMinutes`` is clamped to
    1..1380 so the server always answers before the host-side client
    budgets give up.

    :param store: Preferences store, or ``None`` when the server has no
        synced preferences.
    :param owner: Session owner whose setting applies, or ``None`` when
        the session has no resolvable owner.
    :returns: The owner's :class:`ApprovalTimeout`, or the default.
    """
    default = ApprovalTimeout(
        timeout_s=float(APPROVAL_TIMEOUT_DEFAULT_MINUTES) * 60.0,
        stop_turn=True,
    )
    if store is None or owner is None:
        return default
    try:
        envelope = store.get(owner)
    except UserPreferencesValidationError:
        return default
    if not isinstance(envelope, dict):
        return default
    settings = envelope.get("settings")
    if not isinstance(settings, dict):
        return default
    value = settings.get(APPROVAL_TIMEOUT_NAMESPACE)
    if not isinstance(value, dict):
        return default
    raw_minutes = value.get("timeoutMinutes")
    if isinstance(raw_minutes, int) and not isinstance(raw_minutes, bool):
        minutes = min(max(raw_minutes, 1), APPROVAL_TIMEOUT_MAX_MINUTES)
        timeout_s = float(minutes) * 60.0
    else:
        timeout_s = default.timeout_s
    raw_stop = value.get("stopTurn")
    stop_turn = raw_stop if isinstance(raw_stop, bool) else True
    return ApprovalTimeout(timeout_s=timeout_s, stop_turn=stop_turn)


SESSION_COLLAB_NAMESPACE = "session_collab"


@dataclass(frozen=True)
class CollabSettings:
    """
    Resolved session-collaboration settings for one session owner.

    :param enabled: Whether session collaboration features are on.
    :param open_rate_count: Sessions one owner may open (open + child create) per window.
    :param open_rate_window_s: Open-rate window in seconds.
    :param relay_depth_max: Maximum relay hops before a message is parked.
    :param pair_rate_count: Messages allowed between one sender/receiver session pair per window.
    :param pair_rate_window_s: Pair-rate window in seconds.
    :param sender_rate_count: Messages one sender session may send per window.
    :param sender_rate_window_s: Sender-rate window in seconds.
    :param duplicate_window_s: Window for suppressing duplicate payloads.
    :param undelivered_ttl_s: Lifetime of undelivered messages in seconds.
    :param flow_timer_enabled: Whether collaboration flow timers are on.
    """

    enabled: bool = True
    open_rate_count: int = 5
    open_rate_window_s: int = 60
    relay_depth_max: int = 30
    pair_rate_count: int = 6
    pair_rate_window_s: int = 60
    sender_rate_count: int = 60
    sender_rate_window_s: int = 600
    duplicate_window_s: int = 600
    undelivered_ttl_s: int = 86400
    flow_timer_enabled: bool = True


_CollabFieldKind: TypeAlias = Literal["bool", "positive_int"]

# Stored JSON keys are camelCase because the web client writes them. The third
# entry names how the stored value is read: ``bool`` takes a JSON boolean and
# ``positive_int`` an int of at least 1 (never a bool). One table keeps all
# eleven mappings in a single place.
_COLLAB_SETTING_FIELDS: tuple[tuple[str, str, _CollabFieldKind], ...] = (
    ("enabled", "enabled", "bool"),
    ("openRateCount", "open_rate_count", "positive_int"),
    ("openRateWindowSeconds", "open_rate_window_s", "positive_int"),
    ("relayDepthMax", "relay_depth_max", "positive_int"),
    ("pairRateCount", "pair_rate_count", "positive_int"),
    ("pairRateWindowSeconds", "pair_rate_window_s", "positive_int"),
    ("senderRateCount", "sender_rate_count", "positive_int"),
    ("senderRateWindowSeconds", "sender_rate_window_s", "positive_int"),
    ("duplicateWindowSeconds", "duplicate_window_s", "positive_int"),
    ("undeliveredTtlSeconds", "undelivered_ttl_s", "positive_int"),
    ("flowTimerEnabled", "flow_timer_enabled", "bool"),
)


def _parse_collab_value(kind: _CollabFieldKind, raw: Any) -> Any | None:
    """Return *raw* when it matches the field's kind, else ``None``."""
    match kind:
        case "bool":
            return raw if isinstance(raw, bool) else None
        case "positive_int":
            if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 1:
                return raw
            return None


def read_collab_settings(
    store: SqlAlchemyUserPreferencesStore | None,
    owner: str | None,
) -> CollabSettings:
    """
    Read one owner's session-collaboration settings, defaulting on any gap.

    The collaboration path must never fail on a malformed preference row: a
    missing store / owner / namespace, a non-object value, an invalid field,
    a row that fails store validation, or a database error all resolve to the
    fail-safe defaults. A boolean field accepts only a JSON boolean; an integer
    field accepts only an integer (not a bool) of at least 1. Unknown keys are
    ignored.

    :param store: Preferences store, or ``None`` when the server has no
        synced preferences.
    :param owner: Session owner whose settings apply, or ``None`` when the
        session has no resolvable owner.
    :returns: The owner's :class:`CollabSettings`, or the defaults.
    """
    if store is None or owner is None:
        return CollabSettings()
    try:
        envelope = store.get(owner)
    except UserPreferencesValidationError:
        return CollabSettings()
    except SQLAlchemyError:
        logger.warning(
            "Failed to read session collaboration preferences for %s", owner, exc_info=True
        )
        return CollabSettings()
    if not isinstance(envelope, dict):
        return CollabSettings()
    settings = envelope.get("settings")
    if not isinstance(settings, dict):
        return CollabSettings()
    value = settings.get(SESSION_COLLAB_NAMESPACE)
    if not isinstance(value, dict):
        return CollabSettings()
    resolved: dict[str, Any] = {}
    for json_key, field_name, kind in _COLLAB_SETTING_FIELDS:
        parsed = _parse_collab_value(kind, value.get(json_key))
        if parsed is not None:
            resolved[field_name] = parsed
    return CollabSettings(**resolved)


def _validate_json(value: Any, *, depth: int = 0) -> None:
    """Reject non-JSON values and pathological nesting before serialization."""
    if depth > 32:
        raise UserPreferencesValidationError("preferences nesting exceeds 32 levels")
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int) and not isinstance(value, bool):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise UserPreferencesValidationError("preferences numbers must be finite")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise UserPreferencesValidationError("preferences object keys must be strings")
            _validate_json(item, depth=depth + 1)
        return
    raise UserPreferencesValidationError(
        f"preferences values must be JSON-compatible, got {type(value).__name__}"
    )


def validate_preferences_envelope(envelope: Any) -> PreferencesEnvelope:
    """Validate and copy a version-1 preferences envelope.

    ``settings`` is deliberately namespaced and allowlisted. Namespace values
    remain client-versioned JSON (for example, ``context_indicator`` is a
    string while shortcut settings are objects); the server still guarantees
    JSON-only content, bounded nesting, and a 64 KiB serialized cap.
    """
    if not isinstance(envelope, dict) or set(envelope) != {"version", "settings"}:
        raise UserPreferencesValidationError(
            "preferences must contain exactly 'version' and 'settings'"
        )
    if type(envelope["version"]) is not int or envelope["version"] != USER_PREFERENCE_VERSION:
        raise UserPreferencesValidationError("preferences version must be 1")
    settings = envelope["settings"]
    if not isinstance(settings, dict):
        raise UserPreferencesValidationError("preferences settings must be an object")
    unknown = set(settings) - USER_PREFERENCE_NAMESPACES
    if unknown:
        raise UserPreferencesValidationError(
            f"unsupported preferences namespace: {sorted(unknown)[0]}"
        )
    for value in settings.values():
        _validate_json(value)

    copied: PreferencesEnvelope = {
        "version": USER_PREFERENCE_VERSION,
        "settings": deepcopy(settings),
    }
    try:
        serialized = json.dumps(
            copied,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except UnicodeEncodeError as exc:
        raise UserPreferencesValidationError("preferences strings must be valid UTF-8") from exc
    if len(serialized) > USER_PREFERENCES_MAX_BYTES:
        raise UserPreferencesValidationError("preferences exceed the 64 KiB limit")
    return copied


def _settings_key(namespace: str) -> str:
    return f"{_SETTINGS_KEY_PREFIX}{namespace}"


def _encode_value(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _read_settings(session: Session, user_id: str) -> tuple[dict[str, Any], bool]:
    """Read the user's namespace rows and whether an envelope was initialized.

    Values are decoded from raw bytes so a corrupt compression frame raises the
    store's own validation error instead of failing inside the ORM result
    processor.
    """
    rows = session.execute(
        select(
            SqlPreference.key,
            type_coerce(SqlPreference.value, LargeBinary),
        ).where(
            SqlPreference.workspace_id == current_workspace_id(),
            SqlPreference.user_id == user_id,
            SqlPreference.key.startswith(_SETTINGS_KEY_PREFIX),
        )
    ).all()
    settings: dict[str, Any] = {}
    for key, raw in rows:
        try:
            text = decode(raw, max_decoded_bytes=USER_PREFERENCES_MAX_BYTES)
        except (ValueError, zstandard.ZstdError) as exc:
            raise UserPreferencesValidationError("stored preferences are invalid JSON") from exc
        if key == _ENVELOPE_VERSION_KEY:
            continue
        try:
            settings[key[len(_SETTINGS_KEY_PREFIX) :]] = json.loads(text or "")
        except (TypeError, json.JSONDecodeError) as exc:
            raise UserPreferencesValidationError("stored preferences are invalid JSON") from exc
    return settings, bool(rows)


def _assemble_envelope(settings: dict[str, Any]) -> PreferencesEnvelope:
    """Rebuild and validate the whole envelope from decoded namespace values."""
    return validate_preferences_envelope(
        {"version": USER_PREFERENCE_VERSION, "settings": settings}
    )


def _require_user_row(session: Session, user_id: str, create_if_missing: bool) -> None:
    """Fail closed when an accounts-mode caller outlived its user row.

    The locking read of the account is the write transaction's first statement,
    ordered before any ``preferences`` row: a concurrent ``delete_user`` either
    tombstones the account first (this rejects it) or waits for this
    transaction and deletes the rows it wrote. A missing row is still never
    created here: header/OIDC identities can sync preferences without one,
    matching upstream's project-order rows.
    """
    if create_if_missing:
        return
    row = lock_account(session, user_id)
    if row is None or row.deleted_at is not None:
        raise UserPreferencesUserNotFoundError(user_id)


class SqlAlchemyUserPreferencesStore:
    """SQLAlchemy repository for current-user preferences.

    Lock order: an accounts-mode write locks the caller's account row first,
    then a write upserts the ``settings.version`` row before reading any
    ``settings.*`` row, so the per-user row lock is held across read and write.
    """

    def __init__(self, storage_location: str) -> None:
        self._engine = get_or_create_engine(storage_location)
        self._dialect = self._engine.dialect.name
        # BEGIN IMMEDIATE closes SQLite's read-then-write race. Other dialects
        # pair the transaction with the per-user version-row upsert in writes.
        self._session = make_named_managed_session_maker(
            self._engine,
            query_name_prefix="omnigent.user_preferences_store",
        )
        self._write_session = make_named_managed_session_maker(
            self._engine,
            query_name_prefix="omnigent.user_preferences_store",
            immediate=True,
        )

    def _upsert_row(self, session: Session, *, user_id: str, key: str, value: str) -> None:
        """Write one row with the backend's native upsert."""
        values = {
            "workspace_id": current_workspace_id(),
            "user_id": user_id,
            "key": key,
            "value": value,
        }
        stmt: Insert
        if self._dialect == "mysql":
            stmt = (
                mysql_insert(SqlPreference).values(**values).on_duplicate_key_update(value=value)
            )
        elif self._dialect == "sqlite":
            stmt = (
                sqlite_insert(SqlPreference)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=["workspace_id", "user_id", "key"],
                    set_={"value": value},
                )
            )
        else:
            stmt = (
                pg_insert(SqlPreference)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=["workspace_id", "user_id", "key"],
                    set_={"value": value},
                )
            )
        session.execute(stmt)

    def get(self, user_id: str) -> PreferencesEnvelope | None:
        """Return the user's envelope, or ``None`` when it was never initialized."""
        with self._session("read_user_preferences") as session:
            settings, initialized = _read_settings(session, user_id)
            if not initialized:
                return None
            return _assemble_envelope(settings)

    def initialize(
        self,
        user_id: str,
        envelope: PreferencesEnvelope,
        *,
        create_if_missing: bool = True,
    ) -> PreferencesEnvelope:
        """Set the full envelope only when this user is still uninitialized.

        Repeated/racing first-device migrations are idempotent: once any client
        wins, later calls receive the already-persisted value instead of
        overwriting it with stale localStorage. The version row is the
        first-write lock: only one concurrent writer can insert it, and the
        loser retries into the winner's envelope.
        """
        validated = validate_preferences_envelope(envelope)

        def write(session: Session) -> PreferencesEnvelope:
            _require_user_row(session, user_id, create_if_missing)
            settings, initialized = _read_settings(session, user_id)
            if initialized:
                return _assemble_envelope(settings)
            workspace_id = current_workspace_id()
            session.add(
                SqlPreference(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    key=_ENVELOPE_VERSION_KEY,
                    value=_encode_value(USER_PREFERENCE_VERSION),
                )
            )
            session.add_all(
                SqlPreference(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    key=_settings_key(namespace),
                    value=_encode_value(value),
                )
                for namespace, value in validated["settings"].items()
            )
            return validated

        for attempt in range(2):
            try:
                return run_write_transaction(
                    self._write_session, "initialize_user_preferences", write
                )
            except IntegrityError:
                if attempt == 1:
                    raise
        raise AssertionError("unreachable")

    def patch_namespace(
        self,
        user_id: str,
        namespace: str,
        value: Any | None,
        *,
        create_if_missing: bool = True,
    ) -> PreferencesEnvelope:
        """Atomically shallow-merge one namespace, or delete it with NULL."""
        if namespace not in USER_PREFERENCE_NAMESPACES:
            raise UserPreferencesValidationError(f"unsupported preferences namespace: {namespace}")
        if value is not None:
            _validate_json(value)

        def write(session: Session) -> PreferencesEnvelope:
            _require_user_row(session, user_id, create_if_missing)
            # Lock before reading: accounts mode already holds the account row;
            # the version-row upsert then blocks a concurrent patch of this
            # user until this transaction commits, so the read below sees
            # every committed sibling row.
            self._upsert_row(
                session,
                user_id=user_id,
                key=_ENVELOPE_VERSION_KEY,
                value=_encode_value(USER_PREFERENCE_VERSION),
            )
            settings, _initialized = _read_settings(session, user_id)
            if value is None:
                settings.pop(namespace, None)
            else:
                existing = settings.get(namespace)
                if isinstance(existing, dict) and isinstance(value, dict):
                    settings[namespace] = {**existing, **deepcopy(value)}
                else:
                    settings[namespace] = deepcopy(value)
            merged = _assemble_envelope(settings)
            if namespace in merged["settings"]:
                self._upsert_row(
                    session,
                    user_id=user_id,
                    key=_settings_key(namespace),
                    value=_encode_value(merged["settings"][namespace]),
                )
            else:
                session.execute(
                    delete(SqlPreference).where(
                        SqlPreference.workspace_id == current_workspace_id(),
                        SqlPreference.user_id == user_id,
                        SqlPreference.key == _settings_key(namespace),
                    )
                )
            return merged

        return run_write_transaction(
            self._write_session, "patch_user_preferences_namespace", write
        )
