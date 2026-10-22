"""SQLAlchemy-backed project store."""

from __future__ import annotations

import builtins
import json
import re
import uuid
from collections.abc import Mapping
from typing import Any, Literal

import zstandard
from sqlalchemy import LargeBinary, asc, select, type_coerce, update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session
from sqlalchemy.sql.dml import Insert

from omnigent.calling_defaults import validate_project_calling_defaults
from omnigent.db.account_authority import require_active_account
from omnigent.db.compression import decode, encode
from omnigent.db.db_models import SqlPreference, SqlProject, current_workspace_id
from omnigent.db.utils import (
    get_or_create_engine,
    is_postgresql_family,
    make_named_managed_session_maker,
    now_epoch,
    run_write_transaction,
)
from omnigent.entities import Project
from omnigent.errors import (
    ErrorCode,
    OmnigentError,
    ProjectArchiveBusyError,
    ProjectArchivedError,
    ProjectNameArchivedError,
)
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.stores.project_store import (
    ArchiveOp,
    ProjectOrderPreference,
    ProjectStore,
    project_config_revision,
)

# Max serialized length of a project's config blob. The value is persisted
# verbatim and reflected back on every read, so an unbounded blob is a mild
# storage/response-size amplifier on an otherwise cheap CRUD row. 64 KiB is far
# above any realistic set of default-session hints (a few short keys) while
# still capping abuse.
_CONFIG_MAX_SERIALIZED_LEN = 64 * 1024
_PROJECT_ORDER_KEY = "project_order"
_PREFERENCE_MAX_STORED_BYTES = 65535


def _encode_config(config: dict[str, Any] | None) -> str | None:
    """Pack a project's config dict into a compact JSON blob for storage.

    An empty or ``None`` config stores SQL ``NULL`` rather than ``"{}"``, so
    "no defaults" is one canonical representation.

    :param config: The config object, or ``None``.
    :returns: Compact JSON object string, or ``None`` when empty.
    :raises OmnigentError: ``INVALID_INPUT`` if the serialized config exceeds
        :data:`_CONFIG_MAX_SERIALIZED_LEN`.
    """
    if not config:
        return None
    blob = json.dumps(config, separators=(",", ":"))
    if len(blob) > _CONFIG_MAX_SERIALIZED_LEN:
        raise OmnigentError(
            f"project config too large ({len(blob)} bytes; max {_CONFIG_MAX_SERIALIZED_LEN})",
            code=ErrorCode.INVALID_INPUT,
        )
    return blob


def _decode_config(raw: str | None) -> dict[str, Any]:
    """Unpack the stored ``config`` blob to a dict (``{}`` when unset).

    Defensive against a non-object blob: the encode path only ever writes JSON
    objects, but a future writer or a manual DB edit could store a scalar/array,
    which would otherwise flow back as a non-dict. Coerce anything that isn't a
    dict to ``{}`` so callers can always treat config as a mapping.

    :param raw: The stored JSON blob, or ``None``.
    :returns: The decoded object, or an empty dict when ``NULL`` / empty / non-object.
    """
    if not raw:
        return {}
    decoded = json.loads(raw)
    return decoded if isinstance(decoded, dict) else {}


def _to_entity(row: SqlProject) -> Project:
    """
    Convert a :class:`SqlProject` ORM row to a :class:`Project`.

    :param row: The SQLAlchemy ORM row to convert.
    :returns: A :class:`Project` dataclass instance.
    """
    return Project(
        id=row.id,
        name=row.name,
        user_id=row.user_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        config=_decode_config(row.config),
        archived_at=row.archived_at,
        archive_state=_decode_archive_state(row.archive_state) or {},
    )


