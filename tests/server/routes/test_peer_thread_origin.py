"""Unit tests for :func:`peer_thread_origin_is_receiver`.

A real peer-message store (the SQLAlchemy one the route uses) is seeded
with records whose directions and ``created_at`` are known, so each
expected origin is stated independently of the helper's walk.
"""

from __future__ import annotations

import uuid

from omnigent.entities import SessionPeerMessage
from omnigent.server.routes.sessions.routes_peer import peer_thread_origin_is_receiver
from omnigent.stores.peer_message_store.sqlalchemy_store import SqlAlchemyPeerMessageStore

S = "a" * 32
C = "b" * 32


def _record(
    store: SqlAlchemyPeerMessageStore,
    *,
    sender: str,
    receiver: str,
    ref: str,
    created_at: int,
    correlation_id: str | None = None,
) -> SessionPeerMessage:
    return store.create(
        SessionPeerMessage(
            id=uuid.uuid4().hex,
            sender_session_id=sender,
            receiver_session_id=receiver,
            ref=ref,
            text="body",
            state="delivered",
            correlation_id=correlation_id,
            created_at=created_at,
            expires_at=created_at + 100,
        )
    )


def test_s_started_thread_is_foreign_for_sender_records(db_uri: str) -> None:
    """S→C ref R, C→S answer R, S→C follow-up R: every S record is foreign."""
    store = SqlAlchemyPeerMessageStore(db_uri)
    first = _record(store, sender=S, receiver=C, ref="R", created_at=10)
    _record(store, sender=C, receiver=S, ref="R", created_at=20, correlation_id="R")
    follow_up = _record(store, sender=S, receiver=C, ref="R", created_at=30, correlation_id="R")
    assert peer_thread_origin_is_receiver(store, first) is False
    assert peer_thread_origin_is_receiver(store, follow_up) is False


def test_c_started_thread_is_own_for_s_reply(db_uri: str) -> None:
    """C→S ref R then S→C correlation R: the thread is the receiver's own."""
    store = SqlAlchemyPeerMessageStore(db_uri)
    _record(store, sender=C, receiver=S, ref="R", created_at=10)
    reply = _record(store, sender=S, receiver=C, ref="R", created_at=20, correlation_id="R")
    assert peer_thread_origin_is_receiver(store, reply) is True


def test_correlationless_reply_links_to_child_started_thread(db_uri: str) -> None:
    """A correlation-less S reply linked by ``reply_peer_id`` to C's record."""
    store = SqlAlchemyPeerMessageStore(db_uri)
    question = _record(store, sender=C, receiver=S, ref="R", created_at=10)
    reply = _record(store, sender=S, receiver=C, ref="random", created_at=20)
    # _mark_reply_locked writes the link at admission.
    store.mark_replied(question.id, reply.id, 20)
    assert peer_thread_origin_is_receiver(store, reply) is True


def test_correlationless_reply_survives_newer_questions(db_uri: str) -> None:
    """A linked correlation-less reply stays the receiver's past a 500-page.

    The reply link is read by an exact lookup, so 501 newer C→S records
    cannot push the linked question out of a bounded page and flip the
    answer to foreign.
    """
    store = SqlAlchemyPeerMessageStore(db_uri)
    question = _record(store, sender=C, receiver=S, ref="R", created_at=10)
    reply = _record(store, sender=S, receiver=C, ref="random", created_at=20)
    store.mark_replied(question.id, reply.id, 20)
    assert peer_thread_origin_is_receiver(store, reply) is True
    for index in range(501):
        _record(store, sender=C, receiver=S, ref=f"new-c-{index}", created_at=100 + index)
    assert peer_thread_origin_is_receiver(store, reply) is True


def test_unrelated_record_is_foreign(db_uri: str) -> None:
    """A record with no thread partners is its own origin, sent by S."""
    store = SqlAlchemyPeerMessageStore(db_uri)
    record = _record(store, sender=S, receiver=C, ref="solo", created_at=10)
    assert peer_thread_origin_is_receiver(store, record) is False


def _seed_history(store: SqlAlchemyPeerMessageStore, *, count: int) -> int:
    """Insert *count* unrelated records each way, all older than the next tick.

    :returns: A ``created_at`` a later record can use to be strictly newer.
    """
    for index in range(count):
        _record(store, sender=C, receiver=S, ref=f"c-{index}", created_at=index + 1)
        _record(store, sender=S, receiver=C, ref=f"s-{index}", created_at=index + 1)
    return count + 1


def test_bounded_linked_reply_to_child_question_is_own(db_uri: str) -> None:
    """C's question sits past a 500-record bound; S's link still finds it."""
    store = SqlAlchemyPeerMessageStore(db_uri)
    newer = _seed_history(store, count=501)
    question = _record(store, sender=C, receiver=S, ref="R", created_at=newer)
    reply = _record(store, sender=S, receiver=C, ref="random", created_at=newer + 1)
    store.mark_replied(question.id, reply.id, newer + 1)
    assert peer_thread_origin_is_receiver(store, reply) is True


def test_reply_origin_stable_after_newer_unrelated_traffic(db_uri: str) -> None:
    """A reply stays the receiver's after 501 newer records each way.

    A bounded newest-first page drops the older question, so later traffic on
    the same ref can flip the classification; an exact earliest lookup cannot.
    """
    store = SqlAlchemyPeerMessageStore(db_uri)
    _record(store, sender=C, receiver=S, ref="R", created_at=1)
    for index in range(501):
        _record(store, sender=C, receiver=S, ref=f"u-c-{index}", created_at=100 + index)
        _record(store, sender=S, receiver=C, ref=f"u-s-{index}", created_at=100 + index)
    reply = _record(store, sender=S, receiver=C, ref="R", created_at=700, correlation_id="R")
    assert peer_thread_origin_is_receiver(store, reply) is True
    follow_up = _record(store, sender=S, receiver=C, ref="R", created_at=701, correlation_id="R")
    assert peer_thread_origin_is_receiver(store, reply) is True
    assert peer_thread_origin_is_receiver(store, follow_up) is True


def test_thread_opened_after_older_unrelated_traffic_is_foreign(db_uri: str) -> None:
    """S opens R past 501 older records; C answers, S follows up: origin is S.

    An oldest-first bounded page would never reach the opening record; the
    exact lookup still classifies every S→C record as foreign.
    """
    store = SqlAlchemyPeerMessageStore(db_uri)
    _seed_history(store, count=501)
    _record(store, sender=S, receiver=C, ref="R", created_at=1000)
    _record(store, sender=C, receiver=S, ref="R", created_at=1001, correlation_id="R")
    follow_up = _record(store, sender=S, receiver=C, ref="R", created_at=1002, correlation_id="R")
    assert peer_thread_origin_is_receiver(store, follow_up) is False


def test_fresh_correlation_opened_by_sender_is_foreign(db_uri: str) -> None:
    """S opens a correlation no one used before: the thread is S's, not C's."""
    store = SqlAlchemyPeerMessageStore(db_uri)
    record = _record(store, sender=S, receiver=C, ref="x", created_at=10, correlation_id="x")
    assert peer_thread_origin_is_receiver(store, record) is False


def test_equal_created_at_tie_goes_to_the_receiver(db_uri: str) -> None:
    """On a created_at tie between C and S, the receiver's record is the origin."""
    store = SqlAlchemyPeerMessageStore(db_uri)
    _record(store, sender=C, receiver=S, ref="R", created_at=10)
    reply = _record(store, sender=S, receiver=C, ref="R", created_at=10, correlation_id="R")
    assert peer_thread_origin_is_receiver(store, reply) is True
