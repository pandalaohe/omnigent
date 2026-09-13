"""SQLAlchemy-backed project-host-binding store."""

from __future__ import annotations

import uuid

from sqlalchemy import asc, select
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlProject, SqlProjectHostBinding, current_workspace_id
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch,
    run_write_transaction,
)
from omnigent.entities import ProjectHostBinding
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.stores.project_host_binding_store import (
    DuplicatePrimaryBindingError,
    ProjectHostBindingStore,
)


def _to_entity(row: SqlProjectHostBinding) -> ProjectHostBinding:
    """
    Convert a :class:`SqlProjectHostBinding` ORM row to a
    :class:`ProjectHostBinding`.

    :param row: The SQLAlchemy ORM row to convert.
    :returns: A :class:`ProjectHostBinding` dataclass instance.
    """
    return ProjectHostBinding(
        id=row.id,
        project_id=row.project_id,
        host_id=row.host_id,
        name=row.name,
        repository_id=row.repository_id,
        workspace=row.workspace,
        revision=row.revision,
        created_at=row.created_at,
        is_primary=row.is_primary,
        enabled=row.enabled,
        path_verified_at=row.path_verified_at,
        updated_at=row.updated_at,
        workspace_id=row.workspace_id,
    )


def _lock_project(session: Session, *, project_id: str) -> None:
    """Lock the project row; raise ``NOT_FOUND`` when it is absent.

    :param session: The active SQLAlchemy session.
    :param project_id: The project to lock.
    """
    project = (
        session.execute(
            select(SqlProject)
            .where(
                SqlProject.workspace_id == current_workspace_id(),
                SqlProject.id == project_id,
            )
            .with_for_update()
        )
        .scalars()
        .first()
    )
    if project is None:
        raise OmnigentError(f"project {project_id} not found", code=ErrorCode.NOT_FOUND)


