"""Tests for :class:`SqlAlchemyPeerMessageStore`.

Exercises create, conditional transition, the newest-unreplied lookup
and the ref count against a real SQLite database.
"""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock

import pytest
import sqlalchemy as sa

from omnigent.db.db_models import SqlConversation, SqlSessionPeerMessage, current_workspace_id
from omnigent.entities import SessionPeerMessage
from omnigent.stores.peer_message_store.sqlalchemy_store import SqlAlchemyPeerMessageStore


def _uid(seed: str) -> str:
    """Deterministic bare 32-char hex UUID string from a short readable seed."""
    return uuid.uuid5(uuid.NAMESPACE_DNS, seed).hex


def _record(seed: str, **overrides) -> SessionPeerMessage:
    """A minimal valid peer-message record with deterministic ids."""
    kwargs: dict = {
        "id": _uid(f"{seed}-id"),
        "sender_session_id": _uid(f"{seed}-sender"),
        "receiver_session_id": _uid(f"{seed}-receiver"),
        "ref": _uid(f"{seed}-id"),
        "text": f"text {seed}",
        "state": "pending",
        "created_at": 1000,
        "expires_at": 2000,
    }
    kwargs.update(overrides)
    return SessionPeerMessage(**kwargs)  # type: ignore[arg-type]


class _TestPeerStore(SqlAlchemyPeerMessageStore):
    """Supply real receiver rows when a backend requires a row lock."""

    _seed_lock = Lock()

    def create(self, record: SessionPeerMessage) -> SessionPeerMessage:
        if self._engine.dialect.name != "sqlite":
            with self._seed_lock, self._session_immediate("seed_test_receiver") as session:
                key = (current_workspace_id(), record.receiver_session_id)
                if session.get(SqlConversation, key) is None:
                    session.add(
                        SqlConversation(
                            id=record.receiver_session_id,
                            root_conversation_id=record.receiver_session_id,
                            created_at=0,
                            updated_at=0,
                        )
                    )
        return super().create(record)


@pytest.fixture()
def store(db_uri: str) -> SqlAlchemyPeerMessageStore:
    """A fresh :class:`SqlAlchemyPeerMessageStore` backed by the test SQLite DB.

    :param db_uri: Per-test SQLite URI from the root conftest fixture.
    :returns: A ready-to-use :class:`SqlAlchemyPeerMessageStore` instance.
    """
    return _TestPeerStore(db_uri)


def test_create_and_get_round_trip(store: SqlAlchemyPeerMessageStore) -> None:
    """``create`` echoes the payload; ``get`` reads it back."""
    created = store.create(_record("r1"))
    assert created.state == "pending"
    assert created.text == "text r1"
    assert created.reply_peer_id is None
    assert created.replied_at is None
    assert created.pair_sequence == 1
    fetched = store.get(created.id)
    assert fetched == created
    assert store.get(_uid("missing")) is None


def test_relay_depth_and_not_before_round_trip(store: SqlAlchemyPeerMessageStore) -> None:
    """The relay-depth and not-before columns round-trip, with their defaults."""
    seeded = store.create(_record("rd", relay_depth=7, not_before=1234))
    assert seeded.relay_depth == 7
    assert seeded.not_before == 1234
    fetched = store.get(seeded.id)
    assert fetched is not None
    assert fetched.relay_depth == 7
    assert fetched.not_before == 1234

    defaulted = store.create(_record("rd-default"))
    assert defaulted.relay_depth == 1
    assert defaulted.not_before is None


def test_transition_writes_relay_depth(store: SqlAlchemyPeerMessageStore) -> None:
    """``transition(relay_depth=...))`` resets the depth; omitting it leaves it."""
    record = store.create(_record("tr", relay_depth=31))
    assert (
        store.transition(record.id, "pending", expected_states=("pending",), relay_depth=0) is True
    )
    updated = store.get(record.id)
    assert updated is not None
    assert updated.relay_depth == 0
    assert store.transition(record.id, "queued", expected_states=("pending",)) is True
    kept = store.get(record.id)
    assert kept is not None
    assert kept.relay_depth == 0


