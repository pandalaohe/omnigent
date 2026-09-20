"""Tests for :class:`SqlAlchemyPeerMessageStore`.

Exercises create, conditional transition, the newest-unreplied lookup
and the ref count against a real SQLite database.
"""

from __future__ import annotations

import uuid

import pytest

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


@pytest.fixture()
def store(db_uri: str) -> SqlAlchemyPeerMessageStore:
    """A fresh :class:`SqlAlchemyPeerMessageStore` backed by the test SQLite DB.

    :param db_uri: Per-test SQLite URI from the root conftest fixture.
    :returns: A ready-to-use :class:`SqlAlchemyPeerMessageStore` instance.
    """
    return SqlAlchemyPeerMessageStore(db_uri)


def test_create_and_get_round_trip(store: SqlAlchemyPeerMessageStore) -> None:
    """``create`` echoes the payload; ``get`` reads it back."""
    created = store.create(_record("r1"))
    assert created.state == "pending"
    assert created.text == "text r1"
    assert created.reply_peer_id is None
    assert created.replied_at is None
    fetched = store.get(created.id)
    assert fetched == created
    assert store.get(_uid("missing")) is None


def test_transition_compare_and_set(store: SqlAlchemyPeerMessageStore) -> None:
    """A matching expected state moves; a stale one returns ``False``."""
    record = store.create(_record("t1"))
    assert store.transition(record.id, "queued", expected_states=("pending",)) is True
    assert store.get(record.id).state == "queued"  # type: ignore[union-attr]
    assert store.transition(record.id, "delivered", expected_states=("pending",)) is False
    assert store.get(record.id).state == "queued"  # type: ignore[union-attr]
    assert store.transition("0" * 32, "queued", expected_states=("pending",)) is False


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
