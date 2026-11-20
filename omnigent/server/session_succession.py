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
Archived successors and offline successor runners park at their current
phase until a later external trigger; repeated scans do not log failures.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4
from weakref import WeakValueDictionary

import httpx
from sqlalchemy.exc import IntegrityError

from omnigent.db.utils import generate_task_id, now_epoch
from omnigent.db.workspace_cache import WorkspaceScopedCache
from omnigent.entities import Conversation, ErrorData, MessageData, NewConversationItem
from omnigent.entities.session_resources import terminal_resource_id
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.mcp_selection import MCP_SELECTION_PREFIX
from omnigent.native._native_session_labels import replacement_session_labels
from omnigent.native.native_terminal import url_component
from omnigent.runner.routing import RunnerRouter
from omnigent.runtime import pending_elicitations, session_stream
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.server.auto_rotation import AUTO_ROTATION_STATE_PREFIX
from omnigent.server.child_cap import CHILD_CAP_LABEL_KEY
from omnigent.server.host_registry import RunnerExitReports
from omnigent.server.routes._sessions.helpers import (
    _get_runner_client,
    _message_text,
    _publish_and_persist_resource_event,
    _publish_child_status_to_parent,
    _publish_external_conversation_item,
    _publish_session_superseded,
    _session_status_from_cache,
    _signal_harness_elicitation_resolved_by_id,
)
from omnigent.server.routes._sessions.orchestration import _post_system_message
from omnigent.server.runner_session_init import runner_archive_states_for_conversation
from omnigent.server.schemas import SessionCreatedEvent
from omnigent.stores.conversation_store import (
    _INSTANCE_SCOPED_LABEL_KEYS,
    HANDOVER_ITEM_LABEL_KEY,
    ROTATE_REQUESTED_LABEL_KEY,
    ROTATION_SUCCESSOR_LABEL_KEY,
    SUCCEEDED_BY_LABEL_KEY,
    SUCCEEDS_LABEL_KEY,
    ConversationStore,
    LabelGroupChange,
    SessionSuccession,
)
from omnigent.stores.file_store import FileStore
from omnigent.stores.peer_message_store import PeerMessageStore
from omnigent.stores.scheduled_task_store import ScheduledTaskStore

_logger = logging.getLogger(__name__)

# States in which a peer message has not reached its receiver yet. A record
# being delivered inline (``delivering``) or already delivered stays with the
# receiver it reached — only records that can still be routed move.
_PEER_UNDELIVERED_STATES: tuple[str, ...] = ("pending", "queued", "held")

# Bounded in-process retry for a stalled phase (successor runner not ready,
# runner unreachable, wake not delivered); runner-connect and startup
# triggers remain after it gives up.
_RETRY_BACKOFFS_S: tuple[float, ...] = (2.0, 4.0, 8.0, 16.0, 30.0)

_scheduled_send_locks: WorkspaceScopedCache[str, asyncio.Lock] = WorkspaceScopedCache(
    WeakValueDictionary
)

# One resumer lock per receipt; a phase is never run concurrently in-process.
# custom-lint: disable-next=workspace-scoped-cache -- keyed by server-generated uuid4 receipt id
_resumer_locks: dict[str, asyncio.Lock] = {}
# custom-lint: disable-next=workspace-scoped-cache -- keyed by server-generated uuid4 receipt id
_retry_attempts: dict[str, int] = {}
# custom-lint: disable-next=workspace-scoped-cache -- keyed by server-generated uuid4 receipt id
_retry_tasks: dict[str, asyncio.Task[None]] = {}

_CLAUDE_TERMINAL = terminal_resource_id("claude", "main")
_CLAUDE_TRANSFER_LABEL = f"{AUTO_ROTATION_STATE_PREFIX}claude_transfer"
_CLAUDE_TRANSFER_RESULT_LABEL = f"{_CLAUDE_TRANSFER_LABEL}_result"
_CLAUDE_TRANSFER_LEASE_S = 15 * 60
_claude_transfer_locks: WorkspaceScopedCache[str, asyncio.Lock] = WorkspaceScopedCache(
    WeakValueDictionary
)
_claude_transfer_retries: WorkspaceScopedCache[str, asyncio.Task[None]] = WorkspaceScopedCache(
    dict
)
_claude_transfer_expirations: WorkspaceScopedCache[str, asyncio.Task[None]] = WorkspaceScopedCache(
    dict
)


def scheduled_send_lock(session_id: str) -> asyncio.Lock:
    """Order continued sends and succession moves on this server process."""
    return _scheduled_send_locks.setdefault(session_id, asyncio.Lock())


def claude_transfer_lock(session_id: str) -> asyncio.Lock:
    """Serialize the old pane transfer with a saved answer's delivery attempt."""
    return _claude_transfer_locks.setdefault(session_id, asyncio.Lock())


def _claude_transfer_intent(conversation: Conversation) -> dict[str, Any] | None:
    if conversation.labels.get(SUCCEEDED_BY_LABEL_KEY):
        return None
    intent = _parse_transfer_label(conversation.labels.get(_CLAUDE_TRANSFER_LABEL, ""))
    return intent if intent is not None and not intent.get("abandoned") else None


def _parse_transfer_label(raw: str) -> dict[str, Any] | None:
    try:
        intent = json.loads(raw) if raw else None
    except ValueError:
        return None
    return intent if isinstance(intent, dict) and "target" in intent and "id" in intent else None


def _transfer_expires_at(intent: dict[str, Any]) -> int:
    # Uncommitted intents from before the lease have no provable time remaining.
    deadline = intent.get("expires_at")
    return deadline if isinstance(deadline, int) else 0


def claude_transfer_expired(conversation: Conversation) -> bool:
    intent = _claude_transfer_intent(conversation)
    return intent is not None and _transfer_expires_at(intent) <= now_epoch()


def claude_transfer_pending(conversation: Conversation) -> bool:
    intent = _claude_transfer_intent(conversation)
    return intent is not None and _transfer_expires_at(intent) > now_epoch()


def claude_transfer_fenced(conversation: Conversation) -> bool:
    """Identify a requested transfer even after its intent has been cleaned up."""
    if claude_transfer_pending(conversation):
        return True
    successor = conversation.labels.get(SUCCEEDED_BY_LABEL_KEY)
    result = _claude_transfer_result(conversation)
    return bool(successor and result and result.get("target") == successor)


