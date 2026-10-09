"""Unit tests for :class:`~omnigent.server.peer_sweeper.PeerSweeper`.

Fakes for the store, true-state, delivery, and back-notice posting; every
test drives ``_tick()`` (or ``_reconcile_startup()``) directly rather than
the sleep loop, with an injected clock for expiry control.
"""

from __future__ import annotations

import asyncio
import dataclasses
import threading
import types
import uuid
from typing import Any, cast

import pytest

from omnigent.db.utils import now_epoch
from omnigent.entities import SessionPeerMessage
from omnigent.entities.conversation import Conversation, ConversationItem, MessageData
from omnigent.server.peer_sweeper import PeerSweeper
from omnigent.server.schemas import SessionEventInput
from omnigent.stores.peer_message_store import PeerMessageStore
from omnigent.stores.peer_message_store.sqlalchemy_store import SqlAlchemyPeerMessageStore

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


def _envelope(record: SessionPeerMessage, *, sender: str = "a" * 32) -> str:
    """A valid current-format envelope carrying *record*'s ``msg=`` marker."""
    return (
        f'[Peer message from session {sender} msg={record.id} "Title" '
        f"(agent) ref={record.ref}]\n\nbody"
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
        self,
        session_id: str,
        states: tuple[str, ...] | None = None,
        limit: int = 20,
        *,
        sender_session_id: str | None = None,
        oldest_first: bool = False,
    ) -> list[SessionPeerMessage]:
        rows = [r for r in self._rows.values() if r.receiver_session_id == session_id]
        if states is not None:
            rows = [r for r in rows if r.state in states]
        if sender_session_id is not None:
            rows = [r for r in rows if r.sender_session_id == sender_session_id]
        rows.sort(key=lambda r: (r.created_at, r.id), reverse=not oldest_first)
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
        relay_depth: int | None = None,
        notice: bool = False,
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
            if relay_depth is not None:
                row.relay_depth = relay_depth
            if notice:
                row.notice_owed_at = now_epoch()
            return True

    def list_notice_owed(self) -> list[SessionPeerMessage]:
        rows = [r for r in self._rows.values() if r.notice_owed_at is not None]
        rows.sort(key=lambda r: (r.notice_owed_at or 0, r.id))
        return rows

    def claim_notice(self, peer_id: str) -> bool:
        with self._lock:
            row = self._rows.get(peer_id)
            if row is None or row.notice_owed_at is None:
                return False
            row.notice_owed_at = None
            return True

    def set_notice_owed(self, peer_id: str, owed_at: int) -> None:
        with self._lock:
            row = self._rows.get(peer_id)
            if row is not None:
                row.notice_owed_at = owed_at

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

    def retarget_receiver(
        self,
        old_receiver_id: str,
        new_receiver_id: str,
        states: tuple[str, ...],
    ) -> list[str]:
        moved: list[str] = []
        for row in self._rows.values():
            if row.receiver_session_id == old_receiver_id and row.state in states:
                row.receiver_session_id = new_receiver_id
                moved.append(row.id)
        return moved

    def find_sent(
        self,
        sender_session_id: str,
        receiver_session_id: str,
        ref_or_id: str,
        created_after: int,
    ) -> SessionPeerMessage | None:
        candidates = [
            r
            for r in self._rows.values()
            if r.sender_session_id == sender_session_id
            and r.receiver_session_id == receiver_session_id
            and (r.ref == ref_or_id or r.id == ref_or_id)
            and r.created_at >= created_after
        ]
        candidates.sort(key=lambda r: (r.created_at, r.id), reverse=True)
        return candidates[0] if candidates else None

    def find_replied_by(self, reply_peer_id: str) -> SessionPeerMessage | None:
        return next((r for r in self._rows.values() if r.reply_peer_id == reply_peer_id), None)

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
    ) -> list[ConversationItem]:
        # Mirror the real store: each hit is a decoded conversation item, so
        # the sweeper's marker validation reads role/content, not raw text.
        hits = [t for t in self.visible_text.get(conversation_id, []) if query in t][:limit]
        return [
            ConversationItem(
                id=f"item_{i}",
                type="message",
                status="completed",
                response_id="resp",
                created_at=0,
                data=MessageData(role="user", content=[{"type": "input_text", "text": text}]),
            )
            for i, text in enumerate(hits)
        ]


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
        self.receiver_args: list[bool] = []
        self.raise_once_for: set[str] = set()

    async def __call__(
        self, conv: Conversation, *, receiver: bool = False
    ) -> tuple[str, bool | None]:
        self.calls.append(conv.id)
        self.receiver_args.append(receiver)
        # A real checkpoint, not just an `async def` with no internal
        # await — lets two "concurrent" callers (asyncio.gather) actually
        # interleave instead of one running to completion uninterrupted,
        # which a fake with no suspension point can never exercise.
        await asyncio.sleep(0)
        if conv.id in self.raise_once_for:
            self.raise_once_for.discard(conv.id)
            raise RuntimeError("simulated true_state read failure")
        seq = self.sequences.get(conv.id)
        state = seq.pop(0) if seq else self.states.get(conv.id, "idle")
        # Mirror routes_peer._true_state(receiver=True): a child is never steered.
        if receiver and conv.parent_conversation_id is not None and state == "steerable":
            state = "busy"
        return state, True


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


