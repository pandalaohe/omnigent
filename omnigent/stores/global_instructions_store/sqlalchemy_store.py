"""SQLAlchemy-backed global instructions store."""

from __future__ import annotations

import json
import uuid
from typing import Literal

from sqlalchemy import desc, func, select
from sqlalchemy.exc import IntegrityError
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
    InstructionRevisionConflict,
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
        expected_revision_id: str | None = None,
        check_revision: bool = False,
    ) -> GlobalInstructionRevision:
        """Append a revision and return it."""
        key = InstructionLayerKey(scope, agent_key, host_id)

        def write(session: Session) -> GlobalInstructionRevision:
            current = self._current(session, key)
            current_id = current.id if current else None
            if check_revision and current_id != expected_revision_id:
                raise InstructionRevisionConflict(
                    "Instruction layer changed; read it again before saving"
                )
            parent_id = (
                current_id
                or "empty:"
                + uuid.uuid5(
                    uuid.NAMESPACE_URL, json.dumps([key.scope, key.agent_key, key.host_id])
                ).hex
            )
            row = SqlGlobalInstructionRevision(
                id=uuid.uuid4().hex,
                text=text,
                created_us=max(now_epoch_us(), current.created_us + 1 if current else 0),
                created_by=created_by,
                scope=key.scope,
                agent_key=key.agent_key,
                host_id=key.host_id,
                parent_revision_id=parent_id,
            )
            session.add(row)
            return _to_entity(row)

        for attempt in range(4):
            try:
                return run_write_transaction(
                    self._session_immediate, "append_global_instruction_revision", write
                )
            except IntegrityError as exc:
                if "uq_instruction_parent" not in str(exc) and (
                    "global_instruction_revisions.workspace_id, "
                    "global_instruction_revisions.parent_revision_id"
                ) not in str(exc):
                    raise
                if check_revision:
                    raise InstructionRevisionConflict(
                        "Instruction layer changed; read it again before saving"
                    ) from exc
                if attempt == 3:
                    raise
        raise AssertionError("instruction save retry loop exited unexpectedly")

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
        self,
        host_id: str | None,
        agent_key: str | None,
        *,
        role: Literal["mother", "child"] | None = None,
    ) -> list[GlobalInstructionRevision]:
        """Read route and optional role keys in a single managed session."""
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
        role_keys = [InstructionLayerKey(role)] if role else []
        if role and agent_key:
            role_keys.append(InstructionLayerKey(role, agent_key=agent_key))
        if role and host_id:
            role_keys.append(InstructionLayerKey(role, host_id=host_id))
        if role and host_id and agent_key:
            role_keys.append(InstructionLayerKey(role, agent_key, host_id))
        with self._session("select_route_instruction_layers") as session:
            revisions = []
            if independent is not None:
                revision = self._current(session, independent)
                if revision is not None and revision.text.strip():
                    revisions.append(revision)
                    keys = []
            return revisions + [
                revision
                for key in keys + role_keys
                if (revision := self._current(session, key)) is not None
            ]
