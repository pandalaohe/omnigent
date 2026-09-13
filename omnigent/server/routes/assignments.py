"""REST API routes for project assignments (cross-host handoff).

One assignment is one unit of work handed to one ``(host, agent)``
destination. The dispatching session creates the row in ``preparing``,
pushes the derived input refs, then confirms publication into ``waiting``;
the receiving runner reports back through the runner-bound ``complete``
and ``finish`` routes. Artifacts move by git — the server stores pointers,
addressing and state, never file content.

Gating: only creation and refresh are gated. With
``Feature.PROJECT_ASSIGNMENTS`` off those two 404; with the flag on but
the project's ``collaboration_enabled`` off they 409 naming the project.
Every other route is never gated, so in-flight work can finish, publish
and report after the switch goes off.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import re
import uuid
from typing import Any, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from omnigent.db.utils import now_epoch
from omnigent.entities import (
    Assignment,
    AssignmentAttempt,
    AssignmentInputEntry,
    AssignmentMessage,
    AssignmentOutputEntry,
    AssignmentState,
    Conversation,
)
from omnigent.entities.assignment import inputs_to_json
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id
from omnigent.server.auth import LEVEL_OWNER, AuthProvider
from omnigent.server.feature_flags import Feature, FeatureFlags, resolve_feature_flags
from omnigent.server.routes._auth_helpers import require_access, require_user
from omnigent.server.routes._host_launch import resolve_host_owner
from omnigent.server.routes._session_create_validation import validate_session_agent
from omnigent.server.routes.project_collaboration import _validate_ref_name
from omnigent.stores import AgentStore, ConversationStore, PermissionStore
from omnigent.stores.assignment_store import (
    _UNSET,
    AssignmentIdempotencyConflictError,
    AssignmentStore,
    IllegalAssignmentTransitionError,
    InactiveAttemptError,
)
from omnigent.stores.project_repository_store import ProjectRepositoryStore
from omnigent.stores.project_store import ProjectStore

_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")

_VALID_STATES = frozenset(state.value for state in AssignmentState)


class AssignmentRepositoryInput(BaseModel):
    """One repository pinned into the dispatch snapshot.

    :param repository_name: A repository registered on the project.
    :param commit: The pinned input commit (40 or 64 lowercase hex).
    :param manifest_digest: Hash of the context manifest at dispatch.
    :param context_manifest_path: Repo-relative manifest path; defaults to
        the registration default when omitted.
    :param artifact_paths: Repo-relative paths the receiver materialises.
    """

    model_config = ConfigDict(extra="forbid")

    repository_name: str
    commit: str
    manifest_digest: str
    context_manifest_path: str | None = None
    artifact_paths: list[str] = Field(default_factory=list)


class AssignmentCreateRequest(BaseModel):
    """Body for ``POST /v1/assignments``.

    :param id: Caller-generated id, 32 lowercase hex, stable across retries.
    :param source_session_id: The dispatching session; its project is the
        assignment's project and the caller needs owner access on it.
    :param target_agent_id: The agent to launch on arrival.
    :param requested_host_id: The named destination host, or ``None``.
    :param binding_name: Which binding of the destination host to run in.
    :param task: The natural-language instruction blob.
    :param metadata: Optional structured extras.
    :param repositories: The pinned input repositories, one entry each.
    :param execution_root: Which repository the workspace is prepared from;
        required with more than one repository, defaulted otherwise.
    :param model_override: Per-assignment model override, or ``None``.
    :param harness_override: Per-assignment harness override, or ``None``.
    :param start_deadline: Epoch seconds bounding the wait, or ``None``.
    :param idempotency_key: Caller key; unique with ``source_session_id``.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    source_session_id: str
    target_agent_id: str
    requested_host_id: str | None = None
    binding_name: str = "primary"
    task: str = Field(min_length=1)
    metadata: dict[str, Any] | None = None
    repositories: list[AssignmentRepositoryInput] = Field(min_length=1)
    execution_root: str | None = None
    model_override: str | None = None
    harness_override: str | None = None
    start_deadline: int | None = None
    idempotency_key: str = Field(min_length=1)


class RefCommit(BaseModel):
    """One observed ``(repository, commit)`` pair.

    :param repository_name: The repository the ref belongs to.
    :param commit: The commit observed at the ref.
    """

    model_config = ConfigDict(extra="forbid")

    repository_name: str
    commit: str


class PublishedRefsRequest(BaseModel):
    """Body for ``POST /v1/assignments/{id}/published``.

    :param refs: The pushed input refs with their observed commits.
    """

    model_config = ConfigDict(extra="forbid")

    refs: list[RefCommit]


class AssignmentMessageRequest(BaseModel):
    """Body for ``POST /v1/assignments/{id}/messages``.

    :param sender_session_id: The sending session; the caller needs owner
        access on it.
    :param body: Message text.
    :param idempotency_key: Caller key for exactly-once append.
    """

    model_config = ConfigDict(extra="forbid")

    sender_session_id: str
    body: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)


class CancelRequest(BaseModel):
    """Body for ``POST /v1/assignments/{id}/cancel``.

    :param reason: Optional human-readable reason, recorded on the state
        message.
    """

    model_config = ConfigDict(extra="forbid")

    reason: str | None = None


class AssignmentOutputInput(BaseModel):
    """One advertised output repository.

    :param repository_name: A repository from the assignment's inputs.
    :param commit: The advertised output commit.
    :param artifact_paths: Repo-relative paths produced by the attempt.
    """

    model_config = ConfigDict(extra="forbid")

    repository_name: str
    commit: str
    artifact_paths: list[str] = Field(default_factory=list)


class CompleteRequest(BaseModel):
    """Body for ``POST /v1/assignments/{id}/complete``.

    :param session_id: The calling session; must be the active attempt's
        session on its bound runner.
    :param outputs: The advertised outputs, at most one per repository.
    :param summary: Human-readable outcome summary.
    """

    model_config = ConfigDict(extra="forbid")

    session_id: str
    outputs: list[AssignmentOutputInput] = Field(min_length=1)
    summary: str = Field(min_length=1)


class FinishRequest(BaseModel):
    """Body for ``POST /v1/assignments/{id}/finish``.

    :param session_id: The calling session; must be the active attempt's
        session on its bound runner.
    :param refs: The pushed output refs with their observed commits.
    :param error: Optional failure report; any error fails the assignment.
    """

    model_config = ConfigDict(extra="forbid")

    session_id: str
    refs: list[RefCommit]
    error: str | None = None