def _decode_archive_state(raw: str | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    decoded = json.loads(raw)
    return decoded if isinstance(decoded, dict) else None


def assert_project_admits(session: Session, project_id: str | None) -> None:
    """Check archive admission in the caller's project-metadata write transaction."""
    if project_id is None:
        return
    stmt = select(SqlProject).where(
        SqlProject.workspace_id == current_workspace_id(), SqlProject.id == project_id
    )
    if session.bind is not None and (
        is_postgresql_family(session.bind.dialect.name) or session.bind.dialect.name == "mysql"
    ):
        stmt = stmt.with_for_update(read=True)
    project = session.scalar(stmt)
    if project is not None and project.archived_at is not None:
        raise ProjectArchivedError(project.id, project.name)


def admitted_project_id(
    session: Session, project_id: str | None, *, admission: Literal["reject", "inherit"]
) -> str | None:
    """Resolve inherited project membership while holding the project admission lock."""
    try:
        assert_project_admits(session, project_id)
    except ProjectArchivedError:
        if admission != "inherit":
            raise
        return None
    return project_id


def assert_project_name_admits(
    session: Session, project_name: str | None, *, user_id: str | None
) -> None:
    """Reject a legacy project label that names an archived owned project."""
    if not project_name:
        return
    stmt = (
        select(SqlProject)
        .where(
            SqlProject.workspace_id == current_workspace_id(),
            SqlProject.name == project_name,
            SqlProject.user_id == user_id,
        )
        .order_by(SqlProject.id)
    )
    if session.bind is not None and (
        is_postgresql_family(session.bind.dialect.name) or session.bind.dialect.name == "mysql"
    ):
        stmt = stmt.with_for_update(read=True)
    for project in session.scalars(stmt):
        if project.archived_at is not None:
            raise ProjectArchivedError(project.id, project.name)


def project_archive_op_is_current(
    session: Session, project_id: str, op_id: str, *, batch: str | None = None
) -> bool:
    """Hold a shared project lock through the caller's member transaction."""
    stmt = select(SqlProject).where(
        SqlProject.workspace_id == current_workspace_id(), SqlProject.id == project_id
    )
    if session.bind is not None and (
        is_postgresql_family(session.bind.dialect.name) or session.bind.dialect.name == "mysql"
    ):
        stmt = stmt.with_for_update(read=True)
    project = session.scalar(stmt)
    state = _decode_archive_state(project.archive_state) if project is not None else None
    active = state.get("op") if state is not None else None
    return bool(
        project is not None
        and state is not None
        and isinstance(active, dict)
        and active.get("id") == op_id
        and (batch is None or state.get("batch") == batch)
    )


def project_archive_batch_is_active(
    session: Session, project_id: str, batch: str, op_id: str | None = None
) -> bool:
    """Fence a task pause against both visibility and operation takeover."""
    stmt = select(SqlProject).where(
        SqlProject.workspace_id == current_workspace_id(), SqlProject.id == project_id
    )
    if session.bind is not None and (
        is_postgresql_family(session.bind.dialect.name) or session.bind.dialect.name == "mysql"
    ):
        stmt = stmt.with_for_update(read=True)
    project = session.scalar(stmt)
    state = _decode_archive_state(project.archive_state) if project is not None else None
    active = state.get("op") if state is not None else None
    return bool(
        project is not None
        and project.archived_at is not None
        and state is not None
        and state.get("batch") == batch
        and (op_id is None or (isinstance(active, dict) and active.get("id") == op_id))
    )


def _encode_order(preference: ProjectOrderPreference) -> str:
    """Serialize an order only if its compressed frame fits a MySQL BLOB."""
    serialized = json.dumps(preference, separators=(",", ":"))
    stored = encode(serialized)
    assert stored is not None
    if len(stored) > _PREFERENCE_MAX_STORED_BYTES:
        raise OmnigentError("Project order is too large to save", code=ErrorCode.INVALID_INPUT)
    return serialized


def _decode_order(raw: bytes | str | memoryview | None) -> ProjectOrderPreference:
    """Keep invalid preferences from breaking project discovery."""
    default: ProjectOrderPreference = {"sort_mode": "alphabetical", "ordered_project_ids": None}
    if raw is None:
        return default
    try:
        # The 10,000-ID API limit serializes to under 400 KiB, including legacy spacing.
        decoded = json.loads(decode(raw, max_decoded_bytes=512 * 1024) or "null")
        # The original format stored only the manual ID array.
        if isinstance(decoded, list):
            decoded = {"sort_mode": "manual", "ordered_project_ids": decoded}
        if not isinstance(decoded, dict) or decoded.get("sort_mode") not in (
            "alphabetical",
            "manual",
        ):
            return default
        ids = decoded.get("ordered_project_ids")
        if ids is None:
            return default
        if (
            not isinstance(ids, list)
            or len(ids) > 10000
            or any(
                not isinstance(id, str) or re.fullmatch("[0-9a-f]{32}", id) is None for id in ids
            )
        ):
            return default
        return {
            "sort_mode": decoded["sort_mode"],
            "ordered_project_ids": list(dict.fromkeys(ids)),
        }
    except (ValueError, RecursionError, zstandard.ZstdError):
        return default


class SqlAlchemyProjectStore(ProjectStore):
    """
    SQLAlchemy-backed implementation of :class:`ProjectStore`.

    Persists projects in a relational database via the SQLAlchemy ORM. Every
    query is scoped by ``workspace_id`` (tenant partition) and ``user_id``
    (projects are owner-private).
    """

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the SQLAlchemy project store.

        Creates or reuses a SQLAlchemy engine and session factory for the given
        database URI.

        :param storage_location: SQLAlchemy database URI,
            e.g. ``"sqlite:///chat.db"``.
        """
        super().__init__(storage_location)
        self._engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            self._engine,
            query_name_prefix="omnigent.project_store",
        )
        self._session_immediate = make_named_managed_session_maker(
            self._engine,
            query_name_prefix="omnigent.project_store",
            immediate=True,
        )

    def _name_taken(
        self,
        session: Session,
        *,
        user_id: str | None,
        name: str,
        exclude_id: str | None,
    ) -> SqlProject | None:
        """Return whether ``user_id`` already has a project named ``name``.

        The sole enforcement point for per-owner name uniqueness: there is no
        unique index behind it (see ``SqlProject.__table_args__``). Being a
        check-then-write, it cannot close the concurrency window — two
        simultaneous creates or renames to the same name can both land.

        :param session: The active SQLAlchemy session.
        :param user_id: The owner scope.
        :param name: The candidate name.
        :param exclude_id: A project id to exclude (the row being renamed).
        :returns: ``True`` if another of the owner's projects has this name.
        """
        stmt = select(SqlProject).where(
            SqlProject.workspace_id == current_workspace_id(),
            SqlProject.user_id == user_id,
            SqlProject.name == name,
        )
        if exclude_id is not None:
            stmt = stmt.where(SqlProject.id != exclude_id)
        return session.scalar(stmt)

    def create(
        self,
        project_id: str,
        name: str,
        user_id: str | None,
        config: dict[str, Any] | None = None,
    ) -> Project:
        """Insert a new, empty project.

        Rejects a name the owner already uses via the ``_name_taken`` pre-check.
        That check is the only guard — no unique index backs it — so a
        concurrent create of the same name can slip through; see
        ``SqlProject.__table_args__`` for why that is acceptable.
        """
        created_at = now_epoch()
        encoded_config = _encode_config(config)

        def write(session: Session) -> Project:
            require_active_account(session, user_id)
            taken = self._name_taken(session, user_id=user_id, name=name, exclude_id=None)
            if taken:
                if taken.archived_at is not None:
                    raise ProjectNameArchivedError(taken.id, name)
                raise OmnigentError(
                    f"A project named {name!r} already exists", code=ErrorCode.ALREADY_EXISTS
                )
            row = SqlProject(
                id=project_id,
                name=name,
                user_id=user_id,
                created_at=created_at,
                updated_at=None,
                config=encoded_config,
            )
            session.add(row)
            session.flush()
            return _to_entity(row)

        return run_write_transaction(self._session_immediate, "insert_project", write)

    def get(self, project_id: str, *, user_id: str | None) -> Project | None:
        """Return an owned project by id, or ``None`` if not found."""
        with self._session("select_project_by_id") as session:
            row = session.get(SqlProject, (current_workspace_id(), project_id))
            if row is None or row.user_id != user_id:
                return None
            return _to_entity(row)

    def list(
        self,
        *,
        user_id: str | None,
        include_archived: bool = False,
        archived: Literal["exclude", "include", "only"] = "exclude",
    ) -> list[Project]:
        """List the owner's projects ordered by ``created_at ASC, id ASC``."""
        with self._session("list_projects") as session:
            stmt = (
                select(SqlProject)
                .where(SqlProject.workspace_id == current_workspace_id())
                .where(SqlProject.user_id == user_id)
                .order_by(asc(SqlProject.created_at), asc(SqlProject.id))
            )
            if archived == "only":
                stmt = stmt.where(SqlProject.archived_at.is_not(None))
            elif archived == "exclude" and not include_archived:
                stmt = stmt.where(SqlProject.archived_at.is_(None))
            rows = session.execute(stmt).scalars().all()
            return [_to_entity(r) for r in rows]

    def begin_archive_op(
        self, project_id: str, *, user_id: str | None, kind: Literal["archive", "unarchive"]
    ) -> ArchiveOp:
        if kind not in ("archive", "unarchive"):
            raise ValueError("invalid project archive operation")

        def write(session: Session) -> ArchiveOp:
            row = session.get(
                SqlProject, (current_workspace_id(), project_id), with_for_update=True
            )
            if row is None or row.user_id != user_id:
                raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
            now = now_epoch()
            state = _decode_archive_state(row.archive_state) or {"batch": None, "op": None}
            active = state.get("op")
            if isinstance(active, dict):
                active_kind = active.get("kind")
                heartbeat = active.get("heartbeat_at")
                if active_kind != kind and isinstance(heartbeat, int) and now - heartbeat < 300:
                    raise ProjectArchiveBusyError(str(active_kind))
            batch = state.get("batch")
            if not isinstance(batch, str) or not batch:
                batch = uuid.uuid4().hex
            op_id = uuid.uuid4().hex
            state = {
                "batch": batch,
                "op": {
                    "id": op_id,
                    "kind": kind,
                    "started_at": now,
                    "heartbeat_at": now,
                },
            }
            row.archive_state = json.dumps(state, separators=(",", ":"))
            row.archived_at = (
                now if kind == "archive" and row.archived_at is None else row.archived_at
            )
            if kind == "unarchive":
                row.archived_at = None
            session.flush()
            return ArchiveOp(id=op_id, kind=kind, batch=batch)

        return run_write_transaction(self._session_immediate, "begin_project_archive", write)

    def fence_archive_op(self, project_id: str, op_id: str, *, user_id: str | None) -> bool:
        def write(session: Session) -> bool:
            row = session.get(
                SqlProject, (current_workspace_id(), project_id), with_for_update=True
            )
            if row is None or row.user_id != user_id:
                return False
            state = _decode_archive_state(row.archive_state)
            if not state or not isinstance(state.get("op"), dict):
                return False
            if state["op"].get("id") != op_id:
                return False
            state["op"]["heartbeat_at"] = now_epoch()
            row.archive_state = json.dumps(state, separators=(",", ":"))
            return True

        return run_write_transaction(self._session_immediate, "heartbeat_project_archive", write)

    def finish_archive_op(
        self, project_id: str, op_id: str, *, user_id: str | None, clear_batch: bool = False
    ) -> bool:
        def write(session: Session) -> bool:
            row = session.get(
                SqlProject, (current_workspace_id(), project_id), with_for_update=True
            )
            if row is None or row.user_id != user_id:
                return False
            state = _decode_archive_state(row.archive_state)
            if not state or not isinstance(state.get("op"), dict):
                return False
            if state["op"].get("id") != op_id:
                return False
            state["op"] = None
            if clear_batch:
                state["batch"] = None
            row.archive_state = (
                json.dumps(state, separators=(",", ":")) if state["batch"] else None
            )
            return True

        return run_write_transaction(self._session_immediate, "finish_project_archive", write)

    def update(
        self,
        project_id: str,
        *,
        user_id: str | None,
        name: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> Project | None:
        """Update mutable fields of an owned project.

        ``None`` leaves a field unchanged. Returns ``None`` if the project does
        not exist or is not owned by ``user_id``. A ``config`` of ``{}`` clears
        the stored defaults (distinct from ``None`` = leave unchanged).

        A rename re-checks ``_name_taken``, which — as on ``create`` — is the
        only uniqueness guard, so concurrent renames to the same name can both
        land.
        """
        encoded_config = _encode_config(config) if config is not None else None
        updated_at = now_epoch()

        def write(session: Session) -> Project | None:
            require_active_account(session, user_id)
            row = session.get(
                SqlProject, (current_workspace_id(), project_id), with_for_update=True
            )
            if row is None or row.user_id != user_id:
                return None
            changed = False
            if name is not None and row.name != name:
                taken = self._name_taken(
                    session, user_id=user_id, name=name, exclude_id=project_id
                )
                if taken:
                    if taken.archived_at is not None:
                        raise ProjectNameArchivedError(taken.id, name)
                    raise OmnigentError(
                        f"A project named {name!r} already exists", code=ErrorCode.ALREADY_EXISTS
                    )
                row.name = name
                changed = True
            if config is not None:
                if row.config != encoded_config:
                    row.config = encoded_config
                    changed = True
            if changed:
                row.updated_at = updated_at
            session.flush()
            return _to_entity(row)

        return run_write_transaction(self._session_immediate, "update_project", write)

    def patch_calling_defaults(
        self,
        project_id: str,
        *,
        user_id: str | None,
        expected_revision: str,
        host_id: str,
        harness: str,
        fields: Mapping[Literal["model", "effort", "speed", "permission"], str | None],
        agent_id: str | None = None,
        copy_agent: bool = False,
    ) -> Project | None:
        """Apply one narrow host/harness patch to the current config in one transaction."""

        def write(session: Session) -> Project | None:
            require_active_account(session, user_id)
            row = session.get(
                SqlProject, (current_workspace_id(), project_id), with_for_update=True
            )
            if row is None or row.user_id != user_id:
                return None
            config = _decode_config(row.config)
            if project_config_revision(config) != expected_revision:
                raise OmnigentError(
                    "Project settings changed. Reload before applying these defaults.",
                    code=ErrorCode.CONFLICT,
                )
            next_config = dict(config)
            defaults = dict(next_config.get("calling_defaults") or {})
            host = dict(defaults.get(host_id) or {})
            harnesses = dict(host.get("harnesses") or {})
            setting = dict(harnesses.get(harness) or {})
            for field, value in fields.items():
                if value is None:
                    setting.pop(field, None)
                else:
                    setting[field] = value
            if setting:
                harnesses[harness] = setting
            else:
                harnesses.pop(harness, None)
            if harnesses:
                host["harnesses"] = harnesses
            else:
                host.pop("harnesses", None)
            if copy_agent:
                if agent_id is None:
                    host.pop("agent_id", None)
                else:
                    host["agent_id"] = agent_id
            if host:
                defaults[host_id] = host
            else:
                defaults.pop(host_id, None)
            if defaults:
                next_config["calling_defaults"] = defaults
            else:
                next_config.pop("calling_defaults", None)
            if "calling_defaults" in next_config:
                validate_project_calling_defaults(next_config["calling_defaults"])
            encoded = _encode_config(next_config)
            if row.config != encoded:
                row.config = encoded
                row.updated_at = now_epoch()
            session.flush()
            return _to_entity(row)

        return run_write_transaction(
            self._session_immediate, "patch_project_calling_defaults", write
        )

    def delete(self, project_id: str, *, user_id: str | None) -> bool:
        """Delete an owned project. Idempotent; returns ``False`` if not found."""

        def write(session: Session) -> bool:
            row = session.get(SqlProject, (current_workspace_id(), project_id))
            if row is None or row.user_id != user_id:
                return False
            session.delete(row)
            return True

        return run_write_transaction(self._session_immediate, "delete_project", write)

    def get_order(self, *, user_id: str | None) -> builtins.list[str] | None:
        """Return the active order used by project-list endpoints."""
        preference = self.get_order_preference(user_id=user_id)
        return preference["ordered_project_ids"] if preference["sort_mode"] == "manual" else None

    def get_order_preference(self, *, user_id: str | None) -> ProjectOrderPreference:
        """Read project ordering from the user's preferences."""
        preference_user_id = RESERVED_USER_LOCAL if user_id is None else user_id
        with self._session("read_project_order") as session:
            # Decode raw bytes here so malformed values cannot fail in the ORM result processor.
            raw = session.scalar(
                select(type_coerce(SqlPreference.value, LargeBinary)).where(
                    SqlPreference.workspace_id == current_workspace_id(),
                    SqlPreference.user_id == preference_user_id,
                    SqlPreference.key == _PROJECT_ORDER_KEY,
                )
            )
            return _decode_order(raw)

    def save_order(
        self, ids: builtins.list[str] | None, *, user_id: str | None
    ) -> ProjectOrderPreference:
        """Validate project ownership and update only the user's preference."""
        preference_user_id = RESERVED_USER_LOCAL if user_id is None else user_id

        def write(session: Session) -> ProjectOrderPreference:
            require_active_account(session, user_id)
            workspace_id = current_workspace_id()
            if ids is None:
                raw = session.scalar(
                    select(type_coerce(SqlPreference.value, LargeBinary))
                    .where(
                        SqlPreference.workspace_id == workspace_id,
                        SqlPreference.user_id == preference_user_id,
                        SqlPreference.key == _PROJECT_ORDER_KEY,
                    )
                    .with_for_update()
                )
                preference = _decode_order(raw)
                preference["sort_mode"] = "alphabetical"
                if raw is None:
                    return preference
                session.execute(
                    update(SqlPreference)
                    .where(
                        SqlPreference.workspace_id == workspace_id,
                        SqlPreference.user_id == preference_user_id,
                        SqlPreference.key == _PROJECT_ORDER_KEY,
                    )
                    .values(value=_encode_order(preference))
                )
                return preference
            projects = list(
                session.scalars(
                    select(SqlProject).where(
                        SqlProject.workspace_id == workspace_id,
                        SqlProject.user_id == user_id,
                    )
                )
            )
            owned = {project.id for project in projects}
            if len(set(ids)) != len(ids) or not set(ids).issubset(owned):
                raise OmnigentError("Invalid project order", code=ErrorCode.INVALID_INPUT)
            archived_ids = {project.id for project in projects if project.archived_at is not None}
            old_raw = session.scalar(
                select(type_coerce(SqlPreference.value, LargeBinary))
                .where(
                    SqlPreference.workspace_id == workspace_id,
                    SqlPreference.user_id == preference_user_id,
                    SqlPreference.key == _PROJECT_ORDER_KEY,
                )
                .with_for_update()
            )
            previous = _decode_order(old_raw)["ordered_project_ids"] or []
            merged = list(ids)
            for archived_id in previous:
                if archived_id not in archived_ids or archived_id in merged:
                    continue
                index = previous.index(archived_id)
                predecessor = next((id for id in reversed(previous[:index]) if id in merged), None)
                merged.insert(merged.index(predecessor) + 1 if predecessor else 0, archived_id)
            merged.extend(sorted(archived_ids - set(merged)))
            preference: ProjectOrderPreference = {
                "sort_mode": "manual",
                "ordered_project_ids": merged,
            }
            encoded = _encode_order(preference)
            values = {
                "workspace_id": workspace_id,
                "user_id": preference_user_id,
                "key": _PROJECT_ORDER_KEY,
                "value": encoded,
            }
            dialect = self._engine.dialect.name
            stmt: Insert
            if dialect == "mysql":
                stmt = (
                    mysql_insert(SqlPreference)
                    .values(**values)
                    .on_duplicate_key_update(value=encoded)
                )
            elif dialect == "sqlite":
                stmt = (
                    sqlite_insert(SqlPreference)
                    .values(**values)
                    .on_conflict_do_update(
                        index_elements=["workspace_id", "user_id", "key"],
                        set_={"value": encoded},
                    )
                )
            else:
                stmt = (
                    pg_insert(SqlPreference)
                    .values(**values)
                    .on_conflict_do_update(
                        index_elements=["workspace_id", "user_id", "key"],
                        set_={"value": encoded},
                    )
                )
            session.execute(stmt)
            return preference

        return run_write_transaction(self._session_immediate, "save_project_order", write)
