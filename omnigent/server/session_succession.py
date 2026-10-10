"""Server side of session succession: the durable phase engine.

One receipt (``session_successions`` row) records a top-level session
handing its live children to a successor. The store move
(``reassign_live_children``) writes the receipt at phase ``moved`` in the
same transaction that reparents the children; this module runs every later
phase and commits each one before the next starts, so a crash leaves a
receipt a resume trigger can pick up:

``moved → rekeyed → opened → released → cards_closed → archived → done``

Resumers are serialized per receipt with an in-process lock; a runner that
is not ready yet keeps the receipt at ``moved`` and gets a bounded in-process
retry on top of the runner-connect and startup triggers.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from omnigent.db.utils import generate_task_id
from omnigent.entities import Conversation, ErrorData, MessageData, NewConversationItem
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner.routing import RunnerRouter
from omnigent.runtime import pending_elicitations, session_stream
from omnigent.server.routes._sessions.helpers import (
    _get_runner_client,
    _message_text,
    _publish_child_status_to_parent,
    _publish_external_conversation_item,
    _publish_session_superseded,
    _signal_harness_elicitation_resolved_by_id,
)
from omnigent.server.routes._sessions.orchestration import _post_system_message
from omnigent.server.runner_session_init import runner_archive_states_for_conversation
from omnigent.server.schemas import SessionCreatedEvent
from omnigent.stores.conversation_store import (
    HANDOVER_ITEM_LABEL_KEY,
    ConversationStore,
    SessionSuccession,
)
from omnigent.stores.peer_message_store import PeerMessageStore

_logger = logging.getLogger(__name__)

# States in which a peer message has not reached its receiver yet. A record
# being delivered inline (``delivering``) or already delivered stays with the
# receiver it reached — only records that can still be routed move.
_PEER_UNDELIVERED_STATES: tuple[str, ...] = ("pending", "queued", "held")

# Bounded in-process retry for a stalled phase (successor runner not ready,
# runner unreachable, wake not delivered); runner-connect and startup
# triggers remain after it gives up.
_RETRY_BACKOFFS_S: tuple[float, ...] = (2.0, 4.0, 8.0, 16.0, 30.0)

# One resumer lock per receipt; a phase is never run concurrently in-process.
# custom-lint: disable-next=workspace-scoped-cache -- keyed by server-generated uuid4 receipt id
_resumer_locks: dict[str, asyncio.Lock] = {}
# custom-lint: disable-next=workspace-scoped-cache -- keyed by server-generated uuid4 receipt id
_retry_attempts: dict[str, int] = {}
# custom-lint: disable-next=workspace-scoped-cache -- keyed by server-generated uuid4 receipt id
_retry_tasks: dict[str, asyncio.Task[None]] = {}


@dataclass
class _RunnerGroup:
    """One runner's client plus the succession sessions it holds."""

    client: httpx.AsyncClient
    session_ids: list[str] = field(default_factory=list)


