"""Unit tests for :class:`~omnigent.server.peer_sweeper.PeerSweeper`.

Fakes for the store, true-state, delivery, and back-notice posting; every
test drives ``_tick()`` (or ``_reconcile_startup()``) directly rather than
the sleep loop, with an injected clock for expiry control.
"""

from __future__ import annotations

import asyncio
import dataclasses
import threading
from typing import Any, cast

import pytest

from omnigent.entities import SessionPeerMessage
from omnigent.entities.conversation import Conversation
from omnigent.server.peer_sweeper import PeerSweeper
from omnigent.server.schemas import SessionEventInput
from omnigent.stores.peer_message_store import PeerMessageStore

_APP = object()  # PeerSweeper only ever forwards this; a sentinel is enough.


def _row(store: PeerMessageStore, peer_id: str) -> SessionPeerMessage:
    """Fetch a record the test just seeded/transitioned; a miss is a test bug."""
    record = store.get(peer_id)
    assert record is not None
    return record


def _conv(
    id_: str,
    *,
    title: str | None = "title",
    labels: dict[str, str] | None = None,
    archived_at: int | None = None,
) -> Conversation:
    return Conversation(
        id=id_,
        created_at=0,
        updated_at=0,
        root_conversation_id=id_,
        title=title,
        labels=labels or {},
        archived_at=archived_at,
    )


class _FakePeerStore(PeerMessageStore):
    """In-memory store with a real lock, so CAS races are genuine."""

    def __init__(self) -> None:
        super().__init__("fake://")
        self._rows: dict[str, SessionPeerMessage] = {}
        self._lock = threading.Lock()

    def seed(self, record: SessionPeerMessage) -> None:
        self._rows[record.id] = record

    def create(self, record: SessionPeerMessage) -> SessionPeerMessage:
        self._rows[record.id] = record
        return record

    def get(self, peer_id: str) -> SessionPeerMessage | None:
        return self._rows.get(peer_id)

    def list_for_session(
        self, session_id: str, states: tuple[str, ...] | None = None, limit: int = 20
    ) -> list[SessionPeerMessage]:
        rows = [r for r in self._rows.values() if r.receiver_session_id == session_id]
        if states is not None:
            rows = [r for r in rows if r.state in states]
        rows.sort(key=lambda r: r.created_at, reverse=True)
        return rows[:limit]

    def list_due(self, states: tuple[str, ...], limit: int) -> list[SessionPeerMessage]:
        rows = [r for r in self._rows.values() if r.state in states]
        rows.sort(key=lambda r: (r.expires_at, r.id))
        return rows[:limit]

    def transition(
        self,
        peer_id: str,
        state: str,
        reason: str | None = None,
        expected_states: tuple[str, ...] | None = None,
        *,
        expires_at: int | None = None,
    ) -> bool:
        with self._lock:
            row = self._rows.get(peer_id)
            if row is None:
                return False
            if expected_states is not None and row.state not in expected_states:
                return False
            row.state = state
            if reason is not None:
                row.reason = reason
            if expires_at is not None:
                row.expires_at = expires_at
            return True

    def mark_replied(self, peer_id: str, reply_peer_id: str, replied_at: int) -> bool:
        row = self._rows.get(peer_id)
        if row is None:
            return False
        row.reply_peer_id = reply_peer_id
        row.replied_at = replied_at
        return True

    def find_unreplied(
        self, sender_session_id: str, receiver_session_id: str
    ) -> SessionPeerMessage | None:
        candidates = [
            r
            for r in self._rows.values()
            if r.sender_session_id == sender_session_id
            and r.receiver_session_id == receiver_session_id
            and r.replied_at is None
        ]
        candidates.sort(key=lambda r: r.created_at, reverse=True)
        return candidates[0] if candidates else None

    def count_for_ref(self, ref: str) -> int:
        return sum(1 for r in self._rows.values() if r.ref == ref)


class _FakeConversationStore:
    """Minimal conversation reads + literal search the sweeper needs."""

    def __init__(self) -> None:
        self.convs: dict[str, Conversation] = {}
        self.visible_text: dict[str, list[str]] = {}

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        return self.convs.get(conversation_id)

    def search_visible_items_literal(
        self, conversation_id: str, query: str, limit: int = 20
    ) -> list[str]:
        texts = self.visible_text.get(conversation_id, [])
        return [t for t in texts if query in t][:limit]


