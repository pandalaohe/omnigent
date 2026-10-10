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
import time
from collections.abc import Iterable
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
from omnigent.host.git_worktree import WorktreeError, validate_worktree_path_template

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
        "host_colors",
        "keep_warm",
        "worktree_location",
        "runner_log_warnings",
        "sidebar_layout",
        "sound_alerts",
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

RUNNER_LOG_WARNINGS_NAMESPACE = "runner_log_warnings"
# The server re-touches a dismissal daily while the host still confirms its
# detection, so an entry is dropped only once its detection stopped being
# confirmed for this long.
_RUNNER_LOG_WARNINGS_RETENTION_S = 30 * 24 * 60 * 60

# How stale a still-confirmed dismissal must be before the server refreshes it.
_RUNNER_LOG_WARNINGS_TOUCH_INTERVAL_MS = 24 * 60 * 60 * 1000


def _merge_runner_log_warnings(
    existing: dict[str, Any], incoming: dict[str, Any]
) -> dict[str, Any]:
    """Merge one device's dismissals, never moving a key's touch backwards.

    A device holding a stale snapshot can carry an older dismissed-at for a
    key another device already re-touched, so the larger numeric value wins.
    Entries whose incoming value is not a number are dropped, which is what
    pruning would do to them anyway.

    :param existing: Stored ``runner_log_warnings`` map.
    :param incoming: Incoming map for the same namespace.
    :returns: A new merged map, ready for :func:`_prune_runner_log_warnings`.
    """
    merged: dict[str, Any] = dict(existing)
    for flag, incoming_at in incoming.items():
        if not isinstance(incoming_at, (int, float)) or isinstance(incoming_at, bool):
            merged.pop(flag, None)
            continue
        stored_at = merged.get(flag)
        if (
            not isinstance(stored_at, (int, float))
            or isinstance(stored_at, bool)
            or incoming_at > stored_at
        ):
            merged[flag] = incoming_at
    return merged


def _prune_runner_log_warnings(value: dict[str, Any]) -> dict[str, Any]:
    """Keep only dismissal entries from the last retention window.

    Each entry maps a detection instant to the epoch-ms time it was
    dismissed; a value that is not a non-bool number, or is older than
    ``_RUNNER_LOG_WARNINGS_RETENTION_S`` before now (``time.time()``), is
    dropped.

    :param value: Merged ``runner_log_warnings`` namespace value.
    :returns: A new map holding the retained entries.
    """
    cutoff_ms = (time.time() - _RUNNER_LOG_WARNINGS_RETENTION_S) * 1000.0
    return {
        flag: dismissed_at
        for flag, dismissed_at in value.items()
        if isinstance(dismissed_at, (int, float))
        and not isinstance(dismissed_at, bool)
        and dismissed_at >= cutoff_ms
    }


def touch_runner_log_warning_dismissals(
    store: SqlAlchemyUserPreferencesStore,
    flag: str,
    *,
    create_if_missing: bool,
    now_ms: float | None = None,
) -> int:
    """Refresh every user's dismissal of *flag* that is older than a day.

    A dismissal holds until a new detection. The host re-confirms a live
    detection every few minutes, so touching it here at most daily keeps it
    out of the 30-day prune for exactly as long as the detection is
    confirmed. Any user who dismissed it is refreshed, including viewers of
    a shared session; a flag no user dismissed is never added.

    :param store: Preferences store holding the dismissals.
    :param flag: Detection instant whose dismissal is refreshed.
    :param create_if_missing: The preferences routes' account-row policy:
        ``False`` in accounts mode skips users without a live account.
    :param now_ms: Touch instant in epoch ms; defaults to now.
    :returns: How many users were touched.
    """
    if now_ms is None:
        now_ms = time.time() * 1000.0
    touched = 0
    for user_id, value in store.namespace_values(RUNNER_LOG_WARNINGS_NAMESPACE):
        if not isinstance(value, dict):
            continue
        dismissed_at = value.get(flag)
        if (
            not isinstance(dismissed_at, (int, float))
            or isinstance(dismissed_at, bool)
            or dismissed_at >= now_ms - _RUNNER_LOG_WARNINGS_TOUCH_INTERVAL_MS
        ):
            continue
        try:
            store.patch_namespace(
                user_id,
                RUNNER_LOG_WARNINGS_NAMESPACE,
                {flag: now_ms},
                create_if_missing=create_if_missing,
            )
        except (UserPreferencesValidationError, UserPreferencesUserNotFoundError):
            continue
        touched += 1
    return touched


