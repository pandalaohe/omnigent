"""Unit tests for relay-depth derivation and admission slots (SCC09).

``latest_input_depth`` runs against a real SQLite conversation store and
peer-message store; the admission slot math runs against ``_PeerAdmission``
with an injected monotonic clock. Envelopes are always built with the real
``format_peer_envelope`` so the header regex is pinned to its output.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import pytest

from omnigent.entities import SessionPeerMessage
from omnigent.entities.conversation import (
    FunctionCallData,
    FunctionCallOutputData,
    MessageData,
    NewConversationItem,
)
from omnigent.server.routes.sessions.routes_peer import (
    PEER_DUP_WINDOW,
    PEER_PAIR_LIMIT,
    PEER_PAIR_WINDOW_S,
    PEER_SENDER_LIMIT,
    PEER_SENDER_WINDOW_S,
    _PeerAdmission,
    format_peer_envelope,
    latest_input_depth,
)
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.peer_message_store.sqlalchemy_store import SqlAlchemyPeerMessageStore

Stores = tuple[SqlAlchemyConversationStore, SqlAlchemyPeerMessageStore]


def _conv(conversations: SqlAlchemyConversationStore, title: str) -> str:
    return conversations.create_conversation(title=title).id


def _append(
    conversations: SqlAlchemyConversationStore,
    conversation_id: str,
    data: Any,
    *,
    type_: str,
    created_by: str | None = None,
) -> None:
    conversations.append(
        conversation_id,
        [
            NewConversationItem(
                type=type_,
                response_id=f"resp-{uuid.uuid4().hex}",
                data=data,
                created_by=created_by,
            )
        ],
    )


def _append_user_text(
    conversations: SqlAlchemyConversationStore,
    conversation_id: str,
    text: str,
    *,
    created_by: str | None = None,
) -> None:
    _append(
        conversations,
        conversation_id,
        MessageData(role="user", content=[{"type": "input_text", "text": text}]),
        type_="message",
        created_by=created_by,
    )


def _append_assistant_text(
    conversations: SqlAlchemyConversationStore, conversation_id: str, text: str
) -> None:
    _append(
        conversations,
        conversation_id,
        MessageData(role="assistant", content=[{"type": "output_text", "text": text}], agent="w"),
        type_="message",
    )


def _seed_peer(
    peers: SqlAlchemyPeerMessageStore,
    *,
    sender_id: str,
    receiver_id: str,
    depth: int,
    state: str = "delivered",
    ref: str | None = None,
) -> SessionPeerMessage:
    return peers.create(
        SessionPeerMessage(
            id=uuid.uuid4().hex,
            sender_session_id=sender_id,
            receiver_session_id=receiver_id,
            ref=ref or uuid.uuid4().hex,
            text="payload",
            state=state,
            relay_depth=depth,
            created_at=1000,
            expires_at=2000,
        )
    )


def _append_envelope(
    conversations: SqlAlchemyConversationStore,
    record: SessionPeerMessage,
    *,
    title: str = "Sender",
    ref: str | None = None,
    body: str = "body",
    conversation_id: str | None = None,
) -> None:
    envelope = format_peer_envelope(
        sender_session_id=record.sender_session_id,
        sender_title=title,
        sender_agent_name=None,
        sender_project_id=None,
        ref=ref if ref is not None else record.ref,
        peer_id=record.id,
        text=body,
    )
    _append_user_text(
        conversations,
        conversation_id if conversation_id is not None else record.receiver_session_id,
        envelope,
    )


@pytest.fixture()
def stores(db_uri: str) -> Stores:
    return SqlAlchemyConversationStore(db_uri), SqlAlchemyPeerMessageStore(db_uri)


def test_no_items_is_human(stores: Stores) -> None:
    conversations, peers = stores
    session = _conv(conversations, "empty")
    assert latest_input_depth(conversations, peers, session) == 0


def test_plain_user_message_is_human(stores: Stores) -> None:
    conversations, peers = stores
    session = _conv(conversations, "web")
    _append_assistant_text(conversations, session, "hi")
    _append_user_text(conversations, session, "do the thing")
    assert latest_input_depth(conversations, peers, session) == 0


def test_two_session_chain_depths(stores: Stores) -> None:
    """T2: a human into B, then B→A→B carries 1, 2; the next B send is 3."""
    conversations, peers = stores
    a = _conv(conversations, "a")
    b = _conv(conversations, "b")
    _append_user_text(conversations, b, "human steers B")
    assert latest_input_depth(conversations, peers, b) == 0

    first = _seed_peer(peers, sender_id=b, receiver_id=a, depth=1)
    _append_envelope(conversations, first)
    assert latest_input_depth(conversations, peers, a) == 1

    second = _seed_peer(peers, sender_id=a, receiver_id=b, depth=2)
    _append_envelope(conversations, second)
    assert latest_input_depth(conversations, peers, b) == 2


def test_three_session_chain_depths(stores: Stores) -> None:
    """T3: A→B→C→A climbs 1, 2, 3 with the real envelope format."""
    conversations, peers = stores
    a = _conv(conversations, "a")
    b = _conv(conversations, "b")
    c = _conv(conversations, "c")
    _append_user_text(conversations, b, "human steers B")

    first = _seed_peer(peers, sender_id=b, receiver_id=c, depth=1)
    _append_envelope(conversations, first)
    assert latest_input_depth(conversations, peers, c) == 1

    second = _seed_peer(peers, sender_id=c, receiver_id=a, depth=2)
    _append_envelope(conversations, second)
    assert latest_input_depth(conversations, peers, a) == 2

    third = _seed_peer(peers, sender_id=a, receiver_id=b, depth=3)
    _append_envelope(conversations, third)
    assert latest_input_depth(conversations, peers, b) == 3


def test_mixed_ref_chain_depths(stores: Stores) -> None:
    """T4: the depth climbs even when every hop uses a different ref."""
    conversations, peers = stores
    a = _conv(conversations, "a")
    b = _conv(conversations, "b")
    first = _seed_peer(peers, sender_id=a, receiver_id=b, depth=1, ref="ref-1")
    _append_envelope(conversations, first, ref="ref-1")
    assert latest_input_depth(conversations, peers, b) == 1

    second = _seed_peer(peers, sender_id=b, receiver_id=a, depth=2, ref="ref-2")
    _append_envelope(conversations, second, ref="ref-2")
    assert latest_input_depth(conversations, peers, a) == 2


def test_later_user_message_resets(stores: Stores) -> None:
    """T5: a web message after a depth-5 envelope resets the chain."""
    conversations, peers = stores
    session = _conv(conversations, "web-reset")
    record = _seed_peer(
        peers, sender_id=_conv(conversations, "other"), receiver_id=session, depth=5
    )
    _append_envelope(conversations, record)
    assert latest_input_depth(conversations, peers, session) == 5

    _append_user_text(conversations, session, "actually, do this")
    assert latest_input_depth(conversations, peers, session) == 0


def test_attachment_only_user_message_resets(stores: Stores) -> None:
    """T5b: an attachment-only web message is human input and resets the chain."""
    conversations, peers = stores
    session = _conv(conversations, "attachment-only")
    record = _seed_peer(
        peers, sender_id=_conv(conversations, "other"), receiver_id=session, depth=30
    )
    _append_envelope(conversations, record)
    assert latest_input_depth(conversations, peers, session) == 30

    _append(
        conversations,
        session,
        MessageData(
            role="user",
            content=[{"type": "input_image", "image_url": "data:image/png;base64,AA=="}],
        ),
        type_="message",
    )
    assert latest_input_depth(conversations, peers, session) == 0


def test_mirrored_message_resets(stores: Stores) -> None:
    """T6: a forwarder-mirrored user item is human input all the same."""
    conversations, peers = stores
    session = _conv(conversations, "mirrored")
    record = _seed_peer(
        peers, sender_id=_conv(conversations, "other"), receiver_id=session, depth=5
    )
    _append_envelope(conversations, record)
    _append_user_text(conversations, session, "typed in the TUI", created_by="forwarder")
    assert latest_input_depth(conversations, peers, session) == 0


def test_ask_user_question_output_resets(stores: Stores) -> None:
    """T7: a non-error AskUserQuestion answer after an envelope is human."""
    conversations, peers = stores
    session = _conv(conversations, "ask")
    record = _seed_peer(
        peers, sender_id=_conv(conversations, "other"), receiver_id=session, depth=7
    )
    _append_envelope(conversations, record)
    _append(
        conversations,
        session,
        FunctionCallData(agent="w", name="AskUserQuestion", arguments="{}", call_id="call_ask"),
        type_="function_call",
    )
    _append(
        conversations,
        session,
        FunctionCallOutputData(call_id="call_ask", output="yes"),
        type_="function_call_output",
    )
    assert latest_input_depth(conversations, peers, session) == 0


def test_ask_user_question_error_output_does_not_reset(stores: Stores) -> None:
    """T7: an errored AskUserQuestion output is not a human answer."""
    conversations, peers = stores
    session = _conv(conversations, "ask-error")
    record = _seed_peer(
        peers, sender_id=_conv(conversations, "other"), receiver_id=session, depth=7
    )
    _append_envelope(conversations, record)
    _append(
        conversations,
        session,
        FunctionCallData(agent="w", name="AskUserQuestion", arguments="{}", call_id="call_ask"),
        type_="function_call",
    )
    _append(
        conversations,
        session,
        FunctionCallOutputData(call_id="call_ask", output="failed", is_error=True),
        type_="function_call_output",
    )
    assert latest_input_depth(conversations, peers, session) == 7


def test_answer_newer_than_intervening_envelope_resets(stores: Stores) -> None:
    """T7b: the call is older than the envelope, the answer newer: human wins."""
    conversations, peers = stores
    session = _conv(conversations, "ask-between")
    _append(
        conversations,
        session,
        FunctionCallData(
            agent="w", name="AskUserQuestion", arguments="{}", call_id="call_between"
        ),
        type_="function_call",
    )
    record = _seed_peer(
        peers, sender_id=_conv(conversations, "other"), receiver_id=session, depth=7
    )
    _append_envelope(conversations, record)
    _append(
        conversations,
        session,
        FunctionCallOutputData(call_id="call_between", output="yes"),
        type_="function_call_output",
    )
    assert latest_input_depth(conversations, peers, session) == 0


def test_other_tool_answer_keeps_envelope_depth(stores: Stores) -> None:
    """T7b counterpart: a non-AskUserQuestion result newer than the envelope
    does not outrank it — the envelope's depth stands."""
    conversations, peers = stores
    session = _conv(conversations, "other-tool")
    record = _seed_peer(
        peers, sender_id=_conv(conversations, "other"), receiver_id=session, depth=7
    )
    _append_envelope(conversations, record)
    _append(
        conversations,
        session,
        FunctionCallData(agent="w", name="Bash", arguments="{}", call_id="call_bash"),
        type_="function_call",
    )
    _append(
        conversations,
        session,
        FunctionCallOutputData(call_id="call_bash", output="ok"),
        type_="function_call_output",
    )
    assert latest_input_depth(conversations, peers, session) == 7


