"""Assignment entities — persisted in the ``assignments``,
``assignment_attempts`` and ``assignment_messages`` tables.

An :class:`Assignment` is one unit of work handed to one ``(host, agent)``
destination. Artifacts and context move by git; the row stores pointers,
addressing and state, never file content. This module holds the plain
dataclasses the store converts ORM rows into, the legal state-transition
table, and the JSON (de)serialization of the Text-backed columns.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class AssignmentState(str, Enum):
    """Lifecycle states of an assignment. String-valued so rows, entities
    and wire payloads all carry the same tokens."""

    PREPARING = "preparing"
    WAITING = "waiting"
    STARTING = "starting"
    RUNNING = "running"
    PUBLISHING = "publishing"
    STOPPING = "stopping"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    INTERRUPTED = "interrupted"


#: States with no outgoing transition. Heartbeats and ``wait_reason``
#: changes are not transitions.
TERMINAL_STATES: frozenset[str] = frozenset(
    {
        AssignmentState.SUCCEEDED.value,
        AssignmentState.FAILED.value,
        AssignmentState.CANCELLED.value,
        AssignmentState.EXPIRED.value,
    }
)

#: Every other state: the row still needs coordinator attention.
NON_TERMINAL_STATES: frozenset[str] = frozenset(s.value for s in AssignmentState) - TERMINAL_STATES

#: Every legal transition; anything absent is rejected by the store.
#: Creation is ``None -> preparing``. Terminal states map to the empty set.
LEGAL_TRANSITIONS: dict[str | None, frozenset[str]] = {
    None: frozenset({AssignmentState.PREPARING.value}),
    AssignmentState.PREPARING.value: frozenset(
        {
            AssignmentState.WAITING.value,
            AssignmentState.FAILED.value,
            AssignmentState.CANCELLED.value,
        }
    ),
    AssignmentState.WAITING.value: frozenset(
        {
            AssignmentState.STARTING.value,
            AssignmentState.EXPIRED.value,
            AssignmentState.CANCELLED.value,
        }
    ),
    AssignmentState.STARTING.value: frozenset(
        {
            AssignmentState.RUNNING.value,
            AssignmentState.WAITING.value,
            AssignmentState.EXPIRED.value,
            AssignmentState.FAILED.value,
            AssignmentState.INTERRUPTED.value,
            AssignmentState.STOPPING.value,
        }
    ),
    AssignmentState.RUNNING.value: frozenset(
        {
            AssignmentState.PUBLISHING.value,
            AssignmentState.FAILED.value,
            AssignmentState.INTERRUPTED.value,
            AssignmentState.STOPPING.value,
        }
    ),
    AssignmentState.PUBLISHING.value: frozenset(
        {
            AssignmentState.SUCCEEDED.value,
            AssignmentState.FAILED.value,
            AssignmentState.INTERRUPTED.value,
            AssignmentState.STOPPING.value,
        }
    ),
    AssignmentState.STOPPING.value: frozenset(
        {
            AssignmentState.CANCELLED.value,
            AssignmentState.INTERRUPTED.value,
        }
    ),
    AssignmentState.INTERRUPTED.value: frozenset(
        {
            AssignmentState.WAITING.value,
            AssignmentState.EXPIRED.value,
            AssignmentState.CANCELLED.value,
        }
    ),
    AssignmentState.SUCCEEDED.value: frozenset(),
    AssignmentState.FAILED.value: frozenset(),
    AssignmentState.CANCELLED.value: frozenset(),
    AssignmentState.EXPIRED.value: frozenset(),
}


def is_legal_transition(from_state: str | None, to_state: str) -> bool:
    """Return whether ``from_state -> to_state`` is a legal transition.

    Creation is ``None -> preparing``. Terminal states accept nothing —
    every outgoing transition from them is rejected.

    :param from_state: The current state, or ``None`` for row creation.
    :param to_state: The candidate next state.
    :returns: ``True`` when the pair appears in :data:`LEGAL_TRANSITIONS`.
    """
    return to_state in LEGAL_TRANSITIONS.get(from_state, frozenset())


@dataclass
class AssignmentInputEntry:
    """
    One repository's dispatch snapshot inside ``inputs_json``.

    :param repository_name: The registered repository's stable name.
    :param repository_revision: The repository ``revision`` at dispatch;
        a staleness check compares against this.
    :param remote_url: The shared remote the receiver fetches from.
    :param input_commit: The pinned commit hash the receiver verifies after
        fetching.
    :param input_ref: The ``refs/omnigent/assignments/...`` ref carrying the
        input.
    :param context_manifest_path: Repo-relative manifest path hashed into
        ``manifest_digest``.
    :param manifest_digest: Hash of the manifest at dispatch.
    :param artifact_paths: Repo-relative paths the receiver must materialise.
    :param is_execution_root: Exactly one entry per assignment is the
        execution root — the repository the session workspace is prepared
        from.
    :param observed_commit: The commit actually observed at ``input_ref``
        when publication was confirmed, or ``None`` when unobserved. Lives
        inside ``inputs_json`` (no new column); the ``published`` route
        records it on a failed publication so the state is reconcilable.
    """

    repository_name: str
    repository_revision: int
    remote_url: str
    input_commit: str
    input_ref: str
    context_manifest_path: str
    manifest_digest: str
    artifact_paths: list[str] = field(default_factory=list)
    is_execution_root: bool = False
    observed_commit: str | None = None


@dataclass
class AssignmentOutputEntry:
    """
    One repository's accepted output inside ``outputs_json``.

    :param repository_name: The registered repository's stable name.
    :param commit: The advertised output commit hash.
    :param ref: The attempt-keyed ``refs/omnigent/assignments/...`` ref.
    :param artifact_paths: Repo-relative paths produced by the attempt.
    """

    repository_name: str
    commit: str
    ref: str
    artifact_paths: list[str] = field(default_factory=list)


def inputs_to_json(entries: list[AssignmentInputEntry]) -> str:
    """Pack input entries into the compact JSON blob stored in ``inputs_json``.

    :param entries: The dispatch snapshot, one entry per repository.
    :returns: Compact JSON array string.
    """
    return json.dumps(
        [
            {
                "repository_name": e.repository_name,
                "repository_revision": e.repository_revision,
                "remote_url": e.remote_url,
                "input_commit": e.input_commit,
                "input_ref": e.input_ref,
                "context_manifest_path": e.context_manifest_path,
                "manifest_digest": e.manifest_digest,
                "artifact_paths": list(e.artifact_paths),
                "is_execution_root": e.is_execution_root,
                "observed_commit": e.observed_commit,
            }
            for e in entries
        ],
        separators=(",", ":"),
    )


def inputs_from_json(raw: str | None) -> list[AssignmentInputEntry]:
    """Unpack the ``inputs_json`` blob to input entries (``[]`` when unset).

    :param raw: The stored JSON blob, or ``None``.
    :returns: The decoded entries in stored order.
    """
    if not raw:
        return []
    return [
        AssignmentInputEntry(
            repository_name=item["repository_name"],
            repository_revision=item["repository_revision"],
            remote_url=item["remote_url"],
            input_commit=item["input_commit"],
            input_ref=item["input_ref"],
            context_manifest_path=item["context_manifest_path"],
            manifest_digest=item["manifest_digest"],
            artifact_paths=list(item.get("artifact_paths", [])),
            is_execution_root=item.get("is_execution_root", False),
            observed_commit=item.get("observed_commit"),
        )
        for item in json.loads(raw)
    ]


def outputs_to_json(entries: list[AssignmentOutputEntry]) -> str:
    """Pack output entries into the compact JSON blob stored in ``outputs_json``.

    :param entries: The accepted outputs, one entry per repository.
    :returns: Compact JSON array string.
    """
    return json.dumps(
        [
            {
                "repository_name": e.repository_name,
                "commit": e.commit,
                "ref": e.ref,
                "artifact_paths": list(e.artifact_paths),
            }
            for e in entries
        ],
        separators=(",", ":"),
    )


def outputs_from_json(raw: str | None) -> list[AssignmentOutputEntry]:
    """Unpack the ``outputs_json`` blob to output entries (``[]`` when unset).

    :param raw: The stored JSON blob, or ``None``.
    :returns: The decoded entries in stored order.
    """
    if not raw:
        return []
    return [
        AssignmentOutputEntry(
            repository_name=item["repository_name"],
            commit=item["commit"],
            ref=item["ref"],
            artifact_paths=list(item.get("artifact_paths", [])),
        )
        for item in json.loads(raw)
    ]


@dataclass
class Assignment:
    """
    One unit of work handed to one ``(host, agent)`` destination.

    :param id: Caller-generated UUID (bare 32-char hex), stable across
        retries so a retried create is recognised.
    :param project_id: The collaboration project.
    :param source_session_id: The session that dispatched the assignment.
    :param target_agent_id: The agent to launch on arrival.
    :param task: The natural-language instruction blob.
    :param inputs: The immutable dispatch snapshot: one entry per
        repository, with exactly one ``is_execution_root`` entry.
    :param idempotency_key: Caller key; unique with ``source_session_id``.
    :param request_digest: Digest of the create payload the idempotency
        comparison is made on.
    :param owner_user_id: The dispatching user; ``None`` in single-user mode.
    :param requested_host_id: The named destination host, or ``None``.
    :param resolved_host_id: Written once at claim time, then never changed.
    :param binding_name: Which binding of the destination host to run in.
    :param resolved_binding_id: The binding snapshot the work started against.
    :param resolved_binding_revision: The binding ``revision`` at claim time.
    :param project_revision: ``collaboration_revision`` at create time.
    :param metadata: Optional structured extras.
    :param model_override: Per-assignment LLM model override, or ``None``
        for the agent default.
    :param harness_override: Per-assignment harness override, or ``None``
        for the agent default.
    :param start_deadline: Unix epoch seconds bounding the wait, or ``None``
        to wait until cancelled.
    :param state: Lifecycle state (see :class:`AssignmentState`).
    :param wait_reason: The visible reason while ``waiting``.
    :param next_check_at: Unix epoch seconds when the coordinator may look
        at this row again, or ``None``.
    :param active_attempt_id: At most one.
    :param outputs: Accepted outputs, written atomically with ``succeeded``.
    :param result_summary: Human-readable outcome summary.
    :param error_code: Short failure classification, or ``None``.
    :param cancel_requested_at: Unix epoch seconds cancellation was
        requested, or ``None``.
    :param created_at: Unix epoch seconds at row creation.
    :param updated_at: Unix epoch seconds of the last write, or ``None``.
    :param workspace_id: Tenant partition key that owns this row.
    :raises ValueError: If ``inputs`` does not hold exactly one entry with
        ``is_execution_root`` true.
    """

    id: str
    project_id: str
    source_session_id: str
    target_agent_id: str
    task: str
    inputs: list[AssignmentInputEntry]
    idempotency_key: str
    request_digest: str
    owner_user_id: str | None = None
    requested_host_id: str | None = None
    resolved_host_id: str | None = None
    binding_name: str = "primary"
    resolved_binding_id: str | None = None
    resolved_binding_revision: int | None = None
    project_revision: int = 0
    metadata: dict[str, Any] | None = None
    model_override: str | None = None
    harness_override: str | None = None
    start_deadline: int | None = None
    state: str = AssignmentState.PREPARING.value
    wait_reason: str | None = None
    next_check_at: int | None = None
    active_attempt_id: str | None = None
    outputs: list[AssignmentOutputEntry] | None = None
    result_summary: str | None = None
    error_code: str | None = None
    cancel_requested_at: int | None = None
    created_at: int = 0
    updated_at: int | None = None
    workspace_id: int = 0

    def __post_init__(self) -> None:
        """Enforce the exactly-one-execution-root invariant."""
        roots = sum(1 for entry in self.inputs if entry.is_execution_root)
        if roots != 1:
            raise ValueError(
                f"inputs must hold exactly one entry with is_execution_root "
                f"true, got {roots} in {len(self.inputs)} entries"
            )


@dataclass
class AssignmentAttempt:
    """
    One execution attempt of an assignment.

    :param id: UUID primary key (bare 32-char hex string, no dashes).
    :param assignment_id: The assignment this attempt belongs to.
    :param number: 1-based attempt number; unique per assignment.
    :param host_id: The host the attempt runs on.
    :param runner_id: The runner the attempt launched on, or ``None``
        before launch.
    :param session_id: The conversation this attempt created, or ``None``
        before creation.
    :param state: ``active``/``finished``/``lost``.
    :param lease_expires_at: Unix epoch seconds the server-side liveness
        lease expires, or ``None`` when no lease is held.
    :param event_dispatched_at: Unix epoch seconds the initial assignment
        event was dispatched, or ``None``.
    :param started_at: Unix epoch seconds the attempt started, or ``None``.
    :param ended_at: Unix epoch seconds the attempt ended, or ``None``.
    :param error_code: Short failure classification, or ``None``.
    :param created_at: Unix epoch seconds at row creation.
    :param updated_at: Unix epoch seconds of the last write, or ``None``.
    :param workspace_id: Tenant partition key that owns this row.
    """

    id: str
    assignment_id: str
    number: int
    host_id: str
    runner_id: str | None = None
    session_id: str | None = None
    state: str = "active"
    lease_expires_at: int | None = None
    event_dispatched_at: int | None = None
    started_at: int | None = None
    ended_at: int | None = None
    error_code: str | None = None
    created_at: int = 0
    updated_at: int | None = None
    workspace_id: int = 0


@dataclass
class AssignmentMessage:
    """
    One append-only, assignment-scoped message.

    :param id: UUID primary key (bare 32-char hex string, no dashes).
    :param assignment_id: The assignment this message belongs to.
    :param sender_session_id: The sending session, or ``None`` for a
        server-generated state event.
    :param kind: ``note``/``state``.
    :param body: Message text.
    :param idempotency_key: Caller key for exactly-once append; ``None``
        for server state events, which carry none.
    :param created_at: Unix epoch seconds at row creation.
    :param updated_at: Unix epoch seconds of the last write — stays unset;
        rows are append-only.
    :param workspace_id: Tenant partition key that owns this row.
    """

    id: str
    assignment_id: str
    kind: str
    body: str
    sender_session_id: str | None = None
    idempotency_key: str | None = None
    created_at: int = 0
    updated_at: int | None = None
    workspace_id: int = 0