@dataclass(frozen=True)
class ApprovalTimeout:
    """
    Resolved approval / question wait setting for one session owner.

    :param timeout_s: Wait budget in seconds, e.g. ``3000.0``.
    :param stop_turn: Whether the deadline stops the turn instead of
        falling back to the harness's native timeout behaviour.
    :param async_approvals: Whether an eligible approval is deferred
        ("deny now, approve later") instead of parking the turn. The
        timeout fields above do not apply while it is on.
    """

    timeout_s: float
    stop_turn: bool
    async_approvals: bool = True


def read_approval_timeout(
    store: SqlAlchemyUserPreferencesStore | None,
    owner: str | None,
) -> ApprovalTimeout:
    """
    Read one owner's approval-timeout preference, defaulting on any gap.

    The hook path must never fail on a malformed preference row: a
    missing store / owner / namespace, a non-object value, an invalid
    field, or a row that fails store validation all resolve to the
    default. ``timeoutMinutes`` is clamped to 1..1380 so the server
    always answers before the host-side client budgets give up;
    ``asyncApprovals`` takes any non-bool (including absence) as the
    default, which is on.

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
    raw_async = value.get("asyncApprovals")
    async_approvals = raw_async if isinstance(raw_async, bool) else True
    return ApprovalTimeout(
        timeout_s=timeout_s,
        stop_turn=stop_turn,
        async_approvals=async_approvals,
    )


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
    :param duplicate_window_s: Window for suppressing an identical retry
        on the same thread; past it, a resend still drops while the
        earlier copy is undelivered.
    :param undelivered_ttl_s: Lifetime of undelivered messages in seconds.
    :param flow_timer_enabled: Whether collaboration flow timers are on.
    :param keep_warm_enabled: Whether idle active-zone children are kept
        warm. Off by default.
    :param keep_warm_claude_interval_s: Keep-warm interval for Claude Code
        children, in seconds.
    :param keep_warm_codex_interval_s: Keep-warm interval for Codex children,
        in seconds.
    :param keep_warm_max_s: Longest keep-warm run per child, in seconds.
    """

    enabled: bool = True
    open_rate_count: int = 10
    open_rate_window_s: int = 60
    relay_depth_max: int = 30
    pair_rate_count: int = 6
    pair_rate_window_s: int = 60
    sender_rate_count: int = 60
    sender_rate_window_s: int = 600
    duplicate_window_s: int = 60
    undelivered_ttl_s: int = 86400
    flow_timer_enabled: bool = True
    keep_warm_enabled: bool = False
    keep_warm_claude_interval_s: int = 3300
    keep_warm_codex_interval_s: int = 1500
    keep_warm_max_s: int = 28800


_CollabFieldKind: TypeAlias = Literal["bool", "positive_int"]

# Stored JSON keys are camelCase because the web client writes them. The third
# entry names how the stored value is read: ``bool`` takes a JSON boolean and
# ``positive_int`` an int of at least 1 (never a bool). One table keeps all
# fifteen mappings in a single place.
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
    ("childKeepWarmEnabled", "keep_warm_enabled", "bool"),
    ("childKeepWarmClaudeIntervalSeconds", "keep_warm_claude_interval_s", "positive_int"),
    ("childKeepWarmCodexIntervalSeconds", "keep_warm_codex_interval_s", "positive_int"),
    ("childKeepWarmMaxSeconds", "keep_warm_max_s", "positive_int"),
)

# Keep-warm windows as stored (seconds). The web enforces the same ranges, but
# the reader takes any positive int, so the sweeper works on clamped values.
KEEP_WARM_CLAUDE_INTERVAL_BOUNDS_S = (300, 3540)
KEEP_WARM_CODEX_INTERVAL_BOUNDS_S = (300, 1740)
KEEP_WARM_MAX_BOUNDS_S = (3600, 172800)
KEEP_WARM_COLD_AFTER_BOUNDS_S = (60, 172800)


