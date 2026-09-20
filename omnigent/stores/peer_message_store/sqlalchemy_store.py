"""SQLAlchemy-backed peer-message store."""

from __future__ import annotations

import builtins
from typing import Any, cast

from sqlalchemy import asc, desc, func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlSessionPeerMessage, current_workspace_id
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
    ) -> list[SessionPeerMessage]:
        """Return one receiver's records, newest first."""
        with self._session("list_peer_messages_for_session") as session:
            stmt = (
                select(SqlSessionPeerMessage)
                .where(SqlSessionPeerMessage.workspace_id == current_workspace_id())
                .where(SqlSessionPeerMessage.receiver_session_id == session_id)
            )
            if states is not None:
                stmt = stmt.where(SqlSessionPeerMessage.state.in_(sorted(states)))
            stmt = stmt.order_by(
                desc(SqlSessionPeerMessage.created_at), desc(SqlSessionPeerMessage.id)
            ).limit(limit)
            rows = session.execute(stmt).scalars().all()
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
                .order_by(
                    asc(SqlSessionPeerMessage.expires_at), asc(SqlSessionPeerMessage.id)
                )
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
    ) -> bool:
        """Compare-and-set a record's state; ``False`` on a lost race."""
        values: dict[str, Any] = {"state": state, "updated_at": now_epoch()}
        if reason is not None:
            values["reason"] = reason

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