class _TrueStateScript:
    """Per-session scripted true state; defaults to idle.

    ``sequences`` (when set for a session id) is consumed one entry per
    call, falling back to ``states`` once exhausted — lets a test script a
    state that changes between two calls for the same session (the F2
    busy re-check race).
    """

    def __init__(self) -> None:
        self.states: dict[str, str] = {}
        self.sequences: dict[str, list[str]] = {}
        self.calls: list[str] = []

    async def __call__(self, conv: Conversation) -> tuple[str, bool | None]:
        self.calls.append(conv.id)
        # A real checkpoint, not just an `async def` with no internal
        # await — lets two "concurrent" callers (asyncio.gather) actually
        # interleave instead of one running to completion uninterrupted,
        # which a fake with no suspension point can never exercise.
        await asyncio.sleep(0)
        seq = self.sequences.get(conv.id)
        if seq:
            return seq.pop(0), True
        return self.states.get(conv.id, "idle"), True


class _DeliverScript:
    """Records calls; scriptable outcome or raise, keyed by receiver id.

    ``sequences`` (when set for a receiver id) is consumed one outcome per
    call before falling back to ``outcomes`` — lets a test script a
    transient failure that later succeeds (R3-C retry).
    """

    def __init__(self) -> None:
        self.outcomes: dict[str, tuple[str, str | None]] = {}
        self.sequences: dict[str, list[tuple[str, str | None]]] = {}
        self.raises: dict[str, BaseException] = {}
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self,
        request: Any,
        sender: Conversation,
        receiver: Conversation,
        ref: str,
        peer_id: str,
        text: str,
        *,
        acting_user_id: Any = None,
    ) -> tuple[str, str | None]:
        self.calls.append(
            {
                "sender": sender.id,
                "receiver": receiver.id,
                "ref": ref,
                "peer_id": peer_id,
                "text": text,
                "acting_user_id": acting_user_id,
            }
        )
        if receiver.id in self.raises:
            raise self.raises[receiver.id]
        seq = self.sequences.get(receiver.id)
        if seq:
            return seq.pop(0)
        return self.outcomes.get(receiver.id, ("delivered", None))