class _FakePrefsStore:
    """Preferences stub returning one ``session_collab`` namespace."""

    def __init__(self, **collab: Any) -> None:
        self.collab = collab

    def get(self, owner: str) -> dict[str, Any]:
        del owner
        return {"settings": {"session_collab": self.collab}}


def _app_with_collab(**collab: Any) -> Any:
    """App-state stand-in whose preferences store carries *collab*."""
    app = types.SimpleNamespace()
    app.state = types.SimpleNamespace(user_preferences_store=_FakePrefsStore(**collab))
    return app


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
    assert harness.post_event.calls == []


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


@pytest.mark.parametrize("state", ["pending", "queued"])
async def test_inbound_hold_transitions_deferred_records_to_held(state: str) -> None:
    """A hold receiver's deferred record becomes held, like the inline path."""
    h = _Harness()
    h.add_conv(_conv("sender", title="Sender"))
    h.add_conv(_conv("receiver", title="Receiver", labels={"peer_inbound": "hold"}))
    record = h.seed_record(state=state)
    await h.sweeper._tick()
    updated = _row(h.store, record.id)
    assert updated.state == "held"
    assert updated.reason is None
    assert h.deliver.calls == []
    assert h.post_event.calls == []


async def test_released_record_delivers_despite_inbound_hold() -> None:
    """A user-released record is not re-held while the receiver stays on hold."""
    h = _Harness()
    h.add_conv(_conv("sender", title="Sender"))
    h.add_conv(_conv("receiver", title="Receiver", labels={"peer_inbound": "hold"}))
    record = h.seed_record(state="held")
    # The action route's release transition: held -> pending, reason "released".
    assert h.store.transition(record.id, "pending", "released", ("held",), relay_depth=0)
    await h.sweeper._tick()
    updated = _row(h.store, record.id)
    assert updated.state == "delivered"
    assert len(h.deliver.calls) == 1
    assert h.post_event.calls == []


async def test_master_switch_off_fails_deferred_record_with_notice() -> None:
    """A disabled sender owner ends the record as failed(collab_disabled)."""
    h = _Harness()
    h.add_conv(_conv("sender", title="Sender"))
    h.add_conv(_conv("receiver", title="Receiver"))
    h.sweeper._app = _app_with_collab(enabled=False)
    record = h.seed_record(state="pending")
    await h.sweeper._tick()
    updated = _row(h.store, record.id)
    assert updated.state == "failed"
    assert updated.reason == "collab_disabled"
    assert h.deliver.calls == []
    assert "(collab_disabled)" in h.post_event.calls[0]["text"]


async def test_master_switch_on_still_delivers() -> None:
    """An enabled sender owner with no hold delivers as before."""
    h = _Harness()
    h.add_conv(_conv("sender", title="Sender"))
    h.add_conv(_conv("receiver", title="Receiver"))
    h.sweeper._app = _app_with_collab(enabled=True)
    record = h.seed_record(state="pending")
    await h.sweeper._tick()
    assert _row(h.store, record.id).state == "delivered"
    assert len(h.deliver.calls) == 1


