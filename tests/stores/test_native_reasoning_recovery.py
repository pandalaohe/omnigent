"""Compatibility for thinking omitted by older native transcript projections."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from omnigent.entities import MessageData, NewConversationItem, ReasoningData
from omnigent.stores.conversation_store import (
    NativeRecoveryItemSkipped,
    NativeReplayConflictError,
)
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


def _message(value: str, source: str | None = None) -> NewConversationItem:
    return NewConversationItem(
        type="message",
        response_id="old-turn",
        idempotency_key=source,
        data=MessageData(
            role="assistant",
            agent="claude-native-ui",
            content=[{"type": "output_text", "text": value}],
        ),
    )


def _reasoning(value: str, source: str = "thinking-source") -> NewConversationItem:
    return NewConversationItem(
        type="reasoning",
        response_id="cold-turn",
        idempotency_key=source,
        data=ReasoningData(
            agent="claude-native-ui",
            summary=[],
            content=[{"type": "reasoning_text", "text": value}],
        ),
    )


def _recovery(item: NewConversationItem, after: str | None = None) -> NewConversationItem:
    return item.model_copy(
        update={"native_recovery": True, "recovery_after": after, "response_id": "cold-turn"}
    )


def _snapshot(
    store: SqlAlchemyConversationStore, conversation_id: str
) -> tuple[tuple[tuple[object, ...], ...], ...]:
    with store._conv_session("test_history_snapshot") as session:
        return tuple(
            tuple(
                tuple(row)
                for row in session.execute(
                    text(sql),
                    {"cid": conversation_id if index == 2 else bytes.fromhex(conversation_id)},
                )
            )
            for index, sql in enumerate(
                (
                    "SELECT * FROM conversations WHERE id = :cid",
                    "SELECT * FROM conversation_items "
                    "WHERE conversation_id = :cid ORDER BY position",
                    "SELECT * FROM conversation_items_fts "
                    "WHERE conversation_id = :cid ORDER BY rowid",
                )
            )
        )


@pytest.mark.parametrize("has_prefix", [False, True])
def test_legacy_reasoning_omission_keeps_history_and_cursor(
    conversation_store: SqlAlchemyConversationStore, has_prefix: bool
) -> None:
    conv = conversation_store.create_conversation()
    items = [_message("prefix"), _message("answer")] if has_prefix else [_message("answer")]
    stored = conversation_store.append(conv.id, items)
    after = stored[0].id if has_prefix else None
    before = _snapshot(conversation_store, conv.id)
    assert len(before[0]) == 1 and len(before[1]) == len(stored)
    for _ in range(2):
        with pytest.raises(NativeRecoveryItemSkipped):
            conversation_store.append(conv.id, [_recovery(_reasoning("old thought"), after)])
    assert _snapshot(conversation_store, conv.id) == before
    matched = conversation_store.append(
        conv.id, [_recovery(_message("answer", "answer-source"), after)]
    )[0]
    assert matched.id == stored[-1].id and matched.replayed
    assert _snapshot(conversation_store, conv.id) == before


def test_mixed_history_omits_old_reasoning_then_matches_new_reasoning(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    conv = conversation_store.create_conversation()
    new_reasoning = _reasoning("new thought", "new-thinking-source")
    old, thinking, answer = conversation_store.append(
        conv.id, [_message("old answer"), new_reasoning, _message("new answer")]
    )
    before = _snapshot(conversation_store, conv.id)
    with pytest.raises(NativeRecoveryItemSkipped):
        conversation_store.append(conv.id, [_recovery(_reasoning("omitted old thought"))])
    assert (
        conversation_store.append(
            conv.id, [_recovery(_message("old answer", "old-answer-source"))]
        )[0].id
        == old.id
    )
    recovered = conversation_store.append(conv.id, [_recovery(new_reasoning, old.id)])[0]
    assert recovered.id == thinking.id and recovered.replayed
    with pytest.raises(NativeReplayConflictError, match="prefix differs"):
        conversation_store.append(
            conv.id, [_recovery(_reasoning("extra thought", "unknown-source"), thinking.id)]
        )
    assert (
        conversation_store.append(
            conv.id, [_recovery(_message("new answer", "new-answer-source"), thinking.id)]
        )[0].id
        == answer.id
    )
    assert _snapshot(conversation_store, conv.id) == before


def test_recovery_rejects_changed_reasoning_and_out_of_order_source(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    conv = conversation_store.create_conversation()
    reasoning = _reasoning("persisted thought")
    prefix, _ = conversation_store.append(conv.id, [_message("answer"), reasoning])
    before = _snapshot(conversation_store, conv.id)
    with pytest.raises(NativeReplayConflictError, match="out of order"):
        conversation_store.append(conv.id, [_recovery(reasoning)])
    with pytest.raises(NativeReplayConflictError, match="prefix differs"):
        conversation_store.append(conv.id, [_recovery(_reasoning("tampered"), prefix.id)])
    with pytest.raises(NativeReplayConflictError, match="prefix differs"):
        conversation_store.append(conv.id, [_recovery(_message("wrong", "wrong-source"))])
    assert _snapshot(conversation_store, conv.id) == before


def test_reasoning_omission_does_not_hide_changed_source_type(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    conv = conversation_store.create_conversation()
    conversation_store.append(conv.id, [_message("answer", "same-source")])
    before = _snapshot(conversation_store, conv.id)
    with pytest.raises(NativeReplayConflictError, match="prefix differs"):
        conversation_store.append(conv.id, [_recovery(_reasoning("tampered", "same-source"))])
    assert _snapshot(conversation_store, conv.id) == before


@pytest.mark.parametrize("cursor_kind", ["malformed", "absent", "foreign"])
def test_reasoning_omission_validates_cursor_before_compatibility(
    conversation_store: SqlAlchemyConversationStore, cursor_kind: str
) -> None:
    conv = conversation_store.create_conversation()
    conversation_store.append(conv.id, [_message("answer")])
    foreign = conversation_store.create_conversation()
    foreign_item = conversation_store.append(foreign.id, [_message("other session")])[0]
    cursor = {"malformed": "missing", "absent": "f" * 32, "foreign": foreign_item.id}[cursor_kind]
    before = _snapshot(conversation_store, conv.id)
    with pytest.raises(NativeReplayConflictError, match="cursor is missing"):
        conversation_store.append(conv.id, [_recovery(_reasoning("old thought"), cursor)])
    assert _snapshot(conversation_store, conv.id) == before


@pytest.mark.parametrize("has_prefix", [False, True])
def test_missing_reasoning_tail_is_persisted_once_and_live_reasoning_still_appends(
    conversation_store: SqlAlchemyConversationStore, has_prefix: bool
) -> None:
    conv = conversation_store.create_conversation()
    prefix = conversation_store.append(conv.id, [_message("answer")]) if has_prefix else []
    after = prefix[0].id if prefix else None
    historical = _recovery(_reasoning("offline thought"), after)
    inserted = conversation_store.append(conv.id, [historical])[0]
    replayed = conversation_store.append(conv.id, [historical])[0]
    assert replayed.id == inserted.id and replayed.replayed
    live = conversation_store.append(conv.id, [_reasoning("live thought", "live-source")])[0]
    assert live.id != inserted.id and not live.replayed
    assert len(conversation_store.list_items(conv.id).data) == len(prefix) + 2
