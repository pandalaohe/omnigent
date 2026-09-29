"""Per-owner session-collaboration settings enforcement.

The "Session collaboration" settings section stores one ``session_collab``
namespace per user. This module owns the server-side reads that turn those
stored values into behaviour: the owner rule every enforcement point shares
(the top-level ancestor's owner grant, or the reserved local user when auth
is off), the master-switch snapshot the runner session-init envelope
carries, the refusal raised by collaboration routes while the switch is
off, and the creation-time ``peer_inbound`` stamp for new top-level
sessions. Every read is fail-safe: a missing or broken row resolves to the
accessor's defaults, never an error.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

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

# Mirrors routes_peer's label; defined here so this module need not import its
# private name.
_PEER_INBOUND_LABEL = "peer_inbound"


def collab_owner_for(
    conv: Conversation,
    conversation_store: ConversationStore,
    permission_store: PermissionStore | None,
    *,
    fallback_user: str | None = None,
) -> str:
    """Return the user whose collaboration settings govern *conv*.

    The effective owner is the top-level ancestor's owner grant, so a
    child session follows its root's settings. Without a permission store
    (auth off) the preferences owner is the reserved local user — the same
    owner ``/v1/me/preferences`` writes to.

    :param conv: The session row.
    :param conversation_store: Store used for the parent walk.
    :param permission_store: Permission store, or ``None``.
    :param fallback_user: Caller-known user id, used when no owner grant
        resolves (e.g. a create request's authenticated user).
    :returns: The owner user id, never ``None``.
    """
    # Lazy: routes_peer imports the orchestration module, so a module-level
    # import here would close the cycle.
    from omnigent.server.routes.sessions.routes_peer import effective_owner_id

    return effective_owner_id(conv, conversation_store, permission_store) or (
        fallback_user or RESERVED_USER_LOCAL
    )


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


def stamp_default_inbound(
    labels: dict[str, str],
    *,
    parent_session_id: str | None,
    prefs_store: SqlAlchemyUserPreferencesStore | None,
    owner: str | None,
) -> None:
    """Stamp a new top-level session's inbound policy from *owner*'s default.

    Only ``hold`` and ``refuse`` are stamped: an absent label already means
    accept, so the default setting changes no row in that case. A child
    inherits its parent's policy and an explicit label always wins.

    Blocking: callers run it in a thread.

    :param labels: The create request's initial labels, mutated in place.
    :param parent_session_id: The new session's parent, or ``None``.
    :param prefs_store: Preferences store, or ``None``.
    :param owner: The creating user, or ``None`` for the local user.
    """
    if parent_session_id is not None or _PEER_INBOUND_LABEL in labels:
        return
    settings = read_collab_settings(prefs_store, owner or RESERVED_USER_LOCAL)
    if settings.default_inbound in ("hold", "refuse"):
        labels[_PEER_INBOUND_LABEL] = settings.default_inbound