def _claude_transfer_result(conversation: Conversation) -> dict[str, Any] | None:
    raw_result = conversation.labels.get(_CLAUDE_TRANSFER_RESULT_LABEL)
    try:
        result = json.loads(raw_result) if raw_result else None
    except ValueError:
        return None
    return result if isinstance(result, dict) else None


def released_succession_target(
    store: ConversationStore,
    session_id: str,
    *,
    require_receipt: bool,
    requested_only: bool = False,
) -> str | None:
    """Resolve a delivery only after every move in its lineage has released."""
    from omnigent.server.routes.sessions.routes_peer import _SUCCESSOR_MAX_HOPS

    visited: set[str] = set()
    for _ in range(_SUCCESSOR_MAX_HOPS + 1):
        if session_id in visited:
            break
        visited.add(session_id)
        conversation = store.get_conversation(session_id)
        if conversation is None or claude_transfer_pending(conversation):
            return None
        successor_id = conversation.labels.get(SUCCEEDED_BY_LABEL_KEY)
        if not successor_id:
            return session_id
        if requested_only and not claude_transfer_fenced(conversation):
            if require_receipt and len(visited) == 1:
                return session_id  # Ordinary saved answers keep their existing recipient.
            session_id = successor_id
            continue
        receipt = store.get_succession(session_id, successor_id)
        if require_receipt:
            if receipt is None or receipt.phase not in {
                "released",
                "cards_closed",
                "archived",
                "done",
            }:
                return None
        elif receipt is not None and receipt.phase in {"moved", "rekeyed", "opened"}:
            return None
        session_id = successor_id
    return None


def copy_succession_file(file_store: FileStore, file_id: str, old_id: str, new_id: str) -> str:
    """Give the successor a session-scoped reference to an attachment from the source."""
    source = file_store.get(file_id, session_id=old_id)
    if source is None:
        raise OmnigentError(
            "Attachment not found in the previous session", code=ErrorCode.NOT_FOUND
        )
    new_file_id = hashlib.sha256(f"succession-file:{file_id}:{new_id}".encode()).hexdigest()[:32]
    if file_store.get(new_file_id, session_id=new_id) is None:
        try:
            file_store.create(
                filename=source.filename,
                bytes=source.bytes,
                content_type=source.content_type,
                session_id=new_id,
                file_id=new_file_id,
                blob_key=source.blob_key or source.id,
                source_metadata=source.source_metadata,
            )
        except IntegrityError:
            if file_store.get(new_file_id, session_id=new_id) is None:
                raise
    return new_file_id


async def commit_succession_move(
    store: ConversationStore,
    old_id: str,
    new_id: str,
    receipt_id: str,
    *,
    reverse_of: bool,
    allow_empty: bool,
) -> None:
    async def move() -> None:
        async with AsyncExitStack() as locks:
            # An undo can also change the target's forward link.
            for session_id in sorted({old_id, new_id}):
                await locks.enter_async_context(scheduled_send_lock(session_id))
            await asyncio.to_thread(
                store.reassign_live_children,
                old_id,
                new_id,
                receipt_id,
                reverse_of=reverse_of,
                allow_empty=allow_empty,
            )

    # Cancelling the HTTP waiter cannot stop a move already running in a thread.
    await asyncio.shield(move())


def is_requested_claude_transfer(
    old: Conversation, new: Conversation, terminal_id: str = _CLAUDE_TERMINAL
) -> bool:
    """Only the server-prepared, explicitly requested Claude rotation commits on transfer."""
    if terminal_id != _CLAUDE_TERMINAL or new.labels.get(SUCCEEDS_LABEL_KEY) != old.id:
        return False
    if old.labels.get(SUCCEEDED_BY_LABEL_KEY) == new.id:
        return claude_transfer_fenced(old)
    requested_revision = old.labels.get(ROTATE_REQUESTED_LABEL_KEY)
    if requested_revision != str(old.archive_revision):
        return False
    try:
        prepared = json.loads(old.labels.get(ROTATION_SUCCESSOR_LABEL_KEY, "null"))
    except ValueError:
        return False
    return (
        isinstance(prepared, dict)
        and prepared.get("id") == new.id
        and str(prepared.get("revision")) == requested_revision
    )


def _terminal_path(session_id: str) -> str:
    return f"/v1/sessions/{url_component(session_id)}/resources/terminals/{_CLAUDE_TERMINAL}"


def _terminal_instance(payload: dict[str, Any]) -> str | None:
    metadata = payload.get("metadata")
    if isinstance(metadata, dict) and metadata.get("running") is True:
        instance = metadata.get("instance_id")
        if isinstance(instance, str) and instance:
            return instance
    return None


async def _live_terminal(client: httpx.AsyncClient, session_id: str) -> dict[str, Any]:
    response = await client.get(_terminal_path(session_id), timeout=10.0)
    if response.status_code == 404:
        return {}
    response.raise_for_status()
    return response.json()


async def _clear_transfer_intent(
    store: ConversationStore, old_id: str, intent: str, *, discard_result: bool = False
) -> None:
    def clear(labels: dict[str, str]) -> LabelGroupChange:
        if labels.get(_CLAUDE_TRANSFER_LABEL) != intent:
            return LabelGroupChange(upserts={}, deletes=set())
        deletes = {_CLAUDE_TRANSFER_LABEL}
        if discard_result:
            deletes.add(_CLAUDE_TRANSFER_RESULT_LABEL)
        return LabelGroupChange(upserts={}, deletes=deletes)

    labels = await asyncio.to_thread(
        store.mutate_label_group, old_id, _CLAUDE_TRANSFER_LABEL, clear
    )
    if _CLAUDE_TRANSFER_LABEL not in labels:
        _stop_transfer_expiration(old_id)


