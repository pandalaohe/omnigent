"""SQLAlchemy-backed project-host-binding store."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import replace
from typing import Any

from sqlalchemy import asc, or_, select
from sqlalchemy.orm import Session

from omnigent.calling_defaults import default_agent_for_host
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
    ARCHIVE_WORKTREE_ADMISSION_FENCE_ALIAS_PREFIX,
    ARCHIVE_WORKTREE_ADMISSION_FENCE_LABEL_KEY,
    worktree_admission_ancestor_fingerprints,
)
from omnigent.stores.host_store import HostStore
from omnigent.stores.project_host_binding_store import ProjectHostBindingStore
from omnigent.stores.project_store.sqlalchemy_store import _decode_config, _encode_config
from omnigent.stores.project_store.sqlalchemy_store import _to_entity as _project_to_entity


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


def _settings_revision(config: dict[str, Any], entries: list[ProjectHostEntry]) -> str:
    """Hash the explicit settings, ignoring timestamps and row ordering."""
    payload = {
        "config": config,
        "entries": {entry.host_id: entry.workspace for entry in entries},
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _lock_project(session: Session, *, project_id: str) -> SqlProject:
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
    return project


def _initialize_host_agent(
    session: Session,
    project: SqlProject,
    host_id: str,
    candidate: str | None,
    project_has_agent: bool | None = None,
) -> str | None:
    if not candidate:
        return None
    entry = session.get(SqlProjectHostEntry, (current_workspace_id(), project.id, host_id))
    binding = session.scalar(
        select(SqlProjectHostBinding.id)
        .where(
            SqlProjectHostBinding.workspace_id == current_workspace_id(),
            SqlProjectHostBinding.project_id == project.id,
            SqlProjectHostBinding.host_id == host_id,
        )
        .limit(1)
    )
    config = _decode_config(project.config)
    has_agent = bool(config.get("agent_id")) if project_has_agent is None else project_has_agent
    if entry or binding or has_agent or default_agent_for_host(config, host_id):
        return None
    defaults = dict(config.get("calling_defaults") or {})
    defaults[host_id] = {**defaults.get(host_id, {}), "agent_id": candidate}
    config["calling_defaults"] = defaults
    project.config = _encode_config(config)
    project.updated_at = now_epoch()
    return candidate


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
                        or_(
                            SqlConversationLabel.key == ARCHIVE_WORKTREE_ADMISSION_FENCE_LABEL_KEY,
                            SqlConversationLabel.key.startswith(
                                ARCHIVE_WORKTREE_ADMISSION_FENCE_ALIAS_PREFIX, autoescape=True
                            ),
                        ),
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
        initial_agent_id: str | None = None,
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
            project = _lock_project(session, project_id=project_id)
            _initialize_host_agent(session, project, host_id, initial_agent_id)
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

    def settings_snapshot(
        self, project_id: str, *, caller_id: str | None, is_admin: bool = False
    ) -> tuple[Any, list[ProjectHostEntry], str]:
        """Read config and entries under the same project-row lock as every entry writer."""

        def read(session: Session) -> tuple[Any, list[ProjectHostEntry], str]:
            project = _lock_project(session, project_id=project_id)
            if not is_admin and project.user_id != caller_id:
                raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
            entries = [
                _entry_to_entity(row)
                for row in session.scalars(
                    select(SqlProjectHostEntry)
                    .where(
                        SqlProjectHostEntry.workspace_id == current_workspace_id(),
                        SqlProjectHostEntry.project_id == project_id,
                    )
                    .order_by(asc(SqlProjectHostEntry.host_id))
                )
            ]
            entity = _project_to_entity(project)
            return entity, entries, _settings_revision(entity.config, entries)

        return run_write_transaction(self._session_immediate, "project_settings.snapshot", read)

    def update_settings(
        self,
        project_id: str,
        *,
        caller_id: str | None,
        is_admin: bool,
        expected_revision: str,
        config: dict[str, Any],
        entry_host_id: str | None = None,
        entry_workspace: str | None = None,
        entry_change: bool = False,
        initial_agent_id: str | None = None,
    ) -> tuple[Any, list[ProjectHostEntry], str, bool, bool, str | None]:
        """Guard and commit a config/entry update atomically after external validation."""

        def write(
            session: Session,
        ) -> tuple[Any, list[ProjectHostEntry], str, bool, bool, str | None]:
            project = _lock_project(session, project_id=project_id)
            if not is_admin and project.user_id != caller_id:
                raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
            rows = list(
                session.scalars(
                    select(SqlProjectHostEntry)
                    .where(
                        SqlProjectHostEntry.workspace_id == current_workspace_id(),
                        SqlProjectHostEntry.project_id == project_id,
                    )
                    .order_by(asc(SqlProjectHostEntry.host_id))
                )
            )
            before = [_entry_to_entity(row) for row in rows]
            if _settings_revision(_decode_config(project.config), before) != expected_revision:
                raise OmnigentError(
                    "Project settings changed. Read again before updating.",
                    code=ErrorCode.CONFLICT,
                )
            if "calling_defaults" in config:
                from omnigent.calling_defaults import validate_project_calling_defaults

                validate_project_calling_defaults(config["calling_defaults"])
            encoded = _encode_config(config)
            config_changed = project.config != encoded
            if config_changed:
                project.config = encoded
                project.updated_at = now_epoch()
            entry_changed = False
            seeded_agent = None
            if entry_change and entry_host_id is not None:
                row = next((item for item in rows if item.host_id == entry_host_id), None)
                if entry_workspace is None:
                    if row is not None:
                        session.delete(row)
                        rows.remove(row)
                        entry_changed = True
                elif row is None:
                    seeded_agent = _initialize_host_agent(
                        session, project, entry_host_id, initial_agent_id
                    )
                    row = SqlProjectHostEntry(
                        project_id=project_id,
                        host_id=entry_host_id,
                        workspace=entry_workspace,
                        created_at=now_epoch(),
                        updated_at=None,
                    )
                    session.add(row)
                    rows.append(row)
                    entry_changed = True
                elif row.workspace != entry_workspace:
                    row.workspace = entry_workspace
                    row.updated_at = now_epoch()
                    entry_changed = True
            session.flush()
            entity = _project_to_entity(project)
            after = [_entry_to_entity(row) for row in rows]
            return (
                entity,
                after,
                _settings_revision(entity.config, after),
                config_changed or seeded_agent is not None,
                entry_changed,
                seeded_agent,
            )

        token = None
        if entry_change and entry_host_id and entry_workspace is not None:
            token = self._host_store.acquire_worktree_admission(entry_host_id)
        try:
            if entry_change and entry_host_id and entry_workspace is not None:
                if self._worktree_admission_fenced(entry_host_id, entry_workspace):
                    raise OmnigentError(
                        "Worktree was removed during project entry admission; refresh its path",
                        code=ErrorCode.CONFLICT,
                    )
            return run_write_transaction(self._session_immediate, "project_settings.update", write)
        finally:
            if token is not None and entry_host_id is not None:
                self._host_store.release_cli_retention(entry_host_id, token)

    def put_entry(
        self,
        project_id: str,
        host_id: str,
        workspace: str,
        *,
        initial_agent_id: str | None = None,
        project_has_agent: bool | None = None,
    ) -> ProjectHostEntry:
        """Register a host's entry path or move it."""

        def write(session: Session) -> ProjectHostEntry:
            # Project row first: one lock order per project, and an unknown
            # project is refused rather than left with an orphan entry.
            project = _lock_project(session, project_id=project_id)
            row = session.get(SqlProjectHostEntry, (current_workspace_id(), project_id, host_id))
            if row is None:
                _initialize_host_agent(
                    session, project, host_id, initial_agent_id, project_has_agent
                )
                row = SqlProjectHostEntry(
                    project_id=project_id,
                    host_id=host_id,
                    workspace=workspace,
                    created_at=now_epoch(),
                    updated_at=None,
                )
                session.add(row)
                session.flush()
            elif row.workspace != workspace:
                row.workspace = workspace
                row.updated_at = now_epoch()
                session.flush()
            return replace(
                _entry_to_entity(row),
                host_agent_id=default_agent_for_host(_decode_config(project.config), host_id),
            )

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
            _lock_project(session, project_id=project_id)
            row = session.get(SqlProjectHostEntry, (current_workspace_id(), project_id, host_id))
            if row is None:
                return False
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
