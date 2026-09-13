"""Assignment store — persists assignments, attempts and messages.

An assignment is one unit of work handed to one ``(host, agent)``
destination. This store owns the ``assignments``, ``assignment_attempts``
and ``assignment_messages`` tables. All cross-table references are
application-owned, never DB foreign keys (schema Rule R032).
"""

from __future__ import annotations

import builtins
from abc import ABC, abstractmethod
from typing import Any

from omnigent.entities import Assignment, AssignmentAttempt, AssignmentMessage
from omnigent.entities.pagination import PagedList
from omnigent.errors import ErrorCode, OmnigentError

# Sentinel for conditional-write predicates: an old attempt's write must
# not commit after a retry bound a new attempt (ABA), so "not passed"
# stays distinct from an explicit ``None``.
_UNSET: Any = object()


class AssignmentIdempotencyConflictError(OmnigentError):
    """A retried create carried a different payload than the stored row.

    The caller reused an id (or a ``(source_session_id, idempotency_key)``
    pair) with a changed ``request_digest`` — the existing row is returned
    untouched and the write is refused.
    """

    def __init__(self, assignment_id: str) -> None:
        """
        Initialize the idempotency-conflict error.

        :param assignment_id: The conflicting assignment id.
        """
        super().__init__(
            f"assignment {assignment_id} already exists with a different payload",
            code=ErrorCode.CONFLICT,
        )


class InactiveAttemptError(OmnigentError):
    """An attempt write did not come from the assignment's active attempt.

    The coordinator rejects updates from a non-active attempt so a late
    former runner can never overwrite accepted output.
    """

    def __init__(self, assignment_id: str, attempt_id: str) -> None:
        """
        Initialize the non-active-attempt error.

        :param assignment_id: The assignment the write targeted.
        :param attempt_id: The attempt the write came from.
        """
        super().__init__(
            f"attempt {attempt_id} is not the active attempt of assignment {assignment_id}",
            code=ErrorCode.CONFLICT,
        )


class IllegalAssignmentTransitionError(OmnigentError):
    """A requested state transition is not in the legal transition table."""

    def __init__(self, from_state: str | None, to_state: str) -> None:
        """
        Initialize the illegal-transition error.

        :param from_state: The current state, or ``None`` for creation.
        :param to_state: The rejected next state.
        """
        super().__init__(
            f"illegal assignment transition {from_state!r} -> {to_state!r}",
            code=ErrorCode.INVALID_INPUT,
        )