async def test_not_before_gates_delivery(harness: _Harness) -> None:
    """A queued record waits for its slot, then delivers on a later tick."""
    record = harness.seed_record(state="queued", not_before=harness._now + 30)
    await harness.sweeper._tick()
    assert _row(harness.store, record.id).state == "queued"
    assert harness.deliver.calls == []
    assert harness.post_event.calls == []

    harness._now += 30
    await harness.sweeper._tick()
    assert _row(harness.store, record.id).state == "delivered"
    assert len(harness.deliver.calls) == 1


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


async def test_released_record_revert_keeps_marker_and_delivers_next_tick() -> None:
    """A released record abandoned by the busy re-check keeps its marker.

    The revert must not overwrite ``reason == "released"`` with the revert
    cause, or the next sweep re-holds it under the receiver's hold policy.
    """
    h = _Harness()
    h.add_conv(_conv("sender", title="Sender"))
    h.add_conv(_conv("receiver", title="Receiver", labels={"peer_inbound": "hold"}))
    record = h.seed_record(state="held")
    assert h.store.transition(record.id, "pending", "released", ("held",), relay_depth=0)
    # First tick: CAS to delivering, then the busy re-check abandons the attempt.
    h.true_state.sequences["receiver"] = ["idle", "busy"]
    await h.sweeper._tick()
    updated = _row(h.store, record.id)
    assert updated.state == "pending"
    assert updated.reason == "released"
    assert h.deliver.calls == []

    # Second tick: not re-held despite the receiver's hold policy; it delivers.
    await h.sweeper._tick()
    updated = _row(h.store, record.id)
    assert updated.state == "delivered"
    assert len(h.deliver.calls) == 1
    assert h.post_event.calls == []


async def test_transient_failure_retries_then_delivers(harness: _Harness) -> None:
    """R3-C: a failed delivery reverts for the next tick's retry, no failed notice."""
    record = harness.seed_record(state="pending")
    harness.deliver.sequences["receiver"] = [("failed", "offline")]
    await harness.sweeper._tick()
    assert _row(harness.store, record.id).state == "pending"
    assert harness.post_event.calls == []

    await harness.sweeper._tick()
    assert _row(harness.store, record.id).state == "delivered"
    assert harness.post_event.calls == []


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


async def test_uncertain_delivery_leaves_record_delivering_without_notice(
    harness: _Harness,
) -> None:
    """X2: an uncertain outcome must not be retried blindly.

    Leaves the record in ``delivering`` (no revert, no terminal
    transition), posts no notice, and the next tick does not call
    ``_deliver`` again for it (it's no longer due).
    """
    record = harness.seed_record(state="pending")
    harness.deliver.outcomes["receiver"] = ("uncertain", "not_ready")
    await harness.sweeper._tick()
    assert _row(harness.store, record.id).state == "delivering"
    assert harness.post_event.calls == []

    calls_before = len(harness.deliver.calls)
    await harness.sweeper._tick()
    assert len(harness.deliver.calls) == calls_before
    assert _row(harness.store, record.id).state == "delivering"


async def test_uncertain_delivery_reconciles_delivered_via_marker_after_grace(
    harness: _Harness,
) -> None:
    """X2: past the grace, a marker in the receiver's transcript settles
    an uncertain delivery as delivered, without a notice."""
    record = harness.seed_record(id="1" * 32, state="pending")
    harness.deliver.outcomes["receiver"] = ("uncertain", "not_ready")
    await harness.sweeper._tick()
    assert _row(harness.store, record.id).state == "delivering"

    harness.conv_store.visible_text["receiver"] = [_envelope(record)]
    harness._now += 130
    await harness.sweeper._tick()
    updated = _row(harness.store, record.id)
    assert updated.state == "delivered"
    assert harness.post_event.calls == []


