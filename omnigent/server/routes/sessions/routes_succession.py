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
from fastapi.responses import JSONResponse

from omnigent._wrapper_labels import UI_MODE_LABEL_KEY, WRAPPER_LABEL_KEY
from omnigent.db.utils import generate_task_id
from omnigent.entities import Conversation, ErrorData, MessageData, NewConversationItem
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.mcp_selection import MCP_SELECTION_PREFIX
from omnigent.permission_tiers import PermissionTier
from omnigent.runner.routing import RunnerRouter
from omnigent.server.auth import LEVEL_EDIT, AuthProvider
from omnigent.server.permission_tiers import ConfirmedPermissionState, read_permission_state
from omnigent.server.routes._auth_helpers import (
    get_user_id as _get_user_id,
)
from omnigent.server.routes._auth_helpers import (
    require_access_and_level as _require_access_and_level,
)
from omnigent.server.routes._errors import session_not_found as _session_not_found
from omnigent.server.routes._host_worktree import WORKTREE_ROOT_LABEL_KEY
from omnigent.server.routes._sessions.helpers import (
    _native_coding_agent_for_session,
    _publish_external_conversation_item,
    _resolve_harness,
)
from omnigent.server.schemas import SessionHandoverRequest, SessionSuccessionRequest
from omnigent.server.session_succession import advance_succession
from omnigent.stores import AgentStore
from omnigent.stores.conversation_store import (
    HANDOVER_ITEM_LABEL_KEY,
    PROJECT_LABEL_KEY,
    ROTATE_REQUESTED_LABEL_KEY,
    SUCCEEDED_BY_LABEL_KEY,
    SUCCEEDS_LABEL_KEY,
    ConversationStore,
    SuccessionRefusedError,
)
from omnigent.stores.peer_message_store import PeerMessageStore
from omnigent.stores.permission_store import PermissionStore

#: The only native harnesses whose rotation (a new session taking over the
#: old one's live children) this fork's runner implements.
_ROTATION_NATIVE_HARNESSES = frozenset({"claude-native", "codex-native", "pi", "pi-native"})

#: Visible notice persisted alongside ``omnigent.rotate_requested``.
_ROTATE_NOTICE = "This session will continue in a new session when the current turn ends."


def _fresh_pi_launch_args(args: list[str] | None) -> list[str] | None:
    """Keep launch options while dropping native transcript/session selectors."""
    if args is None:
        return None
    with_value = {"--session-dir", "--session", "--session-id", "--fork"}
    standalone = {"--continue", "-c", "--resume", "-r", "--no-session"}
    kept: list[str] = []
    skip_value = False
    for arg in args:
        if skip_value:
            skip_value = False
        elif arg in with_value:
            skip_value = True
        elif arg in standalone or any(
            arg.startswith(f"{flag}=") for flag in with_value | standalone
        ):
            continue
        else:
            kept.append(arg)
    return kept