async def advance_succession(
    receipt_id: str,
    *,
    conversation_store: ConversationStore,
    runner_router: RunnerRouter | None,
    peer_message_store: PeerMessageStore | None,
    archive_cleanup: Any = None,
) -> SessionSuccession:
    """
    Run one receipt through every phase it can reach, then stop.

    Each phase commits its result with a phase compare-and-set before the
    next starts. A phase that stops early (runner not ready, transport
    failure) leaves the receipt where it was and records the error; the next
    trigger resumes there. The returned receipt is the freshest row read.

    :param receipt_id: Receipt row id, e.g. ``"3f2a...hex"``.
    :param conversation_store: Store owning the receipt and conversations.
    :param runner_router: Router resolving each session's runner client.
    :param peer_message_store: Durable peer-message store, or ``None`` when
        peer messaging is off.
    :returns: The receipt at its resting phase.
    :raises OmnigentError: When the receipt no longer exists.
    """
    lock = _lock_for(receipt_id)
    async with lock:
        while True:
            receipt = await asyncio.to_thread(conversation_store.get_succession_by_id, receipt_id)
            if receipt is None:
                raise OmnigentError(
                    f"succession receipt {receipt_id!r} does not exist",
                    code=ErrorCode.NOT_FOUND,
                )
            phase = receipt.phase
            try:
                if phase == "moved":
                    receipt = await _phase_rekey(
                        receipt,
                        conversation_store=conversation_store,
                        runner_router=runner_router,
                        peer_message_store=peer_message_store,
                    )
                elif phase == "rekeyed":
                    receipt = await _phase_open(
                        receipt,
                        conversation_store=conversation_store,
                        runner_router=runner_router,
                        peer_message_store=peer_message_store,
                    )
                elif phase == "opened":
                    receipt = await _phase_release(
                        receipt,
                        conversation_store=conversation_store,
                        runner_router=runner_router,
                    )
                elif phase == "released":
                    receipt = await _phase_close_questions(
                        receipt,
                        conversation_store=conversation_store,
                    )
                elif phase == "cards_closed":
                    receipt = await _phase_archive(
                        receipt,
                        conversation_store=conversation_store,
                        archive_cleanup=archive_cleanup,
                    )
                elif phase == "archived":
                    receipt = await _phase_publish_done(
                        receipt,
                        conversation_store=conversation_store,
                    )
                else:
                    return receipt
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # The failure is stored on the receipt; the next trigger resumes there.
                _logger.exception(
                    "Succession %s failed in phase %s",
                    receipt.id,
                    phase,
                    extra={"session_id": receipt.old_id},
                )
                _schedule_retry(
                    receipt.id,
                    conversation_store=conversation_store,
                    runner_router=runner_router,
                    peer_message_store=peer_message_store,
                    archive_cleanup=archive_cleanup,
                )
                return await _record_phase_error(receipt, exc, conversation_store)
            if receipt.phase == phase or receipt.phase == "done":
                return receipt
            _retry_attempts.pop(receipt.id, None)


async def resume_unfinished_successions(
    *,
    conversation_store: ConversationStore,
    runner_router: RunnerRouter | None,
    peer_message_store: PeerMessageStore | None,
    archive_cleanup: Any = None,
) -> None:
    """
    Advance every unfinished receipt past ``planned``; never raises.

    The server-startup trigger: receipts already in a phase that changed
    state resume, while a receipt still at ``planned`` had nothing committed
    and is abandoned.

    :param conversation_store: Store owning the receipts.
    :param runner_router: Router resolving each session's runner client.
    :param peer_message_store: Durable peer-message store, or ``None``.
    """
    try:
        receipts = await asyncio.to_thread(conversation_store.list_unfinished_successions)
    except Exception:
        # A failed startup scan must not break boot.
        _logger.exception("Succession startup scan failed")
        return
    for receipt in receipts:
        if receipt.phase == "planned":
            continue
        await _resume_one(
            receipt.id,
            conversation_store=conversation_store,
            runner_router=runner_router,
            peer_message_store=peer_message_store,
            archive_cleanup=archive_cleanup,
        )


async def resume_successions_for_runner(
    runner_id: str,
    *,
    conversation_store: ConversationStore,
    runner_router: RunnerRouter | None,
    peer_message_store: PeerMessageStore | None,
    archive_cleanup: Any = None,
) -> None:
    """
    Resume unfinished receipts whose successor is bound to a runner; never raises.

    The runner-connect trigger: a successor whose runner just came online is
    exactly the case the ``moved`` readiness gate waits for.

    :param runner_id: The runner that just connected.
    :param conversation_store: Store owning the receipts.
    :param runner_router: Router resolving each session's runner client.
    :param peer_message_store: Durable peer-message store, or ``None``.
    """
    try:
        receipts = await asyncio.to_thread(conversation_store.list_unfinished_successions)
    except Exception:
        # A failed scan must not break the connect hook.
        _logger.exception("Succession scan failed for runner %s", runner_id)
        return
    for receipt in receipts:
        if receipt.phase == "planned":
            continue
        try:
            successor = await asyncio.to_thread(
                conversation_store.get_conversation, receipt.new_id
            )
        except Exception:
            # One unreadable receipt must not stop the rest.
            _logger.exception("Succession resume could not read successor %s", receipt.new_id)
            continue
        if successor is None or successor.runner_id != runner_id:
            continue
        await _resume_one(
            receipt.id,
            conversation_store=conversation_store,
            runner_router=runner_router,
            peer_message_store=peer_message_store,
            archive_cleanup=archive_cleanup,
        )


