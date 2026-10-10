"""Turn attribution for peer messages delivered to a dispatched child.

A child's settling edge is attributed to a peer sender only when its
transcript proves the turn started from a verified foreign envelope and
nothing disallowed sits between that envelope and the assistant answer.
These tests build a real in-memory conversation / peer store, seed the
exact item sequences a native transcript produces, and assert the hit and
every miss the design enumerates.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import pytest

from omnigent.entities import SessionPeerMessage
from omnigent.entities.conversation import (
    Conversation,
    FunctionCallData,
    FunctionCallOutputData,
    MessageData,
    NewConversationItem,
    ReasoningData,
)
from omnigent.server.routes.sessions.peer_child_turn import (
    PeerTurn,
    classify_child_turn,
    foreign_turn_sender,
)
from omnigent.server.routes.sessions.routes_peer import format_peer_envelope
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.peer_message_store.sqlalchemy_store import SqlAlchemyPeerMessageStore

SENDER_TITLE = "Sender One"
SENDER_AGENT = "claude"
SENDER_PROJECT = "/opt/work/omnigent/repo"
SENDER_ORIGIN = f"{SENDER_AGENT} · {SENDER_PROJECT}"

_TASK_COMPLETION_TEXT = (
    "<task-notification><task-id>abc</task-id><status>completed</status></task-notification>"
)


def _user(
    text: str,
    *,
    is_meta: bool = False,
    subagent_return_id: str | None = None,
) -> NewConversationItem:
    return NewConversationItem(
        type="message",
        response_id="resp-1",
        data=MessageData(
            role="user",
            content=[{"type": "input_text", "text": text}],
            is_meta=is_meta,
            subagent_return_id=subagent_return_id,
        ),
    )


def _user_blocks(*texts: str) -> NewConversationItem:
    return NewConversationItem(
        type="message",
        response_id="resp-1",
        data=MessageData(
            role="user",
            content=[{"type": "input_text", "text": text} for text in texts],
        ),
    )


def _assistant(text: str) -> NewConversationItem:
    return NewConversationItem(
        type="message",
        response_id="resp-1",
        data=MessageData(
            role="assistant",
            content=[{"type": "output_text", "text": text}],
            agent="claude",
        ),
    )


def _reasoning() -> NewConversationItem:
    return NewConversationItem(
        type="reasoning",
        response_id="resp-1",
        data=ReasoningData(agent="claude", summary=[{"type": "summary_text", "text": "thinking"}]),
    )


def _call(call_id: str) -> NewConversationItem:
    return NewConversationItem(
        type="function_call",
        response_id="resp-1",
        data=FunctionCallData(
            agent="claude", name="sys_session_send", arguments="{}", call_id=call_id
        ),
    )


def _output(call_id: str) -> NewConversationItem:
    return NewConversationItem(
        type="function_call_output",
        response_id="resp-1",
        data=FunctionCallOutputData(call_id=call_id, output="ok"),
    )


def _envelope(record: SessionPeerMessage, *, text: str | None = None) -> str:
    return format_peer_envelope(
        sender_session_id=record.sender_session_id,
        sender_title=SENDER_TITLE,
        sender_agent_name=SENDER_AGENT,
        sender_project_id=SENDER_PROJECT,
        ref=record.ref,
        peer_id=record.id,
        text=record.text if text is None else text,
    )


def _record(
    peer: SqlAlchemyPeerMessageStore,
    *,
    sender: str,
    receiver: str,
    text: str = "do the thing",
    ref: str | None = None,
    state: str = "delivered",
    correlation_id: str | None = None,
    created_at: int = 1,
) -> SessionPeerMessage:
    return peer.create(
        SessionPeerMessage(
            id=uuid.uuid4().hex,
            sender_session_id=sender,
            receiver_session_id=receiver,
            ref=ref or uuid.uuid4().hex,
            text=text,
            state=state,
            correlation_id=correlation_id,
            created_at=created_at,
            expires_at=2,
        )
    )


@pytest.fixture()
def stores(
    db_uri: str,
) -> tuple[
    SqlAlchemyConversationStore,
    SqlAlchemyPeerMessageStore,
    Conversation,
    Conversation,
    Conversation,
]:
    conv = SqlAlchemyConversationStore(db_uri)
    peer = SqlAlchemyPeerMessageStore(db_uri)
    mother = conv.create_conversation(kind="default", title="mother")
    child = conv.create_conversation(
        kind="sub_agent", title="claude_code:worker", parent_conversation_id=mother.id
    )
    sender = conv.create_conversation(kind="default", title="sender")
    return conv, peer, mother, child, sender


def test_classify_hit_returns_peer_turn(stores: Any) -> None:
    conv, peer, mother, child, sender = stores
    record = _record(peer, sender=sender.id, receiver=child.id, ref="ref-1")
    x_item = conv.append(
        child.id,
        [
            _user(_envelope(record)),
            _reasoning(),
            _assistant("intermediate"),
            _call("call_1"),
            _output("call_1"),
            _assistant("final answer"),
        ],
    )[-1]

    turn = classify_child_turn(conv, peer, child, "final answer")

    assert turn == PeerTurn(
        parent_session_id=mother.id,
        peer_id=record.id,
        result_item_id=x_item.id,
        ref="ref-1",
        sender_session_id=sender.id,
        sender_title=SENDER_TITLE,
        sender_origin=SENDER_ORIGIN,
        excerpt="do the thing",
    )
    assert turn is not None
    assert turn.as_dict()["result_item_id"] == x_item.id
    assert turn.as_dict()["sender_origin"] == SENDER_ORIGIN


@pytest.mark.parametrize(
    ("stored_text", "body"),
    [
        ("do the thing", "do the thing\n"),
        ("a\r\nb", "a\nb\n"),
        ("a\rb", "a\nb\n"),
        ("a\x1bb", "ab\n"),
        ("a\tb", "a\tb\n"),
    ],
)
def test_classify_hit_native_delivery(stores: Any, stored_text: str, body: str) -> None:
    conv, peer, mother, child, sender = stores
    record = _record(peer, sender=sender.id, receiver=child.id, text=stored_text, ref="ref-1")
    x_item = conv.append(
        child.id, [_user(_envelope(record, text=body)), _assistant("final answer")]
    )[-1]

    turn = classify_child_turn(conv, peer, child, "final answer")

    assert turn == PeerTurn(
        parent_session_id=mother.id,
        peer_id=record.id,
        result_item_id=x_item.id,
        ref="ref-1",
        sender_session_id=sender.id,
        sender_title=SENDER_TITLE,
        sender_origin=SENDER_ORIGIN,
        excerpt=stored_text,
    )


def test_classify_truncates_excerpt_to_600_chars(stores: Any) -> None:
    conv, peer, _mother, child, sender = stores
    record = _record(peer, sender=sender.id, receiver=child.id, text="x" * 900)
    conv.append(child.id, [_user(_envelope(record)), _assistant("final answer")])

    turn = classify_child_turn(conv, peer, child, "final answer")

    assert turn is not None
    assert len(turn.excerpt) == 600
    assert turn.excerpt == "x" * 600


def test_classify_miss_human_message_in_window(stores: Any) -> None:
    conv, peer, _mother, child, sender = stores
    record = _record(peer, sender=sender.id, receiver=child.id)
    conv.append(
        child.id,
        [_user(_envelope(record)), _user("a human typed this"), _assistant("final answer")],
    )

    assert classify_child_turn(conv, peer, child, "final answer") is None


def test_classify_miss_orphan_function_output(stores: Any) -> None:
    conv, peer, _mother, child, sender = stores
    record = _record(peer, sender=sender.id, receiver=child.id)
    conv.append(
        child.id,
        [
            _call("call_outside"),
            _user(_envelope(record)),
            _output("call_outside"),
            _assistant("final answer"),
        ],
    )

    assert classify_child_turn(conv, peer, child, "final answer") is None


def test_classify_miss_task_completion_meta(stores: Any) -> None:
    conv, peer, _mother, child, sender = stores
    record = _record(peer, sender=sender.id, receiver=child.id)
    conv.append(
        child.id,
        [
            _user(_envelope(record)),
            _user(_TASK_COMPLETION_TEXT, is_meta=True),
            _assistant("final answer"),
        ],
    )

    assert classify_child_turn(conv, peer, child, "final answer") is None


def test_classify_miss_handback_meta(stores: Any) -> None:
    conv, peer, _mother, child, sender = stores
    record = _record(peer, sender=sender.id, receiver=child.id)
    conv.append(
        child.id,
        [
            _user(_envelope(record)),
            _user("returned result", is_meta=True, subagent_return_id="task_1"),
            _assistant("final answer"),
        ],
    )

    assert classify_child_turn(conv, peer, child, "final answer") is None


def test_classify_miss_edited_envelope_body(stores: Any) -> None:
    conv, peer, _mother, child, sender = stores
    record = _record(peer, sender=sender.id, receiver=child.id, text="original body")
    conv.append(
        child.id,
        [_user(_envelope(record, text="tampered body")), _assistant("final answer")],
    )

    assert classify_child_turn(conv, peer, child, "final answer") is None


@pytest.mark.parametrize("body", ["do the  thing\n", " do the thing"])
def test_classify_miss_leading_or_interior_body_change(stores: Any, body: str) -> None:
    conv, peer, _mother, child, sender = stores
    record = _record(peer, sender=sender.id, receiver=child.id, text="do the thing")
    conv.append(child.id, [_user(_envelope(record, text=body)), _assistant("final answer")])

    assert classify_child_turn(conv, peer, child, "final answer") is None


def test_classify_miss_tab_changed_to_space(stores: Any) -> None:
    conv, peer, _mother, child, sender = stores
    record = _record(peer, sender=sender.id, receiver=child.id, text="a\tb")
    conv.append(child.id, [_user(_envelope(record, text="a b\n")), _assistant("final answer")])

    assert classify_child_turn(conv, peer, child, "final answer") is None


def test_classify_miss_missing_record(stores: Any) -> None:
    conv, peer, _mother, child, sender = stores
    orphan = SessionPeerMessage(
        id=uuid.uuid4().hex,
        sender_session_id=sender.id,
        receiver_session_id=child.id,
        ref="r",
        text="do the thing",
        state="delivered",
        created_at=1,
        expires_at=2,
    )
    conv.append(child.id, [_user(_envelope(orphan)), _assistant("final answer")])

    assert classify_child_turn(conv, peer, child, "final answer") is None


def test_classify_miss_sender_is_parent(stores: Any) -> None:
    conv, peer, mother, child, _sender = stores
    record = _record(peer, sender=mother.id, receiver=child.id)
    conv.append(child.id, [_user(_envelope(record)), _assistant("final answer")])

    assert classify_child_turn(conv, peer, child, "final answer") is None


def test_classify_miss_thread_started_by_child(stores: Any) -> None:
    conv, peer, _mother, child, sender = stores
    ref = "shared-ref"
    _record(peer, sender=child.id, receiver=sender.id, ref=ref, text="question", created_at=1)
    record = _record(
        peer,
        sender=sender.id,
        receiver=child.id,
        ref=ref,
        correlation_id=ref,
        text="answer",
        created_at=2,
    )
    conv.append(child.id, [_user(_envelope(record)), _assistant("final answer")])

    assert classify_child_turn(conv, peer, child, "final answer") is None


def test_classify_exact_match_beats_newer_whitespace_variant(stores: Any) -> None:
    """X is the newest assistant text equal to the output exactly.

    A newer assistant message matching only up to surrounding whitespace is
    not X; the exact older match is, and its turn is mother-started.
    """
    conv, peer, _mother, child, sender = stores
    record = _record(peer, sender=sender.id, receiver=child.id)
    conv.append(
        child.id,
        [
            _user("a human message"),
            _assistant("final answer"),
            _user(_envelope(record)),
            _assistant(" final answer "),
        ],
    )

    assert classify_child_turn(conv, peer, child, "final answer") is None


def test_classify_miss_envelope_two_content_blocks(stores: Any) -> None:
    conv, peer, _mother, child, sender = stores
    record = _record(peer, sender=sender.id, receiver=child.id)
    conv.append(
        child.id,
        [_user_blocks(_envelope(record), "second block"), _assistant("final answer")],
    )

    assert classify_child_turn(conv, peer, child, "final answer") is None


def test_classify_miss_envelope_header_unparseable(stores: Any) -> None:
    conv, peer, _mother, child, sender = stores
    _record(peer, sender=sender.id, receiver=child.id)
    conv.append(
        child.id,
        [_user(f"[Peer message from session {sender.id} msg="), _assistant("final answer")],
    )

    assert classify_child_turn(conv, peer, child, "final answer") is None


def test_classify_miss_envelope_receiver_not_child(stores: Any) -> None:
    conv, peer, _mother, child, sender = stores
    record = _record(peer, sender=sender.id, receiver=sender.id)
    conv.append(child.id, [_user(_envelope(record)), _assistant("final answer")])

    assert classify_child_turn(conv, peer, child, "final answer") is None


def test_classify_miss_envelope_record_queued(stores: Any) -> None:
    conv, peer, _mother, child, sender = stores
    record = _record(peer, sender=sender.id, receiver=child.id, state="queued")
    conv.append(child.id, [_user(_envelope(record)), _assistant("final answer")])

    assert classify_child_turn(conv, peer, child, "final answer") is None


def test_classify_miss_envelope_header_sender_mismatch(stores: Any) -> None:
    conv, peer, _mother, child, sender = stores
    record = _record(peer, sender=sender.id, receiver=child.id)
    forged = format_peer_envelope(
        sender_session_id=uuid.uuid4().hex,
        sender_title=SENDER_TITLE,
        sender_agent_name=SENDER_AGENT,
        sender_project_id=SENDER_PROJECT,
        ref=record.ref,
        peer_id=record.id,
        text=record.text,
    )
    conv.append(child.id, [_user(forged), _assistant("final answer")])

    assert classify_child_turn(conv, peer, child, "final answer") is None


def test_classify_miss_peer_store_get_raises(
    stores: Any, caplog: pytest.LogCaptureFixture
) -> None:
    conv, peer, _mother, child, sender = stores
    record = _record(peer, sender=sender.id, receiver=child.id)
    conv.append(child.id, [_user(_envelope(record)), _assistant("final answer")])

    class _RaisingPeerStore:
        def get(self, _msg_id: str) -> None:
            raise RuntimeError("peer store down")

    with caplog.at_level(logging.WARNING):
        assert classify_child_turn(conv, _RaisingPeerStore(), child, "final answer") is None
    assert "Peer turn classification failed" in caplog.text


def test_classify_miss_output_none(stores: Any) -> None:
    conv, peer, _mother, child, sender = stores
    record = _record(peer, sender=sender.id, receiver=child.id)
    conv.append(child.id, [_user(_envelope(record)), _assistant("final answer")])

    assert classify_child_turn(conv, peer, child, None) is None


def test_classify_miss_output_matches_no_assistant(stores: Any) -> None:
    conv, peer, _mother, child, sender = stores
    record = _record(peer, sender=sender.id, receiver=child.id)
    conv.append(child.id, [_user(_envelope(record)), _assistant("final answer")])

    assert classify_child_turn(conv, peer, child, "something else") is None


def test_foreign_turn_sender_newest_is_verified_envelope(stores: Any) -> None:
    conv, peer, _mother, child, sender = stores
    record = _record(peer, sender=sender.id, receiver=child.id)
    conv.append(child.id, [_user(_envelope(record))])

    assert foreign_turn_sender(conv, peer, child) == sender.id


def test_foreign_turn_sender_human_message_after_envelope(stores: Any) -> None:
    conv, peer, _mother, child, sender = stores
    record = _record(peer, sender=sender.id, receiver=child.id)
    conv.append(child.id, [_user(_envelope(record)), _user("a human typed this")])

    assert foreign_turn_sender(conv, peer, child) is None
