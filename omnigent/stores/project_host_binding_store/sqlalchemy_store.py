"""SQLAlchemy-backed project-host-binding store."""

from __future__ import annotations

import uuid

from sqlalchemy import asc, or_, select
from sqlalchemy.orm import Session

from omnigent.db.db_models import (
    SqlConversationLabel,
    SqlProject,
    SqlProjectHostBinding,
    SqlProjectHostEntry,
    SqlProjectRepository,
    current_workspace_id,
)
from omnigent.db.utils import (
    get_or_create_conversation_engine,
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch,
    run_write_transaction,
)
from omnigent.entities import ProjectHostBinding, ProjectHostEntry
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.stores.conversation_store import (
    ARCHIVE_WORKTREE_ADMISSION_FENCE_LABEL_KEY,
    worktree_admission_ancestor_fingerprints,
)
from omnigent.stores.host_store import HostStore
from omnigent.stores.project_host_binding_store import ProjectHostBindingStore


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


def _entry_to_entity(row: SqlProjectHostEntry) -> ProjectHostEntry:
    """
    Convert a :class:`SqlProjectHostEntry` ORM row to a
    :class:`ProjectHostEntry`.

    :param row: The SQLAlchemy ORM row to convert.
    :returns: A :class:`ProjectHostEntry` dataclass instance.
    """
    return ProjectHostEntry(
        project_id=row.project_id,
        host_id=row.host_id,
        workspace=row.workspace,
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


def derive_primary_bindings(session: Session, *, project_id: str) -> list[ProjectHostBinding]:
    """Re-derive every host's primary binding from the project's code repository.

    Per host, candidates are the enabled bindings of the project's code
    repository; the one already primary wins, else the oldest (``created_at``,
    then ``id``). The target is set primary and the host's other bindings are
    cleared, bumping ``revision`` and ``updated_at`` on every changed row. No
    code repository, or no enabled candidate on a host, leaves that host with
    no primary. The caller must hold the project row lock and the same
    transaction: derivation is authoritative and never rejects a state it
    transitions through.

    :param session: The active SQLAlchemy session.
    :param project_id: The project whose bindings are re-derived.
    :returns: The changed bindings as entities, or an empty list.
    """
    code_repository_id = session.execute(
        select(SqlProjectRepository.id)
        .where(SqlProjectRepository.workspace_id == current_workspace_id())
        .where(SqlProjectRepository.project_id == project_id)
        .where(SqlProjectRepository.role == "code")
        .order_by(asc(SqlProjectRepository.created_at), asc(SqlProjectRepository.id))
        .limit(1)
    ).scalar_one_or_none()
    rows = (
        session.execute(
            select(SqlProjectHostBinding)
            .where(SqlProjectHostBinding.workspace_id == current_workspace_id())
            .where(SqlProjectHostBinding.project_id == project_id)
            .order_by(asc(SqlProjectHostBinding.created_at), asc(SqlProjectHostBinding.id))
            .execution_options(populate_existing=True)
        )
        .scalars()
        .all()
    )
    by_host: dict[str, list[SqlProjectHostBinding]] = {}
    for row in rows:
        by_host.setdefault(row.host_id, []).append(row)
    changed: list[ProjectHostBinding] = []
    now = now_epoch()
    for host_rows in by_host.values():
        candidates = [
            row for row in host_rows if row.enabled and row.repository_id == code_repository_id
        ]
        target = next((row for row in candidates if row.is_primary), None)
        if target is None and candidates:
            target = candidates[0]
        for row in host_rows:
            wanted = row is target
            if row.is_primary != wanted:
                row.is_primary = wanted
                row.revision += 1
                row.updated_at = now
                changed.append(_to_entity(row))
    return changed


class SqlAlchemyProjectHostBindingStore(ProjectHostBindingStore):
    """
    SQLAlchemy-backed implementation of :class:`ProjectHostBindingStore`.

    Persists per-host directory bindings in a relational database via the
    SQLAlchemy ORM. Every query is scoped by ``workspace_id`` (tenant
    partition).
    """

    def __init__(
        self, storage_location: str, conversation_storage_location: str | None = None
    ) -> None:
        """
        Initialize the SQLAlchemy project-host-binding store.

        Creates or reuses a SQLAlchemy engine and session factory for the
        given database URI.

        :param storage_location: SQLAlchemy database URI,
            e.g. ``"sqlite:///chat.db"``.
        """
        super().__init__(storage_location)
        self._host_store = HostStore(storage_location)
        self._engine = get_or_create_engine(storage_location)
        conv_uri = conversation_storage_location or storage_location
        self._conv_engine = (
            self._engine
            if conv_uri == storage_location
            else get_or_create_conversation_engine(conv_uri)
        )
        self._conv_session = make_named_managed_session_maker(
            self._conv_engine,
            query_name_prefix="omnigent.project_host_binding_store",
        )
        self._session = make_named_managed_session_maker(
            self._engine,
            query_name_prefix="omnigent.project_host_binding_store",
        )
        self._session_immediate = make_named_managed_session_maker(
            self._engine,
            query_name_prefix="omnigent.project_host_binding_store",
            immediate=True,
        )

    def _worktree_admission_fenced(self, host_id: str, path: str) -> bool:
        fingerprints = worktree_admission_ancestor_fingerprints(host_id, path)
        with self._conv_session("check_project_entry_worktree_fence") as session:
            return (
                session.execute(
                    select(SqlConversationLabel.conversation_id)
                    .where(
                        SqlConversationLabel.workspace_id == current_workspace_id(),
                        SqlConversationLabel.key == ARCHIVE_WORKTREE_ADMISSION_FENCE_LABEL_KEY,
                        SqlConversationLabel.value.in_(fingerprints),
                    )
                    .limit(1)
                ).first()
                is not None
            )

    def apply_binding(
        self,
        *,
        project_id: str,
        host_id: str,
        name: str,
        repository_id: str,
        workspace: str,
        enabled: bool = True,
        verified: bool = True,
    ) -> ProjectHostBinding:
        """Register a binding or revise it, then derive the host's primary.

        The row's stored ``is_primary`` is kept and the client never sets it;
        after the write, one transaction re-derives every host's primary from
        the project's code repository (see :func:`derive_primary_bindings`).
        ``verified=True`` stamps ``path_verified_at``; ``verified=False``
        stores it null, clearing a stale stamp from an earlier verified save.
        """

        def write(session: Session) -> ProjectHostBinding:
            # Project row first, binding row second: one lock order per
            # project, so concurrent writers serialize instead of racing.
            _lock_project(session, project_id=project_id)
            # Same SqlProject row the repository store locks, so a racing
            # repository delete serializes against this check, not past it.
            repository = session.execute(
                select(SqlProjectRepository.id)
                .where(SqlProjectRepository.workspace_id == current_workspace_id())
                .where(SqlProjectRepository.id == repository_id)
                .where(SqlProjectRepository.project_id == project_id)
            ).first()
            if repository is None:
                raise OmnigentError(
                    f"unknown repository {repository_id!r} on project {project_id}",
                    code=ErrorCode.INVALID_INPUT,
                )
            stmt = (
                select(SqlProjectHostBinding)
                .where(SqlProjectHostBinding.workspace_id == current_workspace_id())
                .where(SqlProjectHostBinding.project_id == project_id)
                .where(SqlProjectHostBinding.host_id == host_id)
                .where(SqlProjectHostBinding.name == name)
            )
            row = session.execute(stmt).scalars().first()
            now = now_epoch()
            if row is None:
                row = SqlProjectHostBinding(
                    id=uuid.uuid4().hex,
                    project_id=project_id,
                    host_id=host_id,
                    name=name,
                    is_primary=False,
                    repository_id=repository_id,
                    workspace=workspace,
                    enabled=enabled,
                    revision=1,
                    path_verified_at=now if verified else None,
                    created_at=now,
                    updated_at=None,
                )
                session.add(row)
                session.flush()
            else:
                core_same = (
                    row.repository_id == repository_id
                    and row.workspace == workspace
                    and row.enabled == enabled
                )
                if not core_same:
                    row.repository_id = repository_id
                    row.workspace = workspace
                    row.enabled = enabled
                    row.revision += 1
                    row.updated_at = now
                if verified:
                    row.path_verified_at = now
                    row.updated_at = now
                else:
                    row.path_verified_at = None
                session.flush()
            derive_primary_bindings(session, project_id=project_id)
            session.flush()
            return _to_entity(row)

        return run_write_transaction(self._session_immediate, "apply_binding", write)

    def record_verification(
        self,
        binding_id: str,
        *,
        expected_revision: int,
        workspace: str,
        path_verified_at: int,
    ) -> ProjectHostBinding | None:
        """Stamp a verification; ``None`` when the row moved under the caller."""

        def write(session: Session) -> ProjectHostBinding | None:
            row = session.get(SqlProjectHostBinding, (current_workspace_id(), binding_id))
            if row is None:
                return None
            # Same SqlProject row the repository store locks, so a racing
            # delete or upsert serializes against this check, not past it.
            _lock_project(session, project_id=row.project_id)
            row = (
                session.execute(
                    select(SqlProjectHostBinding)
                    .where(SqlProjectHostBinding.workspace_id == current_workspace_id())
                    .where(SqlProjectHostBinding.id == binding_id)
                    .execution_options(populate_existing=True)
                )
                .scalars()
                .first()
            )
            if row is None or row.revision != expected_revision:
                return None
            if row.workspace == workspace and row.path_verified_at == path_verified_at:
                return _to_entity(row)
            if row.workspace != workspace:
                row.workspace = workspace
                row.revision += 1
            row.path_verified_at = path_verified_at
            row.updated_at = now_epoch()
            session.flush()
            return _to_entity(row)

        return run_write_transaction(self._session_immediate, "record_binding_verification", write)

    def get(self, binding_id: str) -> ProjectHostBinding | None:
        """Return a binding by id, or ``None`` if not found."""
        with self._session("select_binding_by_id") as session:
            row = session.get(SqlProjectHostBinding, (current_workspace_id(), binding_id))
            if row is None:
                return None
            return _to_entity(row)

    def get_by_name(
        self, *, project_id: str, host_id: str, name: str
    ) -> ProjectHostBinding | None:
        """Return one host's binding by name, or ``None`` if not found."""
        with self._session("select_binding_by_name") as session:
            stmt = (
                select(SqlProjectHostBinding)
                .where(SqlProjectHostBinding.workspace_id == current_workspace_id())
                .where(SqlProjectHostBinding.project_id == project_id)
                .where(SqlProjectHostBinding.host_id == host_id)
                .where(SqlProjectHostBinding.name == name)
            )
            row = session.execute(stmt).scalars().first()
            return _to_entity(row) if row is not None else None

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

    def delete_binding(self, project_id: str, host_id: str, name: str) -> bool:
        """Delete a binding and re-derive the host's primary. Idempotent."""

        def write(session: Session) -> bool:
            stmt = (
                select(SqlProjectHostBinding)
                .where(SqlProjectHostBinding.workspace_id == current_workspace_id())
                .where(SqlProjectHostBinding.project_id == project_id)
                .where(SqlProjectHostBinding.host_id == host_id)
                .where(SqlProjectHostBinding.name == name)
            )
            row = session.execute(stmt).scalars().first()
            if row is None:
                return False
            _lock_project(session, project_id=project_id)
            session.delete(row)
            session.flush()
            derive_primary_bindings(session, project_id=project_id)
            session.flush()
            return True

        return run_write_transaction(self._session_immediate, "delete_binding", write)

    def list_entries(self, project_id: str) -> list[ProjectHostEntry]:
        """List a project's entries ordered by ``host_id ASC``."""
        with self._session("project_entries.list") as session:
            stmt = (
                select(SqlProjectHostEntry)
                .where(SqlProjectHostEntry.workspace_id == current_workspace_id())
                .where(SqlProjectHostEntry.project_id == project_id)
                .order_by(asc(SqlProjectHostEntry.host_id))
            )
            rows = session.execute(stmt).scalars().all()
            return [_entry_to_entity(r) for r in rows]

    def put_entry(self, project_id: str, host_id: str, workspace: str) -> ProjectHostEntry:
        """Register a host's entry path or move it."""

        def write(session: Session) -> ProjectHostEntry:
            # Project row first: one lock order per project, and an unknown
            # project is refused rather than left with an orphan entry.
            _lock_project(session, project_id=project_id)
            row = session.get(SqlProjectHostEntry, (current_workspace_id(), project_id, host_id))
            if row is None:
                row = SqlProjectHostEntry(
                    project_id=project_id,
                    host_id=host_id,
                    workspace=workspace,
                    created_at=now_epoch(),
                    updated_at=None,
                )
                session.add(row)
                session.flush()
                return _entry_to_entity(row)
            if row.workspace == workspace:
                return _entry_to_entity(row)
            row.workspace = workspace
            row.updated_at = now_epoch()
            session.flush()
            return _entry_to_entity(row)

        token = self._host_store.acquire_worktree_admission(host_id)
        try:
            if self._worktree_admission_fenced(host_id, workspace):
                raise OmnigentError(
                    "Worktree was removed during project entry admission; refresh its path",
                    code=ErrorCode.CONFLICT,
                )
            return run_write_transaction(self._session_immediate, "project_entries.put", write)
        finally:
            if token is not None:
                self._host_store.release_cli_retention(host_id, token)

    def delete_entry(self, project_id: str, host_id: str) -> bool:
        """Delete a host's entry. Idempotent; ``False`` if not found."""

        def write(session: Session) -> bool:
            row = session.get(SqlProjectHostEntry, (current_workspace_id(), project_id, host_id))
            if row is None:
                return False
            _lock_project(session, project_id=project_id)
            session.delete(row)
            return True

        return run_write_transaction(self._session_immediate, "project_entries.delete", write)

    def entry_at_or_under(self, host_id: str, workspace: str) -> bool:
        """Return whether any project has an entry at or inside ``workspace``.

        A trailing separator on the caller's path is ignored — entry rows
        store canonical paths — and the separator appended for the prefix
        match is literal on either path separator. ``startswith`` escapes
        ``%`` and ``_`` so they cannot widen the match.
        """
        base = workspace.rstrip("/\\")
        with self._session("project_entries.at_or_under") as session:
            stmt = (
                select(SqlProjectHostEntry.project_id)
                .where(SqlProjectHostEntry.workspace_id == current_workspace_id())
                .where(SqlProjectHostEntry.host_id == host_id)
                .where(
                    or_(
                        SqlProjectHostEntry.workspace == base,
                        SqlProjectHostEntry.workspace.startswith(f"{base}/", autoescape=True),
                        SqlProjectHostEntry.workspace.startswith(f"{base}\\", autoescape=True),
                    )
                )
                .limit(1)
            )
            return session.execute(stmt).first() is not None
