"""Session succession routes: handover recording and durable succession.

``POST /v1/sessions/{session_id}/handover`` records the calling session's
handover note (and optionally asks its runner to rotate it at turn end).
``POST /v1/sessions/{session_id}/succession`` is the single entry point the
rotation paths call after a successor exists. A fresh call moves the live
children in one store transaction and runs the phase engine inline; a call
on an existing receipt resumes it at whatever phase it stopped.
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Request

from omnigent.db.utils import generate_task_id
from omnigent.entities import Conversation, ErrorData, MessageData, NewConversationItem
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
from omnigent.server.routes._sessions.helpers import (
    _native_coding_agent_for_session,
    _publish_external_conversation_item,
)
from omnigent.server.schemas import SessionHandoverRequest, SessionSuccessionRequest
from omnigent.server.session_succession import advance_succession
from omnigent.stores import AgentStore
from omnigent.stores.conversation_store import (
    HANDOVER_ITEM_LABEL_KEY,
    ROTATE_REQUESTED_LABEL_KEY,
    SUCCEEDED_BY_LABEL_KEY,
    ConversationStore,
    SuccessionRefusedError,
)
from omnigent.stores.peer_message_store import PeerMessageStore
from omnigent.stores.permission_store import PermissionStore

#: The only native harnesses whose rotation (a new session taking over the
#: old one's live children) this fork's runner implements.
_ROTATION_NATIVE_HARNESSES = frozenset({"claude-native", "codex-native"})

#: Visible notice persisted alongside ``omnigent.rotate_requested``.
_ROTATE_NOTICE = "This session will continue in a new session when the current turn ends."


def _handover_stable_id(session_id: str, text: str) -> str:
    """Return the dedupe id for one handover note on one session."""
    return hashlib.sha256(f"session_handover:{session_id}:{text}".encode()).hexdigest()[:32]


def _rotate_notice_stable_id(handover_item_id: str) -> str:
    """Return the dedupe id for the rotation notice of one handover item."""
    return hashlib.sha256(f"session_rotate:{handover_item_id}".encode()).hexdigest()[:32]


def register_succession_routes(
    router: APIRouter,
    *,
    conversation_store: ConversationStore,
    runner_router: RunnerRouter | None = None,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
    peer_message_store: PeerMessageStore | None = None,
    agent_store: AgentStore | None = None,
) -> None:
    """Register the handover and succession routes on ``router``."""

    def _handover_agent_name(conversation: Conversation) -> str:
        """Name the speaker of the server-authored handover item."""
        agent = (
            agent_store.get(conversation.agent_id)
            if agent_store is not None and conversation.agent_id
            else None
        )
        return agent.name if agent is not None else conversation.agent_id or "omnigent"

    @router.post(
        "/sessions/{session_id}/handover",
        status_code=200,
        # response_model=None: the body is a small status dict, not a
        # domain model.
        response_model=None,
    )
    async def record_session_handover(
        request: Request,
        session_id: str,
        body: SessionHandoverRequest,
    ) -> dict[str, Any]:
        """
        Record a handover note and optionally request a turn-end rotation.

        The note is a persisted assistant message whose id is stored in
        ``omnigent.handover_item``, so the succession engine can open the
        successor with it. With ``rotate``, ``omnigent.rotate_requested``
        carries the archive revision and the runner types ``/clear`` into
        the session's terminal when the current turn ends.

        Only a top-level claude-native / codex-native session may call
        this: other harnesses have no rotation this fork can complete.
        """
        user_id = _get_user_id(request, auth_provider)
        conversation = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if conversation is None:
            raise _session_not_found()
        await _require_access_and_level(
            user_id,
            session_id,
            LEVEL_EDIT,
            permission_store,
            conversation_store,
        )

        text = body.handover.strip()
        if not text:
            raise OmnigentError(
                "handover must be a non-empty string",
                code=ErrorCode.INVALID_INPUT,
            )
        if conversation.parent_conversation_id is not None:
            raise OmnigentError(
                "not_top_level: a sub-agent session cannot record a handover",
                code=ErrorCode.INVALID_INPUT,
            )
        native_agent = await asyncio.to_thread(_native_coding_agent_for_session, conversation)
        if native_agent is None or native_agent.harness not in _ROTATION_NATIVE_HARNESSES:
            raise OmnigentError(
                "not_native: handover rotation supports claude-native and codex-native sessions",
                code=ErrorCode.INVALID_INPUT,
            )

        agent_name = await asyncio.to_thread(_handover_agent_name, conversation)
        item = NewConversationItem(
            type="message",
            response_id=generate_task_id(),
            data=MessageData(
                role="assistant",
                agent=agent_name,
                content=[{"type": "output_text", "text": text}],
            ),
            stable_id=_handover_stable_id(session_id, text),
        )
        persisted = await asyncio.to_thread(conversation_store.append, session_id, [item])
        handover_item = persisted[0]
        if not handover_item.deduplicated:
            _publish_external_conversation_item(session_id, handover_item)

        label_updates = {HANDOVER_ITEM_LABEL_KEY: handover_item.id}
        if body.rotate:
            label_updates[ROTATE_REQUESTED_LABEL_KEY] = str(conversation.archive_revision)
        await asyncio.to_thread(conversation_store.set_labels, session_id, label_updates)

        if body.rotate:
            notice = NewConversationItem(
                type="error",
                response_id=generate_task_id(),
                data=ErrorData(
                    source="harness",
                    code="session_rotation",
                    message=_ROTATE_NOTICE,
                    level="info",
                ),
                stable_id=_rotate_notice_stable_id(handover_item.id),
            )
            persisted_notice = await asyncio.to_thread(
                conversation_store.append,
                session_id,
                [notice],
            )
            if persisted_notice and not persisted_notice[0].deduplicated:
                _publish_external_conversation_item(session_id, persisted_notice[0])

        return {"item_id": handover_item.id, "rotate": body.rotate}

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