def clamp_keep_warm(settings: CollabSettings) -> tuple[int, int, int]:
    """
    Clamp the keep-warm windows to their supported bounds, in seconds.

    :param settings: Resolved collaboration settings.
    :returns: ``(claude_interval_s, codex_interval_s, max_s)``.
    """

    def _clamped(value: int, bounds: tuple[int, int]) -> int:
        low, high = bounds
        return min(max(value, low), high)

    return (
        _clamped(settings.keep_warm_claude_interval_s, KEEP_WARM_CLAUDE_INTERVAL_BOUNDS_S),
        _clamped(settings.keep_warm_codex_interval_s, KEEP_WARM_CODEX_INTERVAL_BOUNDS_S),
        _clamped(settings.keep_warm_max_s, KEEP_WARM_MAX_BOUNDS_S),
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


def _parse_collab_settings(value: Any) -> CollabSettings:
    """Resolve one stored ``session_collab`` namespace value, defaulting per field."""
    if not isinstance(value, dict):
        return CollabSettings()
    resolved: dict[str, Any] = {}
    for json_key, field_name, kind in _COLLAB_SETTING_FIELDS:
        parsed = _parse_collab_value(kind, value.get(json_key))
        if parsed is not None:
            resolved[field_name] = parsed
    return CollabSettings(**resolved)


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
    return _parse_collab_settings(settings.get(SESSION_COLLAB_NAMESPACE))


KEEP_WARM_NAMESPACE = "keep_warm"
KEEP_WARM_CLAUDE_DEFAULT_INTERVAL_S = 3300
KEEP_WARM_CODEX_DEFAULT_INTERVAL_S = 1500
KEEP_WARM_DEFAULT_MAX_S = 14400
KEEP_WARM_DEFAULT_HOST_OFFLINE_ARCHIVE_S = 14400


@dataclass(frozen=True)
class AgentKeepWarm:
    """
    Stored keep-warm row for one agent.

    :param main: Whether this agent's main sessions are kept warm.
    :param child: Whether this agent's active children are kept warm.
    :param interval_s: Stored ping interval in seconds, or ``None`` when the
        row takes the family default.
    :param max_s: Longest keep-warm run per session, in seconds.
    :param cold_after_s: Idle seconds after which the agent's sessions read
        cold, ``0`` when they never do, or ``None`` for the per-family rule.
    """

    main: bool
    child: bool
    interval_s: int | None
    max_s: int
    cold_after_s: int | None = None


@dataclass(frozen=True)
class KeepWarmSettings:
    """
    Resolved ``keep_warm`` namespace for one session owner.

    :param agents: Per-agent rows keyed by agent id; an absent agent is off.
    :param host_offline_archive_s: Seconds a host stays offline before its
        children auto-archive; ``0`` disables auto-archive.
    :param migrated_from_legacy_at: Unix time of the one-time legacy
        migration, or ``None`` when it never ran.
    :param present: Whether the ``keep_warm`` namespace exists in the stored
        envelope; the legacy migration runs only when it does not.
    """

    agents: dict[str, AgentKeepWarm]
    host_offline_archive_s: int
    migrated_from_legacy_at: int | None
    present: bool


@dataclass(frozen=True)
class ResolvedAgentKeepWarm:
    """
    One agent's keep-warm row resolved for a family: defaults applied, clamped.

    :param main: Whether this agent's main sessions are kept warm.
    :param child: Whether this agent's active children are kept warm.
    :param interval_s: Effective ping interval in seconds, clamped to the
        family bounds.
    :param max_s: Effective run cap in seconds, clamped to the shared bounds.
    """

    main: bool
    child: bool
    interval_s: int
    max_s: int


def _default_keep_warm_settings(*, present: bool = False) -> KeepWarmSettings:
    return KeepWarmSettings(
        agents={},
        host_offline_archive_s=KEEP_WARM_DEFAULT_HOST_OFFLINE_ARCHIVE_S,
        migrated_from_legacy_at=None,
        present=present,
    )


def read_keep_warm_settings(
    store: SqlAlchemyUserPreferencesStore | None,
    owner: str | None,
) -> KeepWarmSettings:
    """
    Read one owner's keep-warm settings, defaulting on any gap.

    The keep-warm path must never fail on a malformed preference row: a
    missing store / owner / namespace, a non-object namespace value, an
    invalid field, a row that fails store validation, or a database error all
    resolve to the fail-safe defaults. Per agent row, ``main`` / ``child``
    accept only a JSON boolean (anything else reads ``False``),
    ``intervalSeconds`` accepts only a positive int (anything else reads
    ``None`` = the family default) and ``maxSeconds`` a positive int (else the
    default); ``coldAfterSeconds`` accepts a non-negative int and clamps to
    the cold-after bounds (``0`` stays ``0`` = never cold, anything else
    invalid reads ``None`` = the per-family rule); a row that is not an
    object is skipped.
    ``hostOfflineArchiveSeconds`` accepts a non-negative int and clamps to the
    shared keep-warm bounds (``0`` stays ``0`` = disabled). A namespace that
    exists but holds garbage still reports ``present=True``, so the legacy
    migration stays a no-op for it. Unknown keys are ignored.

    :param store: Preferences store, or ``None`` when the server has no
        synced preferences.
    :param owner: Session owner whose settings apply, or ``None`` when the
        session has no resolvable owner.
    :returns: The owner's :class:`KeepWarmSettings`, or the defaults.
    """
    if store is None or owner is None:
        return _default_keep_warm_settings()
    try:
        envelope = store.get(owner)
    except UserPreferencesValidationError:
        return _default_keep_warm_settings()
    except SQLAlchemyError:
        logger.warning("Failed to read keep-warm preferences for %s", owner, exc_info=True)
        return _default_keep_warm_settings()
    if not isinstance(envelope, dict):
        return _default_keep_warm_settings()
    settings = envelope.get("settings")
    if not isinstance(settings, dict):
        return _default_keep_warm_settings()
    if KEEP_WARM_NAMESPACE not in settings:
        return _default_keep_warm_settings()
    value = settings[KEEP_WARM_NAMESPACE]
    if not isinstance(value, dict):
        return _default_keep_warm_settings(present=True)
    agents: dict[str, AgentKeepWarm] = {}
    raw_agents = value.get("agents")
    if isinstance(raw_agents, dict):
        for agent_id, row in raw_agents.items():
            if not isinstance(row, dict):
                continue
            raw_main = row.get("main")
            raw_child = row.get("child")
            raw_cold = row.get("coldAfterSeconds")
            interval_s = _parse_collab_value("positive_int", row.get("intervalSeconds"))
            max_s = _parse_collab_value("positive_int", row.get("maxSeconds"))
            if isinstance(raw_cold, int) and not isinstance(raw_cold, bool) and raw_cold >= 0:
                low, high = KEEP_WARM_COLD_AFTER_BOUNDS_S
                cold_after_s = 0 if raw_cold == 0 else min(max(raw_cold, low), high)
            else:
                cold_after_s = None
            agents[agent_id] = AgentKeepWarm(
                main=raw_main if isinstance(raw_main, bool) else False,
                child=raw_child if isinstance(raw_child, bool) else False,
                interval_s=interval_s,
                max_s=max_s if max_s is not None else KEEP_WARM_DEFAULT_MAX_S,
                cold_after_s=cold_after_s,
            )
    raw_host = value.get("hostOfflineArchiveSeconds")
    if isinstance(raw_host, int) and not isinstance(raw_host, bool) and raw_host >= 0:
        host_offline_archive_s = clamp_host_offline_archive_s(raw_host)
    else:
        host_offline_archive_s = KEEP_WARM_DEFAULT_HOST_OFFLINE_ARCHIVE_S
    raw_migrated = value.get("migratedFromLegacyAt")
    migrated_from_legacy_at = (
        raw_migrated
        if isinstance(raw_migrated, int) and not isinstance(raw_migrated, bool)
        else None
    )
    return KeepWarmSettings(
        agents=agents,
        host_offline_archive_s=host_offline_archive_s,
        migrated_from_legacy_at=migrated_from_legacy_at,
        present=True,
    )


def keep_warm_for_agent(
    settings: KeepWarmSettings,
    agent_id: str | None,
    family: Literal["claude", "codex"],
) -> ResolvedAgentKeepWarm | None:
    """
    Resolve one agent's keep-warm row, or ``None`` when the agent is off.

    A ``None`` agent id and an agent absent from the settings both resolve to
    ``None`` (absent = off). A stored interval of ``None`` takes the family
    default; the interval clamps to the family bounds and the cap to the
    shared keep-warm bounds.

    :param settings: The owner's resolved keep-warm settings.
    :param agent_id: Agent whose row applies, or ``None``.
    :param family: Model family, selecting the default and interval bounds.
    :returns: The resolved row, or ``None`` when keep-warm is off.
    """
    if agent_id is None:
        return None
    row = settings.agents.get(agent_id)
    if row is None:
        return None
    if family == "claude":
        default_interval = KEEP_WARM_CLAUDE_DEFAULT_INTERVAL_S
        bounds = KEEP_WARM_CLAUDE_INTERVAL_BOUNDS_S
    else:
        default_interval = KEEP_WARM_CODEX_DEFAULT_INTERVAL_S
        bounds = KEEP_WARM_CODEX_INTERVAL_BOUNDS_S
    interval = row.interval_s if row.interval_s is not None else default_interval
    low, high = bounds
    interval_s = min(max(interval, low), high)
    max_low, max_high = KEEP_WARM_MAX_BOUNDS_S
    max_s = min(max(row.max_s, max_low), max_high)
    return ResolvedAgentKeepWarm(
        main=row.main, child=row.child, interval_s=interval_s, max_s=max_s
    )


def clamp_host_offline_archive_s(value: int) -> int:
    """
    Clamp the host-offline auto-archive delay to the keep-warm bounds.

    ``0`` disables auto-archive and stays ``0``.

    :param value: Stored delay in seconds.
    :returns: The clamped delay in seconds.
    """
    if value == 0:
        return 0
    low, high = KEEP_WARM_MAX_BOUNDS_S
    return min(max(value, low), high)


def migrate_legacy_keep_warm(
    store: SqlAlchemyUserPreferencesStore | None,
    owner: str | None,
    native_agents: Iterable[tuple[str, str]],
    now: int,
) -> bool:
    """
    Run the one-time migration of the legacy child keep-warm switch.

    Runs only when the owner's ``keep_warm`` namespace is absent and the
    legacy switch was effectively on — the legacy sweeper required both
    ``session_collab.enabled`` and ``session_collab.childKeepWarmEnabled``.
    It then writes one ``{main: false, child: true}`` row per currently
    existing native agent with the legacy interval of that agent's family,
    plus the default host-offline delay and the migration timestamp. An agent
    created afterwards stays absent, hence off. The legacy keys are never
    changed or deleted. One ``store.get`` decides both the namespace's
    presence and the legacy flags: a store error logs a warning and returns
    ``False`` without writing, so a failed read cannot pass for an absent
    namespace and clobber an explicit setting on a later retry; this never
    raises.

    :param store: Preferences store, or ``None`` when the server has no
        synced preferences.
    :param owner: Owner to migrate, or ``None``.
    :param native_agents: ``(agent_id, family)`` pairs of the claude-native /
        codex-native agents existing now.
    :param now: Unix time recorded as ``migratedFromLegacyAt``.
    :returns: ``True`` when the namespace was written.
    """
    if store is None or owner is None:
        return False
    try:
        envelope = store.get(owner)
        if not isinstance(envelope, dict):
            return False
        settings = envelope.get("settings")
        if not isinstance(settings, dict):
            return False
        if KEEP_WARM_NAMESPACE in settings:
            return False
        collab = _parse_collab_settings(settings.get(SESSION_COLLAB_NAMESPACE))
        if not (collab.enabled and collab.keep_warm_enabled):
            return False
        agents: dict[str, Any] = {}
        for agent_id, family in native_agents:
            interval = (
                collab.keep_warm_claude_interval_s
                if family == "claude"
                else collab.keep_warm_codex_interval_s
            )
            agents[agent_id] = {
                "main": False,
                "child": True,
                "intervalSeconds": interval,
                "maxSeconds": KEEP_WARM_DEFAULT_MAX_S,
            }
        store.patch_namespace(
            owner,
            KEEP_WARM_NAMESPACE,
            {
                "agents": agents,
                "hostOfflineArchiveSeconds": KEEP_WARM_DEFAULT_HOST_OFFLINE_ARCHIVE_S,
                "migratedFromLegacyAt": now,
            },
        )
    except (
        SQLAlchemyError,
        UserPreferencesValidationError,
        UserPreferencesUserNotFoundError,
    ):
        logger.warning("Failed to migrate legacy keep-warm settings for %s", owner, exc_info=True)
        return False
    return True


WORKTREE_LOCATION_NAMESPACE = "worktree_location"


def _validate_worktree_location(value: Any) -> None:
    """Validate one incoming ``worktree_location`` namespace value.

    Runs on writes only (``patch_namespace`` and ``initialize``), never in
    :func:`validate_preferences_envelope`, so a value stored under an older
    template rule still reads back and the host's refusal names the rule.
    The value is an object whose only key is ``pathTemplate`` — a template
    string passing :func:`validate_worktree_path_template`, or ``null``.

    :param value: The namespace value being written.
    :raises UserPreferencesValidationError: Naming the broken rule; the
        template validator's message is surfaced unchanged.
    """
    if not isinstance(value, dict):
        raise UserPreferencesValidationError("worktree_location must be an object")
    unknown = set(value) - {"pathTemplate"}
    if unknown:
        raise UserPreferencesValidationError(
            f"unsupported worktree_location key: {sorted(unknown)[0]}"
        )
    template = value.get("pathTemplate")
    if template is None:
        return
    if not isinstance(template, str):
        raise UserPreferencesValidationError(
            "worktree_location.pathTemplate must be a string or null"
        )
    try:
        validate_worktree_path_template(template)
    except WorktreeError as exc:
        raise UserPreferencesValidationError(exc.message) from exc


def read_worktree_path_template(
    store: SqlAlchemyUserPreferencesStore | None,
    owner: str | None,
) -> str | None:
    """Read one owner's worktree location template, ``None`` on any gap.

    The worktree-create path must never fail on a malformed preference
    row: a missing store / owner / namespace, a non-object value, a
    missing or non-string ``pathTemplate``, a row that cannot be decoded
    or fails store validation, or a database error (all logged) resolve
    to ``None`` — the upstream sibling layout. A stored string is
    returned as is; the host re-validates it before rendering.

    :param store: Preferences store, or ``None`` when the server has no
        synced preferences.
    :param owner: Creating request's owner whose setting applies, or
        ``None`` when it has no resolvable owner.
    :returns: The stored template, e.g.
        ``"{entry}/.worktrees/{repo}/{branch}"``, or ``None``.
    """
    if store is None or owner is None:
        return None
    try:
        envelope = store.get(owner)
    except (UserPreferencesValidationError, ValueError, SQLAlchemyError):
        logger.warning(
            "Failed to read the worktree location preference for %s", owner, exc_info=True
        )
        return None
    if not isinstance(envelope, dict):
        return None
    settings = envelope.get("settings")
    if not isinstance(settings, dict):
        return None
    value = settings.get(WORKTREE_LOCATION_NAMESPACE)
    if not isinstance(value, dict):
        return None
    template = value.get("pathTemplate")
    if not isinstance(template, str) or not template:
        return None
    return template


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

    def namespace_values(self, namespace: str) -> list[tuple[str, Any]]:
        """Return ``(user_id, value)`` for every user storing *namespace* here.

        Scoped to the current workspace. A row that does not decode is
        skipped, so one corrupt user never hides the others.
        """
        with self._session("read_namespace_of_all_users") as session:
            rows = session.execute(
                select(
                    SqlPreference.user_id,
                    type_coerce(SqlPreference.value, LargeBinary),
                ).where(
                    SqlPreference.workspace_id == current_workspace_id(),
                    SqlPreference.key == _settings_key(namespace),
                )
            ).all()
        values: list[tuple[str, Any]] = []
        for user_id, raw in rows:
            try:
                text = decode(raw, max_decoded_bytes=USER_PREFERENCES_MAX_BYTES)
                values.append((user_id, json.loads(text or "")))
            except (ValueError, zstandard.ZstdError):
                continue
        return values

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
        worktree_location = validated["settings"].get(WORKTREE_LOCATION_NAMESPACE)
        if worktree_location is not None:
            _validate_worktree_location(worktree_location)

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
            if namespace == WORKTREE_LOCATION_NAMESPACE:
                _validate_worktree_location(value)

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
                    if namespace == RUNNER_LOG_WARNINGS_NAMESPACE:
                        settings[namespace] = _merge_runner_log_warnings(existing, value)
                    else:
                        settings[namespace] = {**existing, **deepcopy(value)}
                else:
                    settings[namespace] = deepcopy(value)
            if namespace == RUNNER_LOG_WARNINGS_NAMESPACE:
                stored = settings.get(namespace)
                if isinstance(stored, dict):
                    settings[namespace] = _prune_runner_log_warnings(stored)
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
