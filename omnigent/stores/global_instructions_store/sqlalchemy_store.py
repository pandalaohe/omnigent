"""SQLAlchemy-backed global instructions store."""

from __future__ import annotations

import uuid

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlGlobalInstructionRevision, current_workspace_id
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch_us,
    run_write_transaction,
)
from omnigent.stores.global_instructions_store import (
    GlobalInstructionRevision,
    GlobalInstructionsStore,
)


def _to_entity(row: SqlGlobalInstructionRevision) -> GlobalInstructionRevision:
    """
    Convert a :class:`SqlGlobalInstructionRevision` ORM row to an entity.

    :param row: The SQLAlchemy ORM row to convert.
    :returns: A :class:`GlobalInstructionRevision` dataclass instance.
    """
    return GlobalInstructionRevision(
        id=row.id,
        text=row.text,
        created_at=row.created_us // 1_000_000,
        created_by=row.created_by,
    )


class SqlAlchemyGlobalInstructionsStore(GlobalInstructionsStore):
    """
    SQLAlchemy-backed implementation of :class:`GlobalInstructionsStore`.

    Persists one row per save in the workspace the process is bound to;
    the newest row is the live value.
    """

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the SQLAlchemy global instructions store.

        :param storage_location: SQLAlchemy database URI,
            e.g. ``"sqlite:///chat.db"``.
        """
        super().__init__(storage_location)
        self._engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            self._engine,
            query_name_prefix="omnigent.global_instructions_store",
        )
        self._session_immediate = make_named_managed_session_maker(
            self._engine,
            query_name_prefix="omnigent.global_instructions_store",
            immediate=True,
        )

    def current(self) -> GlobalInstructionRevision | None:
        """Return the newest revision, or ``None`` when nothing was saved."""
        with self._session("select_current_global_instruction") as session:
            row = (
                session.execute(
                    select(SqlGlobalInstructionRevision)
                    .where(SqlGlobalInstructionRevision.workspace_id == current_workspace_id())
                    .order_by(
                        desc(SqlGlobalInstructionRevision.created_us),
                        desc(SqlGlobalInstructionRevision.id),
                    )
                    .limit(1)
                )
                .scalars()
                .first()
            )
            return _to_entity(row) if row is not None else None

    def save(self, text: str, *, created_by: str | None) -> GlobalInstructionRevision:
        """Append a revision and return it."""
        # µs, not seconds: back-to-back saves must still order newest-first
        # without the write having to read the current max back.
        created_us = now_epoch_us()
        revision_id = uuid.uuid4().hex

        def write(session: Session) -> GlobalInstructionRevision:
            row = SqlGlobalInstructionRevision(
                id=revision_id,
                text=text,
                created_us=created_us,
                created_by=created_by,
            )
            session.add(row)
            session.flush()
            return _to_entity(row)

        return run_write_transaction(
            self._session_immediate,
            "insert_global_instruction_revision",
            write,
        )

    def list_revisions(self, *, limit: int = 50) -> list[GlobalInstructionRevision]:
        """List revisions newest first."""
        with self._session("list_global_instruction_revisions") as session:
            rows = (
                session.execute(
                    select(SqlGlobalInstructionRevision)
                    .where(SqlGlobalInstructionRevision.workspace_id == current_workspace_id())
                    .order_by(
                        desc(SqlGlobalInstructionRevision.created_us),
                        desc(SqlGlobalInstructionRevision.id),
                    )
                    .limit(limit)
                )
                .scalars()
                .all()
            )
            return [_to_entity(r) for r in rows]
