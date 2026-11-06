"""Parent-session rotation settings and fresh context-round observations."""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy.exc import SQLAlchemyError

from omnigent.server.schemas import AutoRotationStatus
from omnigent.server.session_collab import collab_owner_for
from omnigent.server.user_preferences_store import SESSION_COLLAB_NAMESPACE, _parse_collab_settings
from omnigent.stores.conversation_store import (
    ROTATE_REQUESTED_LABEL_KEY,
    SUCCEEDED_BY_LABEL_KEY,
    ConversationNotFoundError,
    LabelGroupChange,
)

if TYPE_CHECKING:
    from omnigent.entities import Conversation
    from omnigent.server.user_preferences_store import SqlAlchemyUserPreferencesStore
    from omnigent.stores.conversation_store import ConversationStore
    from omnigent.stores.permission_store import PermissionStore

logger = logging.getLogger(__name__)
AUTO_ROTATION_LABEL_KEYS = {
    "enabled": "omnigent.auto_rotation",
    "compactions": "omnigent.auto_rotation.compactions",
    "remaining_pct": "omnigent.auto_rotation.remaining_pct",
}
AUTO_ROTATION_STATE_PREFIX = "omnigent.auto_rotation_state."
FRESH_ROUND_LABEL_KEY = f"{AUTO_ROTATION_STATE_PREFIX}fresh_round"
NOTICED_COUNT_LABEL_KEY = f"{AUTO_ROTATION_STATE_PREFIX}noticed_count"
INFERRED_COMPACTIONS_LABEL_KEY = f"{AUTO_ROTATION_STATE_PREFIX}inferred_compactions"


def _label_int(value: str | None, minimum: int = 0, maximum: int | None = None) -> int | None:
    if value is None or not value.isascii() or not value.isdecimal():
        return None
    number = int(value)
    return number if number >= minimum and (maximum is None or number <= maximum) else None


def compaction_point(conv: Conversation, harness: str | None) -> int | None:
    """Prefer the reported limit, then the harness's window fallback."""
    limit = _label_int(conv.labels.get("omnigent.last_auto_compact_token_limit"), 1)
    if limit is not None:
        return limit
    window = _label_int(conv.labels.get("omnigent.last_context_window"), 1)
    if window is not None and harness in {"pi", "pi-native"} and window > 32768:
        return window - 16384
    return window


def auto_rotation_for(
    conv: Conversation,
    conversation_store: ConversationStore,
    permission_store: PermissionStore | None = None,
    prefs_store: SqlAlchemyUserPreferencesStore | None = None,
) -> AutoRotationStatus:
    """Resolve defaults and overrides; a failed read never makes a session due."""
    from omnigent.server.routes._sessions.helpers import _resolve_harness
    from omnigent.server.routes.sessions.routes_succession import _ROTATION_NATIVE_HARNESSES

    status = AutoRotationStatus(
        eligible=False,
        ineligible_reason="harness",
        harness=None,
        sources=dict.fromkeys(AUTO_ROTATION_LABEL_KEYS, "default"),
        approximate=[],
        rotation_pending=bool(conv.labels.get(ROTATE_REQUESTED_LABEL_KEY)),
    )
    try:
        harness = _resolve_harness(conv)
        status.harness = harness
        reason: Literal["not_top_level", "archived", "harness"] | None = None
        if conv.parent_conversation_id is not None:
            reason = "not_top_level"
        elif conv.archived:
            reason = "archived"
        elif harness not in _ROTATION_NATIVE_HARNESSES:
            reason = "harness"
        status.ineligible_reason = reason
        status.eligible = reason is None
        raw: dict[str, Any] = {}
        if prefs_store is not None:
            owner = collab_owner_for(conv, conversation_store, permission_store)
            envelope = prefs_store.get(owner)
            settings = envelope.get("settings", {}) if isinstance(envelope, dict) else {}
            namespace = settings.get(SESSION_COLLAB_NAMESPACE, {})
            if isinstance(namespace, dict):
                raw = namespace
        defaults = _parse_collab_settings(raw)
        for key, json_key, field, upper in (
            ("enabled", "autoRotationEnabled", "enabled", None),
            ("compactions", "autoRotationCompactions", "compactions_setting", 20),
            ("remaining_pct", "autoRotationRemainingPct", "remaining_pct_setting", 90),
        ):
            default = getattr(defaults, f"auto_rotation_{key}")
            label = conv.labels.get(AUTO_ROTATION_LABEL_KEYS[key])
            override = (
                (label == "on" if label in {"on", "off"} else None)
                if upper is None
                else _label_int(label, 1, upper)
            )
            user_value = raw.get(json_key)
            valid_user = (
                type(user_value) is bool
                if upper is None
                else type(user_value) is int and 1 <= user_value <= upper
            )
            setattr(status, field, default if override is None else override)
            status.sources[key] = (
                "session" if override is not None else "user" if valid_user else "default"
            )
        point = compaction_point(conv, harness)
        tokens = _label_int(conv.labels.get("omnigent.last_context_tokens"))
        if point is not None and tokens is not None:
            status.remaining_pct = 100 * max(0, point - tokens) // point
        if harness == "pi":
            status.approximate.append("usage")
        if (
            harness == "antigravity-native"
            or _label_int(conv.labels.get("omnigent.last_auto_compact_token_limit"), 1) is None
        ):
            status.approximate.append("compaction_point")
        if harness in {"pi", "antigravity-native"} or "omnigent.import.source" in conv.labels:
            status.approximate.append("compactions")
        if harness == "antigravity-native":
            status.compactions = _label_int(conv.labels.get(INFERRED_COMPACTIONS_LABEL_KEY)) or 0
            status.round_marker = status.compactions
        else:
            stats = conversation_store.get_compaction_stats(conv.id)
            status.compactions = stats.count
            status.round_marker = (stats.last_compaction_at or 0) * 1_000_000
        status.due = (
            status.eligible
            and status.enabled
            and status.compactions >= status.compactions_setting
            and _label_int(conv.labels.get(FRESH_ROUND_LABEL_KEY)) == status.round_marker
            and status.remaining_pct is not None
            and status.remaining_pct <= status.remaining_pct_setting
            and not status.rotation_pending
            and not conv.labels.get(SUCCEEDED_BY_LABEL_KEY)
            and _label_int(conv.labels.get(NOTICED_COUNT_LABEL_KEY)) != status.round_marker
        )
    except Exception:
        logger.warning("Could not read auto-rotation state for %s", conv.id, exc_info=True)
        status.enabled = False
        status.due = False
    return status