def test_transition_compare_and_set(store: SqlAlchemyPeerMessageStore) -> None:
    """A matching expected state moves; a stale one returns ``False``."""
    record = store.create(_record("t1"))
    assert store.transition(record.id, "queued", expected_states=("pending",)) is True
    assert store.get(record.id).state == "queued"  # type: ignore[union-attr]
    assert store.transition(record.id, "delivered", expected_states=("pending",)) is False
    assert store.get(record.id).state == "queued"  # type: ignore[union-attr]
    assert store.transition("0" * 32, "queued", expected_states=("pending",)) is False


def test_transition_rejects_stale_native_attempt_reason(store: SqlAlchemyPeerMessageStore) -> None:
    """State alone cannot distinguish two delivery attempts on one record."""
    attempt_a = "native_attempt:" + "a" * 32
    attempt_b = "native_attempt:" + "b" * 32
    record = store.create(_record("attempt-cas", state="delivering", reason=attempt_a))
    assert store.transition(
        record.id, "queued", "native_refused", ("delivering",), expected_reason=attempt_a
    )
    assert store.transition(record.id, "delivering", attempt_b, ("queued",))
    assert not store.transition(
        record.id, "queued", "native_refused", ("delivering",), expected_reason=attempt_a
    )
    current = store.get(record.id)
    assert current is not None
    assert (current.state, current.reason) == ("delivering", attempt_b)


def test_notice_owed_at_round_trips(store: SqlAlchemyPeerMessageStore) -> None:
    """``create`` and ``get`` carry the owed mark."""
    seeded = store.create(_record("no", notice_owed_at=4321))
    assert seeded.notice_owed_at == 4321
    fetched = store.get(seeded.id)
    assert fetched is not None
    assert fetched.notice_owed_at == 4321
    plain = store.create(_record("no-none"))
    assert plain.notice_owed_at is None


def test_transition_notice_mark_follows_the_cas(store: SqlAlchemyPeerMessageStore) -> None:
    """``notice=True`` sets the mark only when the conditional update wins."""
    record = store.create(_record("n1"))
    assert (
        store.transition(record.id, "expired", expected_states=("queued",), notice=True) is False
    )
    assert store.get(record.id).notice_owed_at is None  # type: ignore[union-attr]
    assert (
        store.transition(record.id, "expired", expected_states=("pending",), notice=True) is True
    )
    marked = store.get(record.id)
    assert marked is not None
    assert marked.notice_owed_at is not None


def test_list_notice_owed_returns_only_marked_rows(store: SqlAlchemyPeerMessageStore) -> None:
    """Owed rows come back oldest-mark-first, all of them, only when marked."""
    first = store.create(_record("o1"))
    second = store.create(_record("o2"))
    unmarked = store.create(_record("o3"))
    store.set_notice_owed(first.id, 100)
    store.set_notice_owed(second.id, 200)
    owed = store.list_notice_owed()
    assert [r.id for r in owed] == [first.id, second.id]
    assert [r.notice_owed_at for r in owed] == [100, 200]
    assert store.get(unmarked.id).notice_owed_at is None  # type: ignore[union-attr]


def test_claim_notice_once_then_false_and_set_restores(
    store: SqlAlchemyPeerMessageStore,
) -> None:
    """``claim_notice`` wins exactly once; ``set_notice_owed`` restores it."""
    record = store.create(_record("c1"))
    assert store.claim_notice(record.id) is False
    store.set_notice_owed(record.id, 123)
    marked = store.get(record.id)
    assert marked is not None
    assert marked.notice_owed_at == 123
    assert marked.updated_at is None
    assert store.claim_notice(record.id) is True
    claimed = store.get(record.id)
    assert claimed is not None
    assert claimed.notice_owed_at is None
    assert claimed.updated_at is None
    assert store.claim_notice(record.id) is False