async def _resume_one(
    receipt_id: str,
    *,
    conversation_store: ConversationStore,
    runner_router: RunnerRouter | None,
    peer_message_store: PeerMessageStore | None,
    archive_cleanup: Any = None,
) -> None:
    """Resume one receipt, logging instead of raising."""
    try:
        await advance_succession(
            receipt_id,
            conversation_store=conversation_store,
            runner_router=runner_router,
            peer_message_store=peer_message_store,
            archive_cleanup=archive_cleanup,
        )
    except Exception:
        # Triggers are best-effort by contract.
        _logger.exception("Succession resume failed for receipt %s", receipt_id)


def _lock_for(receipt_id: str) -> asyncio.Lock:
    """Return the in-process resumer lock for one receipt."""
    lock = _resumer_locks.get(receipt_id)
    if lock is None:
        lock = asyncio.Lock()
        _resumer_locks[receipt_id] = lock
    return lock


async def _record_phase_error(
    receipt: SessionSuccession,
    exc: Exception,
    conversation_store: ConversationStore,
) -> SessionSuccession:
    """Store the failure on the receipt without changing its phase."""
    message = f"{type(exc).__name__}: {exc}"[:4000]
    await asyncio.to_thread(
        conversation_store.update_succession,
        receipt.id,
        expected_phase=receipt.phase,
        phase=receipt.phase,
        error=message,
    )
    return await _read(conversation_store, receipt.id) or receipt


async def _read(
    conversation_store: ConversationStore,
    receipt_id: str,
) -> SessionSuccession | None:
    """Re-read a receipt after a write."""
    return await asyncio.to_thread(conversation_store.get_succession_by_id, receipt_id)


async def _advance(
    receipt: SessionSuccession,
    phase: str,
    *,
    conversation_store: ConversationStore,
    **fields: Any,
) -> SessionSuccession:
    """Compare-and-set the receipt to ``phase`` and return the fresh row."""
    fields.setdefault("error", None)
    await asyncio.to_thread(
        conversation_store.update_succession,
        receipt.id,
        expected_phase=receipt.phase,
        phase=phase,
        **fields,
    )
    return await _read(conversation_store, receipt.id) or receipt


async def _phase_rekey(
    receipt: SessionSuccession,
    *,
    conversation_store: ConversationStore,
    runner_router: RunnerRouter | None,
    peer_message_store: PeerMessageStore | None,
) -> SessionSuccession:
    """``moved → rekeyed``: hand each holder of moved state to the successor."""
    groups = await _runner_groups(receipt, conversation_store, runner_router)
    # Every runner gets the full moved set: the successor's runner also holds
    # work entries for children hosted on the other runners and must rewrite
    # them all, while each runner applies only the archive states it owns.
    archive_states: dict[str, list[dict[str, Any]]] = {}
    for moved_id in receipt.moved_ids:
        conversation = await asyncio.to_thread(conversation_store.get_conversation, moved_id)
        if conversation is None:
            continue
        states = await runner_archive_states_for_conversation(conversation, conversation_store)
        archive_states[moved_id] = [state.model_dump(mode="json") for state in states]
    dropped: list[dict[str, Any]] = list(receipt.dropped or [])
    for group in groups.values():
        try:
            response = await group.client.post(
                f"/v1/sessions/{receipt.old_id}/succession",
                json={
                    "target_session_id": receipt.new_id,
                    "moved_ids": receipt.moved_ids,
                    "archive_states": archive_states,
                },
                timeout=30.0,
            )
        except (httpx.HTTPError, ConnectionError) as exc:
            raise RuntimeError(f"runner unreachable during re-key: {exc}") from exc
        if response.status_code == 409:
            _schedule_retry(
                receipt.id,
                conversation_store=conversation_store,
                runner_router=runner_router,
                peer_message_store=peer_message_store,
            )
            _logger.info(
                "Succession %s: successor not ready; retrying later",
                receipt.id,
                extra={"session_id": receipt.old_id},
            )
            return receipt
        if response.status_code >= 400:
            raise RuntimeError(
                f"runner refused re-key ({response.status_code}): {response.text[:300]}"
            )
        try:
            body = response.json()
        except ValueError:
            body = {}
        for item in body.get("dropped") or []:
            if isinstance(item, dict):
                dropped.append(item)
        # A later runner's failure must not lose what an earlier runner
        # already cancelled: a retry calls that runner again, which has
        # nothing left to drop. Persist each report on the same phase.
        receipt = await _advance(
            receipt,
            "moved",
            conversation_store=conversation_store,
            dropped=dropped,
        )
    return await _advance(
        receipt,
        "rekeyed",
        conversation_store=conversation_store,
        dropped=dropped,
    )


