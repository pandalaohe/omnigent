"""SQLAlchemy-backed assignment store."""

from __future__ import annotations

import builtins
import json
import uuid
from typing import Any, cast

from sqlalchemy import and_, asc, func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from omnigent.db.db_models import (
    SqlAssignment,
    SqlAssignmentAttempt,
    SqlAssignmentMessage,
    current_workspace_id,
)
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch,
    run_write_transaction,
)
from omnigent.entities import (
    Assignment,
    AssignmentAttempt,
    AssignmentMessage,
    AssignmentState,
)
from omnigent.entities.assignment import (
    NON_TERMINAL_STATES,
    inputs_from_json,
    inputs_to_json,
    is_legal_transition,
    outputs_from_json,
    outputs_to_json,
)
from omnigent.entities.pagination import PagedList, paginate_in_memory
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.stores.assignment_store import (
    _UNSET,
    AssignmentIdempotencyConflictError,
    AssignmentStore,
    IllegalAssignmentTransitionError,
    InactiveAttemptError,
)

# States the bounded due-work pass acts on. ``interrupted`` needs an
# explicit retry with a confirmed stop, so the pass leaves it alone.
_DUE_STATES = frozenset(
    {
        AssignmentState.PREPARING.value,
        AssignmentState.WAITING.value,
        AssignmentState.STARTING.value,
        AssignmentState.RUNNING.value,
        AssignmentState.PUBLISHING.value,
        AssignmentState.STOPPING.value,
    }
)

# Mutable assignment columns a transition may set atomically besides the
# state itself. Identity, idempotency and creation stamps are excluded.
_TRANSITION_COLUMNS = frozenset(
    {
        "owner_user_id",
        "requested_host_id",
        "resolved_host_id",
        "binding_name",
        "resolved_binding_id",
        "resolved_binding_revision",
        "project_revision",
        "task",
        "metadata_json",
        "inputs_json",
        "model_override",
        "harness_override",
        "start_deadline",
        "wait_reason",
        "next_check_at",
        "active_attempt_id",
        "outputs_json",
        "result_summary",
        "error_code",
        "cancel_requested_at",
    }
)

# Mutable attempt columns an attempt update may set. ``event_dispatched_at``
# is excluded: only ``mark_event_dispatched`` writes it (compare-and-set).
_ATTEMPT_COLUMNS = frozenset(
    {
        "host_id",
        "runner_id",
        "session_id",
        "state",
        "lease_expires_at",
        "started_at",
        "ended_at",
        "error_code",
    }
)


def _encode_metadata(metadata: dict[str, Any] | None) -> str | None:
    """Pack the metadata dict into a compact JSON blob (``None`` when unset).

    :param metadata: The structured extras, or ``None``.
    :returns: Compact JSON object string, or ``None``.
    """
    if metadata is None:
        return None
    return json.dumps(metadata, separators=(",", ":"))


def _decode_metadata(raw: str | None) -> dict[str, Any] | None:
    """Unpack the stored ``metadata_json`` blob (``None`` when unset).

    :param raw: The stored JSON blob, or ``None``.
    :returns: The decoded object, or ``None``.
    """
    if raw is None:
        return None
    decoded = json.loads(raw)
    return decoded if isinstance(decoded, dict) else None


def _assignment_to_entity(row: SqlAssignment) -> Assignment:
    """
    Convert a :class:`SqlAssignment` ORM row to an :class:`Assignment`.

    :param row: The SQLAlchemy ORM row to convert.
    :returns: An :class:`Assignment` dataclass instance.
    """
    return Assignment(
        id=row.id,
        project_id=row.project_id,
        source_session_id=row.source_session_id,
        target_agent_id=row.target_agent_id,
        task=row.task,
        inputs=inputs_from_json(row.inputs_json),
        idempotency_key=row.idempotency_key,
        request_digest=row.request_digest,
        owner_user_id=row.owner_user_id,
        requested_host_id=row.requested_host_id,
        resolved_host_id=row.resolved_host_id,
        binding_name=row.binding_name,
        resolved_binding_id=row.resolved_binding_id,
        resolved_binding_revision=row.resolved_binding_revision,
        project_revision=row.project_revision,
        metadata=_decode_metadata(row.metadata_json),
        model_override=row.model_override,
        harness_override=row.harness_override,
        start_deadline=row.start_deadline,
        state=row.state,
        wait_reason=row.wait_reason,
        next_check_at=row.next_check_at,
        active_attempt_id=row.active_attempt_id,
        outputs=outputs_from_json(row.outputs_json) or None,
        result_summary=row.result_summary,
        error_code=row.error_code,
        cancel_requested_at=row.cancel_requested_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        workspace_id=row.workspace_id,
    )