def record_auto_rotation_usage(
    session_id: str,
    label_updates: dict[str, str],
    clear_compact_limit: bool,
    conversation_store: ConversationStore,
    permission_store: PermissionStore | None = None,
    prefs_store: SqlAlchemyUserPreferencesStore | None = None,
    conv: Conversation | None = None,
) -> AutoRotationStatus | None:
    """Commit one token reading and its round state under the store's session lock."""
    from omnigent.server.routes._sessions.helpers import _resolve_harness
    from omnigent.server.routes.sessions.routes_succession import _ROTATION_NATIVE_HARNESSES

    if conv is None:
        conv = conversation_store.get_conversation(session_id)
    if conv is None:
        raise ConversationNotFoundError(session_id)
    limit_key = "omnigent.last_auto_compact_token_limit"
    deletes = {limit_key} if clear_compact_limit else set()
    if (
        conv.parent_conversation_id is not None
        or conv.archived
        or _resolve_harness(conv) not in _ROTATION_NATIVE_HARNESSES
    ):
        if label_updates:
            conversation_store.set_labels(session_id, label_updates)
        if clear_compact_limit:
            conversation_store.delete_label(session_id, limit_key)
        return None

    eligibility_read_failed = False

    def observe(current: dict[str, str]) -> LabelGroupChange:
        nonlocal eligibility_read_failed
        labels = {
            key: value for key, value in current.items() if key not in deletes
        } | label_updates
        try:
            current_conv = conversation_store.get_conversation(session_id)
        except SQLAlchemyError:
            eligibility_read_failed = True
            logger.warning(
                "Could not read auto-rotation eligibility for %s", session_id, exc_info=True
            )
            return LabelGroupChange(dict(label_updates), deletes)
        if current_conv is None:
            raise ConversationNotFoundError(session_id)
        latest = replace(current_conv, labels=labels)
        status = auto_rotation_for(latest, conversation_store, permission_store, prefs_store)
        updates = dict(label_updates)
        if not status.eligible or not status.enabled:
            return LabelGroupChange(updates, deletes)
        if status.harness == "antigravity-native":
            point = compaction_point(latest, status.harness)
            before = _label_int(current.get("omnigent.last_context_tokens"))
            after = _label_int(labels.get("omnigent.last_context_tokens"))
            if (
                point is not None
                and before is not None
                and after is not None
                and before * 2 >= point
                and after * 2 < before
            ):
                status.compactions += 1
                status.round_marker = status.compactions
                updates[INFERRED_COMPACTIONS_LABEL_KEY] = str(status.compactions)
        if (
            status.remaining_pct is not None
            and status.remaining_pct > status.remaining_pct_setting
            and _label_int(current.get(FRESH_ROUND_LABEL_KEY)) != status.round_marker
        ):
            updates[FRESH_ROUND_LABEL_KEY] = str(status.round_marker)
        return LabelGroupChange(updates, deletes)

    conversation_store.mutate_label_group(session_id, "omnigent.", observe)
    if eligibility_read_failed:
        return None
    try:
        reloaded = conversation_store.get_conversation(session_id)
        if reloaded is not None:
            return auto_rotation_for(reloaded, conversation_store, permission_store, prefs_store)
    except Exception:
        logger.warning("Could not observe auto-rotation context for %s", session_id, exc_info=True)
    return None