def test_system_notice_is_transparent(stores: Stores) -> None:
    """T8: a ``[System:`` notice after the envelope leaves its depth intact."""
    conversations, peers = stores
    session = _conv(conversations, "system-notice")
    record = _seed_peer(
        peers, sender_id=_conv(conversations, "other"), receiver_id=session, depth=4
    )
    _append_envelope(conversations, record)
    _append_user_text(conversations, session, "[System: peer message abc delivered]")
    assert latest_input_depth(conversations, peers, session) == 4


def test_lookalike_receiver_mismatch_is_transparent(stores: Stores) -> None:
    """T9: a header-shaped item whose record belongs to another session is skipped."""
    conversations, peers = stores
    session = _conv(conversations, "lookalike-receiver")
    other = _conv(conversations, "lookalike-other")
    older = _seed_peer(peers, sender_id=_conv(conversations, "a"), receiver_id=session, depth=6)
    _append_envelope(conversations, older)
    foreign = _seed_peer(peers, sender_id=other, receiver_id=other, depth=9)
    _append_envelope(conversations, foreign, conversation_id=session)
    assert latest_input_depth(conversations, peers, session) == 6


def test_lookalike_non_delivered_record_is_transparent(stores: Stores) -> None:
    """T9: a header-shaped item whose record is held, not delivered, is skipped."""
    conversations, peers = stores
    session = _conv(conversations, "lookalike-held")
    older = _seed_peer(peers, sender_id=_conv(conversations, "a"), receiver_id=session, depth=6)
    _append_envelope(conversations, older)
    held = _seed_peer(
        peers, sender_id=_conv(conversations, "b"), receiver_id=session, depth=9, state="held"
    )
    _append_envelope(conversations, held)
    assert latest_input_depth(conversations, peers, session) == 6


