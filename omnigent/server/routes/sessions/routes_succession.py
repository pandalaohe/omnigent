"""Session succession route: start or resume one durable succession.

``POST /v1/sessions/{session_id}/succession`` is the single entry point the
rotation paths call after a successor exists. A fresh call moves the live
children in one store transaction and runs the phase engine inline; a call
on an existing receipt resumes it at whatever phase it stopped.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Request

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner.routing import RunnerRouter
from omnigent.server.auth import LEVEL_EDIT, AuthProvider
from omnigent.server.routes._auth_helpers import (
    get_user_id as _get_user_id,
)
from omnigent.server.routes._auth_helpers import (
    require_access_and_level as _require_access_and_level,
)
from omnigent.server.routes._errors import session_not_found as _session_not_found
from omnigent.server.schemas import SessionSuccessionRequest
from omnigent.server.session_succession import advance_succession
from omnigent.stores.conversation_store import (
    SUCCEEDED_BY_LABEL_KEY,
    ConversationStore,
    SuccessionRefusedError,
)
from omnigent.stores.peer_message_store import PeerMessageStore
from omnigent.stores.permission_store import PermissionStore


def register_succession_routes(
    router: APIRouter,
    *,
    conversation_store: ConversationStore,
    runner_router: RunnerRouter | None = None,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
    peer_message_store: PeerMessageStore | None = None,
) -> None:
    """Register the succession route on ``router``."""

    @router.post(
        "/sessions/{session_id}/succession",
        status_code=200,
        # response_model=None: the body is a small status dict, not a
        # domain model.
        response_model=None,
    )
    async def start_session_succession(
        request: Request,
        session_id: str,
        body: SessionSuccessionRequest,
    ) -> dict[str, Any]:
        """
        Hand a top-level session's live children to its successor.

        The caller must hold edit access on both sessions. A session without
        a parent and without live children is a ``noop`` — ordinary rotations
        must keep today's behaviour. Undo is the same call reversed: when the
        target still points back at the caller via ``omnigent.succeeded_by``,
        the link is cleared first and the move writes the inverse.
        """
        user_id = _get_user_id(request, auth_provider)
        old_conversation = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if old_conversation is None:
            raise _session_not_found()
        target_id = body.target_session_id
        target_conversation = await asyncio.to_thread(
            conversation_store.get_conversation, target_id
        )
        if target_conversation is None:
            raise _session_not_found()
        await _require_access_and_level(
            user_id,
            session_id,
            LEVEL_EDIT,
            permission_store,
            conversation_store,
        )
        await _require_access_and_level(
            user_id,
            target_id,
            LEVEL_EDIT,
            permission_store,
            conversation_store,
        )

        # Undo (D10): the target succeeded the caller, so this call reverses
        # that succession. Clearing the forward pointer first keeps the
        # redirect from looping while the inverse move writes its own link.
        if target_conversation.labels.get(SUCCEEDED_BY_LABEL_KEY) == session_id:
            await asyncio.to_thread(
                conversation_store.clear_succession_link,
                target_id,
                session_id,
            )

        if old_conversation.parent_conversation_id is not None:
            return {"status": "noop"}

        receipt = await asyncio.to_thread(conversation_store.get_succession, session_id, target_id)
        if receipt is None:
            receipt_id = uuid4().hex
            try:
                direct_ids, _moved_ids = await asyncio.to_thread(
                    conversation_store.reassign_live_children,
                    session_id,
                    target_id,
                    receipt_id,
                )
            except SuccessionRefusedError as exc:
                raise OmnigentError(
                    f"succession refused: {exc.code}: {exc}",
                    code=ErrorCode.CONFLICT,
                ) from exc
            if not direct_ids:
                return {"status": "noop"}
        else:
            receipt_id = receipt.id

        receipt = await advance_succession(
            receipt_id,
            conversation_store=conversation_store,
            runner_router=runner_router,
            peer_message_store=peer_message_store,
        )
        return {
            "status": "done" if receipt.phase == "done" else "pending",
            "receipt_id": receipt.id,
            "phase": receipt.phase,
            "moved_ids": receipt.moved_ids,
        }
