"""Persist one continuation through the existing saved-input delivery lifecycle."""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import NAMESPACE_URL, uuid5

from omnigent.entities import Conversation
from omnigent.entities.conversation import MessageData, NewConversationItem, StartupInputOptions
from omnigent.stores.conversation_store import PENDING_INPUT_RESPONSE_ID, ConversationStore

if TYPE_CHECKING:
    from omnigent.server.schemas import ErrorDetail

RECOVERY_INPUT_LABEL = "omnigent.interrupted_turn.recovery_id"
RECOVERY_DEADLINE_LABEL = "omnigent.interrupted_turn.deadline"
CONTINUE_INTERRUPTED_TURN = (
    "Your runner was interrupted while this task was active. "
    "Continue the existing task from its current state. "
    "Check any interrupted operation's outcome before repeating it."
)


def recovery_input_id(conversation: Conversation) -> str | None:
    value = conversation.labels.get(RECOVERY_INPUT_LABEL, "")
    return value if re.fullmatch(r"[0-9a-f]{32}", value) else None


def recovery_journal(store: ConversationStore):
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
    from omnigent.stores.subagent_retry_store.sqlalchemy_store import SqlAlchemySubagentRetryStore

    return (
        SqlAlchemySubagentRetryStore(store.storage_location, store.conversation_storage_location)
        if isinstance(store, SqlAlchemyConversationStore)
        else None
    )


async def reconcile_turn_recovery(store: ConversationStore, conversation: Conversation) -> bool:
    """An accepted receiver receipt, rather than a sent POST, consumes the saved input."""
    cancelled = conversation.labels.get(RECOVERY_INPUT_LABEL, "").startswith("cancelled:")
    identity = recovery_input_id(conversation)
    if cancelled:
        identity = conversation.labels[RECOVERY_INPUT_LABEL].removeprefix("cancelled:")
    journal = recovery_journal(store)
    if identity is None or journal is None:
        return False
    item = await asyncio.to_thread(store.get_item, conversation.id, identity)
    receipt = await asyncio.to_thread(journal.get_input, identity)
    if receipt is not None and receipt.state == "accepted":
        if item is not None and item.response_id == PENDING_INPUT_RESPONSE_ID:
            from omnigent.server.routes._sessions.orchestration import _publish_input_consumed

            [saved] = await asyncio.to_thread(
                store.append,
                conversation.id,
                [
                    NewConversationItem(
                        type=item.type,
                        response_id=receipt.turn_id or identity,
                        stable_id=identity,
                        replaces_pending_id=identity,
                        data=item.data,
                        created_by=item.created_by,
                    )
                ],
            )
            _publish_input_consumed(conversation.id, saved)
        await asyncio.to_thread(
            store.compare_and_set_label,
            conversation.id,
            RECOVERY_INPUT_LABEL,
            identity,
            f"sent:{identity}",
        )
        return True
    if cancelled:
        if item is not None and item.status in {"in_progress", "incomplete"}:
            await asyncio.to_thread(
                store.transition_pending_input,
                conversation.id,
                identity,
                expected=item.status,
                status="failed",
            )
        return False
    if (
        item is not None
        and item.response_id == PENDING_INPUT_RESPONSE_ID
        and item.status == "incomplete"
    ):
        try:
            deadline = float(conversation.labels.get(RECOVERY_DEADLINE_LABEL, "0"))
        except ValueError:
            deadline = 0
        if deadline <= time.time():
            await asyncio.to_thread(
                store.transition_pending_input,
                conversation.id,
                identity,
                expected="incomplete",
                status="in_progress",
                expected_labels={RECOVERY_INPUT_LABEL: identity},
            )
    return False


async def _recovery_identity(
    store: ConversationStore, conversation: Conversation, turn_id: str | None = None
) -> str:
    from omnigent.server.routes._sessions.common import _session_active_response_cache

    journal = recovery_journal(store)
    control = await asyncio.to_thread(journal.get_control, conversation.id) if journal else None
    turn_id = (
        turn_id
        or (control.turn_id if control else None)
        or _session_active_response_cache.get(conversation.id)
    )
    if turn_id is None:
        history = await asyncio.to_thread(
            store.list_items, conversation.id, limit=100, order="desc"
        )
        latest = next(
            (
                item
                for item in history.data
                if not (
                    isinstance(item.data, MessageData)
                    and item.data.startup_options is not None
                    and item.data.startup_options.recovery_id is not None
                )
            ),
            None,
        )
        turn_id = (latest.response_id or latest.id) if latest is not None else "initial"
    return uuid5(NAMESPACE_URL, f"interrupted-turn:{conversation.id}:{turn_id}").hex


async def recovery_allowed(store: ConversationStore, conversation: Conversation) -> bool:
    from omnigent.server.child_session_recovery import _restorable
    from omnigent.server.startup_inputs import STARTUP_LABEL

    seen: set[str] = set()
    row: Conversation | None = conversation
    while row is not None:
        if row.id in seen or not _restorable(row) or row.labels.get(STARTUP_LABEL) == "cancelled":
            return False
        seen.add(row.id)
        if row.parent_conversation_id is None:
            return True
        row = await asyncio.to_thread(store.get_conversation, row.parent_conversation_id)
    return False