class _PostEventScript:
    """Records notice posts; can raise once for a given session id."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.raise_once_for: set[str] = set()

    async def __call__(
        self,
        request: Any,
        session_id: str,
        body: SessionEventInput,
        *,
        acting_user_id: Any = None,
    ) -> dict[str, Any]:
        if session_id in self.raise_once_for:
            self.raise_once_for.discard(session_id)
            raise RuntimeError("simulated notice-post failure")
        self.calls.append(
            {
                "session_id": session_id,
                "text": body.data["content"][0]["text"],
                "acting_user_id": acting_user_id,
            }
        )
        return {"queued": True}


class _Harness:
    """One conversation store + sweeper wired with the fakes above."""

    def __init__(self, now: int = 1000) -> None:
        self.store = _FakePeerStore()
        self.conv_store = _FakeConversationStore()
        self.true_state = _TrueStateScript()
        self.deliver = _DeliverScript()
        self.post_event = _PostEventScript()
        self._now = now
        self.sweeper = PeerSweeper(
            peer_store=self.store,
            conversation_store=cast(Any, self.conv_store),
            permission_store=None,
            true_state=self.true_state,
            deliver=self.deliver,
            post_event_impl=self.post_event,
            clock=lambda: self._now,
        )
        self.sweeper._app = _APP

    def add_conv(self, conv: Conversation) -> Conversation:
        self.conv_store.convs[conv.id] = conv
        return conv

    def seed_record(self, **overrides: Any) -> SessionPeerMessage:
        kwargs: dict[str, Any] = {
            "id": "peer_" + str(len(self.store._rows) + 1),
            "sender_session_id": "sender",
            "receiver_session_id": "receiver",
            "ref": "ref1",
            "text": "hello",
            "state": "pending",
            "created_at": self._now,
            "expires_at": self._now + 3600,
        }
        kwargs.update(overrides)
        record = SessionPeerMessage(**kwargs)
        self.store.seed(record)
        return record


@pytest.fixture()
def harness() -> _Harness:
    h = _Harness()
    h.add_conv(_conv("sender", title="Sender"))
    h.add_conv(_conv("receiver", title="Receiver"))
    return h


async def test_pending_delivers_when_receiver_turns_idle(harness: _Harness) -> None:
    record = harness.seed_record(state="pending")
    harness.true_state.states["receiver"] = "busy"
    await harness.sweeper._tick()
    assert _row(harness.store, record.id).state == "pending"
    assert harness.deliver.calls == []

    harness.true_state.states["receiver"] = "idle"
    await harness.sweeper._tick()
    assert _row(harness.store, record.id).state == "delivered"
    assert len(harness.deliver.calls) == 1
    assert harness.deliver.calls[0]["receiver"] == "receiver"
    assert harness.post_event.calls[0]["session_id"] == "sender"
    assert "delivered" in harness.post_event.calls[0]["text"]
    assert record.id in harness.post_event.calls[0]["text"]


async def test_queued_delivers(harness: _Harness) -> None:
    record = harness.seed_record(state="queued")
    await harness.sweeper._tick()
    assert _row(harness.store, record.id).state == "delivered"
    assert len(harness.deliver.calls) == 1


async def test_expiry_transitions_and_notifies(harness: _Harness) -> None:
    record = harness.seed_record(state="pending", expires_at=999)
    await harness.sweeper._tick()
    assert _row(harness.store, record.id).state == "expired"
    assert harness.deliver.calls == []
    assert "expired" in harness.post_event.calls[0]["text"]


async def test_closed_receiver_fails_with_notice() -> None:
    h = _Harness()
    h.add_conv(_conv("sender", title="Sender"))
    h.add_conv(_conv("receiver", title="Receiver", labels={"omnigent.closed": "true"}))
    record = h.seed_record(state="pending")
    await h.sweeper._tick()
    updated = _row(h.store, record.id)
    assert updated.state == "failed"
    assert updated.reason == "closed"
    assert h.deliver.calls == []
    assert "(closed)" in h.post_event.calls[0]["text"]


async def test_held_record_expires_past_expiry(harness: _Harness) -> None:
    """A held record past its expiry expires with a notice, same as pending."""
    record = harness.seed_record(state="held", expires_at=999)
    await harness.sweeper._tick()
    assert _row(harness.store, record.id).state == "expired"
    assert harness.deliver.calls == []
    assert "expired" in harness.post_event.calls[0]["text"]


async def test_held_record_before_expiry_untouched(harness: _Harness) -> None:
    """A held record before its expiry is never delivered by the sweeper."""
    record = harness.seed_record(state="held")
    await harness.sweeper._tick()
    assert _row(harness.store, record.id).state == "held"
    assert harness.deliver.calls == []
    assert harness.post_event.calls == []


async def test_busy_receiver_left_untouched(harness: _Harness) -> None:
    record = harness.seed_record(state="pending")
    harness.true_state.states["receiver"] = "busy"
    await harness.sweeper._tick()
    assert _row(harness.store, record.id).state == "pending"
    assert harness.deliver.calls == []
    assert harness.post_event.calls == []


async def test_delivery_exception_reverts_record_and_tick_continues(harness: _Harness) -> None:
    """An unexpected raise from ``_deliver`` reverts (R3-C), it doesn't fail terminally."""
    harness.add_conv(_conv("receiver2", title="Receiver Two"))
    boom = harness.seed_record(id="peer_boom", receiver_session_id="receiver", ref="r-boom")
    ok = harness.seed_record(
        id="peer_ok", receiver_session_id="receiver2", ref="r-ok", expires_at=harness._now + 10
    )
    harness.deliver.raises["receiver"] = RuntimeError("boom")
    await harness.sweeper._tick()
    assert _row(harness.store, boom.id).state == boom.state
    assert _row(harness.store, boom.id).reason == "not_ready"
    assert _row(harness.store, ok.id).state == "delivered"
    assert {c["receiver"] for c in harness.deliver.calls} == {"receiver", "receiver2"}
    assert not any("peer_boom" in c["text"] for c in harness.post_event.calls)


