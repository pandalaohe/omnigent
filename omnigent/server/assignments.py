"""Assignment coordinator — scoped triggers, backoff, placement and teardown.

One durable assignment hands work to one ``(host, agent)`` destination.
This coordinator evaluates a single row at a time: a ``waiting`` row is
checked against the project switch, the destination, the binding and the
registered revisions, then claimed, prepared on the host and placed as a
runner session with one initial event. Active rows are watched for runner
liveness, orphaned placements are retired, stops are confirmed before an
attempt is ended, and terminal rows release their worktrees.

Triggers are scoped: one assignment id, one host's rows, or the bounded
due-work pass. The pass is indexed and row-capped, and every row carries
its own backoff so nothing retries hot.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from omnigent.entities import (
    Assignment,
    AssignmentAttempt,
    AssignmentMessage,
    Conversation,
    ProjectHostBinding,
    ProjectRepository,
)
from omnigent.entities.assignment import TERMINAL_STATES
from omnigent.host.frames import (
    HostAssignmentPrepareFrame,
    HostAssignmentPrepareRepository,
    HostAssignmentReleaseFrame,
    HostAssignmentReleaseRepository,
)
from omnigent.runner.routing import RunnerRouter
from omnigent.runner.transports.ws_tunnel.registry import TunnelRegistry

if TYPE_CHECKING:
    from omnigent.server.runner_session_init import RunnerSessionInitializer
from omnigent.server.assignment_host import (
    host_supports_assignments,
    prepare_assignment_on_host,
    release_assignment_on_host,
)
from omnigent.server.auth import LEVEL_OWNER, RESERVED_USER_LOCAL
from omnigent.server.host_registry import HostConnection, HostRegistry, RunnerExitReports
from omnigent.server.schemas import SessionEventInput
from omnigent.stores.artifact_store import ArtifactStore
from omnigent.stores.assignment_store import AssignmentStore, InactiveAttemptError
from omnigent.stores.conversation_store import ConversationAlreadyExistsError, ConversationStore
from omnigent.stores.file_store import FileStore
from omnigent.stores.host_store import Host, HostStore
from omnigent.stores.permission_store import PermissionStore
from omnigent.stores.project_host_binding_store import ProjectHostBindingStore
from omnigent.stores.project_repository_store import ProjectRepositoryStore
from omnigent.stores.project_store import ProjectStore
from omnigent.util.session_lifecycle import CLOSED_LABEL_KEY, CLOSED_LABEL_VALUE

_logger = logging.getLogger(__name__)

_BACKOFF_MIN_S = 15
_BACKOFF_MAX_S = 3600

_PLACEMENT_NEXT_CHECK_S = 420
_RUNNER_CONNECT_TIMEOUT_S = 30.0

_ACTIVE_CHECK_S = 30
_LEASE_S = 90
# Bounds how long a succeeded release waits for its final turn.
_TURN_END_GRACE_S = 600


def next_check_at(created_at: int, now: int) -> int:
    """Return when the coordinator may look at the row again.

    ``now + clamp((now - created_at) // 4, 15, 3600)`` — the archive-close
    coordinator's rule: back off from the row's own age so an unreachable
    destination is never retried hot.

    :param created_at: Unix epoch seconds the row was created.
    :param now: Unix epoch seconds the evaluation runs at.
    :returns: Unix epoch seconds of the next check.
    """
    delay = (now - created_at) // 4
    return now + max(_BACKOFF_MIN_S, min(_BACKOFF_MAX_S, delay))


def _truncate_reason(reason: str) -> str:
    """Clamp a wait reason to the ``wait_reason`` column width."""
    return reason[:256]


def _derived_session_id(attempt_id: str) -> str:
    """Derive an attempt's session id when none was stored.

    :param attempt_id: The attempt whose session to derive.
    :returns: The ``sha256("assignment-attempt:" + id)[:32]`` id.
    """
    return hashlib.sha256(f"assignment-attempt:{attempt_id}".encode()).hexdigest()[:32]


def _attempt_session_id(attempt: AssignmentAttempt) -> str:
    """Return the stored session id, or the derived one when unset.

    :param attempt: The attempt whose session to address.
    :returns: The conversation id of the attempt's session.
    """
    if attempt.session_id is not None:
        return attempt.session_id
    return _derived_session_id(attempt.id)


def _attempt_ended(attempt: AssignmentAttempt) -> bool:
    """Return whether an attempt no longer owns a live execution.

    :param attempt: The attempt to inspect.
    :returns: ``True`` when the attempt left ``active`` (or stamped an
        end); ending it again would raise, so callers skip the write.
    """
    return attempt.state != "active" or attempt.ended_at is not None


def _attempt_runner_id(attempt: AssignmentAttempt, conv: Conversation | None) -> str | None:
    """Return the runner bound to an attempt, or the session's runner.

    :param attempt: The attempt whose runner to resolve.
    :param conv: The attempt's session, or ``None`` when missing.
    :returns: ``attempt.runner_id``, else the session's ``runner_id``,
        else ``None`` when nothing was ever launched.
    """
    if attempt.runner_id is not None:
        return attempt.runner_id
    if conv is not None:
        return conv.runner_id
    return None


def _session_mid_turn(conv: Conversation | None) -> bool:
    if conv is None:
        return False
    from omnigent.server.routes._sessions.common import _session_status_cache
    from omnigent.server.routes._sessions.orchestration import _MID_TURN_STATUSES

    return _session_status_cache.get(conv.id, conv.live_status) in _MID_TURN_STATUSES


@dataclass(frozen=True)
class _ClaimableDestination:
    """A waiting row's evaluated destination; claimable when no reason blocks."""

    host_id: str
    conn: HostConnection
    binding: ProjectHostBinding
    sources: dict[str, str]


def build_initial_event_text(
    assignment: Assignment,
    attempt_id: str,
    directories: dict[str, str],
) -> str:
    """Build the one user message that starts an attempt.

    Pure so it is testable: ids, the verbatim task, the repository to
    prepared-directory map with the execution root marked, the manifest
    path to read first per repository, and the completion instruction.

    :param assignment: The assignment being placed.
    :param attempt_id: The claimed attempt id.
    :param directories: Repository name to prepared directory.
    :returns: The user-message text dispatched to the runner.
    """
    roots = {entry.repository_name for entry in assignment.inputs if entry.is_execution_root}
    lines = [
        f"Assignment {assignment.id} (attempt {attempt_id})",
        "",
        "Task:",
        assignment.task,
        "",
        "Repositories:",
    ]
    for entry in assignment.inputs:
        directory = directories.get(entry.repository_name, "")
        marker = " (execution root)" if entry.repository_name in roots else ""
        lines.append(f"- {entry.repository_name}: {directory}{marker}")
    lines += ["", "Read first:"]
    for entry in assignment.inputs:
        directory = directories.get(entry.repository_name, "")
        lines.append(f"- {directory}/{entry.context_manifest_path}")
    lines += [
        "",
        "Work only in the directories above, commit the work there, then call "
        f'sys_assignment_complete with assignment_id "{assignment.id}", `outputs` '
        "(repository_name and commit for each repository changed) and a `summary`.",
        "Calling sys_assignment_complete is the last work step: dispatch any onward "
        "assignment with sys_assignment_dispatch before it. The session is closed after "
        "this turn ends.",
    ]
    return "\n".join(lines)


class AssignmentCoordinator:
    """Evaluate assignment rows through scoped triggers and a due pass."""

    def __init__(
        self,
        *,
        assignment_store: AssignmentStore,
        project_store: ProjectStore,
        repository_store: ProjectRepositoryStore,
        binding_store: ProjectHostBindingStore,
        host_store: HostStore,
        host_registry: HostRegistry,
        conversation_store: ConversationStore,
        permission_store: PermissionStore | None,
        runner_router: RunnerRouter,
        tunnel_registry: TunnelRegistry,
        runner_exit_reports: RunnerExitReports,
        file_store: FileStore,
        artifact_store: ArtifactStore,
        scan_interval_seconds: float = 15.0,
        due_batch_limit: int = 50,
        runner_session_initializer: RunnerSessionInitializer | None = None,
    ) -> None:
        self._assignment_store = assignment_store
        self._project_store = project_store
        self._repository_store = repository_store
        self._binding_store = binding_store
        self._host_store = host_store
        self._host_registry = host_registry
        self._conversation_store = conversation_store
        self._permission_store = permission_store
        self._runner_router = runner_router
        self._tunnel_registry = tunnel_registry
        self._runner_exit_reports = runner_exit_reports
        self._file_store = file_store
        self._artifact_store = artifact_store
        self._scan_interval_seconds = scan_interval_seconds
        self._due_batch_limit = due_batch_limit
        self._runner_session_initializer = runner_session_initializer
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._host_tasks: dict[str, asyncio.Task[None]] = {}
        self._release_holds: dict[str, tuple[int, int | None]] = {}
        self._scan_task: asyncio.Task[None] | None = None

    def trigger(self, assignment_id: str) -> None:
        """Schedule one evaluation unless one is in flight for that id."""
        existing = self._tasks.get(assignment_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(
            self._run_one(assignment_id), name=f"assignment:{assignment_id}"
        )
        self._tasks[assignment_id] = task

        def _done(done: asyncio.Task[None]) -> None:
            if self._tasks.get(assignment_id) is done:
                self._tasks.pop(assignment_id, None)
            with contextlib.suppress(asyncio.CancelledError, Exception):
                done.result()

        task.add_done_callback(_done)

    def trigger_host(self, host_id: str) -> None:
        """Schedule a host-scoped scan of waiting rows claimable there."""
        existing = self._host_tasks.get(host_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(self._run_host_scan(host_id), name=f"assignment-host:{host_id}")
        self._host_tasks[host_id] = task

        def _host_done(done: asyncio.Task[None]) -> None:
            if self._host_tasks.get(host_id) is done:
                self._host_tasks.pop(host_id, None)
            with contextlib.suppress(asyncio.CancelledError, Exception):
                done.result()

        task.add_done_callback(_host_done)

    async def start(self) -> None:
        """Start the scan loop and run one immediate due pass."""
        if self._scan_task is None or self._scan_task.done():
            self._scan_task = asyncio.create_task(self._scan_loop(), name="assignment-due-scan")
        try:
            await self._due_pass_and_schedule()
        except Exception:  # noqa: BLE001 - periodic recovery remains live.
            _logger.warning("Assignment due pass failed at startup", exc_info=True)

    async def shutdown(self) -> None:
        """Cancel the loop and locally owned work."""
        if self._scan_task is not None:
            self._scan_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._scan_task
            self._scan_task = None
        tasks = list(self._tasks.values()) + list(self._host_tasks.values())
        self._tasks.clear()
        self._host_tasks.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def wait_for_idle(self) -> None:
        """Test seam: drain work currently owned by this coordinator."""
        while True:
            pending = [
                task
                for task in list(self._tasks.values()) + list(self._host_tasks.values())
                if not task.done()
            ]
            if not pending:
                return
            await asyncio.gather(*pending)

    async def _scan_loop(self) -> None:
        while True:
            await asyncio.sleep(self._scan_interval_seconds)
            try:
                await self._due_pass_and_schedule()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one DB fault must not retire recovery.
                _logger.warning("Assignment due recovery scan failed", exc_info=True)

    async def _due_pass_and_schedule(self) -> None:
        now = int(time.time())
        try:
            rows = await asyncio.to_thread(
                self._assignment_store.select_due, now=now, limit=self._due_batch_limit
            )
        except Exception:  # noqa: BLE001
            _logger.warning("Assignment due selection failed", exc_info=True)
            return
        for row in rows:
            existing = self._tasks.get(row.id)
            if existing is not None and not existing.done():
                continue
            self.trigger(row.id)

    async def _run_host_scan(self, host_id: str) -> None:
        try:
            conn = self._host_registry.get(host_id)
            if conn is None:
                return
            # Ownerless rows predate auth; the tunnel reports them as "local".
            raw_owner = conn.owner
            owner: str | None = (
                None if raw_owner is None or raw_owner == RESERVED_USER_LOCAL else raw_owner
            )
            rows = await asyncio.to_thread(
                self._assignment_store.select_waiting_for_host,
                host_id=host_id,
                owner_user_id=owner,
                limit=self._due_batch_limit,
            )
        except Exception:  # noqa: BLE001
            _logger.warning("Assignment host scan failed for %s", host_id, exc_info=True)
            return
        for row in rows:
            existing = self._tasks.get(row.id)
            if existing is not None and not existing.done():
                continue
            self.trigger(row.id)
        try:
            hosted = await asyncio.to_thread(
                self._assignment_store.select_for_host,
                host_id,
                limit=self._due_batch_limit,
            )
        except Exception:  # noqa: BLE001
            _logger.warning("Assignment host scan failed for %s", host_id, exc_info=True)
            return
        for row in hosted:
            if row.state not in ("interrupted", "stopping"):
                continue
            existing = self._tasks.get(row.id)
            if existing is not None and not existing.done():
                continue
            self.trigger(row.id)

    async def _run_one(self, assignment_id: str) -> None:
        try:
            await self._evaluate(assignment_id)
        except Exception:  # noqa: BLE001
            _logger.warning("Assignment evaluation failed for %s", assignment_id, exc_info=True)

    async def _check_lease(self, assignment: Assignment) -> Assignment | None:
        # Parked first so a crash or early exit never retries hot, and a
        # row a concurrent writer moved stops this evaluation now.
        now = int(time.time())
        return await asyncio.to_thread(
            self._assignment_store.reschedule,
            assignment.id,
            expected_state=assignment.state,
            expected_active_attempt_id=assignment.active_attempt_id,
            next_check_at=next_check_at(assignment.created_at, now),
        )

    async def _evaluate(self, assignment_id: str) -> None:
        assignment = await asyncio.to_thread(self._assignment_store.get, assignment_id)
        if assignment is None:
            return
        if assignment.state != "succeeded" or assignment.next_check_at is None:
            self._release_holds.pop(assignment.id, None)
        if assignment.state in TERMINAL_STATES:
            if assignment.next_check_at is None:
                return
            if assignment.next_check_at > int(time.time()):
                return
            if await self._check_lease(assignment) is None:
                return
            await self._evaluate_terminal_release(assignment)
            return
        if assignment.state == "waiting":
            await self._evaluate_waiting(assignment)
            return
        if await self._check_lease(assignment) is None:
            return
        if assignment.state in ("running", "publishing"):
            await self._evaluate_active(assignment)
            return
        if assignment.state == "starting":
            await self._evaluate_starting_orphan(assignment)
            return
        if assignment.state == "interrupted":
            await self._evaluate_interrupted(assignment)
            return
        if assignment.state == "stopping":
            await self._evaluate_stopping(assignment)
            return

    def _runner_alive(self, runner_id: str | None, conv: Conversation | None) -> bool:
        """Return whether the attempt's runner may still complete.

        A relaunch replaces the session's runner, so an old attempt whose
        id no longer matches the session is never alive.

        :param runner_id: The attempt's runner, or ``None``.
        :param conv: The attempt's session, or ``None`` when missing.
        :returns: ``True`` only when the id exists, the runner is
            connected, and the session still points at it.
        """
        if runner_id is None or conv is None:
            return False
        if conv.runner_id != runner_id:
            return False
        return bool(self._runner_router.runner_is_online(runner_id))

    async def _stop_confirmed(
        self,
        assignment: Assignment,
        attempt: AssignmentAttempt,
        runner_id: str | None,
        session_id: str,
    ) -> bool:
        """Return whether the old execution is confirmed stopped.

        Checked in order: no runner was ever bound; the host reported
        the runner exited; the destination host answers a stop with
        ``acked`` or ``unknown_runner``. Anything else is unconfirmed.

        :param assignment: The row being reconciled.
        :param attempt: The attempt whose execution to confirm.
        :param runner_id: The attempt's runner, or ``None``.
        :param session_id: The attempt's session id.
        :returns: ``True`` when the old execution is confirmed stopped.
        """
        if runner_id is None:
            return True
        if self._runner_exit_reports.get(runner_id) is not None:
            return True
        host_id = assignment.resolved_host_id or attempt.host_id
        if host_id is None:
            return False
        if self._host_registry.get(host_id) is None:
            return False
        from omnigent.server.routes.sessions import (
            _intentional_stop_sessions,
            _stop_session_host_runner_outcome,
        )

        had = session_id in _intentional_stop_sessions
        _intentional_stop_sessions.add(session_id)
        outcome: str | None = None
        try:
            outcome = await _stop_session_host_runner_outcome(
                session_id, host_id, runner_id, self._host_registry
            )
        except Exception:  # noqa: BLE001
            return False
        finally:
            if not had and outcome != "acked":
                _intentional_stop_sessions.discard(session_id)
        return outcome in ("acked", "unknown_runner")

    async def _evaluate_waiting(self, assignment: Assignment) -> None:
        outcome = await self._blocking_reason(assignment)
        if isinstance(outcome, str):
            now = int(time.time())
            if assignment.start_deadline is not None and assignment.start_deadline <= now:
                release_at: int | None = now if assignment.resolved_host_id is not None else None
                expired = await asyncio.to_thread(
                    self._assignment_store.transition,
                    assignment.id,
                    from_state="waiting",
                    to_state="expired",
                    expected_active_attempt_id=None,
                    next_check_at=release_at,
                    wait_reason=_truncate_reason(outcome),
                )
                if expired is not None and release_at is not None:
                    if await self._check_lease(expired) is None:
                        return
                    await self._evaluate_terminal_release(expired)
                return
            await asyncio.to_thread(
                self._assignment_store.reschedule,
                assignment.id,
                expected_state="waiting",
                expected_active_attempt_id=assignment.active_attempt_id,
                next_check_at=next_check_at(assignment.created_at, now),
                wait_reason=_truncate_reason(outcome),
            )
            return
        now = int(time.time())
        if assignment.start_deadline is not None and assignment.start_deadline <= now:
            release_at = now if assignment.resolved_host_id is not None else None
            expired = await asyncio.to_thread(
                self._assignment_store.transition,
                assignment.id,
                from_state="waiting",
                to_state="expired",
                expected_active_attempt_id=None,
                next_check_at=release_at,
            )
            if expired is not None and release_at is not None:
                if await self._check_lease(expired) is None:
                    return
                await self._evaluate_terminal_release(expired)
            return
        if await self._check_lease(assignment) is None:
            return
        if assignment.resolved_binding_id is None:
            expected_pin: tuple[str, int | None] | None = None
        else:
            expected_pin = (
                assignment.resolved_binding_id,
                assignment.resolved_binding_revision,
            )
        now = int(time.time())
        attempt = await asyncio.to_thread(
            self._assignment_store.claim_attempt,
            assignment.id,
            host_id=outcome.host_id,
            now=now,
            resolved_binding_id=outcome.binding.id,
            resolved_binding_revision=outcome.binding.revision,
            next_check_at=now + _PLACEMENT_NEXT_CHECK_S,
            expected_binding_pin=expected_pin,
        )
        if attempt is None:
            return
        await self._prepare_and_place(
            assignment, attempt, outcome.host_id, outcome.conn, outcome.sources
        )

    async def _blocking_reason(self, assignment: Assignment) -> str | _ClaimableDestination:
        project = await asyncio.to_thread(
            self._project_store.get, assignment.project_id, user_id=assignment.owner_user_id
        )
        if project is None or not project.collaboration_enabled:
            return "collaboration_disabled"
        dest_host_id = (
            assignment.resolved_host_id
            if assignment.resolved_host_id is not None
            else assignment.requested_host_id
        )
        if dest_host_id is not None:
            conn = self._host_registry.get(dest_host_id)
            if conn is None:
                return f"host_offline:{dest_host_id}"
            if not host_supports_assignments(conn):
                return f"host_unsupported:{dest_host_id}"
            binding = await self._resolve_binding(
                assignment.project_id, dest_host_id, assignment.binding_name
            )
            if binding is None or not binding.enabled:
                return f"binding_missing:{assignment.binding_name}"
            sources = await self._check_binding_and_inputs(assignment, dest_host_id, binding)
            if isinstance(sources, str):
                return sources
            return _ClaimableDestination(
                host_id=dest_host_id, conn=conn, binding=binding, sources=sources
            )
        owner = assignment.owner_user_id
        list_owner = owner if owner is not None else RESERVED_USER_LOCAL
        try:
            hosts: list[Host] = await asyncio.to_thread(self._host_store.list_hosts, list_owner)
        except Exception:  # noqa: BLE001
            _logger.warning("Assignment host listing failed for %s", assignment.id, exc_info=True)
            return "no_eligible_host"
        first_failure: str | None = None
        for host in hosts:
            host_id_value = host.host_id
            conn = self._host_registry.get(host_id_value)
            if conn is None or not host_supports_assignments(conn):
                continue
            binding = await self._resolve_binding(
                assignment.project_id, host_id_value, assignment.binding_name
            )
            if binding is None or not binding.enabled:
                continue
            sources = await self._check_binding_and_inputs(assignment, host_id_value, binding)
            if isinstance(sources, str):
                if first_failure is None:
                    first_failure = sources
                continue
            return _ClaimableDestination(
                host_id=host_id_value, conn=conn, binding=binding, sources=sources
            )
        return first_failure or "no_eligible_host"

    async def _resolve_binding(
        self, project_id: str, host_id: str, binding_name: str
    ) -> ProjectHostBinding | None:
        if binding_name == "primary":
            bindings = await asyncio.to_thread(
                self._binding_store.list_by_host, project_id=project_id, host_id=host_id
            )
            return next((binding for binding in bindings if binding.is_primary), None)
        return await asyncio.to_thread(
            self._binding_store.get_by_name,
            project_id=project_id,
            host_id=host_id,
            name=binding_name,
        )

    async def _enabled_bindings_for_repo(
        self, project_id: str, host_id: str, repo_id: str
    ) -> list[ProjectHostBinding]:
        """Return one host's enabled bindings holding a repository.

        :param project_id: The project the bindings belong to.
        :param host_id: The host whose bindings to list.
        :param repo_id: The registered repository the binding must hold.
        :returns: The matching bindings, empty when none match.
        """
        candidates = await asyncio.to_thread(
            self._binding_store.list_by_host,
            project_id=project_id,
            host_id=host_id,
        )
        return [
            candidate
            for candidate in candidates
            if candidate.enabled and candidate.repository_id == repo_id
        ]

    async def _check_binding_and_inputs(
        self, assignment: Assignment, host_id: str, binding: ProjectHostBinding
    ) -> str | dict[str, str]:
        roots = [entry for entry in assignment.inputs if entry.is_execution_root]
        if not roots:
            return f"binding_mismatch:{assignment.binding_name}"
        root_entry = roots[0]
        root_repo = await asyncio.to_thread(
            self._repository_store.get_by_name,
            project_id=assignment.project_id,
            name=root_entry.repository_name,
        )
        if root_repo is None:
            return f"repository_missing:{root_entry.repository_name}"
        if binding.repository_id != root_repo.id:
            return f"binding_mismatch:{assignment.binding_name}"
        if assignment.resolved_binding_id is not None and (
            binding.id != assignment.resolved_binding_id
            or binding.revision != assignment.resolved_binding_revision
        ):
            return "binding_changed"
        sources: dict[str, str] = {root_entry.repository_name: binding.workspace}
        by_name: dict[str, ProjectRepository] = {root_repo.name: root_repo}
        for entry in assignment.inputs:
            repo = by_name.get(entry.repository_name)
            if repo is None:
                repo = await asyncio.to_thread(
                    self._repository_store.get_by_name,
                    project_id=assignment.project_id,
                    name=entry.repository_name,
                )
                if repo is None:
                    return f"repository_missing:{entry.repository_name}"
                by_name[entry.repository_name] = repo
            if repo.revision != entry.repository_revision:
                return f"repository_changed:{entry.repository_name}"
            if entry.is_execution_root:
                continue
            matches = await self._enabled_bindings_for_repo(
                assignment.project_id, host_id, repo.id
            )
            if not matches:
                return f"binding_missing:{entry.repository_name}"
            if len(matches) > 1:
                return f"binding_ambiguous:{entry.repository_name}"
            sources[entry.repository_name] = matches[0].workspace
        return sources

    async def _prepare_and_place(
        self,
        assignment: Assignment,
        attempt: AssignmentAttempt,
        host_id: str,
        conn: HostConnection,
        sources: dict[str, str],
    ) -> None:
        repositories = [
            HostAssignmentPrepareRepository(
                repository_name=entry.repository_name,
                source_directory=sources[entry.repository_name],
                remote_url=entry.remote_url,
                input_ref=entry.input_ref,
                input_commit=entry.input_commit,
                context_manifest_path=entry.context_manifest_path,
                manifest_digest=entry.manifest_digest,
            )
            for entry in assignment.inputs
        ]
        frame = HostAssignmentPrepareFrame(
            request_id=uuid.uuid4().hex,
            assignment_id=assignment.id,
            repositories=repositories,
        )
        try:
            result = await prepare_assignment_on_host(
                host_registry=self._host_registry, host_conn=conn, frame=frame
            )
        except Exception:  # noqa: BLE001
            _logger.warning("Assignment prepare failed for %s", assignment.id, exc_info=True)
            await self._return_to_waiting(
                assignment, attempt, "host_unavailable", f"host_unavailable:{host_id}"
            )
            return
        if result.status != "ok":
            code = result.error_code or "prepare_failed"
            if result.repository_name:
                reason = f"{code}:{result.repository_name}: {result.error or ''}".rstrip()
            else:
                reason = f"{code}: {result.error or ''}".rstrip()
            await self._return_to_waiting(assignment, attempt, code, _truncate_reason(reason))
            return
        await self._place(assignment, attempt, host_id, result.directories)

    async def _return_to_waiting(
        self,
        assignment: Assignment,
        attempt: AssignmentAttempt,
        error_code: str,
        reason: str,
    ) -> None:
        failure_now = int(time.time())
        with contextlib.suppress(InactiveAttemptError):
            await asyncio.to_thread(
                self._assignment_store.update_attempt,
                assignment.id,
                attempt.id,
                state="finished",
                ended_at=failure_now,
                error_code=error_code,
            )
        await asyncio.to_thread(
            self._assignment_store.transition,
            assignment.id,
            from_state="starting",
            to_state="waiting",
            expected_active_attempt_id=attempt.id,
            active_attempt_id=None,
            wait_reason=_truncate_reason(reason),
            next_check_at=next_check_at(assignment.created_at, failure_now),
        )

    async def _fail_before_launch(
        self, assignment: Assignment, attempt: AssignmentAttempt, message: str
    ) -> None:
        await self._return_to_waiting(
            assignment,
            attempt,
            "launch_failed",
            _truncate_reason(f"launch_failed: {message}"),
        )

    async def _fail_after_launch(
        self, assignment: Assignment, attempt: AssignmentAttempt, stage: str, message: str
    ) -> None:
        # The launch outcome is unknown once a runner id exists: the
        # attempt stays active and the stop is confirmed by the
        # interrupted handling below.
        now = int(time.time())
        interrupted = await asyncio.to_thread(
            self._assignment_store.transition,
            assignment.id,
            from_state="starting",
            to_state="interrupted",
            expected_active_attempt_id=attempt.id,
            wait_reason=_truncate_reason(f"{stage}: {message}"),
            next_check_at=now,
        )
        if interrupted is None:
            return
        if await self._check_lease(interrupted) is None:
            return
        await self._evaluate_interrupted(interrupted)

    async def _evaluate_active(self, assignment: Assignment) -> None:
        """Watch a ``running`` / ``publishing`` row for runner liveness.

        The project switch is never consulted here: an attempt already
        running must be able to finish after the switch goes off.

        :param assignment: The active row to reconcile.
        """
        state = assignment.state
        if assignment.active_attempt_id is None:
            return
        attempt = await asyncio.to_thread(
            self._assignment_store.get_attempt,
            assignment.id,
            assignment.active_attempt_id,
        )
        if attempt is None:
            return
        conv = await asyncio.to_thread(
            self._conversation_store.get_conversation,
            _attempt_session_id(attempt),
        )
        runner_id = _attempt_runner_id(attempt, conv)
        if self._runner_alive(runner_id, conv):
            now = int(time.time())
            await asyncio.to_thread(self._assignment_store.set_lease, attempt.id, None)
            await asyncio.to_thread(
                self._assignment_store.reschedule,
                assignment.id,
                expected_state=state,
                expected_active_attempt_id=assignment.active_attempt_id,
                next_check_at=now + _ACTIVE_CHECK_S,
            )
            return
        lease = attempt.lease_expires_at
        now = int(time.time())
        if lease is None:
            await asyncio.to_thread(self._assignment_store.set_lease, attempt.id, now + _LEASE_S)
            await asyncio.to_thread(
                self._assignment_store.reschedule,
                assignment.id,
                expected_state=state,
                expected_active_attempt_id=assignment.active_attempt_id,
                next_check_at=now + _LEASE_S,
            )
            return
        if lease > now:
            await asyncio.to_thread(
                self._assignment_store.reschedule,
                assignment.id,
                expected_state=state,
                expected_active_attempt_id=assignment.active_attempt_id,
                next_check_at=lease,
            )
            return
        interrupted = await asyncio.to_thread(
            self._assignment_store.transition,
            assignment.id,
            from_state=state,
            to_state="interrupted",
            expected_active_attempt_id=assignment.active_attempt_id,
            wait_reason=_truncate_reason(f"runner_lost:{runner_id or 'none'}"),
            next_check_at=now,
        )
        if interrupted is None:
            return
        if await self._check_lease(interrupted) is None:
            return
        await self._evaluate_interrupted(interrupted)

    async def _evaluate_starting_orphan(self, assignment: Assignment) -> None:
        """Retire a ``starting`` row no live placement still owns.

        This coordinator skips ids it is already evaluating, but a second
        replica has its own in-flight set, so a ``starting`` row is only
        orphaned past the placing attempt's check window.

        :param assignment: The ``starting`` row to reconcile.
        """
        if assignment.active_attempt_id is None:
            now = int(time.time())
            await asyncio.to_thread(
                self._assignment_store.transition,
                assignment.id,
                from_state="starting",
                to_state="waiting",
                expected_active_attempt_id=assignment.active_attempt_id,
                active_attempt_id=None,
                wait_reason="placement_abandoned",
                next_check_at=next_check_at(assignment.created_at, now),
            )
            return
        attempt = await asyncio.to_thread(
            self._assignment_store.get_attempt,
            assignment.id,
            assignment.active_attempt_id,
        )
        if attempt is None:
            now = int(time.time())
            await asyncio.to_thread(
                self._assignment_store.transition,
                assignment.id,
                from_state="starting",
                to_state="waiting",
                expected_active_attempt_id=assignment.active_attempt_id,
                active_attempt_id=None,
                wait_reason="placement_abandoned",
                next_check_at=next_check_at(assignment.created_at, now),
            )
            return
        now = int(time.time())
        if (attempt.started_at or 0) + _PLACEMENT_NEXT_CHECK_S > now:
            # Another coordinator may still be placing: park past its
            # window instead of retiring its in-flight attempt.
            await asyncio.to_thread(
                self._assignment_store.reschedule,
                assignment.id,
                expected_state="starting",
                expected_active_attempt_id=assignment.active_attempt_id,
                next_check_at=(attempt.started_at or 0) + _PLACEMENT_NEXT_CHECK_S,
            )
            return
        conv = await asyncio.to_thread(
            self._conversation_store.get_conversation,
            _attempt_session_id(attempt),
        )
        runner_id = _attempt_runner_id(attempt, conv)
        if runner_id is None:
            if not _attempt_ended(attempt):
                await asyncio.to_thread(
                    self._assignment_store.update_attempt,
                    assignment.id,
                    attempt.id,
                    state="finished",
                    ended_at=now,
                    error_code="placement_abandoned",
                )
            await asyncio.to_thread(
                self._assignment_store.transition,
                assignment.id,
                from_state="starting",
                to_state="waiting",
                expected_active_attempt_id=attempt.id,
                active_attempt_id=None,
                wait_reason="placement_abandoned",
                next_check_at=next_check_at(assignment.created_at, now),
            )
            return
        interrupted = await asyncio.to_thread(
            self._assignment_store.transition,
            assignment.id,
            from_state="starting",
            to_state="interrupted",
            expected_active_attempt_id=attempt.id,
            wait_reason="placement_abandoned",
            next_check_at=now,
        )
        if interrupted is None:
            return
        if await self._check_lease(interrupted) is None:
            return
        await self._evaluate_interrupted(interrupted)

    async def _evaluate_interrupted(self, assignment: Assignment) -> None:
        """Confirm the old execution stopped, then retire or re-arm.

        :param assignment: The ``interrupted`` row to reconcile.
        """
        if assignment.active_attempt_id is None:
            await asyncio.to_thread(
                self._assignment_store.reschedule,
                assignment.id,
                expected_state="interrupted",
                expected_active_attempt_id=assignment.active_attempt_id,
                next_check_at=None,
            )
            return
        attempt = await asyncio.to_thread(
            self._assignment_store.get_attempt,
            assignment.id,
            assignment.active_attempt_id,
        )
        if attempt is not None and not _attempt_ended(attempt):
            conv = await asyncio.to_thread(
                self._conversation_store.get_conversation,
                _attempt_session_id(attempt),
            )
            runner_id = _attempt_runner_id(attempt, conv)
            session_id = _attempt_session_id(attempt)
            if not await self._stop_confirmed(assignment, attempt, runner_id, session_id):
                return
            now = int(time.time())
            await asyncio.to_thread(
                self._assignment_store.update_attempt,
                assignment.id,
                attempt.id,
                state="lost",
                ended_at=now,
                error_code=attempt.error_code or "runner_lost",
            )
            if assignment.start_deadline is not None and assignment.start_deadline <= now:
                await asyncio.to_thread(
                    self._assignment_store.transition,
                    assignment.id,
                    from_state="interrupted",
                    to_state="expired",
                    expected_active_attempt_id=attempt.id,
                    next_check_at=now,
                )
                return
            await asyncio.to_thread(
                self._assignment_store.reschedule,
                assignment.id,
                expected_state="interrupted",
                expected_active_attempt_id=attempt.id,
                next_check_at=None,
            )
            return
        now = int(time.time())
        if assignment.start_deadline is not None and assignment.start_deadline <= now:
            await asyncio.to_thread(
                self._assignment_store.transition,
                assignment.id,
                from_state="interrupted",
                to_state="expired",
                expected_active_attempt_id=assignment.active_attempt_id,
                next_check_at=now,
            )
            return
        await asyncio.to_thread(
            self._assignment_store.reschedule,
            assignment.id,
            expected_state="interrupted",
            expected_active_attempt_id=assignment.active_attempt_id,
            next_check_at=None,
        )

    async def _evaluate_stopping(self, assignment: Assignment) -> None:
        """Stop the runner, then cancel once the stop is confirmed.

        :param assignment: The ``stopping`` row to reconcile.
        """
        attempt: AssignmentAttempt | None = None
        if assignment.active_attempt_id is not None:
            attempt = await asyncio.to_thread(
                self._assignment_store.get_attempt,
                assignment.id,
                assignment.active_attempt_id,
            )
        if attempt is None or _attempt_ended(attempt):
            # An ended attempt owns no execution: cancel without asking
            # the host, and let the release confirm the session's runner.
            now = int(time.time())
            cancelled = await asyncio.to_thread(
                self._assignment_store.transition,
                assignment.id,
                from_state="stopping",
                to_state="cancelled",
                expected_active_attempt_id=assignment.active_attempt_id,
                next_check_at=now,
            )
            if cancelled is None:
                return
            if await self._check_lease(cancelled) is None:
                return
            await self._evaluate_terminal_release(cancelled)
            return
        conv = await asyncio.to_thread(
            self._conversation_store.get_conversation,
            _attempt_session_id(attempt),
        )
        runner_id = _attempt_runner_id(attempt, conv)
        session_id = _attempt_session_id(attempt)
        if await self._stop_confirmed(assignment, attempt, runner_id, session_id):
            now = int(time.time())
            await asyncio.to_thread(
                self._assignment_store.update_attempt,
                assignment.id,
                attempt.id,
                state="finished",
                ended_at=now,
                error_code="cancelled",
            )
            cancelled = await asyncio.to_thread(
                self._assignment_store.transition,
                assignment.id,
                from_state="stopping",
                to_state="cancelled",
                expected_active_attempt_id=attempt.id,
                next_check_at=now,
            )
            if cancelled is None:
                return
            if await self._check_lease(cancelled) is None:
                return
            await self._evaluate_terminal_release(cancelled)
            return
        lease = attempt.lease_expires_at
        now = int(time.time())
        if lease is None:
            await asyncio.to_thread(self._assignment_store.set_lease, attempt.id, now + _LEASE_S)
            await asyncio.to_thread(
                self._assignment_store.reschedule,
                assignment.id,
                expected_state="stopping",
                expected_active_attempt_id=assignment.active_attempt_id,
                next_check_at=now + _ACTIVE_CHECK_S,
            )
            return
        if lease > now:
            await asyncio.to_thread(
                self._assignment_store.reschedule,
                assignment.id,
                expected_state="stopping",
                expected_active_attempt_id=assignment.active_attempt_id,
                next_check_at=min(lease, now + _ACTIVE_CHECK_S),
            )
            return
        await asyncio.to_thread(
            self._assignment_store.transition,
            assignment.id,
            from_state="stopping",
            to_state="interrupted",
            expected_active_attempt_id=attempt.id,
            wait_reason="stop_unconfirmed",
            next_check_at=next_check_at(assignment.created_at, now),
        )

    async def _evaluate_terminal_release(self, assignment: Assignment) -> None:
        """Release a terminal row's worktrees once its check is due.

        :param assignment: The terminal row with a pending release.
        """
        if assignment.state != "succeeded":
            self._release_holds.pop(assignment.id, None)
        now = int(time.time())
        if assignment.next_check_at is None:
            return
        if assignment.next_check_at > now:
            return
        if assignment.resolved_host_id is None:
            released = await asyncio.to_thread(
                self._assignment_store.reschedule,
                assignment.id,
                expected_state=assignment.state,
                expected_active_attempt_id=assignment.active_attempt_id,
                next_check_at=None,
            )
            if released is not None:
                self._release_holds.pop(assignment.id, None)
            return
        host_id = assignment.resolved_host_id
        conn = self._host_registry.get(host_id)
        if conn is None:
            await asyncio.to_thread(
                self._assignment_store.reschedule,
                assignment.id,
                expected_state=assignment.state,
                expected_active_attempt_id=assignment.active_attempt_id,
                next_check_at=next_check_at(assignment.created_at, now),
            )
            return
        attempt: AssignmentAttempt | None = None
        if assignment.active_attempt_id is not None:
            attempt = await asyncio.to_thread(
                self._assignment_store.get_attempt,
                assignment.id,
                assignment.active_attempt_id,
            )
        else:
            attempt = await asyncio.to_thread(
                self._assignment_store.get_latest_attempt,
                assignment.id,
            )
        session_id: str | None = None
        confirmed_runner: str | None = None
        conv: Conversation | None = None
        if attempt is not None:
            session_id = _attempt_session_id(attempt)
            conv = await asyncio.to_thread(
                self._conversation_store.get_conversation,
                session_id,
            )
        if assignment.state == "succeeded":
            started, idle_seen = self._release_holds.get(assignment.id, (now, None))
            start = (attempt.ended_at if attempt is not None else None) or started
            if now - start < _TURN_END_GRACE_S:
                if _session_mid_turn(conv):
                    self._release_holds[assignment.id] = (started, None)
                    await asyncio.to_thread(
                        self._assignment_store.reschedule,
                        assignment.id,
                        expected_state=assignment.state,
                        expected_active_attempt_id=assignment.active_attempt_id,
                        next_check_at=now + _ACTIVE_CHECK_S,
                    )
                    return
                if idle_seen is None or now - idle_seen < _ACTIVE_CHECK_S:
                    self._release_holds[assignment.id] = (started, idle_seen or now)
                    await asyncio.to_thread(
                        self._assignment_store.reschedule,
                        assignment.id,
                        expected_state=assignment.state,
                        expected_active_attempt_id=assignment.active_attempt_id,
                        next_check_at=now + _ACTIVE_CHECK_S,
                    )
                    return
        if attempt is not None:
            if conv is not None and session_id is not None:
                await asyncio.to_thread(
                    self._conversation_store.set_labels,
                    session_id,
                    {CLOSED_LABEL_KEY: CLOSED_LABEL_VALUE},
                )
            # A relaunch replaces the session's runner, so the stop
            # must name the session's current runner, not the attempt's.
            session_runner = conv.runner_id if conv is not None else None
            confirmed_runner = session_runner
            if (
                session_runner is not None
                and session_id is not None
                and not await self._stop_confirmed(assignment, attempt, session_runner, session_id)
            ):
                return
        sources: dict[str, str] = {}
        for entry in assignment.inputs:
            if entry.is_execution_root:
                if assignment.resolved_binding_id is None:
                    await self._release_skipped(
                        assignment,
                        "worktree release skipped: "
                        f"binding {entry.repository_name} no longer exists",
                    )
                    return
                binding = await asyncio.to_thread(
                    self._binding_store.get, assignment.resolved_binding_id
                )
                if binding is None:
                    await self._release_skipped(
                        assignment,
                        "worktree release skipped: "
                        f"binding {assignment.resolved_binding_id} no longer exists",
                    )
                    return
                # The host derives paths from the binding it is sent and
                # drops unknown ones, so a moved binding would silently
                # clear the schedule and leak the real worktree.
                if binding.revision != assignment.resolved_binding_revision:
                    await self._release_skipped(
                        assignment,
                        "worktree release skipped: "
                        f"binding for {entry.repository_name} changed since placement",
                    )
                    return
                sources[entry.repository_name] = binding.workspace
            else:
                repo = await asyncio.to_thread(
                    self._repository_store.get_by_name,
                    project_id=assignment.project_id,
                    name=entry.repository_name,
                )
                if repo is None:
                    await self._release_skipped(
                        assignment,
                        "worktree release skipped: "
                        f"binding {entry.repository_name} no longer exists",
                    )
                    return
                matches = await self._enabled_bindings_for_repo(
                    assignment.project_id, host_id, repo.id
                )
                if len(matches) != 1:
                    await self._release_skipped(
                        assignment,
                        "worktree release skipped: "
                        f"binding {entry.repository_name} no longer exists",
                    )
                    return
                candidate = matches[0]
                if attempt is None or attempt.started_at is None:
                    await self._release_skipped(
                        assignment,
                        "worktree release skipped: "
                        f"binding for {entry.repository_name} changed since placement",
                    )
                    return
                if candidate.updated_at is not None and candidate.updated_at > attempt.started_at:
                    await self._release_skipped(
                        assignment,
                        "worktree release skipped: "
                        f"binding for {entry.repository_name} changed since placement",
                    )
                    return
                sources[entry.repository_name] = candidate.workspace
        if attempt is not None and session_id is not None:
            # The stop may have been awaited while the session
            # relaunched; a changed runner must not be released.
            fresh = await asyncio.to_thread(
                self._conversation_store.get_conversation,
                session_id,
            )
            fresh_runner = fresh.runner_id if fresh is not None else None
            if fresh_runner != confirmed_runner:
                return
        frame = HostAssignmentReleaseFrame(
            request_id=uuid.uuid4().hex,
            assignment_id=assignment.id,
            repositories=[
                HostAssignmentReleaseRepository(
                    repository_name=entry.repository_name,
                    source_directory=sources[entry.repository_name],
                )
                for entry in assignment.inputs
            ],
        )
        try:
            result = await release_assignment_on_host(
                host_registry=self._host_registry, host_conn=conn, frame=frame
            )
        except Exception:  # noqa: BLE001
            _logger.warning("Assignment release failed for %s", assignment.id, exc_info=True)
            await asyncio.to_thread(
                self._assignment_store.reschedule,
                assignment.id,
                expected_state=assignment.state,
                expected_active_attempt_id=assignment.active_attempt_id,
                next_check_at=next_check_at(assignment.created_at, int(time.time())),
            )
            return
        if result.status == "ok":
            released = await asyncio.to_thread(
                self._assignment_store.reschedule,
                assignment.id,
                expected_state=assignment.state,
                expected_active_attempt_id=assignment.active_attempt_id,
                next_check_at=None,
            )
            if released is not None:
                self._release_holds.pop(assignment.id, None)
            return
        if result.status == "partial":
            parts = []
            for entry in assignment.inputs:
                reason = result.failures.get(entry.repository_name)
                if reason is not None:
                    parts.append(f"{entry.repository_name}: {reason}")
            body = "worktree release incomplete: " + "; ".join(parts)
            body = body[:2000]
            await asyncio.to_thread(
                self._assignment_store.append_message,
                AssignmentMessage(
                    id=uuid.uuid4().hex,
                    assignment_id=assignment.id,
                    kind="state",
                    body=body,
                    sender_session_id=None,
                    idempotency_key=None,
                ),
            )
            released = await asyncio.to_thread(
                self._assignment_store.reschedule,
                assignment.id,
                expected_state=assignment.state,
                expected_active_attempt_id=assignment.active_attempt_id,
                next_check_at=None,
            )
            if released is not None:
                self._release_holds.pop(assignment.id, None)
            return
        _logger.warning("Assignment release returned %r for %s", result.status, assignment.id)
        await asyncio.to_thread(
            self._assignment_store.reschedule,
            assignment.id,
            expected_state=assignment.state,
            expected_active_attempt_id=assignment.active_attempt_id,
            next_check_at=next_check_at(assignment.created_at, int(time.time())),
        )

    async def _release_skipped(self, assignment: Assignment, body: str) -> None:
        """Record a skipped release and clear its check.

        :param assignment: The terminal row whose release is skipped.
        :param body: The state message naming the stale binding.
        """
        await asyncio.to_thread(
            self._assignment_store.append_message,
            AssignmentMessage(
                id=uuid.uuid4().hex,
                assignment_id=assignment.id,
                kind="state",
                body=body,
                sender_session_id=None,
                idempotency_key=None,
            ),
        )
        released = await asyncio.to_thread(
            self._assignment_store.reschedule,
            assignment.id,
            expected_state=assignment.state,
            expected_active_attempt_id=assignment.active_attempt_id,
            next_check_at=None,
        )
        if released is not None:
            self._release_holds.pop(assignment.id, None)

    async def _place(
        self,
        assignment: Assignment,
        attempt: AssignmentAttempt,
        host_id: str,
        directories: dict[str, str],
    ) -> None:
        roots = [entry for entry in assignment.inputs if entry.is_execution_root]
        root_name = roots[0].repository_name if roots else assignment.inputs[0].repository_name
        workspace = directories.get(root_name)
        if not workspace:
            await self._fail_before_launch(assignment, attempt, "prepare returned no directory")
            return
        conversation_id = _derived_session_id(attempt.id)
        owner = assignment.owner_user_id or RESERVED_USER_LOCAL
        if self._permission_store is not None:
            try:
                await asyncio.to_thread(self._permission_store.ensure_user, owner)
                await asyncio.to_thread(
                    self._permission_store.grant, owner, conversation_id, LEVEL_OWNER
                )
            except Exception as exc:  # noqa: BLE001
                await self._fail_before_launch(assignment, attempt, str(exc) or "grant failed")
                return
        title = assignment.task.splitlines()[0][:80] if assignment.task else ""
        try:
            conv: Conversation = await asyncio.to_thread(
                self._conversation_store.create_conversation,
                agent_id=assignment.target_agent_id,
                title=f"Assignment: {title}",
                host_id=host_id,
                workspace=workspace,
                conversation_id=conversation_id,
                project_id=assignment.project_id,
            )
        except ConversationAlreadyExistsError:
            try:
                maybe_conv = await asyncio.to_thread(
                    self._conversation_store.get_conversation, conversation_id
                )
            except Exception as exc:  # noqa: BLE001
                await self._fail_before_launch(assignment, attempt, str(exc) or "read failed")
                return
            if maybe_conv is None:
                await self._fail_before_launch(assignment, attempt, "session vanished")
                return
            conv = maybe_conv
        except Exception as exc:  # noqa: BLE001
            await self._fail_before_launch(assignment, attempt, str(exc) or "create failed")
            return
        if assignment.model_override is not None or assignment.harness_override is not None:
            try:
                await asyncio.to_thread(
                    self._conversation_store.update_conversation,
                    conv.id,
                    model_override=assignment.model_override,
                    harness_override=assignment.harness_override,
                )
            except Exception as exc:  # noqa: BLE001
                await self._fail_before_launch(assignment, attempt, str(exc) or "update failed")
                return
        try:
            await asyncio.to_thread(
                self._assignment_store.update_attempt,
                assignment.id,
                attempt.id,
                session_id=conv.id,
            )
        except Exception as exc:  # noqa: BLE001
            _logger.warning("Assignment session bind failed for %s", assignment.id, exc_info=True)
            await self._fail_before_launch(assignment, attempt, str(exc) or "bind failed")
            return
        from omnigent.server.routes._host_launch import resolve_host_launch

        try:
            target = await asyncio.to_thread(
                resolve_host_launch,
                user_id=owner,
                host_id=host_id,
                session_id=conv.id,
                host_store=self._host_store,
                host_registry=self._host_registry,
                conversation_store=self._conversation_store,
                permission_store=self._permission_store,
            )
        except Exception as exc:  # noqa: BLE001
            message = getattr(exc, "message", None) or getattr(exc, "detail", None) or str(exc)
            await self._fail_before_launch(assignment, attempt, message or "resolve failed")
            return
        from omnigent.server.routes.sessions import _launch_runner_on_host

        try:
            launch = await _launch_runner_on_host(
                conv, self._conversation_store, self._host_registry, target.conn
            )
        except Exception as exc:  # noqa: BLE001
            await self._fail_before_launch(assignment, attempt, str(exc) or "launch failed")
            return
        if launch.error is not None:
            await self._fail_before_launch(
                assignment, attempt, str(launch.error) or "host launch failed"
            )
            return
        runner_id = launch.runner_id
        if not runner_id:
            await self._fail_before_launch(assignment, attempt, "host launch failed")
            return
        try:
            await asyncio.to_thread(
                self._assignment_store.update_attempt,
                assignment.id,
                attempt.id,
                runner_id=runner_id,
            )
        except Exception:  # noqa: BLE001
            _logger.warning("Assignment runner bind failed for %s", assignment.id, exc_info=True)
            await self._fail_after_launch(assignment, attempt, "record_runner", "bind failed")
            return
        from omnigent.server.routes.sessions import _wait_for_runner_client

        try:
            client = await _wait_for_runner_client(
                conv.id,
                self._runner_router,
                self._tunnel_registry,
                runner_id=runner_id,
                timeout_s=_RUNNER_CONNECT_TIMEOUT_S,
                runner_exit_reports=self._runner_exit_reports,
            )
        except Exception as exc:  # noqa: BLE001
            await self._fail_after_launch(assignment, attempt, "wait_runner", str(exc))
            return
        if client is None:
            await self._fail_after_launch(
                assignment, attempt, "wait_runner", "runner did not connect before timeout"
            )
            return
        try:
            fresh: Conversation | None = await asyncio.to_thread(
                self._conversation_store.get_conversation, conv.id
            )
        except Exception as exc:  # noqa: BLE001
            await self._fail_after_launch(assignment, attempt, "init", str(exc))
            return
        conv_for_dispatch: Conversation = fresh or conv
        from omnigent.server.routes.sessions import _ensure_runner_session_initialized

        try:
            ready = await _ensure_runner_session_initialized(
                conv.id,
                conv_for_dispatch,
                client,
                self._conversation_store,
                initializer=self._runner_session_initializer,
                require_success=True,
            )
        except Exception as exc:  # noqa: BLE001
            await self._fail_after_launch(assignment, attempt, "init", str(exc))
            return
        try:
            dispatched = await asyncio.to_thread(
                self._assignment_store.mark_event_dispatched, attempt.id, now=int(time.time())
            )
        except Exception as exc:  # noqa: BLE001
            await self._fail_after_launch(assignment, attempt, "dispatch", str(exc))
            return
        if not dispatched:
            await self._fail_after_launch(
                assignment, attempt, "event_already_dispatched", "delivery already claimed"
            )
            return
        from omnigent.server.routes.sessions import (
            _dispatch_session_event_to_runner,
            _is_native_terminal_session,
        )

        event = SessionEventInput(
            type="message",
            data={
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": build_initial_event_text(assignment, attempt.id, directories),
                    }
                ],
            },
        )
        try:
            result = await _dispatch_session_event_to_runner(
                conv.id,
                conv_for_dispatch,
                event,
                self._conversation_store,
                client,
                agent_name=None,
                file_store=self._file_store,
                artifact_store=self._artifact_store,
                created_by=owner,
                runner_router=self._runner_router,
                native_terminal_ready=ready,
                host_store=self._host_store,
            )
        except Exception as exc:  # noqa: BLE001
            await self._fail_after_launch(assignment, attempt, "dispatch", str(exc))
            return
        # A native prompt that never reached the terminal persists a failure
        # item and returns pending_id=None instead of raising.
        is_native = await asyncio.to_thread(_is_native_terminal_session, conv_for_dispatch)
        if is_native and result.pending_id is None:
            await self._fail_after_launch(
                assignment, attempt, "dispatch", "native terminal did not accept the prompt"
            )
            return
        now = int(time.time())
        updated = await asyncio.to_thread(
            self._assignment_store.transition,
            assignment.id,
            from_state="starting",
            to_state="running",
            expected_active_attempt_id=attempt.id,
            next_check_at=now + _ACTIVE_CHECK_S,
        )
        if updated is None:
            _logger.warning(
                "Assignment %s left starting by another writer; leaving for reconcile",
                assignment.id,
            )