class AssignmentStore(ABC):
    """
    Abstract base for assignment persistence.

    Manages assignments (idempotent create / get / list / conditional
    transition), single-flight attempt claims, attempt updates, the
    due-work and host-scoped selection queries, and append-only messages.
    Reads and writes are scoped by the ambient workspace.
    """

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the assignment store.

        :param storage_location: Backend-specific storage URI,
            e.g. ``"sqlite:///chat.db"`` for SQLAlchemy.
        """
        self.storage_location = storage_location

    @abstractmethod
    def create(self, assignment: Assignment) -> Assignment:
        """
        Insert a new assignment in ``preparing``. Idempotent.

        An existing row with the same id, or the same
        ``(source_session_id, idempotency_key)``, and the same
        ``request_digest`` is returned unchanged; a different digest raises.

        :param assignment: The assignment to insert. Its ``state`` is
            ignored — new rows always start in ``preparing``.
        :returns: The inserted or already-stored :class:`Assignment`.
        :raises AssignmentIdempotencyConflictError: If the id (or the
            ``(source_session_id, idempotency_key)`` pair) is taken by a row
            with a different ``request_digest``.
        """
        ...

    @abstractmethod
    def get(self, assignment_id: str) -> Assignment | None:
        """
        Return an assignment by id, or ``None`` if not found.

        :param assignment_id: Opaque assignment identifier.
        :returns: The :class:`Assignment` if found, else ``None``.
        """
        ...

    @abstractmethod
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
        """
        List assignments matching the given filters, cursor-paginated.

        Reads are always owner-scoped: only rows whose ``owner_user_id``
        equals the caller are returned. ``None`` selects single-user rows;
        there is no unfiltered mode.

        :param owner_user_id: Return only this owner's assignments.
        :param project_id: Return only this project's assignments.
        :param state: Return only assignments in this state.
        :param source_session_id: Return only assignments dispatched by this
            session.
        :param attempt_session_id: Return only assignments with an attempt
            that created this session.
        :param limit: Maximum assignments in the page.
        :param after: Cursor id — return assignments after this one.
        :returns: A :class:`PagedList` of :class:`Assignment`.
        """
        ...

    @abstractmethod
    def transition(
        self,
        assignment_id: str,
        *,
        from_state: str,
        to_state: str,
        expected_active_attempt_id: Any = _UNSET,
        **fields: Any,
    ) -> Assignment | None:
        """
        Move an assignment to a new state with a conditional write.

        The pair is first checked against the legal transition table, then
        applied as ``UPDATE ... WHERE state = from_state`` — a concurrent
        writer that moved the row first makes this call return ``None``
        instead of clobbering it. When ``expected_active_attempt_id`` is
        passed (including ``None``), the write also requires
        ``active_attempt_id`` to still equal it.

        :param assignment_id: Opaque assignment identifier.
        :param from_state: The state the caller last saw.
        :param to_state: The desired next state.
        :param expected_active_attempt_id: Pinned attempt the caller
            validated; ``None`` pins no active attempt. Omitted pins
            nothing.
        :param fields: Additional mutable columns to set atomically
            (``wait_reason``, ``next_check_at``, ``active_attempt_id``,
            ``resolved_host_id``, …, plus the ``inputs`` / ``outputs`` /
            ``metadata`` entity conveniences).
        :returns: The updated :class:`Assignment`, or ``None`` when the row
            is missing or no longer in ``from_state`` (or the pinned
            attempt changed).
        :raises IllegalAssignmentTransitionError: If the pair is not legal.
        """
        ...

    @abstractmethod
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
        """
        Claim the next attempt number single-flight.

        In one transaction: ``UPDATE assignments SET state='starting',
        active_attempt_id=<new>, resolved_host_id=<host> WHERE state='waiting'
        AND active_attempt_id IS NULL AND (resolved_host_id IS NULL OR
        resolved_host_id = <host>)``; only when exactly one row changed, the
        attempt row (``number`` = previous max + 1, ``active``) is inserted.
        A row pinned to another host is not claimable. When any of
        ``resolved_binding_id``, ``resolved_binding_revision`` or
        ``next_check_at`` is passed it is written in the same UPDATE, which
        also clears ``wait_reason``. When ``expected_binding_pin`` is passed,
        the claim also requires the stored pin to still equal it: ``None``
        requires ``resolved_binding_id IS NULL``; a ``(binding_id,
        revision)`` tuple requires both columns to still equal it.

        :param assignment_id: The waiting assignment to claim.
        :param host_id: The host the attempt will run on.
        :param now: Unix epoch seconds stamped on the rows.
        :param resolved_binding_id: Binding snapshot to pin, or omitted.
        :param resolved_binding_revision: Binding revision to pin, or omitted.
        :param next_check_at: Next coordinator check to stamp, or omitted.
        :param expected_binding_pin: Pinned binding the caller read; ``None``
            pins no binding, a tuple pins that binding and revision. Omitted
            pins nothing.
        :returns: The new :class:`AssignmentAttempt`, or ``None`` when the
            row is not claimable (missing, not ``waiting``, already
            claimed, pinned to another host, or the binding pin moved) —
            with no attempt row inserted.
        """
        ...

    @abstractmethod
    def reschedule(
        self,
        assignment_id: str,
        *,
        expected_state: str,
        next_check_at: int | None,
        wait_reason: Any = _UNSET,
        expected_active_attempt_id: Any = _UNSET,
    ) -> Assignment | None:
        """
        Stamp the next coordinator check without changing state.

        A conditional write, not a transition: ``UPDATE ... WHERE
        workspace_id, id, state = expected_state`` setting
        ``next_check_at`` and ``updated_at``. ``wait_reason`` is written
        only when passed, so other states can move ``next_check_at``
        without touching it. When ``expected_active_attempt_id`` is passed
        (including ``None``), the write also requires ``active_attempt_id``
        to still equal it.

        :param assignment_id: The assignment to reschedule.
        :param expected_state: The state the caller last saw; a concurrent
            move makes this return ``None``.
        :param next_check_at: Next coordinator check to stamp, or ``None``.
        :param wait_reason: New visible reason, or omitted to leave it.
        :param expected_active_attempt_id: Pinned attempt the caller
            validated; ``None`` pins no active attempt. Omitted pins
            nothing.
        :returns: The updated :class:`Assignment`, or ``None`` when the row
            is missing, no longer in ``expected_state``, or the pinned
            attempt changed.
        """
        ...

    @abstractmethod
    def update_attempt(
        self,
        assignment_id: str,
        attempt_id: str,
        **fields: Any,
    ) -> AssignmentAttempt | None:
        """
        Update the assignment's active attempt.

        :param assignment_id: The assignment the attempt belongs to.
        :param attempt_id: The attempt to update.
        :param fields: Mutable attempt columns (``runner_id``,
            ``session_id``, ``state``, ``lease_expires_at``,
            ``started_at``, ``ended_at``, ``error_code``, ``host_id``).
        :returns: The updated :class:`AssignmentAttempt`, or ``None`` when
            the assignment or attempt row is missing.
        :raises InactiveAttemptError: When ``attempt_id`` is not the
            assignment's ``active_attempt_id`` or the attempt is not
            ``active``.
        """
        ...

    @abstractmethod
    def get_attempt(self, assignment_id: str, attempt_id: str) -> AssignmentAttempt | None:
        """
        Return one attempt of an assignment, or ``None`` if not found.

        A plain read with no activeness check: lifecycle routes need ended
        attempts too (cancellation and retry must confirm the old execution
        stopped), so this never raises :class:`InactiveAttemptError`.

        :param assignment_id: The assignment the attempt belongs to.
        :param attempt_id: The attempt to return.
        :returns: The :class:`AssignmentAttempt`, or ``None`` when the
            attempt row is missing or belongs to another assignment.
        """
        ...

    @abstractmethod
    def get_latest_attempt(self, assignment_id: str) -> AssignmentAttempt | None:
        """
        Return the highest-numbered attempt of an assignment, or ``None``.

        Placement evidence for terminal rows whose link was cleared: a
        prepared-then-failed placement returns to ``waiting`` with
        ``active_attempt_id`` cleared while ``resolved_host_id`` stays, so
        the release still needs the original ``started_at`` and session.

        :param assignment_id: The assignment whose attempts to inspect.
        :returns: The :class:`AssignmentAttempt` with the highest
            ``number``, or ``None`` when the assignment has no attempts.
        """
        ...

    @abstractmethod
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
        """
        Re-pin a ``waiting`` row against the current configuration.

        A conditional write, not a transition: ``wait_reason`` changes are
        not transitions, so the legal-transition table (which has no
        self-loop) does not apply. Sets ``inputs_json``, clears
        ``wait_reason``, stamps ``next_check_at``, and clears the
        claim-time binding snapshot (the coordinator re-pins at claim).
        When ``expected_inputs_json`` is passed, the write also requires
        the stored ``inputs_json`` blob to still equal it. When
        ``expected_project_revision`` is passed, the write also requires
        the stored ``project_revision`` to still equal it.

        :param assignment_id: The waiting assignment to re-pin.
        :param inputs: The re-pinned input entries (revisions refreshed,
            digests untouched).
        :param project_revision: The current ``collaboration_revision``.
        :param now: Unix epoch seconds stamped on ``next_check_at``.
        :param expected_inputs_json: Pinned ``inputs_json`` blob the caller
            computed from. Omitted pins nothing.
        :param expected_project_revision: Pinned ``project_revision`` the
            caller read with the row. Omitted pins nothing.
        :returns: The updated :class:`Assignment`, or ``None`` when the row
            is missing, no longer ``waiting``, or the pinned inputs changed.
        """
        ...

    @abstractmethod
    def mark_event_dispatched(self, attempt_id: str, *, now: int) -> bool:
        """
        Compare-and-set ``event_dispatched_at``.

        :param attempt_id: The attempt the initial event was dispatched for.
        :param now: Unix epoch seconds to stamp.
        :returns: ``True`` only for the first caller; concurrent retries get
            ``False`` and must not re-dispatch.
        """
        ...

    @abstractmethod
    def set_lease(self, attempt_id: str, lease_expires_at: int | None) -> None:
        """
        Write the server-observed liveness lease of an attempt.

        :param attempt_id: The attempt whose lease to set.
        :param lease_expires_at: Unix epoch seconds the lease expires, or
            ``None`` to clear it.
        """
        ...

    @abstractmethod
    def select_due(self, *, now: int, limit: int) -> builtins.list[Assignment]:
        """
        Return due assignments for the bounded recovery pass, in one query.

        Actionable rows with ``next_check_at <= now``, ordered by
        ``next_check_at``, at most ``limit``. ``interrupted`` rows are
        included so a confirmed stop can retire them; terminal rows with
        a due ``next_check_at`` are included so a pending worktree
        release runs. Rows with ``next_check_at`` NULL are never due.

        :param now: Unix epoch seconds the pass runs at.
        :param limit: Maximum rows per pass.
        :returns: Due :class:`Assignment` instances.
        """
        ...

    @abstractmethod
    def select_for_host(self, host_id: str, *, limit: int) -> builtins.list[Assignment]:
        """
        Return one host's non-terminal assignments, in one query.

        :param host_id: The host whose rows to return.
        :param limit: Maximum rows to return.
        :returns: :class:`Assignment` instances with
            ``resolved_host_id == host_id``.
        """
        ...

    @abstractmethod
    def select_waiting_for_host(
        self, *, host_id: str, owner_user_id: str | None, limit: int
    ) -> builtins.list[Assignment]:
        """
        Return waiting rows one host may claim, in one query.

        ``state = 'waiting' AND (requested_host_id = :host OR
        (requested_host_id IS NULL AND owner_user_id = :owner))`` (``IS
        NULL`` when the owner is ``None``), ordered by ``next_check_at``,
        ``id``, at most ``limit``. Served by the ``(workspace_id, state,
        ...)`` prefix of ``ix_assignments_due`` — no new index.

        :param host_id: The host that just connected.
        :param owner_user_id: The connection owner's user, or ``None`` in
            single-user mode; matches only unrequested rows of the same
            owner.
        :param limit: Maximum rows to return.
        :returns: Due-claimable :class:`Assignment` instances.
        """
        ...

    @abstractmethod
    def append_message(self, message: AssignmentMessage) -> AssignmentMessage:
        """
        Append an assignment-scoped message. Idempotent on
        ``(assignment_id, sender_session_id, idempotency_key)`` when a key
        is given — a retry returns the stored row.

        :param message: The message to append.
        :returns: The appended or already-stored :class:`AssignmentMessage`.
        """
        ...

    @abstractmethod
    def read_messages(
        self,
        assignment_id: str,
        *,
        after: str | None = None,
        limit: int = 20,
    ) -> PagedList[AssignmentMessage]:
        """
        Cursor-read an assignment's messages. Repeatable; reading never
        consumes.

        :param assignment_id: The assignment whose messages to read.
        :param after: Cursor id — return messages after this one.
        :param limit: Maximum messages in the page.
        :returns: A :class:`PagedList` of :class:`AssignmentMessage`.
        """
        ...