async def test_busy_recheck_before_deliver_reverts_without_notice(harness: _Harness) -> None:
    """A receiver that goes busy between the CAS and ``_deliver`` is reverted, not delivered."""
    record = harness.seed_record(state="pending")
    harness.true_state.sequences["receiver"] = ["idle", "busy"]
    await harness.sweeper._tick()
    assert _row(harness.store, record.id).state == "pending"
    assert harness.deliver.calls == []
    assert harness.post_event.calls == []


async def test_transient_failure_retries_then_delivers(harness: _Harness) -> None:
    """R3-C: a failed delivery reverts for the next tick's retry, no failed notice."""
    record = harness.seed_record(state="pending")
    harness.deliver.sequences["receiver"] = [("failed", "offline")]
    await harness.sweeper._tick()
    assert _row(harness.store, record.id).state == "pending"
    assert harness.post_event.calls == []

    await harness.sweeper._tick()
    assert _row(harness.store, record.id).state == "delivered"
    assert len(harness.post_event.calls) == 1
    assert "delivered" in harness.post_event.calls[0]["text"]
    assert "failed" not in harness.post_event.calls[0]["text"]


async def test_transient_failure_until_expiry_expires_with_one_notice(harness: _Harness) -> None:
    """R3-C: a delivery that keeps failing still ends at its own expiry."""
    record = harness.seed_record(state="pending", expires_at=harness._now + 1)
    harness.deliver.outcomes["receiver"] = ("failed", "not_ready")
    await harness.sweeper._tick()
    assert _row(harness.store, record.id).state == "pending"
    assert harness.post_event.calls == []

    harness._now += 1
    await harness.sweeper._tick()
    assert _row(harness.store, record.id).state == "expired"
    assert len(harness.post_event.calls) == 1
    assert "expired" in harness.post_event.calls[0]["text"]


async def test_two_concurrent_flushes_post_once(harness: _Harness) -> None:
    """F6: two concurrent flush attempts for one sender post exactly once."""
    record = harness.seed_record(state="pending")
    harness.true_state.states["sender"] = "busy"
    await harness.sweeper._tick()
    assert _row(harness.store, record.id).state == "delivered"
    assert len(harness.sweeper._parked["sender"]) == 1

    harness.true_state.states["sender"] = "idle"
    sender = harness.conv_store.convs["sender"]
    await asyncio.gather(
        harness.sweeper._maybe_flush(sender, _APP),
        harness.sweeper._maybe_flush(sender, _APP),
    )
    assert len(harness.post_event.calls) == 1
    assert harness.sweeper._parked["sender"] == []


async def test_concurrent_ticks_deliver_exactly_once(harness: _Harness) -> None:
    record = harness.seed_record(state="pending")
    now = harness._now
    # Two independent snapshots, as two real ticks would each get from
    # their own ``list_due`` query — never the exact same mutable object,
    # so the CAS race is genuine rather than an artifact of shared state.
    snapshot_a = dataclasses.replace(record)
    snapshot_b = dataclasses.replace(record)
    await asyncio.gather(
        harness.sweeper._process_due(snapshot_a, now),
        harness.sweeper._process_due(snapshot_b, now),
    )
    assert _row(harness.store, record.id).state == "delivered"
    assert len(harness.deliver.calls) == 1


async def test_notices_to_busy_sender_park_and_flush_as_one_message() -> None:
    h = _Harness()
    h.add_conv(_conv("sender", title="Sender"))
    h.add_conv(_conv("receiver", title="Receiver"))
    h.add_conv(_conv("receiver2", title="Receiver Two"))
    h.true_state.states["sender"] = "busy"
    r1 = h.seed_record(id="peer_1", receiver_session_id="receiver", ref="ref-1")
    r2 = h.seed_record(
        id="peer_2", receiver_session_id="receiver2", ref="ref-2", expires_at=h._now + 10
    )
    await h.sweeper._tick()
    assert _row(h.store, r1.id).state == "delivered"
    assert _row(h.store, r2.id).state == "delivered"
    assert h.post_event.calls == []  # sender busy — both parked, nothing posted yet
    assert len(h.sweeper._parked["sender"]) == 2

    h.true_state.states["sender"] = "idle"
    await h.sweeper._tick()  # no new due records; the outstanding-parked pass flushes
    assert len(h.post_event.calls) == 1
    text = h.post_event.calls[0]["text"]
    assert text.count("[System: peer message") == 2
    assert h.sweeper._parked.get("sender") == []