class SqlAlchemyProjectHostBindingStore(ProjectHostBindingStore):
    """
    SQLAlchemy-backed implementation of :class:`ProjectHostBindingStore`.

    Persists per-host directory bindings in a relational database via the
    SQLAlchemy ORM. Every query is scoped by ``workspace_id`` (tenant
    partition).
    """

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the SQLAlchemy project-host-binding store.

        Creates or reuses a SQLAlchemy engine and session factory for the
        given database URI.

        :param storage_location: SQLAlchemy database URI,
            e.g. ``"sqlite:///chat.db"``.
        """
        super().__init__(storage_location)
        self._engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            self._engine,
            query_name_prefix="omnigent.project_host_binding_store",
        )
        self._session_immediate = make_named_managed_session_maker(
            self._engine,
            query_name_prefix="omnigent.project_host_binding_store",
            immediate=True,
        )

    def _other_primary(
        self,
        session: Session,
        *,
        project_id: str,
        host_id: str,
        exclude_id: str | None,
    ) -> bool:
        """Return whether another primary binding exists for ``(project, host)``.

        The sole enforcement point for the one-primary invariant: there is
        no partial unique index behind it, so callers must hold the owning
        project row lock (see ``upsert``) to serialize concurrent writers.

        :param session: The active SQLAlchemy session.
        :param project_id: The project scope.
        :param host_id: The host scope.
        :param exclude_id: A binding id to exclude (the row being updated).
        :returns: ``True`` if a different primary binding already exists.
        """
        stmt = (
            select(SqlProjectHostBinding.id)
            .where(SqlProjectHostBinding.workspace_id == current_workspace_id())
            .where(SqlProjectHostBinding.project_id == project_id)
            .where(SqlProjectHostBinding.host_id == host_id)
            .where(SqlProjectHostBinding.is_primary.is_(True))
        )
        if exclude_id is not None:
            stmt = stmt.where(SqlProjectHostBinding.id != exclude_id)
        return session.execute(stmt).first() is not None

    def upsert(
        self,
        *,
        project_id: str,
        host_id: str,
        name: str,
        repository_id: str,
        workspace: str,
        is_primary: bool = False,
        enabled: bool = True,
    ) -> ProjectHostBinding:
        """Register a binding or revise it, bumping ``revision`` on change.

        A requested primary is rejected when another binding is already
        primary for the ``(project, host)`` pair; the existing primary is
        never silently cleared.
        """

        def write(session: Session) -> ProjectHostBinding:
            # Project row first, binding row second: one lock order per
            # project, so concurrent writers serialize instead of racing.
            _lock_project(session, project_id=project_id)
            stmt = (
                select(SqlProjectHostBinding)
                .where(SqlProjectHostBinding.workspace_id == current_workspace_id())
                .where(SqlProjectHostBinding.project_id == project_id)
                .where(SqlProjectHostBinding.host_id == host_id)
                .where(SqlProjectHostBinding.name == name)
            )
            row = session.execute(stmt).scalars().first()
            if row is None:
                if is_primary and self._other_primary(
                    session, project_id=project_id, host_id=host_id, exclude_id=None
                ):
                    raise DuplicatePrimaryBindingError(project_id, host_id)
                now = now_epoch()
                row = SqlProjectHostBinding(
                    id=uuid.uuid4().hex,
                    project_id=project_id,
                    host_id=host_id,
                    name=name,
                    is_primary=is_primary,
                    repository_id=repository_id,
                    workspace=workspace,
                    enabled=enabled,
                    revision=1,
                    path_verified_at=None,
                    created_at=now,
                    updated_at=None,
                )
                session.add(row)
                session.flush()
                return _to_entity(row)
            if (
                is_primary
                and not row.is_primary
                and self._other_primary(
                    session, project_id=project_id, host_id=host_id, exclude_id=row.id
                )
            ):
                raise DuplicatePrimaryBindingError(project_id, host_id)
            if (
                row.is_primary == is_primary
                and row.repository_id == repository_id
                and row.workspace == workspace
                and row.enabled == enabled
            ):
                return _to_entity(row)
            row.is_primary = is_primary
            row.repository_id = repository_id
            row.workspace = workspace
            row.enabled = enabled
            row.revision += 1
            row.updated_at = now_epoch()
            session.flush()
            return _to_entity(row)

        return run_write_transaction(self._session_immediate, "upsert_binding", write)

    def get(self, binding_id: str) -> ProjectHostBinding | None:
        """Return a binding by id, or ``None`` if not found."""
        with self._session("select_binding_by_id") as session:
            row = session.get(SqlProjectHostBinding, (current_workspace_id(), binding_id))
            if row is None:
                return None
            return _to_entity(row)

    def list_by_project(self, project_id: str) -> list[ProjectHostBinding]:
        """List a project's bindings ordered by ``created_at ASC, id ASC``."""
        with self._session("list_bindings_by_project") as session:
            stmt = (
                select(SqlProjectHostBinding)
                .where(SqlProjectHostBinding.workspace_id == current_workspace_id())
                .where(SqlProjectHostBinding.project_id == project_id)
                .order_by(asc(SqlProjectHostBinding.created_at), asc(SqlProjectHostBinding.id))
            )
            rows = session.execute(stmt).scalars().all()
            return [_to_entity(r) for r in rows]

    def list_by_host(self, *, project_id: str, host_id: str) -> list[ProjectHostBinding]:
        """List one host's bindings ordered by ``created_at ASC, id ASC``."""
        with self._session("list_bindings_by_host") as session:
            stmt = (
                select(SqlProjectHostBinding)
                .where(SqlProjectHostBinding.workspace_id == current_workspace_id())
                .where(SqlProjectHostBinding.project_id == project_id)
                .where(SqlProjectHostBinding.host_id == host_id)
                .order_by(asc(SqlProjectHostBinding.created_at), asc(SqlProjectHostBinding.id))
            )
            rows = session.execute(stmt).scalars().all()
            return [_to_entity(r) for r in rows]

    def delete(self, binding_id: str) -> bool:
        """Delete a binding. Idempotent; ``False`` if not found."""

        def write(session: Session) -> bool:
            row = session.get(SqlProjectHostBinding, (current_workspace_id(), binding_id))
            if row is None:
                return False
            _lock_project(session, project_id=row.project_id)
            session.delete(row)
            return True

        return run_write_transaction(self._session_immediate, "delete_binding", write)
