"""SQLAlchemy-backed project-repository store."""

from __future__ import annotations

import uuid

from sqlalchemy import asc, select
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlProject, SqlProjectRepository, current_workspace_id
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch,
    run_write_transaction,
)
from omnigent.entities import ProjectRepository
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.stores.project_repository_store import ProjectRepositoryStore


def _to_entity(row: SqlProjectRepository) -> ProjectRepository:
    """
    Convert a :class:`SqlProjectRepository` ORM row to a
    :class:`ProjectRepository`.

    :param row: The SQLAlchemy ORM row to convert.
    :returns: A :class:`ProjectRepository` dataclass instance.
    """
    return ProjectRepository(
        id=row.id,
        project_id=row.project_id,
        name=row.name,
        remote_url=row.remote_url,
        default_branch=row.default_branch,
        context_manifest_path=row.context_manifest_path,
        revision=row.revision,
        created_at=row.created_at,
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


class SqlAlchemyProjectRepositoryStore(ProjectRepositoryStore):
    """
    SQLAlchemy-backed implementation of :class:`ProjectRepositoryStore`.

    Persists registered repositories in a relational database via the
    SQLAlchemy ORM. Every query is scoped by ``workspace_id`` (tenant
    partition).
    """

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the SQLAlchemy project-repository store.

        Creates or reuses a SQLAlchemy engine and session factory for the
        given database URI.

        :param storage_location: SQLAlchemy database URI,
            e.g. ``"sqlite:///chat.db"``.
        """
        super().__init__(storage_location)
        self._engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            self._engine,
            query_name_prefix="omnigent.project_repository_store",
        )
        self._session_immediate = make_named_managed_session_maker(
            self._engine,
            query_name_prefix="omnigent.project_repository_store",
            immediate=True,
        )

    def upsert(
        self,
        *,
        project_id: str,
        name: str,
        remote_url: str,
        default_branch: str,
        context_manifest_path: str = ".agents/project/manifest.json",
    ) -> ProjectRepository:
        """Register a repository or revise it, bumping ``revision`` on change."""

        def write(session: Session) -> ProjectRepository:
            # Project row first, repository row second: one lock order per
            # project, so concurrent writers serialize instead of racing.
            _lock_project(session, project_id=project_id)
            stmt = (
                select(SqlProjectRepository)
                .where(SqlProjectRepository.workspace_id == current_workspace_id())
                .where(SqlProjectRepository.project_id == project_id)
                .where(SqlProjectRepository.name == name)
            )
            row = session.execute(stmt).scalars().first()
            if row is None:
                now = now_epoch()
                row = SqlProjectRepository(
                    id=uuid.uuid4().hex,
                    project_id=project_id,
                    name=name,
                    remote_url=remote_url,
                    default_branch=default_branch,
                    context_manifest_path=context_manifest_path,
                    revision=1,
                    created_at=now,
                    updated_at=None,
                )
                session.add(row)
                session.flush()
                return _to_entity(row)
            if (
                row.remote_url == remote_url
                and row.default_branch == default_branch
                and row.context_manifest_path == context_manifest_path
            ):
                return _to_entity(row)
            row.remote_url = remote_url
            row.default_branch = default_branch
            row.context_manifest_path = context_manifest_path
            row.revision += 1
            row.updated_at = now_epoch()
            session.flush()
            return _to_entity(row)

        return run_write_transaction(self._session_immediate, "upsert_repository", write)

    def get(self, repository_id: str) -> ProjectRepository | None:
        """Return a registered repository by id, or ``None`` if not found."""
        with self._session("select_repository_by_id") as session:
            row = session.get(SqlProjectRepository, (current_workspace_id(), repository_id))
            if row is None:
                return None
            return _to_entity(row)

    def get_by_name(self, *, project_id: str, name: str) -> ProjectRepository | None:
        """Return a project's repository by name, or ``None`` if not found."""
        with self._session("select_repository_by_name") as session:
            stmt = (
                select(SqlProjectRepository)
                .where(SqlProjectRepository.workspace_id == current_workspace_id())
                .where(SqlProjectRepository.project_id == project_id)
                .where(SqlProjectRepository.name == name)
            )
            row = session.execute(stmt).scalars().first()
            return _to_entity(row) if row is not None else None

    def list_by_project(self, project_id: str) -> list[ProjectRepository]:
        """List a project's repositories ordered by ``created_at ASC, id ASC``."""
        with self._session("list_repositories_by_project") as session:
            stmt = (
                select(SqlProjectRepository)
                .where(SqlProjectRepository.workspace_id == current_workspace_id())
                .where(SqlProjectRepository.project_id == project_id)
                .order_by(asc(SqlProjectRepository.created_at), asc(SqlProjectRepository.id))
            )
            rows = session.execute(stmt).scalars().all()
            return [_to_entity(r) for r in rows]

    def delete(self, repository_id: str) -> bool:
        """Delete a registered repository. Idempotent; ``False`` if not found."""

        def write(session: Session) -> bool:
            row = session.get(SqlProjectRepository, (current_workspace_id(), repository_id))
            if row is None:
                return False
            _lock_project(session, project_id=row.project_id)
            session.delete(row)
            return True

        return run_write_transaction(self._session_immediate, "delete_repository", write)