async def cancel_turn_recovery(store: ConversationStore, session_id: str) -> None:
    """A tombstone also fences a failure report still holding the previous snapshot."""
    conversation = await asyncio.to_thread(store.get_conversation, session_id)
    if conversation is None:
        return
    if RECOVERY_INPUT_LABEL not in conversation.labels:
        from omnigent.server.routes._sessions.helpers import _resolve_harness_async

        if await _resolve_harness_async(conversation) != "pi":
            return
    identity = await _recovery_identity(store, conversation)
    await asyncio.to_thread(
        store.set_labels, session_id, {RECOVERY_INPUT_LABEL: f"cancelled:{identity}"}
    )
    conversation.labels[RECOVERY_INPUT_LABEL] = f"cancelled:{identity}"
    for item in await asyncio.to_thread(store.list_pending_inputs, session_id):
        options = item.data.startup_options if isinstance(item.data, MessageData) else None
        if (
            options is not None
            and options.recovery_id is not None
            and item.status in {"in_progress", "incomplete"}
        ):
            await reconcile_turn_recovery(
                store,
                replace(
                    conversation,
                    labels={
                        **conversation.labels,
                        RECOVERY_INPUT_LABEL: f"cancelled:{item.id}",
                    },
                ),
            )


async def ensure_turn_recovery_input(store: ConversationStore, conversation: Conversation) -> None:
    """Finish a checkpoint interrupted between saving its identity and its input."""
    recovery_id = recovery_input_id(conversation)
    if not recovery_id or not await recovery_allowed(store, conversation):
        return
    if await asyncio.to_thread(store.get_item, conversation.id, recovery_id) is not None:
        return
    if any(
        item.status != "failed"
        for item in await asyncio.to_thread(store.list_pending_inputs, conversation.id)
    ):
        await cancel_turn_recovery(store, conversation.id)
        return
    owner = await asyncio.to_thread(store.get_session_owner, conversation.id, owner_only=True)
    if owner is None and conversation.parent_conversation_id is not None:
        owner = await asyncio.to_thread(
            store.get_session_owner, conversation.parent_conversation_id, owner_only=True
        )
    await asyncio.to_thread(
        store.append,
        conversation.id,
        [
            NewConversationItem(
                type="message",
                response_id=PENDING_INPUT_RESPONSE_ID,
                stable_id=recovery_id,
                status="in_progress",
                created_by=owner,
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": CONTINUE_INTERRUPTED_TURN}],
                    startup_options=StartupInputOptions(recovery_id=recovery_id),
                ),
            )
        ],
    )


async def record_interrupted_turn(
    store: ConversationStore,
    conversation: Conversation,
    error: ErrorDetail,
    *,
    owner_runner_id: str | None,
    turn_id: str | None = None,
) -> None:
    """Only execution loss, rather than provider/task errors, requests continuation."""
    from omnigent.server.routes._sessions.helpers import _resolve_harness_async
    from omnigent.server.startup_inputs import wake_startup_worker

    if (
        error.code not in {"runner_disconnected", "runner_process_crash"}
        and "Pi process ended before the turn settled" not in error.message
        and "Pi process ended without response" not in error.message
    ):
        return
    if (
        conversation.host_id is None
        or conversation.runner_id != owner_runner_id
        or await _resolve_harness_async(conversation) != "pi"
        or not await recovery_allowed(store, conversation)
    ):
        return
    previous = conversation.labels.get(RECOVERY_INPUT_LABEL)
    if turn_id is None and recovery_input_id(conversation) is not None:
        await ensure_turn_recovery_input(store, conversation)
        wake_startup_worker(conversation.host_id)
        return
    journal = recovery_journal(store)
    control = await asyncio.to_thread(journal.get_control, conversation.id) if journal else None
    if turn_id is not None and control is not None and control.turn_id != turn_id:
        return
    recovery_id = await _recovery_identity(store, conversation, turn_id)
    if previous in {f"cancelled:{recovery_id}", f"sent:{recovery_id}"}:
        return
    prior_input = await asyncio.to_thread(store.get_item, conversation.id, recovery_id)
    if prior_input is not None:
        return
    if previous is None:
        current = await asyncio.to_thread(
            store.insert_label_if_absent, conversation.id, RECOVERY_INPUT_LABEL, recovery_id
        )
        if current != recovery_id:
            return
    elif previous != recovery_id and not await asyncio.to_thread(
        store.compare_and_set_label,
        conversation.id,
        RECOVERY_INPUT_LABEL,
        previous,
        recovery_id,
    ):
        return
    conversation.labels[RECOVERY_INPUT_LABEL] = recovery_id
    await ensure_turn_recovery_input(store, conversation)
    wake_startup_worker(conversation.host_id)
