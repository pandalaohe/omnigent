"""Per-owner session-collaboration settings enforcement.

The "Session collaboration" settings section stores one ``session_collab``
namespace per user. This module owns the server-side reads that turn those
stored values into behaviour: the owner rule every enforcement point shares
(the top-level ancestor's owner grant, or the reserved local user when auth
is off), the master-switch snapshot the runner session-init envelope
carries, and the refusal raised by collaboration routes while the switch is
off. Every read is fail-safe: a missing or broken row resolves to the
accessor's defaults, never an error.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal

from omnigent.entities import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.server.user_preferences_store import (
    CollabSettings,
    read_collab_settings,
)

if TYPE_CHECKING:
    from omnigent.server.user_preferences_store import SqlAlchemyUserPreferencesStore
    from omnigent.stores.conversation_store import ConversationStore
    from omnigent.stores.permission_store import PermissionStore

_logger = logging.getLogger(__name__)

COLLAB_DISABLED_MESSAGE = (
    "Session collaboration is turned off in Settings > Session collaboration."
)

AGENT_SESSION_HEADER = "X-Omnigent-Agent-Session"
SUBAGENT_OPEN_DISABLED_MESSAGE = (
    "Sub-agent sessions may not open or unarchive sessions (Session open/unarchive restrictions). "
    "Report this need to your parent session in your final output."
)
ARCHIVED_SESSION_DISABLED_MESSAGE = (
    "The session restriction setting prevents agents from restoring or messaging "
    "archived sessions. "
    "Open a new session with a handover instead."
)


def require_session_action_allowed(
    conv: Conversation,
    conversation_store: ConversationStore,
    permission_store: PermissionStore | None,
    prefs_store: SqlAlchemyUserPreferencesStore | None,
    *,
    action: Literal["open", "unarchive", "send"] = "open",
    target: Conversation | None = None,
) -> None:
    """Enforce session restrictions from the caller's stored parent and owner's preferences."""
    owner = collab_owner_for(conv, conversation_store, permission_store)
    mode = read_collab_settings(prefs_store, owner).session_restrictions
    if mode == "off":
        return
    if conv.parent_conversation_id is not None and action in {"open", "unarchive"}:
        message = SUBAGENT_OPEN_DISABLED_MESSAGE
        if mode == "all" and action == "unarchive":
            message += " " + ARCHIVED_SESSION_DISABLED_MESSAGE
        raise OmnigentError(message, code=ErrorCode.FORBIDDEN)
    if mode == "all" and (
        action == "unarchive" or (action == "send" and target is not None and target.archived)
    ):
        raise OmnigentError(ARCHIVED_SESSION_DISABLED_MESSAGE, code=ErrorCode.FORBIDDEN)


def require_session_tool_allowed(
    conv: Conversation,
    name: object,
    arguments: object,
    conversation_store: ConversationStore,
    permission_store: PermissionStore | None,
    prefs_store: SqlAlchemyUserPreferencesStore | None,
) -> None:
    """Apply the mandatory restriction before optional tool policies or harness checks."""
    if not isinstance(name, str):
        return
    name = name.removeprefix("mcp__omnigent__")
    if name in {"sys_session_create", "sys_session_open", "sys_session_unarchive"}:
        require_session_action_allowed(
            conv,
            conversation_store,
            permission_store,
            prefs_store,
            action="unarchive" if name == "sys_session_unarchive" else "open",
        )
    elif name in {"sys_session_send", "sys_session_message"} and isinstance(arguments, dict):
        target_id = arguments.get("session_id")
        if isinstance(target_id, str):
            target = conversation_store.get_conversation(target_id)
            require_session_action_allowed(
                conv,
                conversation_store,
                permission_store,
                prefs_store,
                action="send",
                target=target,
            )


def collab_owner_for(
    conv: Conversation,
    conversation_store: ConversationStore,
    permission_store: PermissionStore | None,
) -> str:
    """Return the user whose collaboration settings govern *conv*.

    The effective owner is the top-level ancestor's owner grant, so a
    child session follows its root's settings. Without a permission store
    (auth off) the preferences owner is the reserved local user — the same
    owner ``/v1/me/preferences`` writes to.

    :param conv: The session row.
    :param conversation_store: Store used for the parent walk.
    :param permission_store: Permission store, or ``None``.
    :returns: The owner user id, never ``None``.
    """
    # Lazy: routes_peer imports the orchestration module, so a module-level
    # import here would close the cycle.
    from omnigent.server.routes.sessions.routes_peer import effective_owner_id

    return effective_owner_id(conv, conversation_store, permission_store) or RESERVED_USER_LOCAL


def collab_owners_for(
    convs: list[Conversation],
    conversation_store: ConversationStore,
    permission_store: PermissionStore | None,
) -> dict[str, str | None]:
    """Resolve owners within one read batch, sharing ancestor rows and root grants.

    Missing parents stop the walk at the last existing row, as in
    :func:`collab_owner_for`. A failed read returns ``None`` only for the
    affected branch; no owner or row cache survives this call.
    """
    if permission_store is None:
        return {conv.id: RESERVED_USER_LOCAL for conv in convs}
    from omnigent.server.routes._auth_helpers import get_session_owner_id
    from omnigent.server.routes.sessions.routes_peer import _OWNER_CHAIN_MAX_HOPS

    rows: dict[str, Conversation | None] = {conv.id: conv for conv in convs}
    tops = {conv.id: conv for conv in convs}
    failed_rows: set[str] = set()
    failed_sessions: set[str] = set()
    active = set(tops)
    for _ in range(_OWNER_CHAIN_MAX_HOPS):
        parents = {
            sid: parent_id
            for sid in active
            if (parent_id := tops[sid].parent_conversation_id) is not None
        }
        if not parents:
            break
        missing = sorted(set(parents.values()) - rows.keys())
        if missing:
            try:
                fetched = conversation_store.get_conversations(missing)
                rows.update({sid: fetched.get(sid) for sid in missing})
            except Exception:  # noqa: BLE001 — isolate a failed row from healthy branches
                _logger.warning(
                    "Collaboration ancestor batch read failed; falling back to individual reads",
                    exc_info=True,
                )
                for sid in missing:
                    try:
                        rows[sid] = conversation_store.get_conversation(sid)
                    except Exception:  # noqa: BLE001
                        rows[sid] = None
                        failed_rows.add(sid)
                        _logger.warning(
                            "Collaboration ancestor read failed for %s", sid, exc_info=True
                        )
        active = set()
        for sid, parent_id in parents.items():
            if parent_id in failed_rows:
                failed_sessions.add(sid)
            elif (parent := rows[parent_id]) is not None:
                tops[sid] = parent
                active.add(sid)

    owner_by_root: dict[str, str | None] = {}
    owners: dict[str, str | None] = {}
    for sid, top in tops.items():
        if sid in failed_sessions:
            owners[sid] = None
            continue
        if top.id not in owner_by_root:
            try:
                owner_by_root[top.id] = (
                    get_session_owner_id(top.id, permission_store) or RESERVED_USER_LOCAL
                )
            except Exception:  # noqa: BLE001 — keep the other roots usable
                owner_by_root[top.id] = None
                _logger.warning("Collaboration owner read failed for %s", top.id, exc_info=True)
        owners[sid] = owner_by_root[top.id]
    return owners


def session_peer_enabled(
    conv: Conversation,
    *,
    flag_on: bool,
    conversation_store: ConversationStore,
    permission_store: PermissionStore | None,
    prefs_store: SqlAlchemyUserPreferencesStore | None,
) -> bool:
    """Resolve the session-init peer-messaging snapshot for one session.

    The snapshot is the server feature flag AND the owner's master switch.
    Any failure beyond the flag read — an owner walk or preferences read —
    keeps today's behaviour by returning ``flag_on``.

    Blocking: callers run it in a thread.

    :param conv: The session being initialized.
    :param flag_on: The deployment ``session_peer_messaging`` feature flag.
    :param conversation_store: Store used for the owner walk.
    :param permission_store: Permission store, or ``None``.
    :param prefs_store: Preferences store, or ``None``.
    :returns: ``False`` only when the flag is off or the owner switched
        collaboration off; ``True`` otherwise.
    """
    if not flag_on:
        return False
    try:
        owner = collab_owner_for(conv, conversation_store, permission_store)
        return read_collab_settings(prefs_store, owner).enabled
    except Exception:  # noqa: BLE001
        _logger.warning(
            "Failed to resolve session collaboration settings for %s", conv.id, exc_info=True
        )
        return flag_on


def require_collab_enabled(
    store: SqlAlchemyUserPreferencesStore | None,
    owner: str,
) -> CollabSettings:
    """Read *owner*'s settings, refusing when the master switch is off.

    :param store: Preferences store, or ``None`` (defaults apply).
    :param owner: Owner whose settings govern the call.
    :returns: The owner's resolved settings.
    :raises OmnigentError: ``FORBIDDEN`` naming the settings switch.
    """
    settings = read_collab_settings(store, owner)
    if not settings.enabled:
        raise OmnigentError(COLLAB_DISABLED_MESSAGE, code=ErrorCode.FORBIDDEN)
    return settings
