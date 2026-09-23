"""SQLAlchemy-backed durable session hand-off store."""

from __future__ import annotations

import json
from dataclasses import fields
from typing import Any, cast

from sqlalchemy import and_, asc, desc, func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlSessionHandoff, current_workspace_id
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch,
    run_write_transaction,
)
from omnigent.entities.session_handoff import HANDOFF_UNFINISHED_STATES, SessionHandoff
from omnigent.stores.session_handoff_store import SessionHandoffStore

_JSON_FIELDS = frozenset({"git_plan", "disclosure", "outcome"})
_FIELDS = tuple(field.name for field in fields(SessionHandoff))


def _record_to_entity(row: SqlSessionHandoff) -> SessionHandoff:
    values = {name: getattr(row, name) for name in _FIELDS}
    for name in _JSON_FIELDS:
        values[name] = json.loads(values[name]) if values[name] is not None else None
    return SessionHandoff(**values)


def _db_fields(values: dict[str, Any]) -> dict[str, Any]:
    return {
        name: json.dumps(value, separators=(",", ":"))
        if name in _JSON_FIELDS and value is not None
        else value
        for name, value in values.items()
    }


class SqlAlchemySessionHandoffStore(SessionHandoffStore):
    """Persist hand-off records within the ambient workspace."""

    def __init__(self, db_uri: str) -> None:
        super().__init__(db_uri)
        self._engine = get_or_create_engine(db_uri)
        self._session = make_named_managed_session_maker(
            self._engine, query_name_prefix="omnigent.session_handoff_store"
        )
        self._session_immediate = make_named_managed_session_maker(
            self._engine, query_name_prefix="omnigent.session_handoff_store", immediate=True
        )

    def create(self, record: SessionHandoff) -> SessionHandoff:
        def write(session: Session) -> SessionHandoff:
            row = SqlSessionHandoff(
                **_db_fields({name: getattr(record, name) for name in _FIELDS})
            )
            session.add(row)
            return _record_to_entity(row)

        return run_write_transaction(self._session_immediate, "create_handoff", write)

    def get(self, handoff_id: str) -> SessionHandoff | None:
        with self._session("get_handoff") as session:
            row = session.get(SqlSessionHandoff, (current_workspace_id(), handoff_id))
            return _record_to_entity(row) if row is not None else None

    def claim(self, handoff_id: str, now: int, lease_s: int) -> bool:
        def write(session: Session) -> bool:
            result = cast(
                "CursorResult[Any]",
                session.execute(
                    update(SqlSessionHandoff)
                    .where(
                        SqlSessionHandoff.workspace_id == current_workspace_id(),
                        SqlSessionHandoff.id == handoff_id,
                        or_(
                            SqlSessionHandoff.lease_until.is_(None),
                            SqlSessionHandoff.lease_until < now,
                        ),
                    )
                    .values(lease_until=now + lease_s)
                ),
            )
            return result.rowcount == 1

        return run_write_transaction(self._session_immediate, "claim_handoff_lease", write)

    def release(self, handoff_id: str) -> None:
        def write(session: Session) -> None:
            session.execute(
                update(SqlSessionHandoff)
                .where(
                    SqlSessionHandoff.workspace_id == current_workspace_id(),
                    SqlSessionHandoff.id == handoff_id,
                )
                .values(lease_until=None)
            )

        run_write_transaction(self._session_immediate, "release_handoff_lease", write)

    def transition(
        self,
        handoff_id: str,
        to_state: str,
        reason: str | None,
        from_states: tuple[str, ...],
        **fields: Any,
    ) -> bool:
        values = _db_fields(fields)
        values.update(state=to_state, reason=reason, updated_at=now_epoch())
        return self._conditional_update(handoff_id, from_states, values, "transition_handoff")

    def set_fields(self, handoff_id: str, from_states: tuple[str, ...], **fields: Any) -> bool:
        if "state" in fields:
            raise ValueError("set_fields cannot change hand-off state")
        values = _db_fields(fields)
        values["updated_at"] = now_epoch()
        return self._conditional_update(handoff_id, from_states, values, "update_handoff_fields")

    def _conditional_update(
        self,
        handoff_id: str,
        from_states: tuple[str, ...],
        values: dict[str, Any],
        operation: str,
    ) -> bool:
        def write(session: Session) -> bool:
            result = cast(
                "CursorResult[Any]",
                session.execute(
                    update(SqlSessionHandoff)
                    .where(
                        SqlSessionHandoff.workspace_id == current_workspace_id(),
                        SqlSessionHandoff.id == handoff_id,
                        SqlSessionHandoff.state.in_(from_states),
                    )
                    .values(**values)
                ),
            )
            return result.rowcount == 1

        return run_write_transaction(self._session_immediate, operation, write)

    def count_unfinished(self, owner_user_id: str) -> int:
        with self._session("count_owner_unfinished_handoffs") as session:
            return int(
                session.scalar(
                    select(func.count())
                    .select_from(SqlSessionHandoff)
                    .where(
                        SqlSessionHandoff.workspace_id == current_workspace_id(),
                        SqlSessionHandoff.owner_user_id == owner_user_id,
                        SqlSessionHandoff.state.in_(HANDOFF_UNFINISHED_STATES),
                    )
                )
                or 0
            )

    def count_recent(self, sender_session_id: str, since: int) -> int:
        with self._session("count_recent_sender_handoffs") as session:
            return int(
                session.scalar(
                    select(func.count())
                    .select_from(SqlSessionHandoff)
                    .where(
                        SqlSessionHandoff.workspace_id == current_workspace_id(),
                        SqlSessionHandoff.sender_session_id == sender_session_id,
                        SqlSessionHandoff.created_at >= since,
                    )
                )
                or 0
            )

    def _newest(self, operation: str, *predicates: Any) -> SessionHandoff | None:
        with self._session(operation) as session:
            row = session.scalars(
                select(SqlSessionHandoff)
                .where(
                    SqlSessionHandoff.workspace_id == current_workspace_id(),
                    *predicates,
                )
                .order_by(desc(SqlSessionHandoff.created_at), desc(SqlSessionHandoff.id))
                .limit(1)
            ).first()
            return _record_to_entity(row) if row is not None else None

    def find_unfinished_duplicate(
        self,
        sender_session_id: str,
        brief_hash: str,
    ) -> SessionHandoff | None:
        return self._newest(
            "find_unfinished_duplicate_handoff",
            SqlSessionHandoff.sender_session_id == sender_session_id,
            SqlSessionHandoff.brief_hash == brief_hash,
            SqlSessionHandoff.state.in_(HANDOFF_UNFINISHED_STATES),
        )

    def find_binding_for_receiver(self, session_id: str) -> SessionHandoff | None:
        return self._newest(
            "find_receiver_handoff_binding",
            SqlSessionHandoff.receiver_session_id == session_id,
            or_(
                SqlSessionHandoff.state.in_(HANDOFF_UNFINISHED_STATES),
                and_(
                    SqlSessionHandoff.state == "expired",
                    SqlSessionHandoff.reason == "no_report",
                    SqlSessionHandoff.reported_at.is_(None),
                ),
            ),
        )

    def find_branch_reservation(
        self,
        host_id: str,
        root: str,
        branch: str,
    ) -> SessionHandoff | None:
        return self._newest(
            "find_handoff_branch_reservation",
            SqlSessionHandoff.host_id == host_id,
            SqlSessionHandoff.root == root,
            SqlSessionHandoff.git_branch == branch,
            SqlSessionHandoff.state.in_(HANDOFF_UNFINISHED_STATES),
        )

    def list_for_sender(
        self,
        sender_session_id: str,
        since: int,
        limit: int,
    ) -> list[SessionHandoff]:
        with self._session("list_sender_handoffs") as session:
            rows = session.scalars(
                select(SqlSessionHandoff)
                .where(
                    SqlSessionHandoff.workspace_id == current_workspace_id(),
                    SqlSessionHandoff.sender_session_id == sender_session_id,
                    SqlSessionHandoff.created_at >= since,
                )
                .order_by(desc(SqlSessionHandoff.created_at), desc(SqlSessionHandoff.id))
                .limit(limit)
            ).all()
            return [_record_to_entity(row) for row in rows]

    def list_needing_work(self, now: int, limit: int) -> list[SessionHandoff]:
        with self._session("list_handoffs_needing_work") as session:
            rows = session.scalars(
                select(SqlSessionHandoff)
                .where(
                    SqlSessionHandoff.workspace_id == current_workspace_id(),
                    or_(
                        SqlSessionHandoff.lease_until.is_(None),
                        SqlSessionHandoff.lease_until < now,
                    ),
                    or_(
                        SqlSessionHandoff.state.in_(HANDOFF_UNFINISHED_STATES),
                        SqlSessionHandoff.result_state == "pending",
                        SqlSessionHandoff.stop_state == "pending",
                    ),
                )
                .order_by(asc(SqlSessionHandoff.updated_at), asc(SqlSessionHandoff.id))
                .limit(limit)
            ).all()
            return [_record_to_entity(row) for row in rows]