async def _phase_open(
    receipt: SessionSuccession,
    *,
    conversation_store: ConversationStore,
    runner_router: RunnerRouter | None,
    peer_message_store: PeerMessageStore | None,
) -> SessionSuccession:
    """``rekeyed → opened``: snapshot questions, re-target peers, post the opening."""
    old_conversation = await asyncio.to_thread(conversation_store.get_conversation, receipt.old_id)
    if old_conversation is None:
        raise RuntimeError(f"session {receipt.old_id!r} vanished during succession")

    questions = (
        receipt.questions if receipt.questions is not None else _open_questions(receipt.old_id)
    )
    if receipt.questions is None:
        # The receipt is the only durable copy: the live index entry dies with
        # the process, and the old card is closed a few phases later.
        receipt = await _advance(
            receipt,
            "rekeyed",
            conversation_store=conversation_store,
            questions=questions,
        )

    dropped: list[dict[str, Any]] = list(receipt.dropped or [])
    if peer_message_store is not None:
        retargeted = await asyncio.to_thread(
            peer_message_store.retarget_receiver,
            receipt.old_id,
            receipt.new_id,
            _PEER_UNDELIVERED_STATES,
        )
        if retargeted:
            _logger.info(
                "Succession %s: re-targeted %d peer message(s) to %s",
                receipt.id,
                len(retargeted),
                receipt.new_id,
                extra={"session_id": receipt.old_id},
            )

    handover = await _handover_text(conversation_store, old_conversation)
    children = await _direct_children(receipt, conversation_store)
    text = _opening_text(
        old_id=receipt.old_id,
        handover=handover,
        children=children,
        questions=questions,
        dropped=dropped,
    )
    payload: dict[str, Any] = {
        "text": text,
        "waking": bool(handover) or bool(questions),
    }

    notice = NewConversationItem(
        type="error",
        response_id=generate_task_id(),
        data=ErrorData(
            source="harness",
            code="session_succession",
            message=text,
            level="info",
        ),
        stable_id=hashlib.sha256(f"succession:{receipt.id}".encode()).hexdigest()[:32],
    )
    persisted = await asyncio.to_thread(
        conversation_store.append,
        receipt.new_id,
        [notice],
    )
    opening_item_id = persisted[0].id if persisted else None
    if persisted and not persisted[0].deduplicated:
        _publish_external_conversation_item(receipt.new_id, persisted[0])
    if payload["waking"]:
        # A crash between this wake and the CAS can repeat it once; the
        # opening notice itself is deduplicated by its stable id.
        delivered = await _post_system_message(
            receipt.new_id,
            text,
            conversation_store=conversation_store,
            runner_router=runner_router,
        )
        if not delivered:
            # The successor must be able to re-ask the carried questions; stay
            # at ``rekeyed`` so a retry wakes it (the notice stays deduplicated).
            raise RuntimeError(f"opening wake not delivered to {receipt.new_id!r}")
    return await _advance(
        receipt,
        "opened",
        conversation_store=conversation_store,
        opening=payload,
        opening_item_id=opening_item_id,
        dropped=dropped,
    )


