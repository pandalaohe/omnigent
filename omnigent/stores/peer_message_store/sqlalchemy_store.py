"""SQLAlchemy-backed peer-message store."""

from __future__ import annotations

import builtins
from typing import Any, cast

from sqlalchemy import asc, desc, func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlSessionPeerMessage, current_workspace_id, uuid_to_bytes
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch,
    run_write_transaction,
)
from omnigent.entities import SessionPeerMessage
from omnigent.stores.peer_message_store import PeerMessageStore


def _record_to_entity(row: SqlSessionPeerMessage) -> SessionPeerMessage:
    """
    Convert a :class:`SqlSessionPeerMessage` ORM row to a
    :class:`SessionPeerMessage`.

    :param row: The SQLAlchemy ORM row to convert.
    :returns: A :class:`SessionPeerMessage` dataclass instance.
    """
    return SessionPeerMessage(
        id=row.id,
        sender_session_id=row.sender_session_id,
        receiver_session_id=row.receiver_session_id,
        ref=row.ref,
        text=row.text,
        state=row.state,
        correlation_id=row.correlation_id,
        reason=row.reason,
        created_at=row.created_at,
        updated_at=row.updated_at,
        expires_at=row.expires_at,
        reply_peer_id=row.reply_peer_id,
        replied_at=row.replied_at,
        relay_depth=row.relay_depth,
        not_before=row.not_before,
        notice_owed_at=row.notice_owed_at,
        workspace_id=row.workspace_id,
    )


