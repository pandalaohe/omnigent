"""Owner controls for a durable startup, separate from stopping a shared runner."""

import asyncio

from fastapi import APIRouter, HTTPException, Request

from omnigent.entities.session_startup import SessionStartupStatus
from omnigent.runtime import session_stream
from omnigent.server.auth import LEVEL_OWNER, AuthProvider
from omnigent.server.routes._auth_helpers import get_user_id, require_access_and_level
from omnigent.server.schemas import CancelStartupRequest, SessionStartupEvent
from omnigent.stores.conversation_store import ConversationStore
from omnigent.stores.permission_store import PermissionStore
from omnigent.stores.session_startup_store import SessionStartupStore


def register_startup_routes(
    router: APIRouter,
    *,
    conversation_store: ConversationStore,
    permission_store: PermissionStore | None,
    auth_provider: AuthProvider | None,
) -> None:
    @router.post(
        "/sessions/{session_id}/startup/cancel",
        response_model=SessionStartupStatus,
        responses={
            403: {"description": "Owner access required"},
            404: {"description": "Session startup not found"},
            409: {"description": "Startup attempt changed or already completed"},
        },
    )
    async def cancel_startup(
        request: Request, session_id: str, body: CancelStartupRequest
    ) -> SessionStartupStatus:
        user_id = get_user_id(request, auth_provider)
        await require_access_and_level(
            user_id, session_id, LEVEL_OWNER, permission_store, conversation_store
        )
        store: SessionStartupStore | None = getattr(request.app.state, "startup_store", None)
        current = await asyncio.to_thread(store.get, body.startup_id) if store else None
        if current is None or current.session_id != session_id:
            raise HTTPException(404, "startup not found")
        assert store is not None
        cancelled = await asyncio.to_thread(
            store.cancel_current, current.id, expected_attempt_id=body.attempt_id
        )
        if cancelled is None:
            raise HTTPException(409, "startup attempt changed; refresh before cancelling")
        inputs = getattr(request.app.state, "startup_inputs", None)
        if inputs is not None:
            await inputs.cancel(session_id)
        status = await asyncio.to_thread(store.presentation, session_id)
        assert status is not None
        session_stream.publish(
            session_id,
            SessionStartupEvent(
                type="session.startup", conversation_id=session_id, startup=status
            ).model_dump(),
        )
        return status