def _attempt_to_entity(row: SqlAssignmentAttempt) -> AssignmentAttempt:
    """
    Convert a :class:`SqlAssignmentAttempt` ORM row to an
    :class:`AssignmentAttempt`.

    :param row: The SQLAlchemy ORM row to convert.
    :returns: An :class:`AssignmentAttempt` dataclass instance.
    """
    return AssignmentAttempt(
        id=row.id,
        assignment_id=row.assignment_id,
        number=row.number,
        host_id=row.host_id,
        runner_id=row.runner_id,
        session_id=row.session_id,
        state=row.state,
        lease_expires_at=row.lease_expires_at,
        event_dispatched_at=row.event_dispatched_at,
        started_at=row.started_at,
        ended_at=row.ended_at,
        error_code=row.error_code,
        created_at=row.created_at,
        updated_at=row.updated_at,
        workspace_id=row.workspace_id,
    )


def _message_to_entity(row: SqlAssignmentMessage) -> AssignmentMessage:
    """
    Convert a :class:`SqlAssignmentMessage` ORM row to an
    :class:`AssignmentMessage`.

    :param row: The SQLAlchemy ORM row to convert.
    :returns: An :class:`AssignmentMessage` dataclass instance.
    """
    return AssignmentMessage(
        id=row.id,
        assignment_id=row.assignment_id,
        kind=row.kind,
        body=row.body,
        sender_session_id=row.sender_session_id,
        idempotency_key=row.idempotency_key,
        created_at=row.created_at,
        updated_at=row.updated_at,
        workspace_id=row.workspace_id,
    )


