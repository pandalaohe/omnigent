"""
``GET /v1/sessions/{session_id}/collab-settings`` — the session owner's
session-collaboration settings, read by the runner.

The runner has no preferences store; it reads the owner's settings through
this route when a flow or timer starts, before every flow tick, and before
every timer firing (row ``flow_timer_enabled``). The route is a read and
answers even when collaboration is switched off, so the runner can see that.
"""

from __future__ import annotations

import asyncio
import dataclasses

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from omnigent.server.auth import LEVEL_READ, RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.routes._auth_helpers import get_user_id, require_access_and_level
from omnigent.server.routes.sessions.routes_peer import effective_owner_id
from omnigent.server.user_preferences_store import (
    SqlAlchemyUserPreferencesStore,
    read_collab_settings,
)
from omnigent.stores import ConversationStore
from omnigent.stores.permission_store import PermissionStore


def collab_settings_owner(
    session_id: str,
    *,
    conversation_store: ConversationStore,
    auth_provider: AuthProvider | None,
    permission_store: PermissionStore | None,
) -> str | None:
    """
    Resolve whose collaboration settings govern a session.

    No auth provider → the reserved local user; otherwise the top-level
    ancestor's owner grant (the rule peer sends use). ``None`` reads as the
    defaults.

    :param session_id: Session id, e.g. ``"conv_abc123"``.
    :returns: The owner's user id, or ``None`` when unresolvable.
    """
    if auth_provider is None:
        return RESERVED_USER_LOCAL
    conv = conversation_store.get_conversation(session_id)
    if conv is None:
        return None
    return effective_owner_id(conv, conversation_store, permission_store)


def register_collab_settings_routes(
    router: APIRouter,
    *,
    conversation_store: ConversationStore,
    auth_provider: AuthProvider | None,
    permission_store: PermissionStore | None,
    user_preferences_store: SqlAlchemyUserPreferencesStore | None,
) -> None:
    """Register the collab-settings read route on ``router``."""

    @router.get(
        "/sessions/{session_id}/collab-settings",
        # Runner callback — hidden from the public API reference.
        include_in_schema=False,
    )
    async def get_collab_settings(request: Request, session_id: str) -> JSONResponse:
        """
        Return the session owner's collaboration settings.

        :param request: FastAPI request (caller identity).
        :param session_id: Session whose owner's settings apply.
        :returns: Every ``CollabSettings`` field as JSON.
        :raises OmnigentError: 404 / 403 when the caller cannot read the session.
        """
        user_id = get_user_id(request, auth_provider)
        await require_access_and_level(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        owner = await asyncio.to_thread(
            collab_settings_owner,
            session_id,
            conversation_store=conversation_store,
            auth_provider=auth_provider,
            permission_store=permission_store,
        )
        settings = await asyncio.to_thread(read_collab_settings, user_preferences_store, owner)
        return JSONResponse(dataclasses.asdict(settings))