def test_find_unreplied_returns_newest(store: SqlAlchemyPeerMessageStore) -> None:
    """Only the pair's newest unreplied record is returned."""
    sender, receiver = _uid("u-sender"), _uid("u-receiver")
    first = store.create(
        _record("u1", sender_session_id=sender, receiver_session_id=receiver, created_at=100)
    )
    second = store.create(
        _record("u2", sender_session_id=sender, receiver_session_id=receiver, created_at=200)
    )
    assert store.find_unreplied(sender, receiver).id == second.id  # type: ignore[union-attr]
    store.mark_replied(second.id, _uid("u-reply"), replied_at=300)
    newest = store.find_unreplied(sender, receiver)
    assert newest is not None
    assert newest.id == first.id
    store.mark_replied(first.id, _uid("u-reply-2"), replied_at=400)
    assert store.find_unreplied(sender, receiver) is None
    assert store.find_unreplied(sender, _uid("other")) is None


def test_find_sent_matches_ref_or_id_within_window(store: SqlAlchemyPeerMessageStore) -> None:
    """``find_sent`` matches ref or record id, newest first, any state."""
    sender, receiver = _uid("fs-sender"), _uid("fs-receiver")
    older = store.create(
        _record(
            "fs1",
            sender_session_id=sender,
            receiver_session_id=receiver,
            ref="corr-fs",
            created_at=100,
        )
    )
    newer = store.create(
        _record(
            "fs2",
            sender_session_id=sender,
            receiver_session_id=receiver,
            ref="corr-fs",
            created_at=200,
            state="delivered",
        )
    )
    assert store.find_sent(sender, receiver, "corr-fs", 0).id == newer.id  # type: ignore[union-attr]
    assert store.find_sent(sender, receiver, "corr-fs", 150).id == newer.id  # type: ignore[union-attr]
    assert store.find_sent(sender, receiver, "corr-fs", 201) is None
    assert store.find_sent(sender, receiver, older.id, 0).id == older.id  # type: ignore[union-attr]
    assert store.find_sent(receiver, sender, "corr-fs", 0) is None
    assert store.find_sent(sender, receiver, "missing", 0) is None


def test_find_sent_oldest_returns_earliest_match(store: SqlAlchemyPeerMessageStore) -> None:
    """``oldest=True`` returns the pair's earliest match; default the newest."""
    sender, receiver = _uid("fo-sender"), _uid("fo-receiver")
    earlier = store.create(
        _record(
            "fo1",
            sender_session_id=sender,
            receiver_session_id=receiver,
            ref="corr-fo",
            created_at=100,
        )
    )
    later = store.create(
        _record(
            "fo2",
            sender_session_id=sender,
            receiver_session_id=receiver,
            ref="corr-fo",
            created_at=200,
        )
    )
    assert store.find_sent(sender, receiver, "corr-fo", 0, oldest=True).id == earlier.id  # type: ignore[union-attr]
    assert store.find_sent(sender, receiver, "corr-fo", 0).id == later.id  # type: ignore[union-attr]


def test_find_replied_by_returns_the_linked_record(store: SqlAlchemyPeerMessageStore) -> None:
    """``find_replied_by`` is the exact inverse of ``mark_replied``."""
    original = store.create(_record("frb"))
    reply = store.create(_record("frb-reply"))
    assert store.find_replied_by(reply.id) is None
    assert store.mark_replied(original.id, reply.id, replied_at=10) is True
    linked = store.find_replied_by(reply.id)
    assert linked is not None
    assert linked.id == original.id
    assert store.find_replied_by(_uid("frb-unknown")) is None
    assert store.find_replied_by("not-a-uuid") is None