async def test_closed_sender_gets_no_notice() -> None:
    h = _Harness()
    h.add_conv(_conv("sender", title="Sender", labels={"omnigent.closed": "true"}))
    h.add_conv(_conv("receiver", title="Receiver"))
    record = h.seed_record(state="pending")
    await h.sweeper._tick()
    assert _row(h.store, record.id).state == "delivered"
    assert h.post_event.calls == []
    assert h.sweeper._parked == {}


async def test_notice_post_failure_reparks_for_a_later_attempt(harness: _Harness) -> None:
    """A posting failure re-parks rather than dropping the notice.

    Calls ``_notify_for``/``_maybe_flush`` directly (not ``_tick()``,
    whose own outstanding-parked pass would immediately retry the flush
    within the same tick and mask a re-park bug).
    """
    record = harness.seed_record(state="pending")
    sender = harness.conv_store.convs["sender"]
    harness.post_event.raise_once_for.add("sender")
    await harness.sweeper._notify_for(record, "delivered", None, "Receiver", _APP)
    assert harness.post_event.calls == []
    assert len(harness.sweeper._parked["sender"]) == 1

    await harness.sweeper._maybe_flush(sender, _APP)
    assert len(harness.post_event.calls) == 1
    assert harness.sweeper._parked["sender"] == []


async def test_startup_reconciliation_marker_found_is_delivered() -> None:
    h = _Harness()
    h.add_conv(_conv("sender", title="Sender"))
    h.add_conv(_conv("receiver", title="Receiver"))
    record = h.seed_record(
        id="peer_deliv", state="delivering", ref="ref-found", updated_at=h._now - 200
    )
    h.conv_store.visible_text["receiver"] = [
        f"[Peer message ...] ref=ref-found msg={record.id} ..."
    ]
    await h.sweeper._reconcile_startup()
    updated = _row(h.store, record.id)
    assert updated.state == "delivered"
    assert "delivered" in h.post_event.calls[0]["text"]


async def test_startup_reconciliation_marker_missing_reverts_to_pending() -> None:
    h = _Harness()
    h.add_conv(_conv("sender", title="Sender"))
    h.add_conv(_conv("receiver", title="Receiver"))
    future_expiry = h._now + 500
    still_future = h.seed_record(
        id="peer_future",
        state="delivering",
        ref="ref-future",
        expires_at=future_expiry,
        updated_at=h._now - 200,
    )
    already_past = h.seed_record(
        id="peer_past",
        state="delivering",
        ref="ref-past",
        expires_at=h._now - 10,
        updated_at=h._now - 200,
    )
    await h.sweeper._reconcile_startup()
    assert _row(h.store, still_future.id).state == "pending"
    assert _row(h.store, still_future.id).expires_at == future_expiry
    assert _row(h.store, already_past.id).state == "pending"
    assert _row(h.store, already_past.id).expires_at == h._now + 120
    assert h.post_event.calls == []  # reverting to pending is not a notice-worthy state


async def test_startup_reconciliation_skips_recent_delivering_record() -> None:
    """A ``delivering`` record younger than the grace is left untouched at startup."""
    h = _Harness()
    h.add_conv(_conv("sender", title="Sender"))
    h.add_conv(_conv("receiver", title="Receiver"))
    record = h.seed_record(
        id="peer_recent", state="delivering", ref="ref-recent", updated_at=h._now - 10
    )
    await h.sweeper._reconcile_startup()
    assert _row(h.store, record.id).state == "delivering"
    assert h.post_event.calls == []


async def test_tick_reconciles_delivering_record_once_grace_elapses() -> None:
    """A tick (not just startup) reconciles a ``delivering`` record past the grace."""
    h = _Harness()
    h.add_conv(_conv("sender", title="Sender"))
    h.add_conv(_conv("receiver", title="Receiver"))
    record = h.seed_record(
        id="peer_aged", state="delivering", ref="ref-aged", updated_at=h._now - 130
    )
    h.conv_store.visible_text["receiver"] = [
        f"[Peer message ...] ref=ref-aged msg={record.id} ..."
    ]
    await h.sweeper._tick()
    assert _row(h.store, record.id).state == "delivered"