async def test_uncertain_delivery_reconciles_to_pending_without_marker(
    harness: _Harness,
) -> None:
    """X2: past the grace, no marker reverts to pending; a later tick retries
    and delivers exactly once."""
    record = harness.seed_record(state="pending")
    harness.deliver.sequences["receiver"] = [("uncertain", "not_ready"), ("delivered", None)]
    await harness.sweeper._tick()
    assert _row(harness.store, record.id).state == "delivering"

    harness._now += 130
    await harness.sweeper._tick()
    updated = _row(harness.store, record.id)
    assert updated.state == "pending"
    assert updated.expires_at == harness._now - 130 + 3600
    assert harness.post_event.calls == []

    await harness.sweeper._tick()
    assert _row(harness.store, record.id).state == "delivered"
    assert harness.post_event.calls == []


async def test_uncertain_delivery_past_expiry_expires_with_notice(
    harness: _Harness,
) -> None:
    """An uncertain delivery past its deadline ends as expired, not re-pended."""
    record = harness.seed_record(
        state="delivering",
        updated_at=harness._now - 200,
        expires_at=harness._now - 10,
    )
    await harness.sweeper._reconcile_stale_delivering(harness._now)
    assert _row(harness.store, record.id).state == "expired"
    assert len(harness.post_event.calls) == 1
    assert "expired" in harness.post_event.calls[0]["text"]


async def test_two_concurrent_flushes_post_once(harness: _Harness) -> None:
    """F6: two concurrent flush attempts for one sender post exactly once."""
    record = harness.seed_record(state="pending")
    harness.store.set_notice_owed(record.id, harness._now)
    harness.true_state.states["sender"] = "busy"
    await harness.sweeper._notify_for(record, "delivered", None, "Receiver", _APP)
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
    h.store.set_notice_owed(r1.id, h._now)
    h.store.set_notice_owed(r2.id, h._now)
    await h.sweeper._notify_for(r1, "delivered", None, "Receiver", _APP)
    await h.sweeper._notify_for(r2, "delivered", None, "Receiver Two", _APP)
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
    harness.store.set_notice_owed(record.id, harness._now)
    harness.post_event.raise_once_for.add("sender")
    await harness.sweeper._notify_for(record, "delivered", None, "Receiver", _APP)
    assert harness.post_event.calls == []
    assert len(harness.sweeper._parked["sender"]) == 1

    await harness.sweeper._maybe_flush(sender, _APP)
    assert len(harness.post_event.calls) == 1
    assert harness.sweeper._parked["sender"] == []


async def test_true_state_exception_during_flush_reparks_for_a_later_attempt(
    harness: _Harness,
) -> None:
    """X3: an exception between the swap-out and the post (the fresh
    ``_true_state`` read) must not drop the parked lines — same restore as
    a post failure. The next flush posts once."""
    record = harness.seed_record(state="pending")
    sender = harness.conv_store.convs["sender"]
    harness.store.set_notice_owed(record.id, harness._now)
    harness.true_state.raise_once_for.add("sender")
    await harness.sweeper._notify_for(record, "delivered", None, "Receiver", _APP)
    assert harness.post_event.calls == []
    assert len(harness.sweeper._parked["sender"]) == 1

    await harness.sweeper._maybe_flush(sender, _APP)
    assert len(harness.post_event.calls) == 1
    assert harness.sweeper._parked["sender"] == []


async def test_flush_cancelled_during_true_state_reparks_and_reraises(
    harness: _Harness,
) -> None:
    """A cancel in the swap-to-post window restores the lines, then re-raises."""
    record = harness.seed_record(state="pending")
    sender = harness.conv_store.convs["sender"]

    async def _cancelled(_conv: Conversation) -> tuple[str, bool | None]:
        raise asyncio.CancelledError

    harness.sweeper._true_state = _cancelled  # type: ignore[method-assign]
    harness.sweeper._parked[sender.id] = [(None, "notice-1")]
    with pytest.raises(asyncio.CancelledError):
        await harness.sweeper._maybe_flush(sender, _APP)
    assert harness.sweeper._parked[sender.id] == [(None, "notice-1")]
    assert record.id  # keep linters honest about the seeded row