def test_count_for_ref(store: SqlAlchemyPeerMessageStore) -> None:
    """``count_for_ref`` counts exactly the records carrying the ref."""
    ref = "corr-1"
    store.create(_record("c1", ref=ref, correlation_id=ref))
    store.create(_record("c2", ref=ref, correlation_id=ref))
    store.create(_record("c3", ref="corr-2", correlation_id="corr-2"))
    assert store.count_for_ref(ref) == 2
    assert store.count_for_ref("corr-2") == 1
    assert store.count_for_ref("missing") == 0


def test_list_for_session_and_due(store: SqlAlchemyPeerMessageStore) -> None:
    """Receiver list filters states newest-first; due orders by expiry."""
    receiver = _uid("l-receiver")
    store.create(_record("l1", receiver_session_id=receiver, state="held", created_at=100))
    store.create(_record("l2", receiver_session_id=receiver, state="pending", created_at=200))
    store.create(_record("l3", receiver_session_id=receiver, state="pending", created_at=300))
    held = store.list_for_session(receiver, states=("held",))
    assert [r.id for r in held] == [_uid("l1-id")]
    newest_first = store.list_for_session(receiver)
    assert [r.id for r in newest_first] == [_uid("l3-id"), _uid("l2-id"), _uid("l1-id")]
    due = store.list_due(("pending", "queued"), limit=10)
    assert {r.id for r in due} == {_uid("l2-id"), _uid("l3-id")}
    assert store.list_for_session(_uid("l-other")) == []


def test_list_for_session_sender_filter_finds_record_behind_other_senders(
    store: SqlAlchemyPeerMessageStore,
) -> None:
    """A sender-scoped list is not crowded out by newer other-sender records."""
    sender, receiver = _uid("sf-sender"), _uid("sf-receiver")
    older = store.create(
        _record(
            "sf-old",
            sender_session_id=sender,
            receiver_session_id=receiver,
            created_at=100,
        )
    )
    for index in range(60):
        store.create(
            _record(
                f"sf-other-{index}",
                sender_session_id=_uid(f"sf-other-sender-{index}"),
                receiver_session_id=receiver,
                created_at=200 + index,
            )
        )
    unscoped = store.list_for_session(receiver, ("pending",), 50)
    assert older.id not in {r.id for r in unscoped}
    scoped = store.list_for_session(receiver, ("pending",), 50, sender_session_id=sender)
    assert [r.id for r in scoped] == [older.id]


def test_list_for_session_oldest_first(store: SqlAlchemyPeerMessageStore) -> None:
    """``oldest_first=True`` pages from the oldest record, not the newest."""
    sender, receiver = _uid("of-sender"), _uid("of-receiver")
    oldest = store.create(
        _record(
            "of-old",
            sender_session_id=sender,
            receiver_session_id=receiver,
            created_at=100,
        )
    )
    for index in range(55):
        store.create(
            _record(
                f"of-new-{index}",
                sender_session_id=sender,
                receiver_session_id=receiver,
                created_at=200 + index,
            )
        )
    oldest_two = store.list_for_session(
        receiver, ("pending",), 2, sender_session_id=sender, oldest_first=True
    )
    assert [r.id for r in oldest_two] == [oldest.id, _uid("of-new-0-id")]
    newest_two = store.list_for_session(receiver, ("pending",), 2, sender_session_id=sender)
    assert [r.id for r in newest_two] == [_uid("of-new-54-id"), _uid("of-new-53-id")]


