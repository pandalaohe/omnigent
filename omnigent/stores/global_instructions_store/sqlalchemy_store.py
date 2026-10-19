"""SQLAlchemy-backed global instructions store."""

from __future__ import annotations

import uuid

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session, aliased

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
    InstructionLayerKey,
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
        scope=row.scope,
        agent_key=row.agent_key,
        host_id=row.host_id,
        created_us=row.created_us,
    )


class SqlAlchemyGlobalInstructionsStore(GlobalInstructionsStore):
    """
    SQLAlchemy-backed implementation of :class:`GlobalInstructionsStore`.

    The newest row for each layer in the current workspace is live.
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

    @staticmethod
    def _layer_query(key: InstructionLayerKey):
        return (
            select(SqlGlobalInstructionRevision)
            .where(
                SqlGlobalInstructionRevision.workspace_id == current_workspace_id(),
                SqlGlobalInstructionRevision.scope == key.scope,
                SqlGlobalInstructionRevision.agent_key == key.agent_key,
                SqlGlobalInstructionRevision.host_id == key.host_id,
            )
            .order_by(
                desc(SqlGlobalInstructionRevision.created_us),
                desc(SqlGlobalInstructionRevision.id),
            )
        )

    def _current(
        self, session: Session, key: InstructionLayerKey
    ) -> GlobalInstructionRevision | None:
        row = session.scalars(self._layer_query(key).limit(1)).first()
        return _to_entity(row) if row is not None else None

    def current(
        self, *, scope: str = "shared", agent_key: str = "", host_id: str = ""
    ) -> GlobalInstructionRevision | None:
        """Return the newest revision of a layer; legacy callers read shared."""
        key = InstructionLayerKey(scope, agent_key, host_id)
        with self._session("select_current_global_instruction") as session:
            return self._current(session, key)

    def save(
        self,
        text: str,
        *,
        created_by: str | None,
        scope: str = "shared",
        agent_key: str = "",
        host_id: str = "",
    ) -> GlobalInstructionRevision:
        """Append a revision and return it."""
        key = InstructionLayerKey(scope, agent_key, host_id)
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
                scope=key.scope,
                agent_key=key.agent_key,
                host_id=key.host_id,
            )
            session.add(row)
            return _to_entity(row)

        return run_write_transaction(
            self._session_immediate,
            "insert_global_instruction_revision",
            write,
        )

    def list_revisions(
        self,
        *,
        limit: int = 50,
        scope: str = "shared",
        agent_key: str = "",
        host_id: str = "",
    ) -> list[GlobalInstructionRevision]:
        """List one layer's history newest first."""
        key = InstructionLayerKey(scope, agent_key, host_id)
        if not 1 <= limit <= 200:
            raise ValueError("Instruction revision limit must be between 1 and 200")
        with self._session("list_global_instruction_revisions") as session:
            rows = session.scalars(self._layer_query(key).limit(limit)).all()
            return [_to_entity(r) for r in rows]

    def list_layers(self) -> list[GlobalInstructionRevision]:
        """Read the newest row for each layer without resurrecting cleared text."""
        model = SqlGlobalInstructionRevision
        ranked = (
            select(
                model,
                func.row_number()
                .over(
                    partition_by=(model.scope, model.agent_key, model.host_id),
                    order_by=(desc(model.created_us), desc(model.id)),
                )
                .label("layer_rank"),
            )
            .where(model.workspace_id == current_workspace_id())
            .subquery()
        )
        latest = aliased(model, ranked)
        with self._session("list_current_instruction_layers") as session:
            rows = session.scalars(
                select(latest)
                .where(ranked.c.layer_rank == 1)
                .order_by(latest.scope, latest.agent_key, latest.host_id)
            ).all()
            return [_to_entity(row) for row in rows]

    def latest_layers(
        self, host_id: str | None, agent_key: str | None
    ) -> list[GlobalInstructionRevision]:
        """Read one route through its indexed keys in a single managed session."""
        keys = [InstructionLayerKey()]
        if agent_key:
            keys.append(InstructionLayerKey("agent", agent_key=agent_key))
        if host_id:
            keys.append(InstructionLayerKey("host", host_id=host_id))
        independent = (
            InstructionLayerKey("independent", agent_key, host_id)
            if host_id and agent_key
            else None
        )
        with self._session("select_route_instruction_layers") as session:
            if independent is not None:
                revision = self._current(session, independent)
                if revision is not None and revision.text.strip():
                    return [revision]
            return [
                revision for key in keys if (revision := self._current(session, key)) is not None
            ]