async def _phase_release(
    receipt: SessionSuccession,
    *,
    conversation_store: ConversationStore,
    runner_router: RunnerRouter | None,
) -> SessionSuccession:
    """``opened → released``: drain each runner's held buffer into the successor."""
    groups = await _runner_groups(receipt, conversation_store, runner_router)
    for group in groups.values():
        holds_successor = receipt.new_id in group.session_ids
        try:
            response = await group.client.post(
                f"/v1/sessions/{receipt.new_id}/succession/release",
                timeout=30.0,
            )
        except (httpx.HTTPError, ConnectionError) as exc:
            if holds_successor:
                raise RuntimeError(f"runner unreachable at release: {exc}") from exc
            _logger.warning(
                "Succession %s: best-effort release failed on a moved child's runner",
                receipt.id,
                exc_info=True,
            )
            continue
        if response.status_code >= 400:
            if holds_successor:
                raise RuntimeError(
                    f"runner refused release ({response.status_code}): {response.text[:300]}"
                )
            _logger.warning(
                "Succession %s: best-effort release returned %d on a moved child's runner",
                receipt.id,
                response.status_code,
            )
    return await _advance(receipt, "released", conversation_store=conversation_store)


async def _phase_close_questions(
    receipt: SessionSuccession,
    *,
    conversation_store: ConversationStore,
) -> SessionSuccession:
    """``released → cards_closed``: close the old session's stored questions."""
    for question in receipt.questions or []:
        elicitation_id = question.get("elicitation_id")
        if not isinstance(elicitation_id, str) or not elicitation_id:
            continue
        try:
            _signal_harness_elicitation_resolved_by_id(receipt.old_id, elicitation_id)
        except OmnigentError:
            # Already gone, or never parked on this replica.
            continue
        pending_elicitations.resolve(receipt.old_id, elicitation_id)
        session_stream.publish(
            receipt.old_id,
            {"type": "response.elicitation_resolved", "elicitation_id": elicitation_id},
            track_pending=False,
        )
    return await _advance(receipt, "cards_closed", conversation_store=conversation_store)


async def _phase_archive(
    receipt: SessionSuccession,
    *,
    conversation_store: ConversationStore,
    archive_cleanup: Any = None,
) -> SessionSuccession:
    """``cards_closed → archived``: archive the old session without runner teardown."""
    old_conversation = await asyncio.to_thread(conversation_store.get_conversation, receipt.old_id)
    if old_conversation is not None and not old_conversation.archived:
        cleanup = archive_cleanup is not None and await archive_cleanup.should_cleanup_on_archive(
            old_conversation
        )
        await asyncio.to_thread(
            conversation_store.update_conversation,
            receipt.old_id,
            archived=True,
            close_cli_on_archive=False,
            delete_worktree=cleanup,
        )
        # The normal archive route's only stream publish; for a top-level old
        # session it publishes nothing, and so does this.
        _publish_child_status_to_parent(receipt.old_id, None)
    current = await asyncio.to_thread(conversation_store.get_conversation, receipt.old_id)
    from omnigent.stores.conversation_store import ARCHIVE_DELETE_WORKTREE_LABEL_KEY

    if (
        archive_cleanup is not None
        and current is not None
        and current.archived
        and current.labels.get(ARCHIVE_DELETE_WORKTREE_LABEL_KEY) == str(current.archive_revision)
    ):
        await archive_cleanup.cleanup_archived_without_close(current.id, current.archive_revision)
    return await _advance(receipt, "archived", conversation_store=conversation_store)