async def expire_claude_transfer(store: ConversationStore, old: Conversation) -> None:
    """Persist an expired lease while holding the source transfer lock."""
    intent = _claude_transfer_intent(old)
    if intent is None or _transfer_expires_at(intent) > now_epoch():
        return
    raw_intent = old.labels[_CLAUDE_TRANSFER_LABEL]
    target = intent["target"]
    notice_id = hashlib.sha256(f"transfer-expired:{old.id}:{intent['id']}".encode()).hexdigest()[
        :32
    ]
    notice = NewConversationItem(
        type="error",
        response_id=generate_task_id(),
        data=ErrorData(
            source="execution",
            code="session_succession_expired",
            message=(
                f"The handover to session {target} was not confirmed in time. "
                "This session continues. The successor session was left as is."
            ),
            level="info",
        ),
        stable_id=notice_id,
    )
    abandoned = json.dumps({**intent, "abandoned": True})

    def abandon(labels: dict[str, str]) -> LabelGroupChange:
        if labels.get(_CLAUDE_TRANSFER_LABEL) != raw_intent:
            return LabelGroupChange(upserts={}, deletes=set())
        return LabelGroupChange(
            upserts={_CLAUDE_TRANSFER_LABEL: abandoned},
            deletes={_CLAUDE_TRANSFER_RESULT_LABEL},
        )

    try:
        # A restart between these writes deduplicates the notice and finishes
        # abandoning the lease. The deadline alone controls input admission.
        persisted = await asyncio.to_thread(store.append, old.id, [notice])
        if persisted and not persisted[0].deduplicated:
            _publish_external_conversation_item(old.id, persisted[0])
        labels = await asyncio.to_thread(
            store.mutate_label_group, old.id, _CLAUDE_TRANSFER_LABEL, abandon
        )
    except Exception:
        _logger.exception("Could not record expired terminal handover for %s", old.id)
        return
    if labels.get(_CLAUDE_TRANSFER_LABEL) == abandoned:
        _stop_transfer_expiration(old.id)


def _stop_transfer_expiration(old_id: str) -> None:
    task = _claude_transfer_expirations.get(old_id)
    if task is not None and task is not asyncio.current_task():
        _claude_transfer_expirations.pop(old_id, None)
        task.cancel()


def _schedule_transfer_expiration(store: ConversationStore, old_id: str) -> None:
    existing = _claude_transfer_expirations.get(old_id)
    if existing is not None and not existing.done():
        return

    async def expire() -> None:
        while True:
            async with claude_transfer_lock(old_id):
                old = await asyncio.to_thread(store.get_conversation, old_id)
                intent = _claude_transfer_intent(old) if old is not None else None
                if old is None or intent is None:
                    return
                remaining = _transfer_expires_at(intent) - now_epoch()
                if remaining <= 0:
                    await expire_claude_transfer(store, old)
                    return
            await asyncio.sleep(remaining)

    task = asyncio.create_task(expire(), name=f"claude-transfer-expiry:{old_id}")
    _claude_transfer_expirations[old_id] = task

    def forget(done: asyncio.Task[None]) -> None:
        if _claude_transfer_expirations.get(old_id) is done:
            _claude_transfer_expirations.pop(old_id, None)
        if not done.cancelled() and done.exception() is not None:
            _logger.error("Could not expire transfer for %s", old_id, exc_info=done.exception())

    task.add_done_callback(forget)


def _live_claude_successor(old: Conversation, new: Conversation | None) -> bool:
    return bool(
        new is not None
        and not new.archived
        and old.labels.get(SUCCEEDED_BY_LABEL_KEY) in (None, new.id)
        and is_requested_claude_transfer(old, new)
    )


def _can_start_claude_transfer(
    old: Conversation, new: Conversation | None, runner_id: str | None
) -> bool:
    return bool(
        runner_id
        and not old.archived
        and new is not None
        and old.runner_id == new.runner_id == runner_id
        and _live_claude_successor(old, new)
    )


async def _move_claude_instance(
    client: httpx.AsyncClient, old_id: str, new_id: str, instance: str
) -> tuple[int, dict[str, Any]]:
    payload = await _live_terminal(client, new_id)
    if _terminal_instance(payload) == instance:
        return 200, payload
    try:
        response = await client.post(
            f"{_terminal_path(old_id)}/transfer",
            json={"target_session_id": new_id, "expected_instance_id": instance},
            timeout=30.0,
        )
    except (httpx.HTTPError, ConnectionError):
        payload = await _live_terminal(client, new_id)
        if _terminal_instance(payload) != instance:
            raise
        return 200, payload
    if response.is_error:
        received = await _live_terminal(client, new_id)
        if _terminal_instance(received) == instance:
            return 200, received
        if response.status_code in (404, 409):
            return response.status_code, response.json()
        raise ConnectionError("Runner did not settle the Claude terminal transfer")
    payload = response.json()
    if _terminal_instance(payload) != instance:
        raise ConnectionError("Runner did not confirm the prepared Claude terminal instance")
    return 200, payload


async def _commit_claude_transfer(
    store: ConversationStore, old_id: str, new_id: str
) -> SessionSuccession:
    receipt = await asyncio.to_thread(store.get_succession, old_id, new_id)
    if receipt is None:
        # A prepared successor already opts into retiring an empty source.
        await commit_succession_move(
            store, old_id, new_id, uuid4().hex, reverse_of=False, allow_empty=True
        )
        receipt = await asyncio.to_thread(store.get_succession, old_id, new_id)
    if receipt is None:
        raise RuntimeError("Transferred Claude terminal has no succession receipt")
    return receipt


def _claude_runner_exited(reports: RunnerExitReports | None, runner_id: str | None) -> bool:
    return reports is not None and runner_id is not None and reports.get(runner_id) is not None


