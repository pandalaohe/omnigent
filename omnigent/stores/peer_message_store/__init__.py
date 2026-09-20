"""Peer-message store — persists durable session-to-session messages.

A peer message is one chat-style message from one session to another
session the same user owns. The server stores a record whenever a send
cannot be delivered inline (receiver busy, offline, policy hold) and a
background sweeper delivers, expires, or fails it. This store owns the
``session_peer_messages`` table. All cross-table references are
application-owned, never DB foreign keys (schema Rule R032).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from omnigent.entities import SessionPeerMessage


class PeerMessageStore(ABC):
    """
    Abstract base for peer-message persistence.

    Manages peer-message records (insert / get / receiver-scoped list /
    sweeper selection / conditional transition / reply link / ref count).
    Reads and writes are scoped by the ambient workspace.
    """

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the peer-message store.

        :param storage_location: Backend-specific storage URI,
            e.g. ``"sqlite:///chat.db"`` for SQLAlchemy.
        """
        self.storage_location = storage_location

    @abstractmethod
    def create(self, record: SessionPeerMessage) -> SessionPeerMessage:
        """
        Insert a new peer-message record.

        :param record: The record to insert.
        :returns: The inserted :class:`SessionPeerMessage`.
        """
        ...

    @abstractmethod
    def get(self, peer_id: str) -> SessionPeerMessage | None:
        """
        Return a peer message by id, or ``None`` if not found.

        :param peer_id: Opaque peer-message identifier.
        :returns: The :class:`SessionPeerMessage` if found, else ``None``.
        """
        ...

    @abstractmethod
    def list_for_session(
        self,
        session_id: str,
        states: tuple[str, ...] | None = None,
        limit: int = 20,
    ) -> list[SessionPeerMessage]:
        """
        Return records addressed to one receiver session, newest first.

        :param session_id: The receiver session.
        :param states: When given, return only records in these states.
        :param limit: Maximum records to return.
        :returns: :class:`SessionPeerMessage` instances in reverse
            creation order.
        """
        ...

    @abstractmethod
    def list_due(
        self,
        states: tuple[str, ...],
        limit: int,
    ) -> list[SessionPeerMessage]:
        """
        Return records in one of *states* for the bounded sweeper pass.

        :param states: The sweeper-actionable states (e.g. ``pending``,
            ``queued``).
        :param limit: Maximum records per pass.
        :returns: :class:`SessionPeerMessage` instances ordered by
            ``expires_at``, ``id``.
        """
        ...

    @abstractmethod
    def transition(
        self,
        peer_id: str,
        state: str,
        reason: str | None = None,
        expected_states: tuple[str, ...] | None = None,
    ) -> bool:
        """
        Compare-and-set a record's state.

        The write applies only when the stored state is in
        ``expected_states`` (omitted pins nothing). ``reason`` is written
        only when passed.

        :param peer_id: The record to move.
        :param state: The desired next state.
        :param reason: New disposition classification, or omitted to leave
            it.
        :param expected_states: States the caller last saw; a concurrent
            move makes this return ``False``.
        :returns: ``True`` when exactly one row changed, else ``False``.
        """
        ...

    @abstractmethod
    def mark_replied(
        self,
        peer_id: str,
        reply_peer_id: str,
        replied_at: int,
    ) -> bool:
        """
        Link a record to its reply.

        :param peer_id: The record that was replied to.
        :param reply_peer_id: The reply record's id.
        :param replied_at: Unix epoch seconds the reply was recorded.
        :returns: ``True`` when exactly one row changed, else ``False``.
        """
        ...

    @abstractmethod
    def find_unreplied(
        self,
        sender_session_id: str,
        receiver_session_id: str,
    ) -> SessionPeerMessage | None:
        """
        Return the newest unreplied record of a sender→receiver pair.

        :param sender_session_id: The original sender session.
        :param receiver_session_id: The original receiver session.
        :returns: The newest :class:`SessionPeerMessage` with
            ``replied_at IS NULL``, or ``None`` when the pair has no
            unreplied record.
        """
        ...

    @abstractmethod
    def count_for_ref(self, ref: str) -> int:
        """
        Count records carrying *ref*.

        :param ref: The envelope ref (correlation id or a record id hex).
        :returns: The number of records with this ``ref``.
        """
        ...