class SqlAlchemyPeerMessageStore(PeerMessageStore):
    """
    SQLAlchemy-backed implementation of :class:`PeerMessageStore`.

    Persists peer-message records in a relational database via the
    SQLAlchemy ORM. Every query is scoped by ``workspace_id`` (tenant
    partition). State changes are conditional writes so concurrent
    sweeper and route writers cannot clobber each other.
    """

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the SQLAlchemy peer-message store.

        Creates or reuses a SQLAlchemy engine and session factory for the
        given database URI.

        :param storage_location: SQLAlchemy database URI,
            e.g. ``"sqlite:///chat.db"``.
        """
        super().__init__(storage_location)
        self._engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            self._engine,
            query_name_prefix="omnigent.peer_message_store",
        )
        self._session_immediate = make_named_managed_session_maker(
            self._engine,
            query_name_prefix="omnigent.peer_message_store",
            immediate=True,
        )

    def create(self, record: SessionPeerMessage) -> SessionPeerMessage:
        """Insert a new peer-message record."""

        def write(session: Session) -> SessionPeerMessage:
            row = SqlSessionPeerMessage(
                id=record.id,
                sender_session_id=record.sender_session_id,
                receiver_session_id=record.receiver_session_id,
                correlation_id=record.correlation_id,
                ref=record.ref,
                text=record.text,
                state=record.state,
                reason=record.reason,
                created_at=record.created_at,
                updated_at=record.updated_at,
                expires_at=record.expires_at,
                reply_peer_id=record.reply_peer_id,
                replied_at=record.replied_at,
                relay_depth=record.relay_depth,
                not_before=record.not_before,
                notice_owed_at=record.notice_owed_at,
            )
            session.add(row)
            session.flush()
            return _record_to_entity(row)

        return run_write_transaction(self._session_immediate, "insert_peer_message", write)

    def get(self, peer_id: str) -> SessionPeerMessage | None:
        """Return a peer message by id, or ``None`` if not found."""
        with self._session("select_peer_message_by_id") as session:
            row = session.get(SqlSessionPeerMessage, (current_workspace_id(), peer_id))
            if row is None:
                return None
            return _record_to_entity(row)

    def list_for_session(
        self,
        session_id: str,
        states: tuple[str, ...] | None = None,
        limit: int = 20,
        *,
        sender_session_id: str | None = None,
        oldest_first: bool = False,
    ) -> list[SessionPeerMessage]:
        """Return one receiver's records, newest first unless ``oldest_first``."""
        with self._session("list_peer_messages_for_session") as session:
            stmt = (
                select(SqlSessionPeerMessage)
                .where(SqlSessionPeerMessage.workspace_id == current_workspace_id())
                .where(SqlSessionPeerMessage.receiver_session_id == session_id)
            )
            if states is not None:
                stmt = stmt.where(SqlSessionPeerMessage.state.in_(sorted(states)))
            if sender_session_id is not None:
                stmt = stmt.where(SqlSessionPeerMessage.sender_session_id == sender_session_id)
            if oldest_first:
                stmt = stmt.order_by(
                    asc(SqlSessionPeerMessage.created_at), asc(SqlSessionPeerMessage.id)
                )
            else:
                stmt = stmt.order_by(
                    desc(SqlSessionPeerMessage.created_at), desc(SqlSessionPeerMessage.id)
                )
            rows = session.execute(stmt.limit(limit)).scalars().all()
            return [_record_to_entity(r) for r in rows]

    def list_due(
        self,
        states: tuple[str, ...],
        limit: int,
    ) -> list[SessionPeerMessage]:
        """Return sweeper-actionable records, ordered by expiry."""
        with self._session("select_due_peer_messages") as session:
            stmt = (
                select(SqlSessionPeerMessage)
                .where(SqlSessionPeerMessage.workspace_id == current_workspace_id())
                .where(SqlSessionPeerMessage.state.in_(sorted(states)))
                .order_by(asc(SqlSessionPeerMessage.expires_at), asc(SqlSessionPeerMessage.id))
                .limit(limit)
            )
            rows = session.execute(stmt).scalars().all()
            return [_record_to_entity(r) for r in rows]

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
        """Compare-and-set a record's state; ``False`` on a lost race."""
        now = now_epoch()
        values: dict[str, Any] = {"state": state, "updated_at": now}
        if reason is not None:
            values["reason"] = reason
        if expires_at is not None:
            values["expires_at"] = expires_at
        if relay_depth is not None:
            values["relay_depth"] = relay_depth
        if notice:
            values["notice_owed_at"] = now

        def write(session: Session) -> bool:
            stmt = update(SqlSessionPeerMessage).where(
                SqlSessionPeerMessage.workspace_id == current_workspace_id(),
                SqlSessionPeerMessage.id == peer_id,
            )
            if expected_states is not None:
                stmt = stmt.where(SqlSessionPeerMessage.state.in_(sorted(expected_states)))
            result = cast(
                "CursorResult[Any]",
                session.execute(stmt.values(**values)),
            )
            return bool(result.rowcount)

        return run_write_transaction(self._session_immediate, "transition_peer_message", write)

    def list_notice_owed(self) -> list[SessionPeerMessage]:
        """Return every record whose back-notice has not posted yet."""
        with self._session("select_peer_messages_owed_notice") as session:
            stmt = (
                select(SqlSessionPeerMessage)
                .where(SqlSessionPeerMessage.workspace_id == current_workspace_id())
                .where(SqlSessionPeerMessage.notice_owed_at.is_not(None))
                .order_by(
                    asc(SqlSessionPeerMessage.notice_owed_at),
                    asc(SqlSessionPeerMessage.id),
                )
            )
            rows = session.execute(stmt).scalars().all()
            return [_record_to_entity(r) for r in rows]

    def claim_notice(self, peer_id: str) -> bool:
        """Clear one record's owed mark; ``False`` when none was owed."""

        def write(session: Session) -> bool:
            result = cast(
                "CursorResult[Any]",
                session.execute(
                    update(SqlSessionPeerMessage)
                    .where(
                        SqlSessionPeerMessage.workspace_id == current_workspace_id(),
                        SqlSessionPeerMessage.id == peer_id,
                        SqlSessionPeerMessage.notice_owed_at.is_not(None),
                    )
                    .values(notice_owed_at=None)
                ),
            )
            return bool(result.rowcount)

        return run_write_transaction(self._session_immediate, "claim_peer_notice", write)

    def set_notice_owed(self, peer_id: str, owed_at: int) -> None:
        """Restore one record's owed mark for a later post attempt."""

        def write(session: Session) -> None:
            session.execute(
                update(SqlSessionPeerMessage)
                .where(
                    SqlSessionPeerMessage.workspace_id == current_workspace_id(),
                    SqlSessionPeerMessage.id == peer_id,
                )
                .values(notice_owed_at=owed_at)
            )

        run_write_transaction(self._session_immediate, "restore_peer_notice", write)

    def mark_replied(
        self,
        peer_id: str,
        reply_peer_id: str,
        replied_at: int,
    ) -> bool:
        """Link a record to its reply."""
        now = now_epoch()

        def write(session: Session) -> bool:
            result = cast(
                "CursorResult[Any]",
                session.execute(
                    update(SqlSessionPeerMessage)
                    .where(
                        SqlSessionPeerMessage.workspace_id == current_workspace_id(),
                        SqlSessionPeerMessage.id == peer_id,
                    )
                    .values(
                        reply_peer_id=reply_peer_id,
                        replied_at=replied_at,
                        updated_at=now,
                    )
                ),
            )
            return bool(result.rowcount)

        return run_write_transaction(self._session_immediate, "mark_peer_message_replied", write)

    def find_unreplied(
        self,
        sender_session_id: str,
        receiver_session_id: str,
    ) -> SessionPeerMessage | None:
        """Return the pair's newest unreplied record, or ``None``."""
        with self._session("find_unreplied_peer_message") as session:
            row = (
                session.execute(
                    select(SqlSessionPeerMessage)
                    .where(SqlSessionPeerMessage.workspace_id == current_workspace_id())
                    .where(SqlSessionPeerMessage.sender_session_id == sender_session_id)
                    .where(SqlSessionPeerMessage.receiver_session_id == receiver_session_id)
                    .where(SqlSessionPeerMessage.replied_at.is_(None))
                    .order_by(
                        desc(SqlSessionPeerMessage.created_at),
                        desc(SqlSessionPeerMessage.id),
                    )
                    .limit(1)
                )
                .scalars()
                .first()
            )
            if row is None:
                return None
            return _record_to_entity(row)

    def retarget_receiver(
        self,
        old_receiver_id: str,
        new_receiver_id: str,
        states: tuple[str, ...],
    ) -> list[str]:
        """Re-address one receiver's not-yet-delivered records to another."""

        def write(session: Session) -> list[str]:
            stmt = select(SqlSessionPeerMessage.id).where(
                SqlSessionPeerMessage.workspace_id == current_workspace_id(),
                SqlSessionPeerMessage.receiver_session_id == old_receiver_id,
                SqlSessionPeerMessage.state.in_(sorted(states)),
            )
            ids = list(session.execute(stmt).scalars().all())
            if not ids:
                return []
            session.execute(
                update(SqlSessionPeerMessage)
                .where(
                    SqlSessionPeerMessage.workspace_id == current_workspace_id(),
                    SqlSessionPeerMessage.id.in_(ids),
                )
                .values(
                    receiver_session_id=new_receiver_id,
                    updated_at=now_epoch(),
                )
            )
            return ids

        return run_write_transaction(self._session_immediate, "retarget_peer_messages", write)

    def find_sent(
        self,
        sender_session_id: str,
        receiver_session_id: str,
        ref_or_id: str,
        created_after: int,
    ) -> SessionPeerMessage | None:
        """Return the pair's newest matching record, or ``None``."""
        # A correlation id is arbitrary text; comparing it against the
        # Uuid16 ``id`` column would fail at bind time unless it is a valid
        # id, so that half of the OR is added only when it can match.
        matches = [SqlSessionPeerMessage.ref == ref_or_id]
        try:
            uuid_to_bytes(ref_or_id)
        except ValueError:
            pass
        else:
            matches.append(SqlSessionPeerMessage.id == ref_or_id)
        with self._session("find_sent_peer_message") as session:
            row = (
                session.execute(
                    select(SqlSessionPeerMessage)
                    .where(SqlSessionPeerMessage.workspace_id == current_workspace_id())
                    .where(SqlSessionPeerMessage.sender_session_id == sender_session_id)
                    .where(SqlSessionPeerMessage.receiver_session_id == receiver_session_id)
                    .where(or_(*matches))
                    .where(SqlSessionPeerMessage.created_at >= created_after)
                    .order_by(
                        desc(SqlSessionPeerMessage.created_at),
                        desc(SqlSessionPeerMessage.id),
                    )
                    .limit(1)
                )
                .scalars()
                .first()
            )
            if row is None:
                return None
            return _record_to_entity(row)

    def count_for_ref(self, ref: str) -> int:
        """Count records carrying *ref*."""
        with self._session("count_peer_messages_for_ref") as session:
            return int(
                session.execute(
                    select(func.count())
                    .select_from(SqlSessionPeerMessage)
                    .where(SqlSessionPeerMessage.workspace_id == current_workspace_id())
                    .where(SqlSessionPeerMessage.ref == ref)
                ).scalar()
                or 0
            )


__all__: builtins.list[str] = ["SqlAlchemyPeerMessageStore", "_record_to_entity"]