def _successor_labels(
    source: Conversation, harness: str, tier: PermissionTier | None
) -> dict[str, str]:
    """Copy configuration labels only; mint fresh permission authority."""
    retained = {
        key: value
        for key, value in source.labels.items()
        if key
        in {WRAPPER_LABEL_KEY, UI_MODE_LABEL_KEY, WORKTREE_ROOT_LABEL_KEY, PROJECT_LABEL_KEY}
        or key.startswith(MCP_SELECTION_PREFIX)
        or not key.startswith(("omnigent.", "omnigent:", "cost_control."))
    }
    if harness in {"codex-native", "claude-native"}:
        from omnigent.native._native_session_labels import replacement_session_labels

        retained = replacement_session_labels(source.labels)
        bridge_key = (
            "omnigent.codex_native.bridge_id"
            if harness == "codex-native"
            else "omnigent.claude_native.bridge_id"
        )
        retained[bridge_key] = source.labels.get(bridge_key, source.id)
    if tier is not None:
        retained.update(ConfirmedPermissionState(tier, 1, "blocked").labels(harness))
    return retained


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

    @router.post("/sessions/{session_id}/successor", status_code=200, response_model=None)
    async def create_session_successor(request: Request, session_id: str) -> dict[str, str]:
        """Create or recover a successor before its runner can initialize it."""
        user_id = _get_user_id(request, auth_provider)
        source = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if source is None:
            raise _session_not_found()
        await _require_access_and_level(
            user_id, session_id, LEVEL_EDIT, permission_store, conversation_store
        )
        if (
            source.parent_conversation_id is not None
            or not source.agent_id
            or not source.runner_id
        ):
            raise OmnigentError(
                "Successor requires a top-level session with an agent and runner",
                code=ErrorCode.CONFLICT,
            )
        harness = await asyncio.to_thread(_resolve_harness, source, agent_store=agent_store)
        if harness is None or harness not in _ROTATION_NATIVE_HARNESSES:
            raise OmnigentError(
                "Harness does not support session succession", code=ErrorCode.CONFLICT
            )
        tier = None
        if harness in {"pi", "pi-native"}:
            try:
                permission = read_permission_state(source.labels, harness=harness)
            except ValueError as exc:
                raise OmnigentError(
                    f"Source permission state requires recovery: {exc}", code=ErrorCode.CONFLICT
                ) from exc
            if permission.status != "ready":
                raise OmnigentError(
                    "Source permission state is not ready", code=ErrorCode.CONFLICT
                )
            tier = permission.tier
        await asyncio.to_thread(request.app.state.standing_pass_service.resolve, source)
        try:
            successor = await asyncio.to_thread(
                conversation_store.create_session_successor,
                source,
                initial_labels=_successor_labels(source, harness, tier),
                terminal_launch_args=(
                    _fresh_pi_launch_args(source.terminal_launch_args)
                    if harness in {"pi", "pi-native"}
                    else None
                ),
            )
        except SuccessionRefusedError as exc:
            raise OmnigentError(f"Successor refused: {exc}", code=ErrorCode.CONFLICT) from exc
        return {"id": successor.id}

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

        Only a top-level session with a supported rotation harness may call
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
        harness = (
            native_agent.harness
            if native_agent is not None
            else await asyncio.to_thread(_resolve_harness, conversation, agent_store=agent_store)
        )
        if harness not in _ROTATION_NATIVE_HARNESSES:
            raise OmnigentError(
                "not_native: handover rotation supports claude-native, codex-native, pi and pi-native sessions",
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
    ) -> Any:
        """
        Hand a top-level session's live children to its successor.

        The caller must hold edit access on both sessions. A session without
        a parent and without live children is a ``noop``, unless ``allow_empty``
        requests retirement or the server already prepared this successor.
        Undo is the same call reversed: when the
        target still points back at the caller via ``omnigent.succeeded_by``,
        the move transaction clears the forward pair and writes the inverse.
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
        # that succession. The store swaps the link inside the move
        # transaction, so a refused move leaves the forward pointer intact.
        reverse_of = target_conversation.labels.get(SUCCEEDED_BY_LABEL_KEY) == session_id

        if old_conversation.parent_conversation_id is not None:
            return {"status": "noop"}

        receipt = await asyncio.to_thread(conversation_store.get_succession, session_id, target_id)
        if receipt is None:
            receipt_id = uuid4().hex
            try:
                await asyncio.to_thread(
                    conversation_store.reassign_live_children,
                    session_id,
                    target_id,
                    receipt_id,
                    reverse_of=reverse_of,
                    allow_empty=(
                        body.allow_empty
                        or target_conversation.labels.get(SUCCEEDS_LABEL_KEY) == session_id
                    ),
                )
            except SuccessionRefusedError as exc:
                if exc.code == "already_succeeded":
                    return JSONResponse(
                        status_code=409,
                        content={
                            "error": {
                                "code": "already_succeeded",
                                "message": f"succession refused: {exc.code}: {exc}",
                                "successor_id": exc.successor_id,
                            }
                        },
                    )
                raise OmnigentError(
                    f"succession refused: {exc.code}: {exc}",
                    code=ErrorCode.CONFLICT,
                ) from exc
            receipt = await asyncio.to_thread(
                conversation_store.get_succession, session_id, target_id
            )
            if receipt is None:
                return {"status": "noop"}
        receipt_id = receipt.id

        receipt = await advance_succession(
            receipt_id,
            conversation_store=conversation_store,
            runner_router=runner_router,
            peer_message_store=peer_message_store,
            archive_cleanup=getattr(request.app.state, "archive_close_coordinator", None),
        )
        if receipt.phase == "void":
            current = await asyncio.to_thread(conversation_store.get_conversation, session_id)
            return {
                "status": "retired",
                "successor_id": (
                    current.labels.get(SUCCEEDED_BY_LABEL_KEY) if current is not None else None
                ),
            }
        return {
            "status": "done" if receipt.phase == "done" else "pending",
            "receipt_id": receipt.id,
            "phase": receipt.phase,
            "moved_ids": receipt.moved_ids,
        }