async def transfer_requested_claude_terminal(
    old_id: str,
    new_id: str,
    *,
    conversation_store: ConversationStore,
    runner_router: RunnerRouter | None,
    peer_message_store: PeerMessageStore | None,
    scheduled_task_store: ScheduledTaskStore | None,
    archive_cleanup: Any,
    runner_exit_reports: RunnerExitReports | None = None,
    recover_intent_id: str | None = None,
) -> tuple[int, dict[str, Any]]:
    """Transfer the pane and commit the move before acknowledging the transfer."""
    store = conversation_store

    def drive(receipt: SessionSuccession) -> None:
        if receipt.phase not in {"done", "void"}:
            existing = _retry_tasks.get(receipt.id)
            if existing is not None and not existing.done():
                return
            if _retry_attempts.get(receipt.id, 0) >= len(_RETRY_BACKOFFS_S):
                _retry_attempts.pop(receipt.id, None)
            _schedule_retry(
                receipt.id,
                conversation_store=store,
                runner_router=runner_router,
                peer_message_store=peer_message_store,
                scheduled_task_store=scheduled_task_store,
                archive_cleanup=archive_cleanup,
            )

    async def recover(intent_id: str) -> None:
        for delay in _RETRY_BACKOFFS_S:
            await asyncio.sleep(delay)
            try:
                await transfer_requested_claude_terminal(
                    old_id,
                    new_id,
                    conversation_store=store,
                    runner_router=runner_router,
                    peer_message_store=peer_message_store,
                    scheduled_task_store=scheduled_task_store,
                    archive_cleanup=archive_cleanup,
                    runner_exit_reports=runner_exit_reports,
                    recover_intent_id=intent_id,
                )
            except (httpx.HTTPError, ConnectionError):
                continue
            except Exception:
                _logger.exception("Could not resume Claude transfer for %s", old_id)
                continue
            return

    def schedule_recovery(intent_id: str) -> None:
        existing = _claude_transfer_retries.get(old_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(recover(intent_id), name=f"claude-transfer-retry:{old_id}")
        _claude_transfer_retries[old_id] = task

        def forget(done: asyncio.Task[None]) -> None:
            if _claude_transfer_retries.get(old_id) is done:
                _claude_transfer_retries.pop(old_id, None)

        task.add_done_callback(forget)

    async with claude_transfer_lock(old_id):
        old, new = await asyncio.gather(
            asyncio.to_thread(store.get_conversation, old_id),
            asyncio.to_thread(store.get_conversation, new_id),
        )
        if old is None:
            if recover_intent_id is not None:
                return 409, {}
            raise OmnigentError("Claude rotation changed before transfer", code=ErrorCode.CONFLICT)
        intent = old.labels.get(_CLAUDE_TRANSFER_LABEL, "")
        transfer: dict[str, Any] | None = _claude_transfer_intent(old)
        if recover_intent_id is not None and (
            transfer is None or transfer.get("id") != recover_intent_id
        ):
            return 409, {}
        if not intent and (new is None or not is_requested_claude_transfer(old, new)):
            raise OmnigentError("Claude rotation changed before transfer", code=ErrorCode.CONFLICT)
        receipt = await asyncio.to_thread(store.get_succession, old_id, new_id)
        if receipt is not None and old.labels.get(SUCCEEDED_BY_LABEL_KEY) == new_id:
            drive(receipt)
            if intent:
                await _clear_transfer_intent(store, old_id, intent)
            saved_result = _claude_transfer_result(old)
            if saved_result is None or saved_result.get("target") != new_id:
                raise RuntimeError("Claude transfer receipt has no terminal result")
            return 200, saved_result["terminal"]

        placement_conflict: dict[str, Any] = {
            "error": {"message": "The terminal handover could not be confirmed"}
        }
        if claude_transfer_expired(old):
            await expire_claude_transfer(store, old)
            return 409, placement_conflict
        abandoned = _parse_transfer_label(intent) if transfer is None else None
        if abandoned is not None and abandoned.get("abandoned") and abandoned["target"] == new_id:
            return 409, placement_conflict
        try:
            if transfer is not None and transfer["target"] != new_id:
                raise OmnigentError(
                    "A different Claude transfer is pending", code=ErrorCode.CONFLICT
                )
            runner_id = transfer.get("runner_id") if transfer is not None else old.runner_id
            if _claude_runner_exited(runner_exit_reports, runner_id):
                if transfer is not None:
                    await _clear_transfer_intent(store, old_id, intent, discard_result=True)
                return 409, placement_conflict
            if transfer is not None and not runner_id:
                raise ConnectionError("The original Claude transfer runner is unknown")
            can_start = _can_start_claude_transfer(old, new, runner_id)
            if transfer is None and not can_start:
                return 409, placement_conflict
            client = await _get_runner_client(
                old_id, runner_router, conversation=replace(old, runner_id=runner_id)
            )
            if client is None:
                raise ConnectionError("The original Claude transfer runner is not connected")
            if transfer is None:
                instance = _terminal_instance(await _live_terminal(client, old_id))
                if instance is None:
                    return 404, {}
                transfer = {
                    "target": new_id,
                    "instance": instance,
                    "runner_id": runner_id,
                    "id": uuid4().hex,
                    "expires_at": now_epoch() + _CLAUDE_TRANSFER_LEASE_S,
                }
                encoded = json.dumps(transfer)
                if intent:
                    written = await asyncio.to_thread(
                        store.compare_and_set_label,
                        old_id,
                        _CLAUDE_TRANSFER_LABEL,
                        intent,
                        encoded,
                    )
                else:
                    written = encoded == await asyncio.to_thread(
                        store.insert_label_if_absent, old_id, _CLAUDE_TRANSFER_LABEL, encoded
                    )
                if not written:
                    return 409, placement_conflict
                intent = encoded
            _schedule_transfer_expiration(store, old_id)

            instance = transfer["instance"]
            if can_start:
                status, payload = await _move_claude_instance(client, old_id, new_id, instance)
            else:
                # A changed binding prevents starting a move, but the physical
                # pane may already belong to the prepared successor.
                _, payload = await asyncio.gather(
                    _live_terminal(client, old_id), _live_terminal(client, new_id)
                )
                status, payload = (
                    (200, payload)
                    if _terminal_instance(payload) == instance
                    else (409, placement_conflict)
                )
            current_old, current_new = await asyncio.gather(
                asyncio.to_thread(store.get_conversation, old_id),
                asyncio.to_thread(store.get_conversation, new_id),
            )
            if current_old is None or current_old.labels.get(_CLAUDE_TRANSFER_LABEL) != intent:
                return 409, placement_conflict
            if claude_transfer_expired(current_old):
                await expire_claude_transfer(store, current_old)
                return 409, placement_conflict
            if status != 200:
                await _clear_transfer_intent(store, old_id, intent, discard_result=True)
                return status, payload
            if not _live_claude_successor(current_old, current_new):
                return 409, placement_conflict
            if _claude_transfer_result(current_old) is None:
                # Confirming this pane on the original runner is progress once.
                renewed = json.dumps(
                    {**transfer, "expires_at": now_epoch() + _CLAUDE_TRANSFER_LEASE_S}
                )
                if not await asyncio.to_thread(
                    store.compare_and_set_label,
                    old_id,
                    _CLAUDE_TRANSFER_LABEL,
                    intent,
                    renewed,
                    updates={
                        _CLAUDE_TRANSFER_RESULT_LABEL: json.dumps(
                            {"target": new_id, "terminal": payload}
                        )
                    },
                ):
                    return 409, placement_conflict
                intent = renewed
            receipt = await _commit_claude_transfer(store, old_id, new_id)
        except Exception:
            if intent and transfer is not None:
                schedule_recovery(transfer["id"])
            raise
        drive(receipt)
        _publish_and_persist_resource_event(
            old_id, "session.resource.deleted", _CLAUDE_TERMINAL, "terminal", store
        )
        _publish_and_persist_resource_event(
            new_id, "session.resource.created", payload["id"], "terminal", store, payload
        )
        await _clear_transfer_intent(store, old_id, intent)
        return 200, payload


async def recover_claude_transfer_on_input(
    conversation: Conversation,
    *,
    conversation_store: ConversationStore,
    runner_router: RunnerRouter | None,
    peer_message_store: PeerMessageStore | None,
    scheduled_task_store: ScheduledTaskStore | None,
    archive_cleanup: Any,
    runner_exit_reports: RunnerExitReports | None = None,
) -> None:
    """Give a stalled transfer another driver when input reaches the old session."""
    old_id = conversation.id
    transfer = _claude_transfer_intent(conversation)
    if transfer is None:
        return
    retry = _claude_transfer_retries.get(old_id)
    runner_id = transfer.get("runner_id")
    exited = _claude_runner_exited(runner_exit_reports, runner_id)
    if not exited and (
        (retry is not None and not retry.done()) or claude_transfer_lock(old_id).locked()
    ):
        return
    # An unconfirmed attempt leaves the transfer fenced and retryable.
    with suppress(httpx.HTTPError, ConnectionError):
        await asyncio.shield(
            transfer_requested_claude_terminal(
                old_id,
                transfer["target"],
                conversation_store=conversation_store,
                runner_router=runner_router,
                peer_message_store=peer_message_store,
                scheduled_task_store=scheduled_task_store,
                archive_cleanup=archive_cleanup,
                runner_exit_reports=runner_exit_reports,
                recover_intent_id=transfer["id"],
            )
        )


async def _recover_claude_transfers(
    store: ConversationStore,
    runner_router: RunnerRouter | None,
    peer_message_store: PeerMessageStore | None,
    scheduled_task_store: ScheduledTaskStore | None,
    archive_cleanup: Any,
    runner_id: str | None = None,
    runner_exit_reports: RunnerExitReports | None = None,
) -> None:
    intents = await asyncio.to_thread(store.list_label_values, _CLAUDE_TRANSFER_LABEL)
    rebound_sessions: set[str] | None = None
    for old_id, intent in intents.items():
        try:
            transfer = _parse_transfer_label(intent)
            if transfer is None or transfer.get("abandoned"):
                continue
            _schedule_transfer_expiration(store, old_id)
            new_id = transfer["target"]
            original_runner = transfer.get("runner_id")
            if (
                runner_id is not None
                and original_runner is not None
                and runner_id != original_runner
                and not _claude_runner_exited(runner_exit_reports, original_runner)
            ):
                if rebound_sessions is None:
                    bound = await asyncio.to_thread(
                        store.list_conversations_by_runner_id, runner_id
                    )
                    rebound_sessions = {session.id for session in bound}
                if old_id not in rebound_sessions and new_id not in rebound_sessions:
                    continue
            await transfer_requested_claude_terminal(
                old_id,
                new_id,
                conversation_store=store,
                runner_router=runner_router,
                peer_message_store=peer_message_store,
                scheduled_task_store=scheduled_task_store,
                archive_cleanup=archive_cleanup,
                runner_exit_reports=runner_exit_reports,
                recover_intent_id=transfer["id"],
            )
        except (ConnectionError, httpx.HTTPError):
            continue  # The runner-connect hook will try again when the tunnel is reachable.
        except Exception:
            _logger.exception("Could not reconcile Claude rotation transfer for %s", old_id)


class _SuccessionParked(RuntimeError):
    """A lifecycle prerequisite needs an external change before resuming."""


@dataclass
class _RunnerGroup:
    """One runner's client plus the succession sessions it holds."""

    client: httpx.AsyncClient | None
    child_only: bool = False
    session_ids: list[str] = field(default_factory=list)


async def advance_succession(
    receipt_id: str,
    *,
    conversation_store: ConversationStore,
    runner_router: RunnerRouter | None,
    peer_message_store: PeerMessageStore | None,
    scheduled_task_store: ScheduledTaskStore | None = None,
    archive_cleanup: Any = None,
) -> SessionSuccession:
    """
    Run one receipt through every phase it can reach, then stop.

    Each phase commits its result with a phase compare-and-set before the
    next starts. A phase that stops early (runner not ready, transport
    failure) leaves the receipt where it was and records the error; the next
    trigger resumes there. Archived or offline successors park without an
    in-process retry. The returned receipt is the freshest row read.

    :param receipt_id: Receipt row id, e.g. ``"3f2a...hex"``.
    :param conversation_store: Store owning the receipt and conversations.
    :param runner_router: Router resolving each session's runner client.
    :param peer_message_store: Durable peer-message store, or ``None`` when
        peer messaging is off.
    :param scheduled_task_store: Store for continuing automations, when configured.
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
                if phase == "void":
                    return receipt
                receipt = await _retire_stale(receipt, conversation_store)
                if receipt.phase == "void":
                    return receipt
                if phase == "done":
                    return await _replay_deferred(
                        receipt,
                        conversation_store=conversation_store,
                        runner_router=runner_router,
                        peer_message_store=peer_message_store,
                        archive_cleanup=archive_cleanup,
                    )
                if phase in {"moved", "rekeyed", "opened"}:
                    successor = await asyncio.to_thread(
                        conversation_store.get_conversation, receipt.new_id
                    )
                    if successor is not None and successor.archived:
                        raise _SuccessionParked(f"successor {receipt.new_id!r} is archived")
                if phase == "moved":
                    receipt = await _phase_rekey(
                        receipt,
                        conversation_store=conversation_store,
                        runner_router=runner_router,
                        peer_message_store=peer_message_store,
                        scheduled_task_store=scheduled_task_store,
                        archive_cleanup=archive_cleanup,
                    )
                elif phase == "rekeyed":
                    receipt = await _phase_open(
                        receipt,
                        conversation_store=conversation_store,
                        runner_router=runner_router,
                        peer_message_store=peer_message_store,
                        scheduled_task_store=scheduled_task_store,
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
                        scheduled_task_store=scheduled_task_store,
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
            except _SuccessionParked as exc:
                if phase == "rekeyed" and receipt.questions is None:
                    receipt = await _advance(
                        receipt,
                        phase,
                        conversation_store=conversation_store,
                        questions=_open_questions(receipt.old_id),
                        error=receipt.error,
                    )
                if receipt.error == f"{type(exc).__name__}: {exc}"[:4000]:
                    return receipt
                _logger.info(
                    "Succession %s parked in phase %s: %s",
                    receipt.id,
                    phase,
                    exc,
                    extra={"session_id": receipt.old_id},
                )
                return await _record_phase_error(receipt, exc, conversation_store)
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
                    scheduled_task_store=scheduled_task_store,
                    archive_cleanup=archive_cleanup,
                )
                return await _record_phase_error(receipt, exc, conversation_store)
            if receipt.phase == phase:
                return receipt
            _retry_attempts.pop(receipt.id, None)


async def resume_unfinished_successions(
    *,
    conversation_store: ConversationStore,
    runner_router: RunnerRouter | None,
    peer_message_store: PeerMessageStore | None,
    scheduled_task_store: ScheduledTaskStore | None = None,
    archive_cleanup: Any = None,
    runner_exit_reports: RunnerExitReports | None = None,
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
        await _recover_claude_transfers(
            conversation_store,
            runner_router,
            peer_message_store,
            scheduled_task_store,
            archive_cleanup,
            runner_exit_reports=runner_exit_reports,
        )
        receipts = await _resumable_receipts(conversation_store)
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
            scheduled_task_store=scheduled_task_store,
            archive_cleanup=archive_cleanup,
        )


async def resume_successions_for_runner(
    runner_id: str,
    *,
    conversation_store: ConversationStore,
    runner_router: RunnerRouter | None,
    peer_message_store: PeerMessageStore | None,
    scheduled_task_store: ScheduledTaskStore | None = None,
    archive_cleanup: Any = None,
    runner_exit_reports: RunnerExitReports | None = None,
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
        await _recover_claude_transfers(
            conversation_store,
            runner_router,
            peer_message_store,
            scheduled_task_store,
            archive_cleanup,
            runner_id=runner_id,
            runner_exit_reports=runner_exit_reports,
        )
        if _claude_runner_exited(runner_exit_reports, runner_id):
            return  # A process-exit wake reconciles intents, without driving its receipts.
        receipts = await _resumable_receipts(conversation_store)
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
        deferred_ids = {entry["runner_id"] for entry in receipt.deferred_runners or []}
        if (
            successor is None or successor.runner_id != runner_id
        ) and runner_id not in deferred_ids:
            continue
        await _resume_one(
            receipt.id,
            conversation_store=conversation_store,
            runner_router=runner_router,
            peer_message_store=peer_message_store,
            scheduled_task_store=scheduled_task_store,
            archive_cleanup=archive_cleanup,
        )


async def _resumable_receipts(store: ConversationStore) -> list[SessionSuccession]:
    unfinished = await asyncio.to_thread(store.list_unfinished_successions)
    deferred = await asyncio.to_thread(store.list_deferred_successions)
    return list({receipt.id: receipt for receipt in [*unfinished, *deferred]}.values())


async def _retire_stale(receipt: SessionSuccession, store: ConversationStore) -> SessionSuccession:
    old = await asyncio.to_thread(store.get_conversation, receipt.old_id)
    successor_id = old.labels.get(SUCCEEDED_BY_LABEL_KEY) if old is not None else None
    if successor_id == receipt.new_id:
        return receipt
    return await _advance(
        receipt,
        "void",
        conversation_store=store,
        deferred_runners=None,
        error=f"old session succeeded_by is {successor_id!r}, expected {receipt.new_id!r}",
    )


async def _resume_one(
    receipt_id: str,
    *,
    conversation_store: ConversationStore,
    runner_router: RunnerRouter | None,
    peer_message_store: PeerMessageStore | None,
    scheduled_task_store: ScheduledTaskStore | None = None,
    archive_cleanup: Any = None,
) -> None:
    """Resume one receipt, logging instead of raising."""
    try:
        await advance_succession(
            receipt_id,
            conversation_store=conversation_store,
            runner_router=runner_router,
            peer_message_store=peer_message_store,
            scheduled_task_store=scheduled_task_store,
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
    scheduled_task_store: ScheduledTaskStore | None,
    archive_cleanup: Any,
) -> SessionSuccession:
    """``moved → rekeyed``: hand each holder of moved state to the successor."""
    groups = await _runner_groups(receipt, conversation_store, runner_router)
    # Every runner gets the full moved set: the successor's runner also holds
    # work entries for children hosted on the other runners and must rewrite
    # them all, while each runner applies only the archive states it owns.
    archive_states = await _archive_states(receipt.moved_ids, conversation_store)
    dropped: list[dict[str, Any]] = list(receipt.dropped or [])
    deferred = {entry["runner_id"]: entry for entry in receipt.deferred_runners or []}
    for runner_id, group in groups.items():
        if group.child_only and group.client is None:
            deferred[runner_id] = {"runner_id": runner_id, "session_ids": group.session_ids}
            receipt = await _advance(
                receipt,
                "moved",
                conversation_store=conversation_store,
                deferred_runners=list(deferred.values()),
            )
            continue
        if group.client is None:
            raise RuntimeError(f"runner {runner_id!r} offline during re-key")
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
            if group.child_only:
                deferred[runner_id] = {"runner_id": runner_id, "session_ids": group.session_ids}
                receipt = await _advance(
                    receipt,
                    "moved",
                    conversation_store=conversation_store,
                    deferred_runners=list(deferred.values()),
                )
                continue
            raise RuntimeError(f"runner unreachable during re-key: {exc}") from exc
        if response.status_code == 409:
            deferred.pop(runner_id, None)
            receipt = await _advance(
                receipt,
                "moved",
                conversation_store=conversation_store,
                deferred_runners=list(deferred.values()) or None,
            )
            _schedule_retry(
                receipt.id,
                conversation_store=conversation_store,
                runner_router=runner_router,
                peer_message_store=peer_message_store,
                scheduled_task_store=scheduled_task_store,
                archive_cleanup=archive_cleanup,
            )
            _logger.info(
                "Succession %s: successor not ready; retrying later",
                receipt.id,
                extra={"session_id": receipt.old_id},
            )
            return receipt
        if response.status_code >= 500 and group.child_only:
            deferred[runner_id] = {"runner_id": runner_id, "session_ids": group.session_ids}
            receipt = await _advance(
                receipt,
                "moved",
                conversation_store=conversation_store,
                deferred_runners=list(deferred.values()),
            )
            continue
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
        deferred.pop(runner_id, None)
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
        deferred_runners=list(deferred.values()) or None,
    )


async def _phase_open(
    receipt: SessionSuccession,
    *,
    conversation_store: ConversationStore,
    runner_router: RunnerRouter | None,
    peer_message_store: PeerMessageStore | None,
    scheduled_task_store: ScheduledTaskStore | None,
) -> SessionSuccession:
    """``rekeyed → opened``: snapshot questions, retarget peers and tasks, post the opening."""
    successor = await asyncio.to_thread(conversation_store.get_conversation, receipt.new_id)
    if successor is not None and successor.runner_id is not None:
        client = await _get_runner_client(receipt.new_id, runner_router, conversation=successor)
        if client is None:
            raise _SuccessionParked(f"successor {receipt.new_id!r} runner is offline")
    old_conversation = await asyncio.to_thread(conversation_store.get_conversation, receipt.old_id)
    if old_conversation is None:
        raise RuntimeError(f"session {receipt.old_id!r} vanished during succession")

    questions = (
        receipt.questions if receipt.questions is not None else _open_questions(receipt.old_id)
    )
    if questions and claude_transfer_fenced(old_conversation):
        saved_answers = {
            card.elicitation_id
            for card in await asyncio.to_thread(conversation_store.list_detached_cards)
            if card.session_id == receipt.old_id and card.state in {"answered", "settled"}
        }
        # Requested Claude transfers send saved answers to the successor.
        # Ordinary handovers still rely on re-asking the carried questions.
        opening_questions = [q for q in questions if q.get("elicitation_id") not in saved_answers]
    else:
        opening_questions = questions
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
    if successor is not None:
        configuration_keys = replacement_session_labels(old_conversation.labels).keys() | {
            key for key in old_conversation.labels if key.startswith(MCP_SELECTION_PREFIX)
        }
        runtime_keys = (_INSTANCE_SCOPED_LABEL_KEYS - {CHILD_CAP_LABEL_KEY}) | {
            "omnigent.pi.bridge_id",
            "omnigent.pi_native.bridge_id",
            "omnigent.keep_warm",
            "omnigent.keep_warm_stats",
            "omnigent.pi_cache",
            "omnigent.last_cache",
            "omnigent.running_since",
            "omnigent.last_real_turn_settled",
        }
        for key in sorted(configuration_keys - successor.labels.keys() - runtime_keys):
            item = {"kind": "configuration label", "id": key}
            if item not in dropped:
                dropped.append(item)
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
    if scheduled_task_store is not None:
        await asyncio.to_thread(
            _retarget_scheduled_tasks, receipt, conversation_store, scheduled_task_store
        )

    handover = await _handover_text(conversation_store, old_conversation)
    children = await _direct_children(receipt, conversation_store)
    child_states: dict[str, str] = {}
    for child in children:
        client = (
            await _get_runner_client(child.id, runner_router, conversation=child)
            if child.runner_id is not None
            else None
        )
        child_states[child.id] = (
            _session_status_from_cache(child.id, child.live_status)
            if client is not None
            else "runner offline — send a message to restart it"
        )
    text = _opening_text(
        old_id=receipt.old_id,
        handover=handover,
        children=children,
        child_states=child_states,
        questions=opening_questions,
        dropped=dropped,
        created_at=receipt.created_at,
    )
    payload: dict[str, Any] = {
        "text": text,
        "waking": bool(handover) or bool(opening_questions),
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
    deferred = {entry["runner_id"]: entry for entry in receipt.deferred_runners or []}
    for runner_id, group in groups.items():
        if runner_id in deferred:
            continue
        holds_successor = receipt.new_id in group.session_ids
        if group.client is None:
            if group.child_only:
                deferred[runner_id] = {"runner_id": runner_id, "session_ids": group.session_ids}
                continue
            raise RuntimeError(f"runner {runner_id!r} offline at release")
        try:
            response = await group.client.post(
                f"/v1/sessions/{receipt.new_id}/succession/release",
                timeout=30.0,
            )
        except (httpx.HTTPError, ConnectionError) as exc:
            if group.child_only:
                deferred[runner_id] = {"runner_id": runner_id, "session_ids": group.session_ids}
                continue
            if holds_successor:
                raise RuntimeError(f"runner unreachable at release: {exc}") from exc
            _logger.warning(
                "Succession %s: best-effort release failed on a moved child's runner",
                receipt.id,
                exc_info=True,
            )
            continue
        if response.status_code >= 400:
            if group.child_only and response.status_code >= 500:
                deferred[runner_id] = {"runner_id": runner_id, "session_ids": group.session_ids}
                continue
            if holds_successor:
                raise RuntimeError(
                    f"runner refused release ({response.status_code}): {response.text[:300]}"
                )
            _logger.warning(
                "Succession %s: best-effort release returned %d on a moved child's runner",
                receipt.id,
                response.status_code,
            )
    return await _advance(
        receipt,
        "released",
        conversation_store=conversation_store,
        deferred_runners=list(deferred.values()) or None,
    )


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


def _retarget_scheduled_tasks(
    receipt: SessionSuccession,
    conversation_store: ConversationStore,
    scheduled_task_store: ScheduledTaskStore,
) -> None:
    from omnigent.server.routes.sessions.routes_peer import resolve_succeeded_by

    # Split databases and receipts created before atomic rebinding can lag the AP move.
    target_id = resolve_succeeded_by(conversation_store, receipt.old_id)
    if target_id == receipt.old_id:
        return
    old_owner = conversation_store.get_session_owner_authority(receipt.old_id, owner_only=True)
    new_owner = conversation_store.get_session_owner_authority(target_id, owner_only=True)
    if old_owner != new_owner:
        return
    user_id = old_owner.user_id if old_owner is not None else None
    scheduled_task_store.retarget_continued_sessions(
        receipt.old_id,
        target_id,
        user_id=None if user_id == RESERVED_USER_LOCAL else user_id,
        generation=old_owner.generation if old_owner is not None else None,
    )


async def _phase_archive(
    receipt: SessionSuccession,
    *,
    conversation_store: ConversationStore,
    scheduled_task_store: ScheduledTaskStore | None = None,
    archive_cleanup: Any = None,
) -> SessionSuccession:
    """``cards_closed → archived``: archive the old session without runner teardown."""
    old_conversation = await asyncio.to_thread(conversation_store.get_conversation, receipt.old_id)
    if old_conversation is not None and not old_conversation.archived:
        if scheduled_task_store is not None:
            # Catch receipts that reached this phase before an upgrade.
            await asyncio.to_thread(
                _retarget_scheduled_tasks, receipt, conversation_store, scheduled_task_store
            )
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
        # Only the session.created SSE per moved child;
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

    Sessions without runners and archived moved sessions have no active
    state to re-key. Only runners whose live bindings are all moved sessions
    can be deferred; a successor's offline runner parks the rotation.

    :param receipt: The succession receipt.
    :param conversation_store: Store holding the conversation rows.
    :param runner_router: Router resolving each session's runner client.
    :returns: ``{runner_id: _RunnerGroup}``.
    :raises _SuccessionParked: When the successor's bound runner has no client.
    :raises RuntimeError: When a moved session's bound runner has no client.
    """
    conversations: list[Conversation] = []
    successor = await asyncio.to_thread(conversation_store.get_conversation, receipt.new_id)
    if successor is not None:
        conversations.append(successor)
    for moved_id in receipt.moved_ids:
        conversation = await asyncio.to_thread(conversation_store.get_conversation, moved_id)
        if conversation is not None and not conversation.archived:
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
        bound = await asyncio.to_thread(
            conversation_store.list_conversations_by_runner_id, conversation.runner_id
        )
        child_only = conversation.runner_id != (
            successor.runner_id if successor else None
        ) and all(row.id in receipt.moved_ids for row in bound if not row.archived)
        if client is None and not child_only:
            message = (
                f"runner {conversation.runner_id!r} is offline for session {conversation.id!r}"
            )
            if conversation.id == receipt.new_id:
                raise _SuccessionParked(message)
            raise RuntimeError(message)
        groups[conversation.runner_id] = _RunnerGroup(
            client=client,
            child_only=child_only,
            session_ids=[conversation.id],
        )
    return groups


async def _archive_states(
    session_ids: list[str],
    store: ConversationStore,
) -> dict[str, list[dict[str, Any]]]:
    archive_states: dict[str, list[dict[str, Any]]] = {}
    for session_id in session_ids:
        conversation = await asyncio.to_thread(store.get_conversation, session_id)
        if conversation is not None:
            states = await runner_archive_states_for_conversation(conversation, store)
            archive_states[session_id] = [state.model_dump(mode="json") for state in states]
    return archive_states


async def _replay_deferred(
    receipt: SessionSuccession,
    *,
    conversation_store: ConversationStore,
    runner_router: RunnerRouter | None,
    peer_message_store: PeerMessageStore | None,
    archive_cleanup: Any,
) -> SessionSuccession:
    """Finish child-runner work without delaying the successor's opening."""
    for entry in list(receipt.deferred_runners or []):
        receipt = await _read(conversation_store, receipt.id) or receipt
        if receipt.phase == "void":
            return receipt
        receipt = await _retire_stale(receipt, conversation_store)
        if receipt.phase == "void":
            return receipt
        runner_id = entry["runner_id"]
        conversations = []
        for session_id in entry["session_ids"]:
            row = await asyncio.to_thread(conversation_store.get_conversation, session_id)
            if row is not None and row.runner_id == runner_id:
                conversations.append(row)
        remaining = [
            item for item in receipt.deferred_runners or [] if item["runner_id"] != runner_id
        ]
        if not conversations:
            receipt = await _advance(
                receipt,
                receipt.phase,
                conversation_store=conversation_store,
                deferred_runners=remaining or None,
            )
            continue
        client = await _get_runner_client(
            conversations[0].id, runner_router, conversation=conversations[0]
        )
        if client is None:
            continue
        session_ids = [row.id for row in conversations]
        try:
            response = await client.post(
                f"/v1/sessions/{receipt.old_id}/succession",
                json={
                    "target_session_id": receipt.new_id,
                    "moved_ids": session_ids,
                    "archive_states": await _archive_states(session_ids, conversation_store),
                },
                timeout=30.0,
            )
            if response.status_code >= 400:
                raise RuntimeError(f"deferred runner re-key returned {response.status_code}")
            try:
                body = response.json()
            except ValueError:
                body = {}
            dropped = list(receipt.dropped or [])
            for item in body.get("dropped") or []:
                if isinstance(item, dict) and item not in dropped:
                    dropped.append(item)
            receipt = await _advance(
                receipt, receipt.phase, conversation_store=conversation_store, dropped=dropped
            )
            # Revalidate after the network call: an undo must not release old work.
            receipt = await _retire_stale(receipt, conversation_store)
            if receipt.phase == "void":
                return receipt
            response = await client.post(
                f"/v1/sessions/{receipt.new_id}/succession/release", timeout=30.0
            )
            if response.status_code >= 400:
                raise RuntimeError(f"deferred runner release returned {response.status_code}")
        except (httpx.HTTPError, ConnectionError, RuntimeError):
            _schedule_retry(
                receipt.id,
                conversation_store=conversation_store,
                runner_router=runner_router,
                peer_message_store=peer_message_store,
                archive_cleanup=archive_cleanup,
            )
            continue
        receipt = await _advance(
            receipt,
            receipt.phase,
            conversation_store=conversation_store,
            deferred_runners=remaining or None,
        )
    return receipt


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
    child_states: dict[str, str],
    questions: list[dict[str, Any]],
    dropped: list[dict[str, Any]],
    created_at: int | None = None,
) -> str:
    """Render the successor's opening message (mechanism, plain English)."""
    sections: list[str] = []
    if handover:
        sections.append(handover)
    if created_at is not None and now_epoch() - created_at > 600:
        rotated_at = datetime.fromtimestamp(created_at, UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        sections.append(f"Delivered late: rotation at {rotated_at}.")
    sections.append(f"Previous session: /c/{old_id}")
    if children:
        lines = []
        lines.append("Children now under this session:")
        for child in children:
            lines.append(f"- {child.id}: {child.title or child.id} ({child_states[child.id]})")
        sections.append("\n".join(lines))
    if questions:
        lines = []
        lines.append("Questions the previous session had open (closed there — ask again):")
        for question in questions:
            lines.append(f"- {_question_text(question)}")
        sections.append("\n".join(lines))
    if dropped:
        lines = []
        lines.append("Stopped and not carried over:")
        for item in dropped:
            kind = item.get("kind", "item")
            item_id = item.get("id", "")
            label = item.get("label")
            detail = f"- {kind} {item_id}".rstrip()
            if isinstance(label, str) and label:
                detail += f": {label}"
            lines.append(detail)
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def _schedule_retry(
    receipt_id: str,
    *,
    conversation_store: ConversationStore,
    runner_router: RunnerRouter | None,
    peer_message_store: PeerMessageStore | None,
    scheduled_task_store: ScheduledTaskStore | None = None,
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
            scheduled_task_store=scheduled_task_store,
            archive_cleanup=archive_cleanup,
        )

    task = asyncio.create_task(_retry(), name=f"succession-retry:{receipt_id}")
    _retry_tasks[receipt_id] = task

    def _clear(done: asyncio.Task[None]) -> None:
        if _retry_tasks.get(receipt_id) is done:
            _retry_tasks.pop(receipt_id, None)

    task.add_done_callback(_clear)
