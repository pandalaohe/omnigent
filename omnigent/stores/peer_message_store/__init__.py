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
        *,
        sender_session_id: str | None = None,
        oldest_first: bool = False,
    ) -> list[SessionPeerMessage]:
        """
        Return records addressed to one receiver session, newest first.

        :param session_id: The receiver session.
        :param states: When given, return only records in these states.
        :param limit: Maximum records to return.
        :param sender_session_id: When given, return only records from
            this sender. Used for a per-pair lookup that must not be
            crowded out by other senders' records.
        :param oldest_first: When ``True``, order by creation time
            ascending instead of descending, so a bounded per-pair
            query finds the oldest records rather than the newest page.
        :returns: :class:`SessionPeerMessage` instances in reverse
            creation order, or forward creation order when
            ``oldest_first`` is set.
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
        *,
        expires_at: int | None = None,
        relay_depth: int | None = None,
        notice: bool = False,
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
        :param expires_at: New expiry, or omitted to leave it. Used by the
            sweeper's startup reconciliation, which resets a crash-orphaned
            ``delivering`` record's expiry when it reverts to ``pending``.
        :param relay_depth: New relay depth, or omitted to leave it. Used
            by the release action, which resets a held record's chain.
        :param notice: When ``True``, the same write records that the
            terminal state owes its sender a back-notice. Atomic with the
            compare-and-set so a lost race leaves no mark.
        :returns: ``True`` when exactly one row changed, else ``False``.
        """
        ...

    @abstractmethod
    def list_notice_owed(self) -> list[SessionPeerMessage]:
        """
        Return every record whose back-notice has not posted yet.

        :returns: :class:`SessionPeerMessage` instances with a non-NULL
            ``notice_owed_at``, ordered by ``notice_owed_at``, ``id``.
        """
        ...

    @abstractmethod
    def claim_notice(self, peer_id: str) -> bool:
        """
        Clear one record's owed back-notice, claiming it for posting.

        :param peer_id: The record whose notice to claim.
        :returns: ``True`` when a mark was present and cleared, else
            ``False``.
        """
        ...

    @abstractmethod
    def set_notice_owed(self, peer_id: str, owed_at: int) -> None:
        """
        Restore one record's owed back-notice after a failed post.

        :param peer_id: The record whose notice to restore.
        :param owed_at: Unix epoch seconds to record as the owed time.
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
    def retarget_receiver(
        self,
        old_receiver_id: str,
        new_receiver_id: str,
        states: tuple[str, ...],
    ) -> list[str]:
        """
        Re-address one receiver's not-yet-delivered records to another.

        Used by session succession: messages still queued or held for a
        retired session follow its successor instead of being lost with the
        old id. Only records in ``states`` move — one already delivered (or
        being delivered inline) keeps the receiver it reached.

        :param old_receiver_id: Retired receiver session.
        :param new_receiver_id: Successor that now receives the records.
        :param states: Record states eligible to move.
        :returns: Ids of the records moved, in no particular order.
        """
        ...

    @abstractmethod
    def find_sent(
        self,
        sender_session_id: str,
        receiver_session_id: str,
        ref_or_id: str,
        created_after: int,
    ) -> SessionPeerMessage | None:
        """
        Return the newest sender→receiver record matching *ref_or_id*.

        Used by the reply exemption: a refusing session's own thread is
        identified by a record it sent to the replier whose ``ref`` or
        ``id`` equals the reply's correlation. Any state counts (the
        original may be delivered, queued or failed).

        :param sender_session_id: The refuser session (original sender).
        :param receiver_session_id: The replier session (original receiver).
        :param ref_or_id: Correlation id to match against ``ref`` or ``id``.
        :param created_after: Inclusive lower bound on ``created_at``.
        :returns: The newest matching :class:`SessionPeerMessage`, or
            ``None``.
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
