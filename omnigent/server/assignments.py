"""Assignment coordinator — scoped triggers, backoff and placement.

One durable assignment hands work to one ``(host, agent)`` destination.
This coordinator evaluates a single row at a time: a ``waiting`` row is
checked against the project switch, the destination, the binding and the
registered revisions, then claimed, prepared on the host and placed as a
runner session with one initial event. Any other non-terminal row only
moves its ``next_check_at``.

Triggers are scoped: one assignment id, one host's waiting rows, or the
bounded due-work pass. The pass is indexed, terminal-excluding and
row-capped, and every row carries its own backoff so nothing retries hot.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import time
import uuid
from dataclasses import dataclass

from omnigent.entities import (
    Assignment,
    AssignmentAttempt,
    Conversation,
    ProjectHostBinding,
    ProjectRepository,
)
from omnigent.entities.assignment import TERMINAL_STATES
from omnigent.host.frames import (
    HostAssignmentPrepareFrame,
    HostAssignmentPrepareRepository,
)
from omnigent.runner.routing import RunnerRouter
from omnigent.runner.transports.ws_tunnel.registry import TunnelRegistry
from omnigent.server.assignment_host import (
    host_supports_assignments,
    prepare_assignment_on_host,
)
from omnigent.server.auth import LEVEL_OWNER, RESERVED_USER_LOCAL
from omnigent.server.host_registry import HostConnection, HostRegistry, RunnerExitReports
from omnigent.server.schemas import SessionEventInput
from omnigent.stores.artifact_store import ArtifactStore
from omnigent.stores.assignment_store import AssignmentStore
from omnigent.stores.conversation_store import ConversationAlreadyExistsError, ConversationStore
from omnigent.stores.file_store import FileStore
from omnigent.stores.host_store import Host, HostStore
from omnigent.stores.permission_store import PermissionStore
from omnigent.stores.project_host_binding_store import ProjectHostBindingStore
from omnigent.stores.project_repository_store import ProjectRepositoryStore
from omnigent.stores.project_store import ProjectStore

_logger = logging.getLogger(__name__)

_BACKOFF_MIN_S = 15
_BACKOFF_MAX_S = 3600

_PLACEMENT_NEXT_CHECK_S = 420
_RUNNER_CONNECT_TIMEOUT_S = 30.0


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
        "Work only in the directories above, commit the work there, and call "
        f'sys_assignment_complete with assignment_id "{assignment.id}" and '
        f'attempt_id "{attempt_id}" when done.',
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
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._host_tasks: dict[str, asyncio.Task[None]] = {}
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

    async def _run_one(self, assignment_id: str) -> None:
        try:
            await self._evaluate(assignment_id)
        except Exception:  # noqa: BLE001
            _logger.warning("Assignment evaluation failed for %s", assignment_id, exc_info=True)

    async def _evaluate(self, assignment_id: str) -> None:
        assignment = await asyncio.to_thread(self._assignment_store.get, assignment_id)
        if assignment is None or assignment.state in TERMINAL_STATES:
            return
        if assignment.state != "waiting":
            await self._reschedule_other(assignment)
            return
        await self._evaluate_waiting(assignment)

    async def _reschedule_other(self, assignment: Assignment) -> None:
        now = int(time.time())
        try:
            await asyncio.to_thread(
                self._assignment_store.reschedule,
                assignment.id,
                expected_state=assignment.state,
                expected_active_attempt_id=assignment.active_attempt_id,
                next_check_at=next_check_at(assignment.created_at, now),
            )
        except Exception:  # noqa: BLE001
            _logger.warning("Assignment reschedule failed for %s", assignment.id, exc_info=True)

    async def _evaluate_waiting(self, assignment: Assignment) -> None:
        now = int(time.time())
        outcome = await self._blocking_reason(assignment)
        if isinstance(outcome, str):
            if assignment.start_deadline is not None and assignment.start_deadline <= now:
                try:
                    await asyncio.to_thread(
                        self._assignment_store.transition,
                        assignment.id,
                        from_state="waiting",
                        to_state="expired",
                        expected_active_attempt_id=None,
                        next_check_at=None,
                        wait_reason=_truncate_reason(outcome),
                    )
                except Exception:  # noqa: BLE001
                    _logger.warning(
                        "Assignment expiry failed for %s", assignment.id, exc_info=True
                    )
                return
            try:
                await asyncio.to_thread(
                    self._assignment_store.reschedule,
                    assignment.id,
                    expected_state="waiting",
                    expected_active_attempt_id=assignment.active_attempt_id,
                    next_check_at=next_check_at(assignment.created_at, now),
                    wait_reason=_truncate_reason(outcome),
                )
            except Exception:  # noqa: BLE001
                _logger.warning(
                    "Assignment wait reschedule failed for %s", assignment.id, exc_info=True
                )
            return
        if assignment.start_deadline is not None and assignment.start_deadline <= now:
            try:
                await asyncio.to_thread(
                    self._assignment_store.transition,
                    assignment.id,
                    from_state="waiting",
                    to_state="expired",
                    expected_active_attempt_id=None,
                    next_check_at=None,
                )
            except Exception:  # noqa: BLE001
                _logger.warning("Assignment expiry failed for %s", assignment.id, exc_info=True)
            return
        if assignment.resolved_binding_id is None:
            expected_pin: tuple[str, int | None] | None = None
        else:
            expected_pin = (
                assignment.resolved_binding_id,
                assignment.resolved_binding_revision,
            )
        try:
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
        except Exception:  # noqa: BLE001 - coordinator retry owns recovery.
            _logger.warning("Assignment claim failed for %s", assignment.id, exc_info=True)
            return
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
            binding = await asyncio.to_thread(
                self._binding_store.get_by_name,
                project_id=assignment.project_id,
                host_id=dest_host_id,
                name=assignment.binding_name,
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
        for host in hosts:
            host_id_value = host.host_id
            conn = self._host_registry.get(host_id_value)
            if conn is None or not host_supports_assignments(conn):
                continue
            binding = await asyncio.to_thread(
                self._binding_store.get_by_name,
                project_id=assignment.project_id,
                host_id=host_id_value,
                name=assignment.binding_name,
            )
            if binding is None or not binding.enabled:
                continue
            sources = await self._check_binding_and_inputs(assignment, host_id_value, binding)
            if isinstance(sources, str):
                return sources
            return _ClaimableDestination(
                host_id=host_id_value, conn=conn, binding=binding, sources=sources
            )
        return "no_eligible_host"

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
            bindings = await asyncio.to_thread(
                self._binding_store.list_by_host,
                project_id=assignment.project_id,
                host_id=host_id,
            )
            matches = [
                candidate
                for candidate in bindings
                if candidate.enabled and candidate.repository_id == repo.id
            ]
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
        try:
            await asyncio.to_thread(
                self._assignment_store.update_attempt,
                assignment.id,
                attempt.id,
                state="finished",
                ended_at=failure_now,
                error_code=error_code,
            )
        except Exception:  # noqa: BLE001 - coordinator retry owns recovery.
            _logger.warning(
                "Assignment attempt finish failed for %s", assignment.id, exc_info=True
            )
            return
        try:
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
        except Exception:  # noqa: BLE001 - coordinator retry owns recovery.
            _logger.warning(
                "Assignment return to waiting failed for %s", assignment.id, exc_info=True
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
        # Interrupted rows keep the attempt link so /retry and /cancel can
        # read the ended attempt through active_attempt_id.
        now = int(time.time())
        try:
            await asyncio.to_thread(
                self._assignment_store.update_attempt,
                assignment.id,
                attempt.id,
                state="lost",
                ended_at=now,
                error_code=stage,
            )
        except Exception:  # noqa: BLE001
            _logger.warning("Assignment attempt loss failed for %s", assignment.id, exc_info=True)
            return
        try:
            await asyncio.to_thread(
                self._assignment_store.transition,
                assignment.id,
                from_state="starting",
                to_state="interrupted",
                expected_active_attempt_id=attempt.id,
                wait_reason=_truncate_reason(f"{stage}: {message}"),
                next_check_at=None,
            )
        except Exception:  # noqa: BLE001
            _logger.warning("Assignment interrupt failed for %s", assignment.id, exc_info=True)

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
        conversation_id = hashlib.sha256(f"assignment-attempt:{attempt.id}".encode()).hexdigest()[
            :32
        ]
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
        try:
            updated = await asyncio.to_thread(
                self._assignment_store.transition,
                assignment.id,
                from_state="starting",
                to_state="running",
                expected_active_attempt_id=attempt.id,
                next_check_at=next_check_at(assignment.created_at, int(time.time())),
            )
        except Exception:  # noqa: BLE001
            _logger.warning(
                "Assignment run transition failed for %s", assignment.id, exc_info=True
            )
            return
        if updated is None:
            _logger.warning(
                "Assignment %s left starting by another writer; leaving for reconcile",
                assignment.id,
            )