async def _phase_publish_done(
    receipt: SessionSuccession,
    *,
    conversation_store: ConversationStore,
) -> SessionSuccession:
    """``archived → done``: let viewers follow the rotation and move the children."""
    _publish_session_superseded(receipt.old_id, receipt.new_id)
    for child_id in receipt.direct_ids:
        child = await asyncio.to_thread(conversation_store.get_conversation, child_id)
        if child is None:
            continue
        # Only the session.created SSE per moved child (the scc24 contract);
        # the child was relocated, not created, so no delegated transcript
        # item or creation debug row.
        session_stream.publish(
            receipt.new_id,
            SessionCreatedEvent(
                type="session.created",
                conversation_id=receipt.new_id,
                child_session_id=child.id,
                agent_id=child.agent_id,
                parent_session_id=receipt.new_id,
            ).model_dump(),
        )
        _publish_child_status_to_parent(child.id, None)
    return await _advance(receipt, "done", conversation_store=conversation_store)


async def _runner_groups(
    receipt: SessionSuccession,
    conversation_store: ConversationStore,
    runner_router: RunnerRouter | None,
) -> dict[str, _RunnerGroup]:
    """
    Group the successor and every moved session by their runner.

    Sessions with no runner are skipped (nothing to re-key there); a bound
    runner that cannot be resolved is an error — skipping it would advance
    the phase with that runner's state left behind.

    :param receipt: The succession receipt.
    :param conversation_store: Store holding the conversation rows.
    :param runner_router: Router resolving each session's runner client.
    :returns: ``{runner_id: _RunnerGroup}``.
    :raises RuntimeError: When a session with a bound runner has no client.
    """
    conversations: list[Conversation] = []
    successor = await asyncio.to_thread(conversation_store.get_conversation, receipt.new_id)
    if successor is not None:
        conversations.append(successor)
    for moved_id in receipt.moved_ids:
        conversation = await asyncio.to_thread(conversation_store.get_conversation, moved_id)
        if conversation is not None:
            conversations.append(conversation)

    groups: dict[str, _RunnerGroup] = {}
    for conversation in conversations:
        if conversation.runner_id is None:
            continue
        group = groups.get(conversation.runner_id)
        if group is not None:
            group.session_ids.append(conversation.id)
            continue
        client = await _get_runner_client(
            conversation.id,
            runner_router,
            conversation=conversation,
        )
        if client is None:
            raise RuntimeError(
                f"runner {conversation.runner_id!r} is offline for session {conversation.id!r}"
            )
        groups[conversation.runner_id] = _RunnerGroup(
            client=client,
            session_ids=[conversation.id],
        )
    return groups


async def _direct_children(
    receipt: SessionSuccession,
    conversation_store: ConversationStore,
) -> list[Conversation]:
    """Return the moved direct children, in receipt order."""
    children: list[Conversation] = []
    for child_id in receipt.direct_ids:
        child = await asyncio.to_thread(conversation_store.get_conversation, child_id)
        if child is not None:
            children.append(child)
    return children


def _open_questions(session_id: str) -> list[dict[str, Any]]:
    """
    Return the session's outstanding async question cards.

    Only cards whose params carry ``async_kind == "question"`` are carried:
    ordinary approval cards are tied to a running turn, while async questions
    are the ones a successor must be able to re-ask.

    :param session_id: Session whose live elicitation index to snapshot.
    :returns: The stored ``response.elicitation_request`` events.
    """
    questions: list[dict[str, Any]] = []
    for event in pending_elicitations.snapshot_for(session_id):
        params = event.get("params")
        if not isinstance(params, dict) or params.get("async_kind") != "question":
            continue
        elicitation_id = event.get("elicitation_id")
        if not isinstance(elicitation_id, str) or not elicitation_id:
            continue
        questions.append(event)
    return questions