def _input_ref(assignment_id: str, repository_name: str) -> str:
    """Derive the input ref the caller pushes one repository to.

    Ref names are server-derived, never accepted from the client.

    :param assignment_id: The assignment being published.
    :param repository_name: The registered repository name.
    :returns: The ``refs/omnigent/assignments/...`` input ref.
    """
    return f"refs/omnigent/assignments/{assignment_id}/input/{repository_name}"


def _output_ref(assignment_id: str, attempt_id: str, repository_name: str) -> str:
    """Derive the attempt-keyed output ref the runner pushes to.

    :param assignment_id: The assignment being completed.
    :param attempt_id: The active attempt publishing the output.
    :param repository_name: The repository the output belongs to.
    :returns: The ``refs/omnigent/assignments/...`` output ref.
    """
    return f"refs/omnigent/assignments/{assignment_id}/output/{attempt_id}/{repository_name}"


def _create_digest(payload: dict[str, Any]) -> str:
    """Hash the canonical JSON of the client-supplied dispatch fields.

    Sorted keys and no whitespace, so semantically identical payloads hash
    identically regardless of key order.

    :param payload: The dispatch fields (never the id or idempotency key).
    :returns: The sha256 hex digest stored as ``request_digest``.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _input_to_response(entry: AssignmentInputEntry) -> dict[str, Any]:
    """Convert an input entry to a response dict.

    :param entry: The entry to convert.
    :returns: Dict with the entry fields, including the derived input ref.
    """
    return {
        "repository_name": entry.repository_name,
        "repository_revision": entry.repository_revision,
        "remote_url": entry.remote_url,
        "input_commit": entry.input_commit,
        "input_ref": entry.input_ref,
        "context_manifest_path": entry.context_manifest_path,
        "manifest_digest": entry.manifest_digest,
        "artifact_paths": list(entry.artifact_paths),
        "is_execution_root": entry.is_execution_root,
        "observed_commit": entry.observed_commit,
    }


def _output_to_response(entry: AssignmentOutputEntry) -> dict[str, Any]:
    """Convert an output entry to a response dict.

    :param entry: The entry to convert.
    :returns: Dict with the entry fields, including the derived output ref.
    """
    return {
        "repository_name": entry.repository_name,
        "commit": entry.commit,
        "ref": entry.ref,
        "artifact_paths": list(entry.artifact_paths),
    }


def _assignment_to_response(assignment: Assignment) -> dict[str, Any]:
    """Convert an assignment entity to a response dict.

    :param assignment: The entity to convert.
    :returns: Dict with the assignment fields.
    """
    return {
        "id": assignment.id,
        "project_id": assignment.project_id,
        "source_session_id": assignment.source_session_id,
        "target_agent_id": assignment.target_agent_id,
        "task": assignment.task,
        "inputs": [_input_to_response(entry) for entry in assignment.inputs],
        "idempotency_key": assignment.idempotency_key,
        "owner_user_id": assignment.owner_user_id,
        "requested_host_id": assignment.requested_host_id,
        "resolved_host_id": assignment.resolved_host_id,
        "binding_name": assignment.binding_name,
        "resolved_binding_id": assignment.resolved_binding_id,
        "resolved_binding_revision": assignment.resolved_binding_revision,
        "project_revision": assignment.project_revision,
        "metadata": assignment.metadata,
        "model_override": assignment.model_override,
        "harness_override": assignment.harness_override,
        "start_deadline": assignment.start_deadline,
        "state": assignment.state,
        "wait_reason": assignment.wait_reason,
        "next_check_at": assignment.next_check_at,
        "active_attempt_id": assignment.active_attempt_id,
        "outputs": (
            [_output_to_response(entry) for entry in assignment.outputs]
            if assignment.outputs is not None
            else None
        ),
        "result_summary": assignment.result_summary,
        "error_code": assignment.error_code,
        "cancel_requested_at": assignment.cancel_requested_at,
        "created_at": assignment.created_at,
        "updated_at": assignment.updated_at,
    }


def _message_to_response(message: AssignmentMessage) -> dict[str, Any]:
    """Convert a message entity to a response dict.

    :param message: The entity to convert.
    :returns: Dict with the message fields.
    """
    return {
        "id": message.id,
        "assignment_id": message.assignment_id,
        "kind": message.kind,
        "body": message.body,
        "sender_session_id": message.sender_session_id,
        "created_at": message.created_at,
    }


async def _require_owned_assignment(
    assignment_store: AssignmentStore,
    assignment_id: str,
    owner: str | None,
) -> Assignment:
    """Return the caller's assignment, or 404 when absent / not owned.

    Another owner's row 404s (not 403) so assignments are not enumerable
    across users.

    :param assignment_store: The store backing assignment persistence.
    :param assignment_id: The assignment to fetch.
    :param owner: The requesting owner.
    :returns: The assignment entity.
    :raises OmnigentError: ``NOT_FOUND`` when not found / not owned.
    """
    assignment = await asyncio.to_thread(assignment_store.get, assignment_id)
    if assignment is None or assignment.owner_user_id != owner:
        raise OmnigentError("Assignment not found", code=ErrorCode.NOT_FOUND)
    return assignment


async def _read_active_attempt(
    assignment_store: AssignmentStore,
    assignment: Assignment,
) -> AssignmentAttempt | None:
    """Return the assignment's active attempt row, or ``None``.

    A plain read: ended attempts must stay visible so cancellation and
    retry can confirm the old execution stopped.

    :param assignment_store: The store backing assignment persistence.
    :param assignment: The assignment whose attempt to read.
    :returns: The attempt, or ``None`` when none is recorded.
    """
    if assignment.active_attempt_id is None:
        return None
    return await asyncio.to_thread(
        assignment_store.get_attempt, assignment.id, assignment.active_attempt_id
    )


def _require_attempt_binding(
    assignment: Assignment,
    session_id: str,
    attempt: AssignmentAttempt | None,
    conv: Conversation,
) -> AssignmentAttempt:
    """Require the calling session to be the active attempt's session.

    :param assignment: The assignment being reported on.
    :param session_id: The calling session.
    :param attempt: The active attempt row, or ``None``.
    :param conv: The calling conversation, for the runner check.
    :returns: The bound attempt.
    :raises InactiveAttemptError: When the caller is not the active
        attempt's session (maps to 409; a late former runner never
        overwrites accepted output).
    """
    if (
        attempt is None
        or assignment.active_attempt_id != attempt.id
        or attempt.state != "active"
        or attempt.session_id != session_id
        or attempt.runner_id is None
        or attempt.runner_id != conv.runner_id
    ):
        raise InactiveAttemptError(assignment.id, session_id)
    return attempt


async def _record_state_message(
    assignment_store: AssignmentStore,
    assignment_id: str,
    from_state: str,
    to_state: str,
    reason: str | None = None,
) -> None:
    """Append the ``"<from> -> <to>"`` state message after a transition.

    :param assignment_store: The store backing assignment persistence.
    :param assignment_id: The transitioned assignment.
    :param from_state: The state the row left.
    :param to_state: The state the row entered.
    :param reason: Optional reason appended in parentheses.
    """
    body = f"{from_state} -> {to_state}"
    if reason:
        body += f" ({reason})"
    await asyncio.to_thread(
        assignment_store.append_message,
        AssignmentMessage(
            id=uuid.uuid4().hex,
            assignment_id=assignment_id,
            kind="state",
            body=body,
            sender_session_id=None,
            idempotency_key=None,
        ),
    )


async def _raise_lost_race(
    assignment_store: AssignmentStore, assignment_id: str, action: str
) -> NoReturn:
    """Raise a 409 naming the current state after a lost conditional write.

    :param assignment_store: The store backing assignment persistence.
    :param assignment_id: The assignment whose write lost the race.
    :param action: The attempted action, for the error message.
    :raises OmnigentError: Always; ``CONFLICT`` with the current state.
    """
    current = await asyncio.to_thread(assignment_store.get, assignment_id)
    state = current.state if current is not None else "missing"
    raise OmnigentError(
        f"assignment {assignment_id} changed while {action} (now {state}); retry",
        code=ErrorCode.CONFLICT,
    )


async def _transition_or_raise(
    assignment_store: AssignmentStore,
    assignment_id: str,
    *,
    from_state: str,
    to_state: str,
    action: str,
    expected_active_attempt_id: Any = _UNSET,
    **fields: Any,
) -> Assignment:
    """Apply a conditional transition, mapping store outcomes to 409s.

    An illegal pair and a lost race are both 409 with the current state
    in the message.

    :param assignment_store: The store backing assignment persistence.
    :param assignment_id: The assignment to move.
    :param from_state: The state the caller last saw.
    :param to_state: The desired next state.
    :param action: The attempted action, for race messages.
    :param expected_active_attempt_id: Pinned attempt the caller validated.
    :param fields: Additional mutable columns to set atomically.
    :returns: The updated assignment.
    :raises OmnigentError: ``CONFLICT`` on an illegal pair or lost race.
    """
    try:
        updated = await asyncio.to_thread(
            assignment_store.transition,
            assignment_id,
            from_state=from_state,
            to_state=to_state,
            expected_active_attempt_id=expected_active_attempt_id,
            **fields,
        )
    except IllegalAssignmentTransitionError as exc:
        raise OmnigentError(exc.message, code=ErrorCode.CONFLICT) from exc
    if updated is None:
        await _raise_lost_race(assignment_store, assignment_id, action)
    return updated


def create_assignments_router(
    assignment_store: AssignmentStore,
    project_store: ProjectStore,
    repository_store: ProjectRepositoryStore,
    *,
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    permission_store: PermissionStore | None = None,
    auth_provider: AuthProvider | None = None,
    host_store: Any | None = None,
    feature_flags: FeatureFlags | None = None,
) -> APIRouter:
    """Build the assignments router (under ``/v1/assignments``).

    Only creation and refresh are flag-gated (route-level, so a disabled
    flag 404s before body validation 422s); every other route stays
    served so in-flight work can finish after the switch goes off.

    :param assignment_store: The store backing assignment persistence.
    :param project_store: The store backing project persistence.
    :param repository_store: The store backing registered repositories.
    :param conversation_store: The store backing session lookups.
    :param agent_store: The store backing agent validation.
    :param permission_store: Session permission store, or ``None`` to
        skip session-access checks (auth disabled).
    :param auth_provider: Auth provider used to identify the requesting
        user. ``None`` in single-user mode (owner scope is ``None``).
    :param host_store: Persistent host registrations for the
        ``requested_host_id`` ownership check; ``None`` skips it.
    :param feature_flags: Immutable deployment release-feature snapshot.
        When omitted, resolves ``OMNIGENT_FEATURES`` at router construction.
    :returns: A configured :class:`APIRouter`.
    """
    flags = feature_flags or resolve_feature_flags()

    def _require_flag() -> None:
        # A disabled route is indistinguishable from a non-existent one.
        # Route-level (not router-level): only create and refresh are
        # gated; the rest of the surface must keep serving in-flight work.
        if not flags.enabled(Feature.PROJECT_ASSIGNMENTS):
            raise HTTPException(status_code=404, detail="not found")

    router = APIRouter()

    async def _require_runner_caller(request: Request, session_id: str) -> Conversation:
        """Require the runner-bound caller of ``complete`` / ``finish``.

        The caller needs owner access on the session and a runner tunnel
        token bound to that conversation's runner. Any failure is a 403;
        a caller that passes auth but is not the active attempt's session
        is a 409 via :func:`_require_attempt_binding` instead.

        :param request: The incoming request, carrying the tunnel token.
        :param session_id: The calling session.
        :returns: The calling conversation.
        :raises OmnigentError: ``FORBIDDEN`` on any auth failure.
        """
        user_id = require_user(request, auth_provider)
        conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if conv is None:
            raise OmnigentError(
                f"session {session_id!r} not found",
                code=ErrorCode.FORBIDDEN,
            )
        try:
            await require_access(
                user_id, session_id, LEVEL_OWNER, permission_store, conversation_store
            )
        except OmnigentError as exc:
            if exc.code == ErrorCode.NOT_FOUND:
                raise OmnigentError(exc.message, code=ErrorCode.FORBIDDEN) from exc
            raise
        token = (request.headers.get(RUNNER_TUNNEL_TOKEN_HEADER) or "").strip()
        if not token or not conv.runner_id or token_bound_runner_id(token) != conv.runner_id:
            raise OmnigentError(
                "runner tunnel token is missing or not bound to this session's runner",
                code=ErrorCode.FORBIDDEN,
            )
        return conv

    @router.post("/assignments", status_code=201, dependencies=[Depends(_require_flag)])
    async def create_assignment(
        request: Request, response: Response, body: AssignmentCreateRequest
    ) -> dict[str, Any]:
        """Create an assignment in ``preparing`` with a caller-supplied id.

        The session's project is the assignment's project; input refs are
        derived server-side and returned so the caller pushes to exactly
        those. Idempotent: the same id (or ``source_session_id`` +
        ``idempotency_key``) with the same digest returns the stored row.

        :param request: The incoming request, used to identify the user.
        :param response: The outgoing response, used to select 200 vs 201.
        :param body: The dispatch fields.
        :returns: The inserted (201) or already-stored (200) assignment.
        :raises HTTPException: 404 when the feature is disabled.
        :raises OmnigentError: 401 if unauthenticated, 403/404 per session
            access, 400 on a bad id, unfiled session, unknown repository,
            bad commit or missing execution root, 409 when the project
            switch is off or the payload changed under a reused key.
        """
        owner = require_user(request, auth_provider)
        if not _ID_RE.fullmatch(body.id):
            raise OmnigentError(
                f"invalid assignment id {body.id!r}: must be 32 lowercase hex",
                code=ErrorCode.INVALID_INPUT,
            )
        await require_access(
            owner,
            body.source_session_id,
            LEVEL_OWNER,
            permission_store,
            conversation_store,
        )
        conv = await asyncio.to_thread(conversation_store.get_conversation, body.source_session_id)
        if conv is None:
            raise OmnigentError(
                f"session {body.source_session_id!r} not found",
                code=ErrorCode.NOT_FOUND,
            )
        if conv.project_id is None:
            raise OmnigentError(
                f"session {body.source_session_id!r} is not filed in a project; "
                "file it first, then dispatch",
                code=ErrorCode.INVALID_INPUT,
            )
        project = await asyncio.to_thread(project_store.get, conv.project_id, user_id=owner)
        if project is None:
            raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
        if not project.collaboration_enabled:
            raise OmnigentError(
                f"project {project.name!r} has collaboration disabled; "
                "enable it before dispatching assignments",
                code=ErrorCode.CONFLICT,
            )
        if body.requested_host_id is not None and host_store is not None:
            # Ownership only: an assignment may target an offline host or
            # one on another replica (it waits), so no liveness check.
            await asyncio.to_thread(
                resolve_host_owner,
                user_id=owner,
                host_id=body.requested_host_id,
                host_store=host_store,
            )
        try:
            await validate_session_agent(
                user_id=owner,
                agent_id=body.target_agent_id,
                agent_store=agent_store,
                permission_store=permission_store,
                conversation_store=conversation_store,
            )
        except OmnigentError as exc:
            if exc.code == ErrorCode.NOT_FOUND and "Agent not found" in exc.message:
                raise OmnigentError(
                    f"unknown target agent {body.target_agent_id!r}",
                    code=ErrorCode.INVALID_INPUT,
                ) from exc
            raise
        names = [repo.repository_name for repo in body.repositories]
        for name in names:
            _validate_ref_name(name, kind="repository")
        if len(set(names)) != len(names):
            raise OmnigentError(
                "repositories must not repeat a repository_name",
                code=ErrorCode.INVALID_INPUT,
            )
        registered: dict[str, Any] = {}
        for name in dict.fromkeys(names):
            repo = await asyncio.to_thread(
                repository_store.get_by_name, project_id=project.id, name=name
            )
            if repo is None:
                raise OmnigentError(
                    f"unknown repository {name!r} on project {project.name!r}",
                    code=ErrorCode.INVALID_INPUT,
                )
            registered[name] = repo
        for repo in body.repositories:
            if not _COMMIT_RE.fullmatch(repo.commit):
                raise OmnigentError(
                    f"invalid commit {repo.commit!r} for repository "
                    f"{repo.repository_name!r}: must be 40 or 64 lowercase hex",
                    code=ErrorCode.INVALID_INPUT,
                )
        if len(body.repositories) > 1 and not body.execution_root:
            raise OmnigentError(
                "execution_root is required when dispatching more than one repository",
                code=ErrorCode.INVALID_INPUT,
            )
        execution_root = body.execution_root or names[0]
        if execution_root not in set(names):
            raise OmnigentError(
                f"unknown execution_root {execution_root!r}: "
                "must name one of the dispatched repositories",
                code=ErrorCode.INVALID_INPUT,
            )
        sent_repositories = [repo.model_dump() for repo in body.repositories]
        digest = _create_digest(
            {
                "source_session_id": body.source_session_id,
                "target_agent_id": body.target_agent_id,
                "requested_host_id": body.requested_host_id,
                "binding_name": body.binding_name,
                "task": body.task,
                "metadata": body.metadata,
                "repositories": sent_repositories,
                "execution_root": body.execution_root,
                "model_override": body.model_override,
                "harness_override": body.harness_override,
                "start_deadline": body.start_deadline,
            }
        )
        pre_existing = await asyncio.to_thread(assignment_store.get, body.id)
        if pre_existing is not None:
            if pre_existing.owner_user_id != owner:
                raise OmnigentError("Assignment not found", code=ErrorCode.NOT_FOUND)
            if pre_existing.request_digest != digest:
                raise AssignmentIdempotencyConflictError(pre_existing.id)
            response.status_code = 200
            return _assignment_to_response(pre_existing)
        inputs = [
            AssignmentInputEntry(
                repository_name=repo.repository_name,
                repository_revision=registered[repo.repository_name].revision,
                remote_url=registered[repo.repository_name].remote_url,
                input_commit=repo.commit,
                input_ref=_input_ref(body.id, repo.repository_name),
                context_manifest_path=(
                    repo.context_manifest_path
                    or registered[repo.repository_name].context_manifest_path
                ),
                manifest_digest=repo.manifest_digest,
                artifact_paths=list(repo.artifact_paths),
                is_execution_root=repo.repository_name == execution_root,
            )
            for repo in body.repositories
        ]
        created = await asyncio.to_thread(
            assignment_store.create,
            Assignment(
                id=body.id,
                project_id=project.id,
                source_session_id=body.source_session_id,
                target_agent_id=body.target_agent_id,
                task=body.task,
                inputs=inputs,
                idempotency_key=body.idempotency_key,
                request_digest=digest,
                owner_user_id=owner,
                requested_host_id=body.requested_host_id,
                binding_name=body.binding_name,
                project_revision=project.collaboration_revision,
                metadata=body.metadata,
                model_override=body.model_override,
                harness_override=body.harness_override,
                start_deadline=body.start_deadline,
            ),
        )
        if created.owner_user_id != owner:
            # Won by a concurrent create under another owner with the same
            # id: their row stays invisible to this caller.
            raise OmnigentError("Assignment not found", code=ErrorCode.NOT_FOUND)
        if created.id != body.id:
            # Matched by (source_session_id, idempotency_key) under another
            # id: the identical row already existed.
            response.status_code = 200
        return _assignment_to_response(created)

    @router.post("/assignments/{assignment_id}/published")
    async def mark_published(
        request: Request, assignment_id: str, body: PublishedRefsRequest
    ) -> dict[str, Any]:
        """Confirm input publication: ``preparing`` to ``waiting``/``failed``.

        Every input present at its advertised commit moves the row to
        ``waiting``; anything else fails it with
        ``error_code="publication_failed"`` and the landed refs recorded
        on the matching inputs entries. A repeat carrying exactly what was
        recorded returns the current row; any other call on a
        non-preparing row is a 409.

        :param request: The incoming request, used to identify the user.
        :param assignment_id: The assignment being published.
        :param body: The pushed input refs with observed commits.
        :returns: The updated (or already-recorded) assignment.
        :raises OmnigentError: 401 if unauthenticated, 403/404 per source
            session access and assignment ownership, 400 on a bad ref,
            409 on a non-preparing row.
        """
        owner = require_user(request, auth_provider)
        assignment = await _require_owned_assignment(assignment_store, assignment_id, owner)
        await require_access(
            owner,
            assignment.source_session_id,
            LEVEL_OWNER,
            permission_store,
            conversation_store,
        )
        for ref in body.refs:
            _validate_ref_name(ref.repository_name, kind="repository")
            if not _COMMIT_RE.fullmatch(ref.commit):
                raise OmnigentError(
                    f"invalid commit {ref.commit!r} for repository "
                    f"{ref.repository_name!r}: must be 40 or 64 lowercase hex",
                    code=ErrorCode.INVALID_INPUT,
                )
        observed: dict[str, str] = {}
        conflicted = False
        for ref in body.refs:
            if ref.repository_name in observed and observed[ref.repository_name] != ref.commit:
                conflicted = True
            observed[ref.repository_name] = ref.commit
        advertised = {entry.repository_name: entry.input_commit for entry in assignment.inputs}
        matches = (
            not conflicted
            and set(observed) == set(advertised)
            and all(observed[name] == commit for name, commit in advertised.items())
        )
        if assignment.state != AssignmentState.PREPARING.value:
            if (
                assignment.state == AssignmentState.FAILED.value
                and assignment.error_code == "publication_failed"
                and assignment.active_attempt_id is None
            ):
                # Only an input-publication failure leaves no attempt behind.
                recorded = {
                    entry.repository_name: entry.observed_commit for entry in assignment.inputs
                }
                identical = (
                    not conflicted
                    and set(observed) <= set(recorded)
                    and all(observed.get(name) == commit for name, commit in recorded.items())
                )
            else:
                identical = matches
            if identical:
                return _assignment_to_response(assignment)
            raise OmnigentError(
                f"assignment {assignment_id} is {assignment.state}; "
                "publication was already recorded",
                code=ErrorCode.CONFLICT,
            )
        now = now_epoch()
        if matches:
            updated = await _transition_or_raise(
                assignment_store,
                assignment_id,
                from_state=AssignmentState.PREPARING.value,
                to_state=AssignmentState.WAITING.value,
                action="recording publication",
                next_check_at=now,
            )
            await _record_state_message(
                assignment_store,
                assignment_id,
                AssignmentState.PREPARING.value,
                AssignmentState.WAITING.value,
            )
            coordinator = getattr(request.app.state, "assignment_coordinator", None)
            if coordinator is not None:
                coordinator.trigger(assignment_id)
            return _assignment_to_response(updated)
        landed = {name: commit for name, commit in observed.items() if name in advertised}
        updated = await _transition_or_raise(
            assignment_store,
            assignment_id,
            from_state=AssignmentState.PREPARING.value,
            to_state=AssignmentState.FAILED.value,
            action="recording publication",
            error_code="publication_failed",
            inputs=[
                dataclasses.replace(entry, observed_commit=landed.get(entry.repository_name))
                for entry in assignment.inputs
            ],
        )
        await _record_state_message(
            assignment_store,
            assignment_id,
            AssignmentState.PREPARING.value,
            AssignmentState.FAILED.value,
            reason="publication_failed",
        )
        return _assignment_to_response(updated)

    @router.post("/assignments/{assignment_id}/refresh", dependencies=[Depends(_require_flag)])
    async def refresh_assignment(request: Request, assignment_id: str) -> dict[str, Any]:
        """Re-pin a ``waiting`` assignment against the current configuration.

        Re-reads each input's registered ``repository_revision`` and the
        current ``collaboration_revision``, clears ``wait_reason`` and the
        claim-time binding snapshot; digests are untouched. Rejected on
        anything but ``waiting``.

        :param request: The incoming request, used to identify the user.
        :param assignment_id: The assignment to re-pin.
        :returns: The re-pinned assignment.
        :raises HTTPException: 404 when the feature is disabled.
        :raises OmnigentError: 401 if unauthenticated, 404 unless owned,
            409 when the project switch is off or the row is not waiting.
        """
        owner = require_user(request, auth_provider)
        assignment = await _require_owned_assignment(assignment_store, assignment_id, owner)
        project = await asyncio.to_thread(project_store.get, assignment.project_id, user_id=owner)
        if project is None:
            raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
        if not project.collaboration_enabled:
            raise OmnigentError(
                f"project {project.name!r} has collaboration disabled; "
                "enable it before refreshing assignments",
                code=ErrorCode.CONFLICT,
            )
        if assignment.state != AssignmentState.WAITING.value:
            raise OmnigentError(
                f"assignment {assignment_id} is {assignment.state}; "
                "only waiting assignments can be refreshed",
                code=ErrorCode.CONFLICT,
            )
        repositories = await asyncio.to_thread(
            repository_store.list_by_project, assignment.project_id
        )
        revisions = {repo.name: repo.revision for repo in repositories}
        now = now_epoch()
        # Pin the blob this read saw: the store writes inputs with the
        # same encoder, so a concurrent refresh changes the comparison.
        expected_inputs_json = inputs_to_json(assignment.inputs)
        updated = await asyncio.to_thread(
            assignment_store.refresh_waiting,
            assignment_id,
            inputs=[
                dataclasses.replace(
                    entry,
                    repository_revision=revisions.get(
                        entry.repository_name, entry.repository_revision
                    ),
                )
                for entry in assignment.inputs
            ],
            project_revision=project.collaboration_revision,
            now=now,
            expected_inputs_json=expected_inputs_json,
            expected_project_revision=assignment.project_revision,
        )
        if updated is None:
            await _raise_lost_race(assignment_store, assignment_id, "refreshing")
        coordinator = getattr(request.app.state, "assignment_coordinator", None)
        if coordinator is not None:
            coordinator.trigger(assignment_id)
        return _assignment_to_response(updated)

    @router.get("/assignments")
    async def list_assignments(
        request: Request,
        project_id: str | None = None,
        state: str | None = None,
        role: str | None = None,
        source_session_id: str | None = None,
        session_id: str | None = None,
        after: str | None = None,
        limit: int = Query(default=20, ge=1, le=100),
    ) -> dict[str, Any]:
        """List the caller's assignments, filtered and cursor-paginated.

        Always scoped to rows the caller owns. ``role=sent`` needs an
        owned ``source_session_id``; ``role=received`` needs an owned
        ``session_id`` (mapped to the attempt's session).

        :param request: The incoming request, used to identify the user.
        :param project_id: Return only this project's assignments.
        :param state: Return only assignments in this state.
        :param role: ``sent`` or ``received``.
        :param source_session_id: Dispatching session (``sent`` filter).
        :param session_id: Attempt session (``received`` filter).
        :param after: Cursor id — return assignments after this one.
        :param limit: Maximum assignments in the page (at most 100).
        :returns: ``{data, first_id, last_id, has_more}``.
        :raises OmnigentError: 401 if unauthenticated, 400 on a bad
            state, role or missing role session, 403/404 per session access.
        """
        owner = require_user(request, auth_provider)
        if state is not None and state not in _VALID_STATES:
            raise OmnigentError(
                f"unknown assignment state {state!r}",
                code=ErrorCode.INVALID_INPUT,
            )
        if role is not None and role not in ("sent", "received"):
            raise OmnigentError(
                f"unknown role {role!r}: expected 'sent' or 'received'",
                code=ErrorCode.INVALID_INPUT,
            )
        attempt_session_id: str | None = None
        if role == "sent":
            if not source_session_id:
                raise OmnigentError(
                    "source_session_id is required with role='sent'",
                    code=ErrorCode.INVALID_INPUT,
                )
            await require_access(
                owner,
                source_session_id,
                LEVEL_OWNER,
                permission_store,
                conversation_store,
            )
        elif role == "received":
            if not session_id:
                raise OmnigentError(
                    "session_id is required with role='received'",
                    code=ErrorCode.INVALID_INPUT,
                )
            await require_access(
                owner, session_id, LEVEL_OWNER, permission_store, conversation_store
            )
            attempt_session_id = session_id
        page = await asyncio.to_thread(
            assignment_store.list,
            owner_user_id=owner,
            project_id=project_id,
            state=state,
            source_session_id=source_session_id,
            attempt_session_id=attempt_session_id,
            limit=limit,
            after=after,
        )
        return {
            "data": [_assignment_to_response(item) for item in page.data],
            "first_id": page.first_id,
            "last_id": page.last_id,
            "has_more": page.has_more,
        }

    @router.get("/assignments/{assignment_id}")
    async def get_assignment(request: Request, assignment_id: str) -> dict[str, Any]:
        """Return one assignment the caller owns.

        :param request: The incoming request, used to identify the user.
        :param assignment_id: The assignment to read.
        :returns: The assignment.
        :raises OmnigentError: 401 if unauthenticated, 404 unless owned.
        """
        owner = require_user(request, auth_provider)
        assignment = await _require_owned_assignment(assignment_store, assignment_id, owner)
        return _assignment_to_response(assignment)

    @router.post("/assignments/{assignment_id}/messages", status_code=201)
    async def post_assignment_message(
        request: Request, assignment_id: str, body: AssignmentMessageRequest
    ) -> dict[str, Any]:
        """Append a note to an assignment, idempotently on the key.

        :param request: The incoming request, used to identify the user.
        :param assignment_id: The assignment to message.
        :param body: The sender session, text and idempotency key.
        :returns: The appended (or already-stored) message.
        :raises OmnigentError: 401 if unauthenticated, 404 unless the
            assignment is owned, 403/404 per sender-session access.
        """
        owner = require_user(request, auth_provider)
        await _require_owned_assignment(assignment_store, assignment_id, owner)
        await require_access(
            owner,
            body.sender_session_id,
            LEVEL_OWNER,
            permission_store,
            conversation_store,
        )
        stored = await asyncio.to_thread(
            assignment_store.append_message,
            AssignmentMessage(
                id=uuid.uuid4().hex,
                assignment_id=assignment_id,
                kind="note",
                body=body.body,
                sender_session_id=body.sender_session_id,
                idempotency_key=body.idempotency_key,
            ),
        )
        return _message_to_response(stored)

    @router.get("/assignments/{assignment_id}/messages")
    async def read_assignment_messages(
        request: Request,
        assignment_id: str,
        after: str | None = None,
        limit: int = Query(default=20, ge=1, le=100),
    ) -> dict[str, Any]:
        """Cursor-read an assignment's messages; repeatable, never consuming.

        :param request: The incoming request, used to identify the user.
        :param assignment_id: The assignment whose messages to read.
        :param after: Cursor id — return messages after this one.
        :param limit: Maximum messages in the page (at most 100).
        :returns: ``{data, first_id, last_id, has_more}``.
        :raises OmnigentError: 401 if unauthenticated, 404 unless owned.
        """
        owner = require_user(request, auth_provider)
        await _require_owned_assignment(assignment_store, assignment_id, owner)
        page = await asyncio.to_thread(
            assignment_store.read_messages, assignment_id, after=after, limit=limit
        )
        return {
            "data": [_message_to_response(item) for item in page.data],
            "first_id": page.first_id,
            "last_id": page.last_id,
            "has_more": page.has_more,
        }

    @router.post("/assignments/{assignment_id}/cancel")
    async def cancel_assignment(
        request: Request, assignment_id: str, body: CancelRequest
    ) -> dict[str, Any]:
        """Request cancellation of an assignment.

        ``preparing``/``waiting`` cancel at once; ``starting``/``running``/
        ``publishing`` become ``stopping`` (the coordinator performs the
        stop); ``stopping`` returns the row; ``interrupted`` cancels only
        when the old execution is confirmed stopped; terminal rows 409.

        :param request: The incoming request, used to identify the user.
        :param assignment_id: The assignment to cancel.
        :param body: Optional reason, recorded on the state message.
        :returns: The updated (or already-stopping) assignment.
        :raises OmnigentError: 401 if unauthenticated, 404 unless owned,
            409 when the row cannot cancel from its state.
        """
        owner = require_user(request, auth_provider)
        assignment = await _require_owned_assignment(assignment_store, assignment_id, owner)
        now = now_epoch()
        from_state = assignment.state
        expected_active_attempt_id: Any = _UNSET
        if from_state in (
            AssignmentState.PREPARING.value,
            AssignmentState.WAITING.value,
        ):
            to_state = AssignmentState.CANCELLED.value
            fields: dict[str, Any] = {}
            if assignment.resolved_host_id is not None:
                fields["next_check_at"] = now
        elif from_state in (
            AssignmentState.STARTING.value,
            AssignmentState.RUNNING.value,
            AssignmentState.PUBLISHING.value,
        ):
            to_state = AssignmentState.STOPPING.value
            fields = {"cancel_requested_at": now, "next_check_at": now}
            expected_active_attempt_id = assignment.active_attempt_id
        elif from_state == AssignmentState.STOPPING.value:
            return _assignment_to_response(assignment)
        elif from_state == AssignmentState.INTERRUPTED.value:
            attempt = await _read_active_attempt(assignment_store, assignment)
            if attempt is None:
                raise OmnigentError(
                    f"assignment {assignment_id} is interrupted with no attempt record; "
                    "no attempt record confirms the old execution stopped",
                    code=ErrorCode.CONFLICT,
                )
            if attempt.state == "active" or attempt.ended_at is None:
                raise OmnigentError(
                    f"assignment {assignment_id} is interrupted but attempt "
                    f"{attempt.id} has not stopped (state={attempt.state}); "
                    "retry only after the old execution is confirmed stopped",
                    code=ErrorCode.CONFLICT,
                )
            to_state = AssignmentState.CANCELLED.value
            fields = {}
            if assignment.resolved_host_id is not None:
                fields["next_check_at"] = now
            expected_active_attempt_id = attempt.id
        else:
            raise OmnigentError(
                f"assignment {assignment_id} is {from_state}; "
                "terminal assignments cannot be cancelled",
                code=ErrorCode.CONFLICT,
            )
        updated = await _transition_or_raise(
            assignment_store,
            assignment_id,
            from_state=from_state,
            to_state=to_state,
            action="cancelling",
            expected_active_attempt_id=expected_active_attempt_id,
            **fields,
        )
        await _record_state_message(
            assignment_store, assignment_id, from_state, to_state, reason=body.reason
        )
        coordinator = getattr(request.app.state, "assignment_coordinator", None)
        if coordinator is not None:
            if to_state == AssignmentState.STOPPING.value or (
                to_state == AssignmentState.CANCELLED.value
                and updated.resolved_host_id is not None
            ):
                coordinator.trigger(assignment_id)
        return _assignment_to_response(updated)

    @router.post("/assignments/{assignment_id}/retry")
    async def retry_assignment(request: Request, assignment_id: str) -> dict[str, Any]:
        """Resume an ``interrupted`` assignment after a confirmed stop.

        Allowed only when the active attempt is not ``active`` and has
        ``ended_at`` set. A passed ``start_deadline`` expires the row;
        otherwise it returns to ``waiting`` with no active attempt.

        :param request: The incoming request, used to identify the user.
        :param assignment_id: The assignment to resume.
        :returns: The updated assignment.
        :raises OmnigentError: 401 if unauthenticated, 404 unless owned,
            409 when the row is not interrupted or the old execution has
            not stopped.
        """
        owner = require_user(request, auth_provider)
        assignment = await _require_owned_assignment(assignment_store, assignment_id, owner)
        if assignment.state != AssignmentState.INTERRUPTED.value:
            raise OmnigentError(
                f"assignment {assignment_id} is {assignment.state}; "
                "only interrupted assignments can be retried",
                code=ErrorCode.CONFLICT,
            )
        attempt = await _read_active_attempt(assignment_store, assignment)
        if attempt is None:
            raise OmnigentError(
                f"assignment {assignment_id} is interrupted with no attempt record; "
                "no attempt record confirms the old execution stopped",
                code=ErrorCode.CONFLICT,
            )
        if attempt.state == "active" or attempt.ended_at is None:
            raise OmnigentError(
                f"assignment {assignment_id} is interrupted but attempt "
                f"{attempt.id} has not stopped (state={attempt.state}); "
                "retry only after the old execution is confirmed stopped",
                code=ErrorCode.CONFLICT,
            )
        now = now_epoch()
        if assignment.start_deadline is not None and now >= assignment.start_deadline:
            to_state = AssignmentState.EXPIRED.value
            fields = {"next_check_at": now} if assignment.resolved_host_id is not None else {}
        else:
            to_state = AssignmentState.WAITING.value
            fields = {"active_attempt_id": None, "next_check_at": now}
        updated = await _transition_or_raise(
            assignment_store,
            assignment_id,
            from_state=AssignmentState.INTERRUPTED.value,
            to_state=to_state,
            action="retrying",
            expected_active_attempt_id=attempt.id,
            **fields,
        )
        await _record_state_message(
            assignment_store,
            assignment_id,
            AssignmentState.INTERRUPTED.value,
            to_state,
        )
        if to_state == AssignmentState.WAITING.value or updated.resolved_host_id is not None:
            coordinator = getattr(request.app.state, "assignment_coordinator", None)
            if coordinator is not None:
                coordinator.trigger(assignment_id)
        return _assignment_to_response(updated)

    @router.post("/assignments/{assignment_id}/complete")
    async def complete_assignment(
        request: Request, assignment_id: str, body: CompleteRequest
    ) -> dict[str, Any]:
        """Report work complete: ``running`` to ``publishing``.

        Runner-bound: the caller needs owner access on the calling
        session, which must be the active attempt's session on its bound
        runner. Each output must name a repository from the inputs, at
        most one per repository. An identical repeat while ``publishing``
        returns the row.

        :param request: The incoming request, carrying the tunnel token.
        :param assignment_id: The assignment being completed.
        :param body: The calling session, advertised outputs and summary.
        :returns: The updated assignment, whose outputs carry the derived
            output refs to push to.
        :raises OmnigentError: 401 if unauthenticated, 404 unless owned,
            403 on any runner-auth failure, 400 on a bad output, 409 when
            the row cannot complete from its state.
        """
        owner = require_user(request, auth_provider)
        assignment = await _require_owned_assignment(assignment_store, assignment_id, owner)
        conv = await _require_runner_caller(request, body.session_id)
        attempt = await _read_active_attempt(assignment_store, assignment)
        bound = _require_attempt_binding(assignment, body.session_id, attempt, conv)
        output_names = [output.repository_name for output in body.outputs]
        if len(set(output_names)) != len(output_names):
            raise OmnigentError(
                "outputs must hold at most one entry per repository",
                code=ErrorCode.INVALID_INPUT,
            )
        known = {entry.repository_name for entry in assignment.inputs}
        for output in body.outputs:
            if output.repository_name not in known:
                raise OmnigentError(
                    f"unknown repository {output.repository_name!r}: "
                    "outputs must name a repository from the assignment inputs",
                    code=ErrorCode.INVALID_INPUT,
                )
            _validate_ref_name(output.repository_name, kind="repository")
            if not _COMMIT_RE.fullmatch(output.commit):
                raise OmnigentError(
                    f"invalid commit {output.commit!r} for repository "
                    f"{output.repository_name!r}: must be 40 or 64 lowercase hex",
                    code=ErrorCode.INVALID_INPUT,
                )
        if assignment.state == AssignmentState.PUBLISHING.value:
            stored_by_name = {o.repository_name: o for o in assignment.outputs or []}
            identical = (
                body.summary == assignment.result_summary
                and set(stored_by_name) == {o.repository_name for o in body.outputs}
                and all(
                    stored_by_name[o.repository_name].commit == o.commit
                    and list(stored_by_name[o.repository_name].artifact_paths)
                    == list(o.artifact_paths)
                    for o in body.outputs
                )
            )
            if identical:
                return _assignment_to_response(assignment)
            raise OmnigentError(
                f"assignment {assignment_id} is already publishing; "
                "a different completion is rejected",
                code=ErrorCode.CONFLICT,
            )
        if assignment.state != AssignmentState.RUNNING.value:
            raise OmnigentError(
                f"assignment {assignment_id} is {assignment.state}; "
                "only running assignments can complete",
                code=ErrorCode.CONFLICT,
            )
        outputs = [
            AssignmentOutputEntry(
                repository_name=output.repository_name,
                commit=output.commit,
                ref=_output_ref(assignment_id, bound.id, output.repository_name),
                artifact_paths=list(output.artifact_paths),
            )
            for output in body.outputs
        ]
        updated = await _transition_or_raise(
            assignment_store,
            assignment_id,
            from_state=AssignmentState.RUNNING.value,
            to_state=AssignmentState.PUBLISHING.value,
            action="completing",
            expected_active_attempt_id=bound.id,
            outputs=outputs,
            result_summary=body.summary,
        )
        await _record_state_message(
            assignment_store,
            assignment_id,
            AssignmentState.RUNNING.value,
            AssignmentState.PUBLISHING.value,
        )
        return _assignment_to_response(updated)

    @router.post("/assignments/{assignment_id}/finish")
    async def finish_assignment(
        request: Request, assignment_id: str, body: FinishRequest
    ) -> dict[str, Any]:
        """Verify output publication: ``publishing`` to ``succeeded``/``failed``.

        Runner-bound like ``complete``. Every advertised output observed
        at its commit with no error succeeds the row — outputs and success
        commit in the same transition call; any mismatch or error fails it
        with ``error_code="publication_failed"``. The conditional write is
        what lets a concurrent interruption win cleanly (``None`` → 409);
        a late call from a non-active attempt 409s with the row untouched.

        :param request: The incoming request, carrying the tunnel token.
        :param assignment_id: The assignment being finished.
        :param body: The calling session, observed output refs and error.
        :returns: The updated assignment.
        :raises OmnigentError: 401 if unauthenticated, 404 unless owned,
            403 on any runner-auth failure, 409 when the row cannot finish
            from its state or the caller is not the active attempt.
        """
        owner = require_user(request, auth_provider)
        assignment = await _require_owned_assignment(assignment_store, assignment_id, owner)
        conv = await _require_runner_caller(request, body.session_id)
        if assignment.state != AssignmentState.PUBLISHING.value:
            raise OmnigentError(
                f"assignment {assignment_id} is {assignment.state}; "
                "only publishing assignments can finish",
                code=ErrorCode.CONFLICT,
            )
        attempt = await _read_active_attempt(assignment_store, assignment)
        bound = _require_attempt_binding(assignment, body.session_id, attempt, conv)
        for ref in body.refs:
            _validate_ref_name(ref.repository_name, kind="repository")
            if not _COMMIT_RE.fullmatch(ref.commit):
                raise OmnigentError(
                    f"invalid commit {ref.commit!r} for repository "
                    f"{ref.repository_name!r}: must be 40 or 64 lowercase hex",
                    code=ErrorCode.INVALID_INPUT,
                )
        advertised = (
            {entry.repository_name: entry.commit for entry in assignment.outputs}
            if assignment.outputs is not None
            else None
        )
        seen: dict[str, str] = {}
        conflicted = False
        for ref in body.refs:
            if ref.repository_name in seen and seen[ref.repository_name] != ref.commit:
                conflicted = True
            seen[ref.repository_name] = ref.commit
        matches = (
            advertised is not None
            and not conflicted
            and len(body.refs) == len(advertised)
            and set(seen) == set(advertised)
            and all(seen[name] == commit for name, commit in advertised.items())
        )
        now = now_epoch()
        release_fields: dict[str, Any] = (
            {"next_check_at": now} if assignment.resolved_host_id is not None else {}
        )
        if matches and not body.error:
            updated = await _transition_or_raise(
                assignment_store,
                assignment_id,
                from_state=AssignmentState.PUBLISHING.value,
                to_state=AssignmentState.SUCCEEDED.value,
                action="finishing",
                expected_active_attempt_id=bound.id,
                outputs=list(assignment.outputs or []),
                **release_fields,
            )
            await asyncio.to_thread(
                assignment_store.update_attempt,
                assignment_id,
                bound.id,
                state="finished",
                ended_at=now,
            )
            await _record_state_message(
                assignment_store,
                assignment_id,
                AssignmentState.PUBLISHING.value,
                AssignmentState.SUCCEEDED.value,
            )
            if updated.resolved_host_id is not None:
                coordinator = getattr(request.app.state, "assignment_coordinator", None)
                if coordinator is not None:
                    coordinator.trigger(assignment_id)
            return _assignment_to_response(updated)
        reason = (body.error or "output refs do not match the advertised outputs")[:500]
        updated = await _transition_or_raise(
            assignment_store,
            assignment_id,
            from_state=AssignmentState.PUBLISHING.value,
            to_state=AssignmentState.FAILED.value,
            action="finishing",
            expected_active_attempt_id=bound.id,
            error_code="publication_failed",
            **release_fields,
        )
        await asyncio.to_thread(
            assignment_store.update_attempt,
            assignment_id,
            bound.id,
            state="finished",
            ended_at=now,
            error_code="publication_failed",
        )
        await _record_state_message(
            assignment_store,
            assignment_id,
            AssignmentState.PUBLISHING.value,
            AssignmentState.FAILED.value,
            reason=f"publication_failed: {reason}",
        )
        if updated.resolved_host_id is not None:
            coordinator = getattr(request.app.state, "assignment_coordinator", None)
            if coordinator is not None:
                coordinator.trigger(assignment_id)
        return _assignment_to_response(updated)

    return router