class SqlAlchemyAssignmentStore(AssignmentStore):
    """
    SQLAlchemy-backed implementation of :class:`AssignmentStore`.

    Persists assignments, attempts and messages in a relational database
    via the SQLAlchemy ORM. Every query is scoped by ``workspace_id``
    (tenant partition). State changes are conditional writes so concurrent
    coordinators cannot clobber each other.
    """

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the SQLAlchemy assignment store.

        Creates or reuses a SQLAlchemy engine and session factory for the
        given database URI.

        :param storage_location: SQLAlchemy database URI,
            e.g. ``"sqlite:///chat.db"``.
        """
        super().__init__(storage_location)
        self._engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            self._engine,
            query_name_prefix="omnigent.assignment_store",
        )
        self._session_immediate = make_named_managed_session_maker(
            self._engine,
            query_name_prefix="omnigent.assignment_store",
            immediate=True,
        )

    def create(self, assignment: Assignment) -> Assignment:
        """Insert a new assignment in ``preparing``, idempotently.

        A row matching by id or by ``(source_session_id, idempotency_key)``
        with the same digest is returned unchanged; a digest mismatch
        raises. A unique-violation from a concurrent create re-reads the
        winner and applies the same rule instead of surfacing a constraint
        error.
        """
        created_at = now_epoch()

        def write(session: Session) -> Assignment:
            wid = current_workspace_id()
            row = session.get(SqlAssignment, (wid, assignment.id))
            if row is None:
                row = (
                    session.execute(
                        select(SqlAssignment)
                        .where(SqlAssignment.workspace_id == wid)
                        .where(SqlAssignment.source_session_id == assignment.source_session_id)
                        .where(SqlAssignment.idempotency_key == assignment.idempotency_key)
                    )
                    .scalars()
                    .first()
                )
            if row is not None:
                if row.request_digest != assignment.request_digest:
                    raise AssignmentIdempotencyConflictError(row.id)
                return _assignment_to_entity(row)
            row = SqlAssignment(
                id=assignment.id,
                project_id=assignment.project_id,
                source_session_id=assignment.source_session_id,
                owner_user_id=assignment.owner_user_id,
                target_agent_id=assignment.target_agent_id,
                requested_host_id=assignment.requested_host_id,
                resolved_host_id=None,
                binding_name=assignment.binding_name,
                resolved_binding_id=None,
                resolved_binding_revision=None,
                project_revision=assignment.project_revision,
                task=assignment.task,
                metadata_json=_encode_metadata(assignment.metadata),
                inputs_json=inputs_to_json(assignment.inputs),
                model_override=assignment.model_override,
                harness_override=assignment.harness_override,
                start_deadline=assignment.start_deadline,
                idempotency_key=assignment.idempotency_key,
                request_digest=assignment.request_digest,
                state=AssignmentState.PREPARING.value,
                wait_reason=None,
                next_check_at=assignment.next_check_at,
                active_attempt_id=None,
                outputs_json=None,
                result_summary=None,
                error_code=None,
                cancel_requested_at=None,
                created_at=created_at,
                updated_at=None,
            )
            session.add(row)
            try:
                session.flush()
            except IntegrityError:
                session.rollback()
                winner = session.get(SqlAssignment, (wid, assignment.id))
                if winner is None:
                    winner = (
                        session.execute(
                            select(SqlAssignment)
                            .where(SqlAssignment.workspace_id == wid)
                            .where(SqlAssignment.source_session_id == assignment.source_session_id)
                            .where(SqlAssignment.idempotency_key == assignment.idempotency_key)
                        )
                        .scalars()
                        .first()
                    )
                if winner is None:  # pragma: no cover - defensive; the flush failed on a row
                    raise
                if winner.request_digest != assignment.request_digest:
                    raise AssignmentIdempotencyConflictError(winner.id) from None
                return _assignment_to_entity(winner)
            return _assignment_to_entity(row)

        return run_write_transaction(self._session_immediate, "insert_assignment", write)

    def get(self, assignment_id: str) -> Assignment | None:
        """Return an assignment by id, or ``None`` if not found."""
        with self._session("select_assignment_by_id") as session:
            row = session.get(SqlAssignment, (current_workspace_id(), assignment_id))
            if row is None:
                return None
            return _assignment_to_entity(row)

    def list(
        self,
        *,
        owner_user_id: str | None = None,
        project_id: str | None = None,
        state: str | None = None,
        source_session_id: str | None = None,
        attempt_session_id: str | None = None,
        limit: int = 20,
        after: str | None = None,
    ) -> PagedList[Assignment]:
        """List assignments matching the filters, cursor-paginated."""
        with self._session("list_assignments") as session:
            stmt = select(SqlAssignment).where(
                SqlAssignment.workspace_id == current_workspace_id()
            )
            if owner_user_id is None:
                stmt = stmt.where(SqlAssignment.owner_user_id.is_(None))
            else:
                stmt = stmt.where(SqlAssignment.owner_user_id == owner_user_id)
            if project_id is not None:
                stmt = stmt.where(SqlAssignment.project_id == project_id)
            if state is not None:
                stmt = stmt.where(SqlAssignment.state == state)
            if source_session_id is not None:
                stmt = stmt.where(SqlAssignment.source_session_id == source_session_id)
            if attempt_session_id is not None:
                stmt = stmt.where(
                    SqlAssignment.id.in_(
                        select(SqlAssignmentAttempt.assignment_id).where(
                            SqlAssignmentAttempt.workspace_id == current_workspace_id(),
                            SqlAssignmentAttempt.session_id == attempt_session_id,
                        )
                    )
                )
            stmt = stmt.order_by(asc(SqlAssignment.created_at), asc(SqlAssignment.id))
            rows = session.execute(stmt).scalars().all()
            items = [_assignment_to_entity(r) for r in rows]
            return paginate_in_memory(items, lambda a: a.id, limit=limit, after=after, order="asc")

    def transition(
        self,
        assignment_id: str,
        *,
        from_state: str,
        to_state: str,
        expected_active_attempt_id: Any = _UNSET,
        **fields: Any,
    ) -> Assignment | None:
        """Conditionally move an assignment to a new state.

        The pair is legality-checked first; the write then applies only
        when the stored state still equals ``from_state``. ``inputs``,
        ``outputs`` and ``metadata`` accept entity values and are encoded;
        every other key must name a mutable column.
        """
        if not is_legal_transition(from_state, to_state):
            raise IllegalAssignmentTransitionError(from_state, to_state)
        values: dict[str, Any] = {}
        for key, value in fields.items():
            if key == "outputs":
                value = outputs_to_json(value) if isinstance(value, list) else value
                key = "outputs_json"
            elif key == "inputs":
                value = inputs_to_json(value) if isinstance(value, list) else value
                key = "inputs_json"
            elif key == "metadata":
                value = _encode_metadata(value)
                key = "metadata_json"
            if key not in _TRANSITION_COLUMNS:
                raise OmnigentError(
                    f"cannot transition with unknown field {key!r}",
                    code=ErrorCode.INVALID_INPUT,
                )
            values[key] = value

        def write(session: Session) -> Assignment | None:
            wid = current_workspace_id()
            # A DML execute returns a CursorResult at runtime; the rowcount
            # is the single-flight signal (1 = this writer won, 0 = lost).
            stmt = update(SqlAssignment).where(
                SqlAssignment.workspace_id == wid,
                SqlAssignment.id == assignment_id,
                SqlAssignment.state == from_state,
            )
            if expected_active_attempt_id is not _UNSET:
                if expected_active_attempt_id is None:
                    stmt = stmt.where(SqlAssignment.active_attempt_id.is_(None))
                else:
                    stmt = stmt.where(
                        SqlAssignment.active_attempt_id == expected_active_attempt_id
                    )
            result = cast(
                "CursorResult[Any]",
                session.execute(stmt.values(state=to_state, updated_at=now_epoch(), **values)),
            )
            if not result.rowcount:
                return None
            row = session.get(SqlAssignment, (wid, assignment_id))
            return _assignment_to_entity(row) if row is not None else None

        return run_write_transaction(self._session_immediate, "transition_assignment", write)

    def reschedule(
        self,
        assignment_id: str,
        *,
        expected_state: str,
        next_check_at: int | None,
        wait_reason: Any = _UNSET,
        expected_active_attempt_id: Any = _UNSET,
    ) -> Assignment | None:
        """Stamp the next check without changing state, conditionally."""

        def write(session: Session) -> Assignment | None:
            wid = current_workspace_id()
            values: dict[str, Any] = {
                "next_check_at": next_check_at,
                "updated_at": now_epoch(),
            }
            if wait_reason is not _UNSET:
                values["wait_reason"] = wait_reason
            stmt = update(SqlAssignment).where(
                SqlAssignment.workspace_id == wid,
                SqlAssignment.id == assignment_id,
                SqlAssignment.state == expected_state,
            )
            if expected_active_attempt_id is not _UNSET:
                if expected_active_attempt_id is None:
                    stmt = stmt.where(SqlAssignment.active_attempt_id.is_(None))
                else:
                    stmt = stmt.where(
                        SqlAssignment.active_attempt_id == expected_active_attempt_id
                    )
            result = cast(
                "CursorResult[Any]",
                session.execute(stmt.values(**values)),
            )
            if not result.rowcount:
                return None
            row = session.get(SqlAssignment, (wid, assignment_id))
            return _assignment_to_entity(row) if row is not None else None

        return run_write_transaction(self._session_immediate, "reschedule_assignment", write)

    def claim_attempt(
        self,
        assignment_id: str,
        *,
        host_id: str,
        now: int,
        resolved_binding_id: Any = _UNSET,
        resolved_binding_revision: Any = _UNSET,
        next_check_at: Any = _UNSET,
        expected_binding_pin: Any = _UNSET,
    ) -> AssignmentAttempt | None:
        """Claim the next attempt number single-flight.

        One conditional UPDATE flips ``waiting`` → ``starting``, pins
        ``resolved_host_id`` and binds the new attempt id; the attempt row
        is inserted only when exactly one row changed, so two racing
        claimants produce one attempt. A row pinned to another host stays
        unclaimable — no substitute destination may execute it. Passed
        binding pin / ``next_check_at`` values are written in the same
        UPDATE, which also clears ``wait_reason``. A passed
        ``expected_binding_pin`` requires the stored pin to still equal it.
        """
        attempt_id = uuid.uuid4().hex

        def write(session: Session) -> AssignmentAttempt | None:
            wid = current_workspace_id()
            values: dict[str, Any] = {
                "state": AssignmentState.STARTING.value,
                "active_attempt_id": attempt_id,
                "resolved_host_id": host_id,
                "updated_at": now,
            }
            pinned = False
            if resolved_binding_id is not _UNSET:
                values["resolved_binding_id"] = resolved_binding_id
                pinned = True
            if resolved_binding_revision is not _UNSET:
                values["resolved_binding_revision"] = resolved_binding_revision
                pinned = True
            if next_check_at is not _UNSET:
                values["next_check_at"] = next_check_at
                pinned = True
            if pinned:
                values["wait_reason"] = None
            stmt = update(SqlAssignment).where(
                SqlAssignment.workspace_id == wid,
                SqlAssignment.id == assignment_id,
                SqlAssignment.state == AssignmentState.WAITING.value,
                SqlAssignment.active_attempt_id.is_(None),
                or_(
                    SqlAssignment.resolved_host_id.is_(None),
                    SqlAssignment.resolved_host_id == host_id,
                ),
            )
            if expected_binding_pin is not _UNSET:
                if expected_binding_pin is None:
                    stmt = stmt.where(SqlAssignment.resolved_binding_id.is_(None))
                else:
                    pin_id, pin_rev = expected_binding_pin
                    stmt = stmt.where(SqlAssignment.resolved_binding_id == pin_id)
                    if pin_rev is None:
                        stmt = stmt.where(SqlAssignment.resolved_binding_revision.is_(None))
                    else:
                        stmt = stmt.where(SqlAssignment.resolved_binding_revision == pin_rev)
            result = cast(
                "CursorResult[Any]",
                session.execute(stmt.values(**values)),
            )
            if not result.rowcount:
                return None
            max_number = (
                session.execute(
                    select(func.coalesce(func.max(SqlAssignmentAttempt.number), 0)).where(
                        SqlAssignmentAttempt.workspace_id == wid,
                        SqlAssignmentAttempt.assignment_id == assignment_id,
                    )
                ).scalar()
                or 0
            )
            row = SqlAssignmentAttempt(
                id=attempt_id,
                assignment_id=assignment_id,
                number=max_number + 1,
                host_id=host_id,
                runner_id=None,
                session_id=None,
                state="active",
                lease_expires_at=None,
                event_dispatched_at=None,
                started_at=now,
                ended_at=None,
                error_code=None,
                created_at=now,
                updated_at=None,
            )
            session.add(row)
            session.flush()
            return _attempt_to_entity(row)

        return run_write_transaction(self._session_immediate, "claim_attempt", write)

    def update_attempt(
        self,
        assignment_id: str,
        attempt_id: str,
        **fields: Any,
    ) -> AssignmentAttempt | None:
        """Update the active attempt; reject writes from a non-active one."""
        unknown = set(fields) - _ATTEMPT_COLUMNS
        if unknown:
            raise OmnigentError(
                f"cannot update attempt with unknown fields {sorted(unknown)!r}",
                code=ErrorCode.INVALID_INPUT,
            )

        def write(session: Session) -> AssignmentAttempt | None:
            wid = current_workspace_id()
            # Assignment row first, attempt row second: one lock order, so a
            # concurrent retirement cannot slip between check and write.
            assignment = (
                session.execute(
                    select(SqlAssignment)
                    .where(
                        SqlAssignment.workspace_id == wid,
                        SqlAssignment.id == assignment_id,
                    )
                    .with_for_update()
                )
                .scalars()
                .first()
            )
            attempt = (
                session.execute(
                    select(SqlAssignmentAttempt)
                    .where(
                        SqlAssignmentAttempt.workspace_id == wid,
                        SqlAssignmentAttempt.id == attempt_id,
                    )
                    .with_for_update()
                )
                .scalars()
                .first()
            )
            if assignment is None or attempt is None:
                return None
            if (
                attempt.assignment_id != assignment_id
                or assignment.active_attempt_id != attempt_id
                or attempt.state != "active"
            ):
                raise InactiveAttemptError(assignment_id, attempt_id)
            for key, value in fields.items():
                setattr(attempt, key, value)
            attempt.updated_at = now_epoch()
            session.flush()
            return _attempt_to_entity(attempt)

        return run_write_transaction(self._session_immediate, "update_attempt", write)

    def get_attempt(self, assignment_id: str, attempt_id: str) -> AssignmentAttempt | None:
        """Return one attempt of an assignment, with no activeness check."""
        with self._session("select_assignment_attempt") as session:
            row = session.get(SqlAssignmentAttempt, (current_workspace_id(), attempt_id))
            if row is None or row.assignment_id != assignment_id:
                return None
            return _attempt_to_entity(row)

    def refresh_waiting(
        self,
        assignment_id: str,
        *,
        inputs: builtins.list[Any],
        project_revision: int,
        now: int,
        expected_inputs_json: Any = _UNSET,
        expected_project_revision: Any = _UNSET,
    ) -> Assignment | None:
        """Re-pin a ``waiting`` row; ``None`` when missing or not waiting."""

        def write(session: Session) -> Assignment | None:
            wid = current_workspace_id()
            stmt = update(SqlAssignment).where(
                SqlAssignment.workspace_id == wid,
                SqlAssignment.id == assignment_id,
                SqlAssignment.state == AssignmentState.WAITING.value,
            )
            if expected_inputs_json is not _UNSET:
                stmt = stmt.where(SqlAssignment.inputs_json == expected_inputs_json)
            if expected_project_revision is not _UNSET:
                stmt = stmt.where(SqlAssignment.project_revision == expected_project_revision)
            result = cast(
                "CursorResult[Any]",
                session.execute(
                    stmt.values(
                        inputs_json=(
                            inputs_to_json(inputs) if isinstance(inputs, list) else inputs
                        ),
                        project_revision=project_revision,
                        wait_reason=None,
                        next_check_at=now,
                        resolved_binding_id=None,
                        resolved_binding_revision=None,
                        updated_at=now,
                    )
                ),
            )
            if not result.rowcount:
                return None
            row = session.get(SqlAssignment, (wid, assignment_id))
            return _assignment_to_entity(row) if row is not None else None

        return run_write_transaction(self._session_immediate, "refresh_waiting", write)

    def mark_event_dispatched(self, attempt_id: str, *, now: int) -> bool:
        """Compare-and-set ``event_dispatched_at``; ``True`` for the first caller."""

        def write(session: Session) -> bool:
            result = cast(
                "CursorResult[Any]",
                session.execute(
                    update(SqlAssignmentAttempt)
                    .where(
                        SqlAssignmentAttempt.workspace_id == current_workspace_id(),
                        SqlAssignmentAttempt.id == attempt_id,
                        SqlAssignmentAttempt.event_dispatched_at.is_(None),
                    )
                    .values(event_dispatched_at=now)
                ),
            )
            return bool(result.rowcount)

        return run_write_transaction(self._session_immediate, "mark_event_dispatched", write)

    def set_lease(self, attempt_id: str, lease_expires_at: int | None) -> None:
        """Write the server-observed liveness lease of an attempt."""

        def write(session: Session) -> None:
            session.execute(
                update(SqlAssignmentAttempt)
                .where(
                    SqlAssignmentAttempt.workspace_id == current_workspace_id(),
                    SqlAssignmentAttempt.id == attempt_id,
                )
                .values(lease_expires_at=lease_expires_at, updated_at=now_epoch())
            )

        run_write_transaction(self._session_immediate, "set_lease", write)

    def select_due(self, *, now: int, limit: int) -> builtins.list[Assignment]:
        """Select due rows for the bounded recovery pass in one statement."""
        with self._session("select_due_assignments") as session:
            stmt = (
                select(SqlAssignment)
                .where(SqlAssignment.workspace_id == current_workspace_id())
                .where(SqlAssignment.state.in_(sorted(_DUE_STATES)))
                .where(SqlAssignment.next_check_at.is_not(None))
                .where(SqlAssignment.next_check_at <= now)
                .order_by(asc(SqlAssignment.next_check_at), asc(SqlAssignment.id))
                .limit(limit)
            )
            rows = session.execute(stmt).scalars().all()
            return [_assignment_to_entity(r) for r in rows]

    def select_for_host(self, host_id: str, *, limit: int) -> builtins.list[Assignment]:
        """Select one host's non-terminal rows in one statement."""
        with self._session("select_assignments_for_host") as session:
            stmt = (
                select(SqlAssignment)
                .where(SqlAssignment.workspace_id == current_workspace_id())
                .where(SqlAssignment.resolved_host_id == host_id)
                .where(SqlAssignment.state.in_(sorted(NON_TERMINAL_STATES)))
                .order_by(asc(SqlAssignment.created_at), asc(SqlAssignment.id))
                .limit(limit)
            )
            rows = session.execute(stmt).scalars().all()
            return [_assignment_to_entity(r) for r in rows]

    def select_waiting_for_host(
        self, *, host_id: str, owner_user_id: str | None, limit: int
    ) -> builtins.list[Assignment]:
        """Select waiting rows one host may claim, in one statement."""
        with self._session("select_waiting_assignments_for_host") as session:
            if owner_user_id is None:
                owner_predicate = SqlAssignment.owner_user_id.is_(None)
            else:
                owner_predicate = SqlAssignment.owner_user_id == owner_user_id
            stmt = (
                select(SqlAssignment)
                .where(SqlAssignment.workspace_id == current_workspace_id())
                .where(SqlAssignment.state == AssignmentState.WAITING.value)
                .where(
                    or_(
                        SqlAssignment.requested_host_id == host_id,
                        and_(
                            SqlAssignment.requested_host_id.is_(None),
                            owner_predicate,
                        ),
                    )
                )
                .order_by(asc(SqlAssignment.next_check_at), asc(SqlAssignment.id))
                .limit(limit)
            )
            rows = session.execute(stmt).scalars().all()
            return [_assignment_to_entity(r) for r in rows]

    def append_message(self, message: AssignmentMessage) -> AssignmentMessage:
        """Append a message; a keyed retry returns the stored row.

        A unique-violation from a concurrent append re-reads the winner
        instead of surfacing a constraint error.
        """
        created_at = now_epoch()

        def write(session: Session) -> AssignmentMessage:
            wid = current_workspace_id()
            if message.idempotency_key is not None:
                existing = (
                    session.execute(
                        select(SqlAssignmentMessage)
                        .where(SqlAssignmentMessage.workspace_id == wid)
                        .where(SqlAssignmentMessage.assignment_id == message.assignment_id)
                        .where(SqlAssignmentMessage.sender_session_id == message.sender_session_id)
                        .where(SqlAssignmentMessage.idempotency_key == message.idempotency_key)
                    )
                    .scalars()
                    .first()
                )
                if existing is not None:
                    return _message_to_entity(existing)
            row = SqlAssignmentMessage(
                id=message.id or uuid.uuid4().hex,
                assignment_id=message.assignment_id,
                sender_session_id=message.sender_session_id,
                kind=message.kind,
                body=message.body,
                idempotency_key=message.idempotency_key,
                created_at=created_at,
                updated_at=None,
            )
            session.add(row)
            try:
                session.flush()
            except IntegrityError:
                if message.idempotency_key is None:
                    raise
                session.rollback()
                existing = (
                    session.execute(
                        select(SqlAssignmentMessage)
                        .where(SqlAssignmentMessage.workspace_id == wid)
                        .where(SqlAssignmentMessage.assignment_id == message.assignment_id)
                        .where(SqlAssignmentMessage.sender_session_id == message.sender_session_id)
                        .where(SqlAssignmentMessage.idempotency_key == message.idempotency_key)
                    )
                    .scalars()
                    .first()
                )
                if existing is None:  # pragma: no cover - defensive; the flush failed on a row
                    raise
                return _message_to_entity(existing)
            return _message_to_entity(row)

        return run_write_transaction(self._session_immediate, "append_message", write)

    def read_messages(
        self,
        assignment_id: str,
        *,
        after: str | None = None,
        limit: int = 20,
    ) -> PagedList[AssignmentMessage]:
        """Cursor-read an assignment's messages; repeatable, never consuming."""
        with self._session("read_assignment_messages") as session:
            stmt = (
                select(SqlAssignmentMessage)
                .where(SqlAssignmentMessage.workspace_id == current_workspace_id())
                .where(SqlAssignmentMessage.assignment_id == assignment_id)
                .order_by(asc(SqlAssignmentMessage.created_at), asc(SqlAssignmentMessage.id))
            )
            rows = session.execute(stmt).scalars().all()
            items = [_message_to_entity(r) for r in rows]
            return paginate_in_memory(items, lambda m: m.id, limit=limit, after=after, order="asc")