async def _handover_text(
    conversation_store: ConversationStore,
    old_conversation: Conversation,
) -> str | None:
    """
    Read the handover item named by ``omnigent.handover_item``, if any.

    :param conversation_store: Store holding the old session's items.
    :param old_conversation: The retired session.
    :returns: The item's text, or ``None`` when unlabelled or missing.
    """
    item_id = old_conversation.labels.get(HANDOVER_ITEM_LABEL_KEY)
    if not item_id:
        return None
    page = await asyncio.to_thread(
        conversation_store.list_items,
        old_conversation.id,
        limit=200,
        order="desc",
        type="message",
    )
    for item in page.data:
        if item.id != item_id:
            continue
        if isinstance(item.data, MessageData):
            return _message_text(item.data.content)
        return None
    return None


def _question_text(question: dict[str, Any]) -> str:
    """Project one stored question event to its human-readable text."""
    params = question.get("params")
    if isinstance(params, dict):
        ask = params.get("ask_user_question")
        if isinstance(ask, dict):
            entries = ask.get("questions")
            if isinstance(entries, list):
                texts = [
                    entry.get("question")
                    for entry in entries
                    if isinstance(entry, dict)
                    and isinstance(entry.get("question"), str)
                    and entry["question"]
                ]
                if texts:
                    return "; ".join(texts)
        message = params.get("message")
        if isinstance(message, str) and message:
            return message
    elicitation_id = question.get("elicitation_id")
    return elicitation_id if isinstance(elicitation_id, str) and elicitation_id else "question"


def _opening_text(
    *,
    old_id: str,
    handover: str | None,
    children: list[Conversation],
    questions: list[dict[str, Any]],
    dropped: list[dict[str, Any]],
) -> str:
    """Render the successor's opening message (mechanism, plain English)."""
    lines: list[str] = []
    if handover:
        lines.append(handover)
        lines.append("")
    lines.append(f"Previous session: /c/{old_id}")
    if children:
        lines.append("Children now under this session:")
        for child in children:
            lines.append(f"- {child.id}: {child.title or child.id}")
    if questions:
        lines.append("Questions the previous session had open (closed there — ask again):")
        for question in questions:
            lines.append(f"- {_question_text(question)}")
    if dropped:
        lines.append("Stopped and not carried over:")
        for item in dropped:
            kind = item.get("kind", "item")
            item_id = item.get("id", "")
            label = item.get("label")
            detail = f"- {kind} {item_id}".rstrip()
            if isinstance(label, str) and label:
                detail += f": {label}"
            lines.append(detail)
    return "\n".join(lines)


def _schedule_retry(
    receipt_id: str,
    *,
    conversation_store: ConversationStore,
    runner_router: RunnerRouter | None,
    peer_message_store: PeerMessageStore | None,
    archive_cleanup: Any = None,
) -> None:
    """Retry a stalled receipt in-process, within a bounded schedule."""
    attempt = _retry_attempts.get(receipt_id, 0)
    if attempt >= len(_RETRY_BACKOFFS_S):
        _retry_attempts.pop(receipt_id, None)
        return
    existing = _retry_tasks.get(receipt_id)
    if existing is not None and not existing.done():
        _retry_attempts[receipt_id] = attempt + 1
        return
    _retry_attempts[receipt_id] = attempt + 1
    delay = _RETRY_BACKOFFS_S[attempt]

    task: asyncio.Task[None] | None = None

    async def _retry() -> None:
        await asyncio.sleep(delay)
        # A failure inside ``_resume_one`` calls ``_schedule_retry`` again,
        # which would find this still-running task and only bump the counter.
        # Dropping the entry first lets it schedule the next bounded attempt.
        if task is not None and _retry_tasks.get(receipt_id) is task:
            _retry_tasks.pop(receipt_id, None)
        await _resume_one(
            receipt_id,
            conversation_store=conversation_store,
            runner_router=runner_router,
            peer_message_store=peer_message_store,
            archive_cleanup=archive_cleanup,
        )

    task = asyncio.create_task(_retry(), name=f"succession-retry:{receipt_id}")
    _retry_tasks[receipt_id] = task

    def _clear(done: asyncio.Task[None]) -> None:
        if _retry_tasks.get(receipt_id) is done:
            _retry_tasks.pop(receipt_id, None)

    task.add_done_callback(_clear)
