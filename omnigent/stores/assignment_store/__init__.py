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
        project_id: str | None = None,
        state: str | None = None,
        source_session_id: str | None = None,
        attempt_session_id: str | None = None,
        limit: int = 20,
        after: str | None = None,
    ) -> PagedList[Assignment]:
        """
        List assignments matching the given filters, cursor-paginated.

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
        **fields: Any,
    ) -> Assignment | None:
        """
        Move an assignment to a new state with a conditional write.

        The pair is first checked against the legal transition table, then
        applied as ``UPDATE ... WHERE state = from_state`` — a concurrent
        writer that moved the row first makes this call return ``None``
        instead of clobbering it.

        :param assignment_id: Opaque assignment identifier.
        :param from_state: The state the caller last saw.
        :param to_state: The desired next state.
        :param fields: Additional mutable columns to set atomically
            (``wait_reason``, ``next_check_at``, ``active_attempt_id``,
            ``resolved_host_id``, …, plus the ``outputs`` /
            ``metadata`` entity conveniences).
        :returns: The updated :class:`Assignment`, or ``None`` when the row
            is missing or no longer in ``from_state``.
        :raises IllegalAssignmentTransitionError: If the pair is not legal.
        """
        ...

    @abstractmethod
    def claim_attempt(
        self, assignment_id: str, *, host_id: str, now: int
    ) -> AssignmentAttempt | None:
        """
        Claim the next attempt number single-flight.

        In one transaction: ``UPDATE assignments SET state='starting',
        active_attempt_id=<new>, resolved_host_id=<host> WHERE state='waiting'
        AND active_attempt_id IS NULL AND (resolved_host_id IS NULL OR
        resolved_host_id = <host>)``; only when exactly one row changed, the
        attempt row (``number`` = previous max + 1, ``active``) is inserted.
        A row pinned to another host is not claimable.

        :param assignment_id: The waiting assignment to claim.
        :param host_id: The host the attempt will run on.
        :param now: Unix epoch seconds stamped on the rows.
        :returns: The new :class:`AssignmentAttempt`, or ``None`` when the
            row is not claimable (missing, not ``waiting``, already
            claimed, pinned to another host) — with no attempt row inserted.
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

        Non-terminal, actionable rows with ``next_check_at <= now``,
        ordered by ``next_check_at``, at most ``limit``.

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