async def test_startup_reconciliation_marker_found_is_delivered() -> None:
    h = _Harness()
    h.add_conv(_conv("sender", title="Sender"))
    h.add_conv(_conv("receiver", title="Receiver"))
    record = h.seed_record(
        id="2" * 32, state="delivering", ref="ref-found", updated_at=h._now - 200
    )
    h.conv_store.visible_text["receiver"] = [_envelope(record)]
    await h.sweeper._reconcile_startup()
    updated = _row(h.store, record.id)
    assert updated.state == "delivered"
    assert h.post_event.calls == []


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
    assert _row(h.store, already_past.id).state == "expired"
    assert len(h.post_event.calls) == 1
    assert "expired" in h.post_event.calls[0]["text"]


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
        id="3" * 32, state="delivering", ref="ref-aged", updated_at=h._now - 130
    )
    h.conv_store.visible_text["receiver"] = [_envelope(record)]
    await h.sweeper._tick()
    assert _row(h.store, record.id).state == "delivered"


async def test_rejected_delivery_ends_failed_without_retry(harness: _Harness) -> None:
    record = harness.seed_record(state="pending")
    harness.deliver.outcomes["receiver"] = ("rejected", "not_forwarded")
    await harness.sweeper._tick()
    assert _row(harness.store, record.id).state == "failed"
    assert _row(harness.store, record.id).reason == "not_forwarded"
    await harness.sweeper._tick()
    assert len(harness.deliver.calls) == 1


async def test_deferred_rechecks_inbound_refuse_even_when_held(harness: _Harness) -> None:
    record = harness.seed_record(state="held")
    harness.add_conv(_conv("receiver", labels={"peer_inbound": "refuse"}))
    await harness.sweeper._tick()
    assert _row(harness.store, record.id).state == "refused_by_user"
    assert _row(harness.store, record.id).reason == "receiver_refuses"


async def test_deferred_recheck_exempts_a_correlated_reply() -> None:
    """A reply on a thread the refusing receiver started is delivered."""
    h = _Harness(now=1_000_000)
    h.add_conv(_conv("sender", title="Sender"))
    h.add_conv(_conv("receiver", title="Receiver", labels={"peer_inbound": "refuse"}))
    h.seed_record(
        id="peer_original",
        sender_session_id="receiver",
        receiver_session_id="sender",
        ref="cid-1",
        state="delivered",
        created_at=h._now - 1000,
    )
    reply = h.seed_record(
        id="peer_reply",
        sender_session_id="sender",
        receiver_session_id="receiver",
        ref="cid-1",
        state="pending",
    )
    await h.sweeper._tick()
    assert _row(h.store, reply.id).state == "delivered"
    assert len(h.deliver.calls) == 1


async def test_deferred_recheck_refuses_a_correlation_past_the_ttl() -> None:
    """A correlation older than the refuser's ttl does not exempt delivery."""
    h = _Harness(now=1_000_000)
    h.add_conv(_conv("sender", title="Sender"))
    h.add_conv(_conv("receiver", title="Receiver", labels={"peer_inbound": "refuse"}))
    h.seed_record(
        id="peer_original",
        sender_session_id="receiver",
        receiver_session_id="sender",
        ref="cid-old",
        state="delivered",
        created_at=h._now - 90_000,
    )
    reply = h.seed_record(
        id="peer_reply",
        sender_session_id="sender",
        receiver_session_id="receiver",
        ref="cid-old",
        state="pending",
    )
    await h.sweeper._tick()
    assert _row(h.store, reply.id).state == "refused_by_user"
    assert _row(h.store, reply.id).reason == "receiver_refuses"
    assert h.deliver.calls == []