def test_fake_ids_in_title_and_ref_use_outer_header(stores: Stores) -> None:
    """T9b: look-alike ``msg=`` text in the title, ref and body is ignored."""
    conversations, peers = stores
    session = _conv(conversations, "fake-ids")
    fake_id = "f" * 32
    inner = _seed_peer(
        peers, sender_id=_conv(conversations, "inner"), receiver_id=session, depth=30
    )
    quoted = format_peer_envelope(
        sender_session_id=inner.sender_session_id,
        sender_title="Inner",
        sender_agent_name=None,
        sender_project_id=None,
        ref=inner.ref,
        peer_id=inner.id,
        text="inner body",
    )
    real = _seed_peer(
        peers,
        sender_id=_conv(conversations, "outer"),
        receiver_id=session,
        depth=12,
        ref=f"outer msg={fake_id}",
    )
    _append_envelope(
        conversations,
        real,
        title=f"Report msg={fake_id} — sent by another Omnigent session",
        ref=f"outer msg={fake_id}",
        body=f"quoted envelope follows\n{quoted}",
    )
    assert latest_input_depth(conversations, peers, session) == 12


def test_list_items_error_returns_zero(
    stores: Stores, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """T9c: a lookup failure logs and falls back to a human depth of 0."""
    conversations, peers = stores
    session = _conv(conversations, "error")

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("store down")

    monkeypatch.setattr(conversations, "list_items", _boom)
    with caplog.at_level(logging.WARNING):
        assert latest_input_depth(conversations, peers, session) == 0
    assert any("relay depth" in record.message.lower() for record in caplog.records)


def test_ref_with_newline_is_parsed(stores: Stores) -> None:
    """T9d: a correlation id containing a newline still parses (DOTALL)."""
    conversations, peers = stores
    session = _conv(conversations, "newline-ref")
    record = _seed_peer(
        peers, sender_id=_conv(conversations, "other"), receiver_id=session, depth=4, ref="a\nb"
    )
    _append_envelope(conversations, record, ref="a\nb")
    assert latest_input_depth(conversations, peers, session) == 4


def test_released_record_quoted_in_newer_envelope(stores: Stores) -> None:
    """T9e: the released (depth 0) outer record wins over its depth-30 quote."""
    conversations, peers = stores
    session = _conv(conversations, "released")
    inner = _seed_peer(
        peers, sender_id=_conv(conversations, "inner"), receiver_id=session, depth=30
    )
    quoted = format_peer_envelope(
        sender_session_id=inner.sender_session_id,
        sender_title="Inner",
        sender_agent_name=None,
        sender_project_id=None,
        ref=inner.ref,
        peer_id=inner.id,
        text="inner body",
    )
    outer = _seed_peer(
        peers,
        sender_id=_conv(conversations, "outer"),
        receiver_id=session,
        depth=0,
        ref=uuid.uuid4().hex,
    )
    _append_envelope(conversations, outer, body=f"quoted follows\n{quoted}")
    assert latest_input_depth(conversations, peers, session) == 0


def _fill(conversations: SqlAlchemyConversationStore, conversation_id: str, count: int) -> None:
    for index in range(0, count, 100):
        chunk = min(100, count - index)
        conversations.append(
            conversation_id,
            [
                NewConversationItem(
                    type="message",
                    response_id=f"resp-fill-{index + offset}",
                    data=MessageData(
                        role="assistant",
                        content=[{"type": "output_text", "text": f"filler {index + offset}"}],
                        agent="w",
                    ),
                )
                for offset in range(chunk)
            ],
        )


def test_lookback_bound_skips_envelope_beyond_max_items(stores: Stores) -> None:
    """T10: an envelope 1001 items back is outside the 1000-item bound."""
    conversations, peers = stores
    session = _conv(conversations, "bound-over")
    record = _seed_peer(
        peers, sender_id=_conv(conversations, "other"), receiver_id=session, depth=9
    )
    _append_envelope(conversations, record)
    _fill(conversations, session, 1001)
    assert latest_input_depth(conversations, peers, session) == 0


def test_lookback_bound_finds_envelope_across_pages(stores: Stores) -> None:
    """T10: an envelope 999 items back is found across the last page boundary."""
    conversations, peers = stores
    session = _conv(conversations, "bound-under")
    record = _seed_peer(
        peers, sender_id=_conv(conversations, "other"), receiver_id=session, depth=9
    )
    _append_envelope(conversations, record)
    _fill(conversations, session, 999)
    assert latest_input_depth(conversations, peers, session) == 9


def test_reserve_pair_overflow_takes_future_slot() -> None:
    """T14: the seventh send to one receiver in the window is delayed 60 s."""
    admission = _PeerAdmission()
    for index in range(PEER_PAIR_LIMIT):
        assert admission.reserve("s", "r", f"t{index}", now=1000.0) == (None, 0.0, 1000.0)
    verdict, delay, slot = admission.reserve("s", "r", "t7", now=1000.0)
    assert verdict is None
    assert slot == 1000.0 + PEER_PAIR_WINDOW_S
    assert delay == float(PEER_PAIR_WINDOW_S)


def test_reserve_other_receiver_not_blocked() -> None:
    """T14b: a pair backlog to one receiver does not delay another receiver."""
    admission = _PeerAdmission()
    for index in range(PEER_PAIR_LIMIT):
        admission.reserve("s", "r1", f"t{index}", now=1000.0)
    assert admission.reserve("s", "r2", "elsewhere", now=1000.0) == (None, 0.0, 1000.0)


def test_reserve_sender_budget_takes_future_slot() -> None:
    """T16: the 61st send in the sender window is delayed 600 s."""
    admission = _PeerAdmission()
    receivers = [f"r{index}" for index in range(11)]
    for index in range(PEER_SENDER_LIMIT):
        verdict, delay, slot = admission.reserve(
            "s", receivers[index % len(receivers)], f"t{index}", now=1000.0
        )
        assert (verdict, delay, slot) == (None, 0.0, 1000.0)
    verdict, delay, slot = admission.reserve("s", receivers[0], "t60", now=1000.0)
    assert verdict is None
    assert slot == 1000.0 + PEER_SENDER_WINDOW_S
    assert delay == float(PEER_SENDER_WINDOW_S)


def test_reserve_deadline_rounds_up_from_wall_clock() -> None:
    """T16b: the slot is the wall deadline, not now_epoch() + ceil(delay)."""
    admission = _PeerAdmission()
    for index in range(PEER_PAIR_LIMIT):
        admission.reserve("s", "r", f"t{index}", now=1000.90)
    _verdict, delay, slot = admission.reserve("s", "r", "t7", now=1001.95)
    assert slot == 1060.90
    assert delay == pytest.approx(58.95)


def test_reserve_duplicate_drops_without_slot() -> None:
    """T17: an identical text inside the window drops and takes no slot."""
    admission = _PeerAdmission()
    assert admission.reserve("s", "r", "same text", now=1000.0) == (None, 0.0, 1000.0)
    assert admission.reserve("s", "r", "same  text", now=1001.0) == (
        "dropped:duplicate",
        0.0,
        None,
    )


def test_reserve_same_text_other_thread_admits() -> None:
    """Same text on a different correlation id is a different send."""
    admission = _PeerAdmission()
    assert admission.reserve("s", "r", "same text", correlation_id="a", now=1000.0) == (
        None,
        0.0,
        1000.0,
    )
    assert admission.reserve("s", "r", "same text", correlation_id="b", now=1000.5) == (
        None,
        0.0,
        1000.5,
    )


def test_reserve_drops_past_the_window_while_undelivered() -> None:
    """An identical send past the window drops while the earlier copy is live."""
    admission = _PeerAdmission()
    admission.reserve("s", "r", "same text", now=1000.0)
    admission.note_record("s", "r", None, "same text", "peer-1")
    assert admission.pending_peer_id("s", "r", None, "same text") == "peer-1"
    after = 1000.0 + PEER_DUP_WINDOW + 1
    assert admission.reserve("s", "r", "same text", now=after, earlier_undelivered=True) == (
        "dropped:duplicate",
        0.0,
        None,
    )


def test_reserve_admits_past_the_window_once_delivered() -> None:
    """Once the earlier copy settled, the window alone governs the resend."""
    admission = _PeerAdmission()
    admission.reserve("s", "r", "same text", now=1000.0)
    admission.note_record("s", "r", None, "same text", "peer-1")
    after = 1000.0 + PEER_DUP_WINDOW + 1
    assert admission.reserve("s", "r", "same text", now=after, earlier_undelivered=False) == (
        None,
        0.0,
        after,
    )


def test_note_record_is_a_noop_after_release() -> None:
    """A released key is never rebound by a late note_record."""
    admission = _PeerAdmission()
    _v, _d, slot = admission.reserve("s", "r", "text", correlation_id="t", now=1000.0)
    admission.release("s", "r", "text", correlation_id="t", verdict="failed", slot=slot)
    admission.note_record("s", "r", "t", "text", "peer-1")
    assert admission.pending_peer_id("s", "r", "t", "text") is None
    assert admission.reserve("s", "r", "text", correlation_id="t", now=1000.1)[0] is None


def test_release_removes_slot_by_value() -> None:
    """A failed send frees exactly its own slot from both ledgers."""
    admission = _PeerAdmission()
    _v, _d, first = admission.reserve("s", "r", "first", now=1000.0)
    _v, _d, second = admission.reserve("s", "r", "second", now=1000.5)
    assert first is not None and second is not None
    admission.release("s", "r", "first", verdict="failed", slot=first)
    assert admission._pair_sends[("s", "r")] == [second]
    assert admission._sender_sends["s"] == [second]
    assert admission.reserve("s", "r", "third", now=1000.6)[1] == 0.0