@pytest.mark.parametrize("first_state", ["pending", "queued"])
def test_pair_order_uses_admission_not_second_or_id(
    store: SqlAlchemyPeerMessageStore, first_state: str
) -> None:
    sender, receiver = _uid("admission-sender"), _uid("admission-receiver")
    first = store.create(
        _record(
            "z-admission",
            id="f" * 32,
            sender_session_id=sender,
            receiver_session_id=receiver,
            state=first_state,
        )
    )
    second = store.create(
        _record("a-admission", id="1" * 32, sender_session_id=sender, receiver_session_id=receiver)
    )
    assert [first.pair_sequence, second.pair_sequence] == [1, 2]
    assert [
        row.id
        for row in store.list_for_session(receiver, sender_session_id=sender, oldest_first=True)
    ] == [first.id, second.id]
    assert not store.claim_oldest(second.id, "sdk_wait:second", ("pending",))
    assert store.claim_oldest(first.id, "sdk_wait:first", (first_state,))
    assert not store.claim_oldest(second.id, "sdk_wait:second", ("pending",))
    assert store.transition(first.id, "delivered", expected_states=("delivering",))
    assert store.claim_oldest(second.id, "sdk_wait:second", ("pending",))
    assert store.get(second.id).reason == "sdk_wait:second"  # type: ignore[union-attr]


def test_create_repairs_null_legacy_rows_before_new_admission(
    store: SqlAlchemyPeerMessageStore,
) -> None:
    sender, receiver = _uid("legacy-sender"), _uid("legacy-receiver")
    old = store.create(
        _record("legacy-old", sender_session_id=sender, receiver_session_id=receiver)
    )
    with store._engine.begin() as conn:
        conn.execute(
            sa.update(SqlSessionPeerMessage)
            .where(SqlSessionPeerMessage.workspace_id == 0, SqlSessionPeerMessage.id == old.id)
            .values(pair_sequence=None)
        )
    new = store.create(
        _record("legacy-new", sender_session_id=sender, receiver_session_id=receiver)
    )
    assert store.get(old.id).pair_sequence == 1  # type: ignore[union-attr]
    assert new.pair_sequence == 2
    assert not store.claim_oldest(new.id, None, ("pending",))


def test_concurrent_writers_allocate_unique_pair_sequence(
    store: SqlAlchemyPeerMessageStore,
) -> None:
    sender, receiver = _uid("concurrent-sender"), _uid("concurrent-receiver")
    gate = Barrier(6)

    def write(index: int) -> SessionPeerMessage:
        gate.wait(timeout=10)
        return store.create(
            _record(
                f"concurrent-{index}",
                sender_session_id=sender,
                receiver_session_id=receiver,
                created_at=1000,
            )
        )

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(write, range(6)))
    assert sorted(row.pair_sequence for row in results) == list(range(1, 7))
    oldest = min(results, key=lambda row: row.pair_sequence)
    newer = max(results, key=lambda row: row.pair_sequence)
    assert not store.claim_oldest(newer.id, None, ("pending",))
    assert store.claim_oldest(oldest.id, None, ("pending",))


def test_concurrent_claims_reserve_only_oldest(store: SqlAlchemyPeerMessageStore) -> None:
    sender, receiver = _uid("claim-sender"), _uid("claim-receiver")
    first = store.create(
        _record("claim-first", sender_session_id=sender, receiver_session_id=receiver)
    )
    second = store.create(
        _record("claim-second", sender_session_id=sender, receiver_session_id=receiver)
    )
    gate = Barrier(2)

    def claim(peer_id: str) -> bool:
        gate.wait(timeout=10)
        return store.claim_oldest(peer_id, "sdk_wait:claim", ("pending",))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, (first.id, second.id)))
    assert results == [True, False]
    assert store.get(first.id).state == "delivering"  # type: ignore[union-attr]
    assert store.get(second.id).state == "pending"  # type: ignore[union-attr]


def test_row_lock_backend_requires_receiver(store: SqlAlchemyPeerMessageStore) -> None:
    if store._engine.dialect.name == "sqlite":
        pytest.skip("SQLite uses the immediate transaction without a receiver row")
    real_store = SqlAlchemyPeerMessageStore(store.storage_location)
    with pytest.raises(RuntimeError, match="receiver conversation missing"):
        real_store.create(_record("missing-receiver-lock"))