async def test_deferred_rechecks_owner(harness: _Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    from omnigent.server import peer_sweeper

    record = harness.seed_record(state="pending")
    harness.sweeper._permission_store = cast(Any, object())
    monkeypatch.setattr(peer_sweeper, "effective_owner_id", lambda conv, *_: conv.id)
    await harness.sweeper._tick()
    assert _row(harness.store, record.id).state == "failed"
    assert _row(harness.store, record.id).reason == "not_same_owner"
    assert harness.deliver.calls == []


async def test_notify_line_uses_sender_notice_path(harness: _Harness) -> None:
    await harness.sweeper.notify_line("sender", "peer result ready")
    assert harness.post_event.calls[0]["session_id"] == "sender"
    assert harness.post_event.calls[0]["text"] == "peer result ready"


async def test_steerable_receiver_delivers(harness: _Harness) -> None:
    """A busy-but-steerable receiver takes a due record without interrupting."""
    record = harness.seed_record(state="queued")
    harness.true_state.states["receiver"] = "steerable"
    await harness.sweeper._tick()
    assert _row(harness.store, record.id).state == "delivered"
    assert len(harness.deliver.calls) == 1
    assert harness.post_event.calls == []


async def test_steerable_child_receiver_stays_queued(harness: _Harness) -> None:
    """A busy steerable child receiver is never steered: it stays queued."""
    child = dataclasses.replace(
        harness.add_conv(_conv("child", title="Child")), parent_conversation_id="parent"
    )
    harness.conv_store.convs["child"] = child
    record = harness.seed_record(receiver_session_id="child", state="queued")
    harness.true_state.states["child"] = "steerable"
    await harness.sweeper._tick()
    assert _row(harness.store, record.id).state == "queued"
    assert harness.deliver.calls == []
    assert True in harness.true_state.receiver_args


async def test_older_queued_record_holds_newer_from_same_sender(harness: _Harness) -> None:
    """Per-pair FIFO: a newer record waits for the sender's older one."""
    older = harness.seed_record(
        id="peer_old",
        state="queued",
        not_before=harness._now + 30,
        created_at=harness._now - 5,
    )
    newer = harness.seed_record(id="peer_new", state="queued", created_at=harness._now - 1)
    await harness.sweeper._tick()
    assert _row(harness.store, older.id).state == "queued"
    assert _row(harness.store, newer.id).state == "queued"
    assert harness.deliver.calls == []

    harness._now += 30
    await harness.sweeper._tick()
    assert _row(harness.store, older.id).state == "delivered"
    assert _row(harness.store, newer.id).state == "delivered"


async def test_older_delivering_record_holds_newer_from_same_sender(harness: _Harness) -> None:
    """An in-flight delivering record blocks this pair's newer message too."""
    older = harness.seed_record(
        id="peer_inflight", state="delivering", created_at=harness._now - 5
    )
    newer = harness.seed_record(id="peer_newer", state="queued", created_at=harness._now - 1)
    await harness.sweeper._tick()
    assert _row(harness.store, older.id).state == "delivering"
    assert _row(harness.store, newer.id).state == "queued"
    assert harness.deliver.calls == []


async def test_older_delivering_record_found_behind_55_newer_same_pair(
    harness: _Harness,
) -> None:
    """FIFO finds the oldest record even behind 50+ newer same-pair records."""
    older = harness.seed_record(
        id="peer_oldest", state="delivering", created_at=harness._now - 100
    )
    fillers = [
        harness.seed_record(
            id=f"peer_filler_{index}",
            state="queued",
            created_at=harness._now - 55 + index,
        )
        for index in range(55)
    ]
    await harness.sweeper._tick()
    assert _row(harness.store, older.id).state == "delivering"
    assert all(_row(harness.store, record.id).state == "queued" for record in fillers)
    assert harness.deliver.calls == []


async def test_older_record_from_another_sender_does_not_hold(harness: _Harness) -> None:
    """FIFO is per pair: another sender's backlog never blocks this one."""
    harness.add_conv(_conv("other", title="Other Sender"))
    harness.seed_record(
        id="peer_other",
        sender_session_id="other",
        state="queued",
        not_before=harness._now + 30,
        created_at=harness._now - 5,
    )
    newer = harness.seed_record(id="peer_mine", state="queued", created_at=harness._now - 1)
    await harness.sweeper._tick()
    assert _row(harness.store, newer.id).state == "delivered"
    assert len(harness.deliver.calls) == 1


async def test_notice_flushes_to_steerable_sender(harness: _Harness) -> None:
    """A parked notice posts once the sender reads steerable, not idle."""
    record = harness.seed_record(state="pending")
    harness.store.set_notice_owed(record.id, harness._now)
    harness.true_state.states["sender"] = "steerable"
    await harness.sweeper._notify_for(record, "expired", None, "Receiver", _APP)
    assert len(harness.post_event.calls) == 1
    assert harness.sweeper._parked.get("sender") == []


async def test_reconcile_ignores_a_non_envelope_marker_hit(harness: _Harness) -> None:
    """A literal ``msg=`` hit that is not this record's envelope is ignored."""
    record = harness.seed_record(state="delivering", updated_at=harness._now - 200)
    harness.conv_store.visible_text["receiver"] = [
        f'[Peer message from session {"a" * 32} msg={"b" * 32} "T" (agent) ref=r]\n\n'
        f"quotes msg={record.id} in the body"
    ]
    await harness.sweeper._reconcile_startup()
    assert _row(harness.store, record.id).state == "pending"


async def test_reconcile_ignores_a_truncated_envelope_prefix(harness: _Harness) -> None:
    """A truncated current-format prefix is not this record's envelope."""
    record = harness.seed_record(id="4" * 32, state="delivering", updated_at=harness._now - 200)
    harness.conv_store.visible_text["receiver"] = [
        f'[Peer message from session {"a" * 32} msg={record.id} "'
    ]
    await harness.sweeper._reconcile_startup()
    assert _row(harness.store, record.id).state == "pending"


async def test_reconcile_leaves_unmarked_delivering_while_receiver_running(
    harness: _Harness,
) -> None:
    """A running receiver's queued prompt reaches the transcript only at the
    next tool boundary; reverting now would re-send a copy still on its way."""
    receiver = harness.conv_store.convs["receiver"]
    harness.conv_store.convs["receiver"] = dataclasses.replace(receiver, live_status="running")
    record = harness.seed_record(state="delivering", updated_at=harness._now - 200)
    await harness.sweeper._reconcile_startup()
    assert _row(harness.store, record.id).state == "delivering"

    harness.conv_store.convs["receiver"] = dataclasses.replace(receiver, live_status=None)
    await harness.sweeper._reconcile_startup()
    assert _row(harness.store, record.id).state == "pending"


def _store_sweeper(
    store: SqlAlchemyPeerMessageStore,
    conv_store: _FakeConversationStore,
    true_state: _TrueStateScript,
    deliver: _DeliverScript,
    post_event: _PostEventScript,
    now: int = 1000,
) -> PeerSweeper:
    """A sweeper over the real SQL store, sharing the fakes across restarts."""
    sweeper = PeerSweeper(
        peer_store=store,
        conversation_store=cast(Any, conv_store),
        permission_store=None,
        true_state=true_state,
        deliver=deliver,
        post_event_impl=post_event,
        clock=lambda: now,
    )
    sweeper._app = _APP
    return sweeper


async def test_owed_notice_survives_restart_and_posts_exactly_once(db_uri: str) -> None:
    """A back-notice parked before a restart replays once, then never again."""
    store = SqlAlchemyPeerMessageStore(db_uri)
    conv_store = _FakeConversationStore()
    sender = _conv(uuid.uuid4().hex, title="Sender")
    receiver = _conv(uuid.uuid4().hex, title="Receiver")
    conv_store.convs[sender.id] = sender
    conv_store.convs[receiver.id] = receiver
    true_state = _TrueStateScript()
    deliver = _DeliverScript()
    post_event = _PostEventScript()
    record = store.create(
        SessionPeerMessage(
            id=uuid.uuid4().hex,
            sender_session_id=sender.id,
            receiver_session_id=receiver.id,
            ref="ref-1",
            text="hello",
            state="pending",
            created_at=1000,
            expires_at=999,
        )
    )

    true_state.states[sender.id] = "busy"
    first = _store_sweeper(store, conv_store, true_state, deliver, post_event)
    await first._tick()
    assert store.get(record.id).state == "expired"  # type: ignore[union-attr]
    assert post_event.calls == []  # parked while the sender was busy

    true_state.states[sender.id] = "idle"
    second = _store_sweeper(store, conv_store, true_state, deliver, post_event)
    await second.start(_APP)
    await second.shutdown()
    assert len(post_event.calls) == 1
    assert f"peer message {record.id}" in post_event.calls[0]["text"]
    assert "expired" in post_event.calls[0]["text"]

    third = _store_sweeper(store, conv_store, true_state, deliver, post_event)
    await third.start(_APP)
    await third.shutdown()
    assert len(post_event.calls) == 1


async def test_two_sweepers_claim_one_owed_notice_once(db_uri: str) -> None:
    """Two sweepers holding the same owed notice post it exactly once."""
    store = SqlAlchemyPeerMessageStore(db_uri)
    conv_store = _FakeConversationStore()
    sender = _conv(uuid.uuid4().hex, title="Sender")
    conv_store.convs[sender.id] = sender
    true_state = _TrueStateScript()
    deliver = _DeliverScript()
    post_event = _PostEventScript()
    record = store.create(
        SessionPeerMessage(
            id=uuid.uuid4().hex,
            sender_session_id=sender.id,
            receiver_session_id=uuid.uuid4().hex,
            ref="ref-1",
            text="hello",
            state="expired",
            created_at=1000,
            expires_at=999,
        )
    )
    store.set_notice_owed(record.id, 900)
    line = f'[System: peer message {record.id} to session x "R" expired]'

    true_state.states[sender.id] = "busy"
    first = _store_sweeper(store, conv_store, true_state, deliver, post_event)
    second = _store_sweeper(store, conv_store, true_state, deliver, post_event)
    await first.notify_line(sender.id, line, peer_id=record.id)
    await second.notify_line(sender.id, line, peer_id=record.id)
    assert len(first._parked[sender.id]) == 1
    assert len(second._parked[sender.id]) == 1

    true_state.states[sender.id] = "idle"
    await asyncio.gather(
        first._maybe_flush(sender, _APP),
        second._maybe_flush(sender, _APP),
    )
    assert len(post_event.calls) == 1
    assert first._parked.get(sender.id) == []
    assert second._parked.get(sender.id) == []
    updated = store.get(record.id)
    assert updated is not None
    assert updated.notice_owed_at is None


async def test_failed_flush_restores_owed_mark_and_reparks(db_uri: str) -> None:
    """A post failure restores the durable mark so a later flush posts once."""
    store = SqlAlchemyPeerMessageStore(db_uri)
    conv_store = _FakeConversationStore()
    sender = _conv(uuid.uuid4().hex, title="Sender")
    conv_store.convs[sender.id] = sender
    true_state = _TrueStateScript()
    deliver = _DeliverScript()
    post_event = _PostEventScript()
    record = store.create(
        SessionPeerMessage(
            id=uuid.uuid4().hex,
            sender_session_id=sender.id,
            receiver_session_id=uuid.uuid4().hex,
            ref="ref-1",
            text="hello",
            state="expired",
            created_at=1000,
            expires_at=999,
        )
    )
    store.set_notice_owed(record.id, 900)
    line = f'[System: peer message {record.id} to session x "R" expired]'

    true_state.states[sender.id] = "busy"
    sweeper = _store_sweeper(store, conv_store, true_state, deliver, post_event)
    await sweeper.notify_line(sender.id, line, peer_id=record.id)
    true_state.states[sender.id] = "idle"
    post_event.raise_once_for.add(sender.id)

    await sweeper._maybe_flush(sender, _APP)
    restored = store.get(record.id)
    assert restored is not None
    assert restored.notice_owed_at is not None
    assert len(sweeper._parked[sender.id]) == 1

    await sweeper._maybe_flush(sender, _APP)
    assert len(post_event.calls) == 1
    updated = store.get(record.id)
    assert updated is not None
    assert updated.notice_owed_at is None


async def test_replay_owed_notices_posts_all_past_the_batch_limit() -> None:
    """Startup replay parks every owed record, not only one batch page."""
    h = _Harness()
    h.add_conv(_conv("sender", title="Sender"))
    h.add_conv(_conv("receiver", title="Receiver"))
    first = h.seed_record(id="peer_replay_1", state="expired", notice_owed_at=900)
    second = h.seed_record(id="peer_replay_2", state="expired", notice_owed_at=901)
    sweeper = PeerSweeper(
        peer_store=h.store,
        conversation_store=cast(Any, h.conv_store),
        permission_store=None,
        true_state=h.true_state,
        deliver=h.deliver,
        post_event_impl=h.post_event,
        clock=lambda: h._now,
        batch_limit=1,
    )
    sweeper._app = _APP

    await sweeper._replay_owed_notices()

    posted = "\n".join(call["text"] for call in h.post_event.calls)
    assert f"peer message {first.id}" in posted
    assert f"peer message {second.id}" in posted
