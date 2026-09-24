"""Tests for the assignment coordinator.

Uses real SQLAlchemy stores for assignment, project, repository, binding
and conversation rows, with fakes for the host registry, host listing,
permissions and the runner placement seam.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from omnigent.db.utils import now_epoch
from omnigent.entities import Assignment, AssignmentInputEntry
from omnigent.host.frames import HostAssignmentPrepareResultFrame
from omnigent.server import assignments as assignments_mod
from omnigent.server.assignment_host import AssignmentHostUnavailableError
from omnigent.server.assignments import AssignmentCoordinator, next_check_at
from omnigent.server.auth import LEVEL_OWNER
from omnigent.stores.assignment_store.sqlalchemy_store import SqlAlchemyAssignmentStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.project_host_binding_store.sqlalchemy_store import (
    SqlAlchemyProjectHostBindingStore,
)
from omnigent.stores.project_repository_store.sqlalchemy_store import (
    SqlAlchemyProjectRepositoryStore,
)
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore

pytestmark = [pytest.mark.asyncio]

ALICE = "alice@example.com"
AGENT_ID = "087b7cb7ac30abf4debfaa578d052ec6"


@pytest.fixture(autouse=True)
def _clear_intentional_stop_markers() -> Iterator[None]:
    yield
    from omnigent.server.routes._sessions.common import _intentional_stop_sessions

    _intentional_stop_sessions.clear()


def _uid(seed: str) -> str:
    return uuid.uuid5(uuid.NAMESPACE_DNS, seed).hex


def _input(
    name: str = "root",
    *,
    revision: int = 1,
    root: bool = True,
    commit: str = "a" * 40,
) -> AssignmentInputEntry:
    return AssignmentInputEntry(
        repository_name=name,
        repository_revision=revision,
        remote_url="https://example.com/org/repo.git",
        input_commit=commit,
        input_ref=f"refs/omnigent/assignments/x/input/{name}",
        context_manifest_path=".agents/project/manifest.json",
        manifest_digest="d" * 64,
        artifact_paths=[],
        is_execution_root=root,
    )


@dataclass
class _FakeHost:
    host_id: str
    user_id: str


class FakeHostStore:
    def __init__(self, hosts: list[_FakeHost] | None = None) -> None:
        self._hosts = hosts or []

    def list_hosts(self, owner: str) -> list[_FakeHost]:
        return [h for h in self._hosts if h.user_id == owner]

    def get_host(self, host_id: str) -> _FakeHost | None:
        for host in self._hosts:
            if host.host_id == host_id:
                return host
        return None


class FakeHostRegistry:
    def __init__(self, conns: dict[str, Any] | None = None) -> None:
        self._conns = conns or {}

    def get(self, host_id: str, workspace_id: int | None = None) -> Any | None:
        return self._conns.get(host_id)


def _conn(host_id: str, *, owner: str | None, assignments: bool = True) -> Any:
    return SimpleNamespace(
        host_id=host_id,
        owner=owner,
        hello=SimpleNamespace(assignments=assignments),
        pending_assignment_prepares={},
        pending_assignment_releases={},
    )


class FakePermissionStore:
    def __init__(self) -> None:
        self.ensured: list[str] = []
        self.grants: list[tuple[str, str, int]] = []
        self.order: list[str] = []

    def ensure_user(self, user_id: str, *, is_admin: bool = False) -> None:
        self.ensured.append(user_id)

    def grant(self, user_id: str, conversation_id: str, level: int) -> None:
        self.grants.append((user_id, conversation_id, level))
        self.order.append("grant")


def _stores(db_uri: str) -> dict[str, Any]:
    return {
        "assignment": SqlAlchemyAssignmentStore(db_uri),
        "project": SqlAlchemyProjectStore(db_uri),
        "repository": SqlAlchemyProjectRepositoryStore(db_uri),
        "binding": SqlAlchemyProjectHostBindingStore(db_uri),
        "conversation": SqlAlchemyConversationStore(db_uri),
    }


def _make_project(
    project_store: SqlAlchemyProjectStore,
    project_id: str,
    *,
    owner: str | None = ALICE,
    enabled: bool = True,
) -> None:
    project_store.create(project_id, f"P-{project_id[:6]}", owner)
    if enabled:
        project_store.set_collaboration(
            project_id, user_id=owner, enabled=True, expected_revision=0
        )


def _make_repo(
    repository_store: SqlAlchemyProjectRepositoryStore,
    project_id: str,
    name: str = "root",
) -> Any:
    return repository_store.upsert(
        project_id=project_id,
        name=name,
        remote_url="https://example.com/org/repo.git",
        default_branch="main",
    )


def _make_binding(
    binding_store: SqlAlchemyProjectHostBindingStore,
    *,
    project_id: str,
    host_id: str,
    repo_id: str,
    workspace: str = "/work/p",
    name: str = "primary",
    enabled: bool = True,
    is_primary: bool | None = None,
) -> Any:
    return binding_store.upsert(
        project_id=project_id,
        host_id=host_id,
        name=name,
        repository_id=repo_id,
        workspace=workspace,
        enabled=enabled,
        is_primary=name == "primary" if is_primary is None else is_primary,
    )


def _seed_waiting(
    assignment_store: SqlAlchemyAssignmentStore,
    seed: str,
    *,
    project_id: str,
    owner: str | None = ALICE,
    requested_host_id: str | None = None,
    task: str = "Do the thing",
    inputs: list[AssignmentInputEntry] | None = None,
    start_deadline: int | None = None,
    model_override: str | None = None,
    harness_override: str | None = None,
    binding_name: str = "primary",
) -> Assignment:
    created = assignment_store.create(
        Assignment(
            id=_uid(f"{seed}-id"),
            project_id=project_id,
            source_session_id=_uid(f"{seed}-sess"),
            target_agent_id=AGENT_ID,
            task=task,
            inputs=inputs or [_input()],
            idempotency_key=f"key-{seed}",
            request_digest="e" * 64,
            owner_user_id=owner,
            requested_host_id=requested_host_id,
            binding_name=binding_name,
            model_override=model_override,
            harness_override=harness_override,
            start_deadline=start_deadline,
        )
    )
    moved = assignment_store.transition(
        created.id,
        from_state="preparing",
        to_state="waiting",
        next_check_at=now_epoch(),
    )
    assert moved is not None
    return moved


class FakeRunnerRouter:
    def __init__(self, online: set[str] | None = None) -> None:
        self._online = set(online or set())

    def runner_is_online(self, runner_id: str) -> bool:
        return runner_id in self._online


class FakeExitReports:
    def __init__(self, reports: dict[str, str] | None = None) -> None:
        self._reports = dict(reports or {})

    def get(self, runner_id: str) -> str | None:
        return self._reports.get(runner_id)


def _coordinator(
    stores: dict[str, Any],
    *,
    registry: FakeHostRegistry,
    host_store: FakeHostStore,
    permission_store: FakePermissionStore,
    scan_interval_seconds: float = 3600.0,
    due_batch_limit: int = 50,
    runner_router: Any | None = None,
    runner_exit_reports: Any | None = None,
    runner_session_initializer: Any | None = None,
    agent_store: Any | None = None,
    agent_cache: Any | None = None,
) -> AssignmentCoordinator:
    return AssignmentCoordinator(
        assignment_store=stores["assignment"],
        project_store=stores["project"],
        repository_store=stores["repository"],
        binding_store=stores["binding"],
        host_store=host_store,
        host_registry=registry,
        conversation_store=stores["conversation"],
        permission_store=permission_store,
        runner_router=runner_router if runner_router is not None else FakeRunnerRouter(),
        tunnel_registry=SimpleNamespace(),
        runner_exit_reports=runner_exit_reports
        if runner_exit_reports is not None
        else FakeExitReports(),
        file_store=SimpleNamespace(),
        artifact_store=SimpleNamespace(),
        scan_interval_seconds=scan_interval_seconds,
        due_batch_limit=due_batch_limit,
        runner_session_initializer=runner_session_initializer,
        agent_store=agent_store,
        agent_cache=agent_cache,
    )


def _install_placement_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    order: list[str] | None = None,
    dispatch_texts: list[str] | None = None,
    launch_error: str | None = None,
    wait_none: bool = False,
    dispatch_raises: bool = False,
    init_bodies: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    import omnigent.server.routes._host_launch as host_launch
    import omnigent.server.routes.sessions as sessions_routes
    from omnigent.server.routes._sessions.helpers import _SessionEventDispatchResult

    captured: dict[str, Any] = {}

    class _ReadyStreamResponse:
        """Runner SSE stream that goes ready on its first heartbeat, then ends."""

        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *_exc: Any) -> None:
            return None

        def raise_for_status(self) -> None:
            return None

        async def aiter_text(self) -> Any:
            yield 'data: {"type": "session.heartbeat"}\n\n'
            yield "data: [DONE]\n\n"

    class _RecordingRunnerClient:
        """Stand-in runner client capturing the real init handshake body."""

        def stream(self, _method: str, _path: str, **_kwargs: Any) -> Any:
            # Placement waits for the relay's ready heartbeat before it
            # dispatches, so the fake client has to answer GET /stream — an
            # exception here reads as "relay exited before becoming ready".
            return _ReadyStreamResponse()

        async def post(self, _path: str, **kwargs: Any) -> Any:
            if "json" in kwargs:
                assert init_bodies is not None
                init_bodies.append(kwargs["json"])

            class _Ok:
                status_code = 200

                def raise_for_status(self) -> None:
                    return None

                def json(self) -> dict[str, Any]:
                    return {}

            return _Ok()

    def _resolve_host_launch(**kwargs: Any) -> Any:
        if order is not None:
            order.append("resolve")
        captured["resolve_user"] = kwargs.get("user_id")
        conv = kwargs["conversation_store"].get_conversation(kwargs["session_id"])
        assert conv is not None
        return SimpleNamespace(
            host=SimpleNamespace(), conn=kwargs["host_registry"].get(kwargs["host_id"]), conv=conv
        )

    async def _launch_runner_on_host(
        conv: Any, conversation_store: Any, host_registry: Any, conn: Any
    ) -> Any:
        captured["launch_conv"] = conv
        if launch_error is not None:
            return SimpleNamespace(error=launch_error, runner_id="runner_1")
        return SimpleNamespace(error=None, runner_id="runner_1")

    async def _wait_for_runner_client(*args: Any, **kwargs: Any) -> Any:
        if wait_none:
            return None
        if init_bodies is not None:
            return _RecordingRunnerClient()
        return object()

    async def _ensure_runner_session_initialized(*args: Any, **kwargs: Any) -> bool:
        captured["ensure_require_success"] = kwargs.get("require_success")
        captured["ensure_initializer"] = kwargs.get("initializer")
        return False

    async def _dispatch_session_event_to_runner(*args: Any, **kwargs: Any) -> Any:
        captured["dispatch_calls"] = captured.get("dispatch_calls", 0) + 1
        captured["dispatch_native_ready"] = kwargs.get("native_terminal_ready")
        if dispatch_texts is not None:
            body = args[2] if len(args) > 2 else kwargs.get("body")
            data = getattr(body, "data", {})
            content = (data.get("content") or [{}])[0].get("text", "")
            dispatch_texts.append(content)
        if dispatch_raises:
            raise RuntimeError("dispatch boom")
        return _SessionEventDispatchResult(item_id="item_x", pending_id=None)

    monkeypatch.setattr(host_launch, "resolve_host_launch", _resolve_host_launch)
    monkeypatch.setattr(sessions_routes, "_launch_runner_on_host", _launch_runner_on_host)
    monkeypatch.setattr(sessions_routes, "_wait_for_runner_client", _wait_for_runner_client)
    if init_bodies is None:
        monkeypatch.setattr(
            sessions_routes,
            "_ensure_runner_session_initialized",
            _ensure_runner_session_initialized,
        )
    monkeypatch.setattr(
        sessions_routes,
        "_dispatch_session_event_to_runner",
        _dispatch_session_event_to_runner,
    )
    return captured


def _prepare_ok(directories: dict[str, str]) -> Any:
    async def _fake(**kwargs: Any) -> HostAssignmentPrepareResultFrame:
        frame = kwargs.get("frame")
        _fake.captured = frame  # type: ignore[attr-defined]
        _fake.calls = getattr(_fake, "calls", 0) + 1  # type: ignore[attr-defined]
        return HostAssignmentPrepareResultFrame(
            request_id=frame.request_id,
            status="ok",
            directories=dict(directories),
        )

    _fake.calls = 0  # type: ignore[attr-defined]
    return _fake


# ── 1. backoff ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_next_check_at_values() -> None:
    """Clamp(age/4, 15, 3600) added to now."""
    assert next_check_at(1000, 1000) == 1015
    assert next_check_at(600, 1000) == 1100
    assert next_check_at(1000 - 36000, 1000) == 1000 + 3600


# ── 2. happy path ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_happy_path_places_session_and_dispatches_once(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prepare carries workspaces verbatim; placement grants, creates and prompts."""
    stores = _stores(db_uri)
    host_id = _uid("host-a")
    project_id = _uid("happy-proj")
    _make_project(stores["project"], project_id, owner=ALICE, enabled=True)
    repo = _make_repo(stores["repository"], project_id, "root")
    _make_binding(
        stores["binding"],
        project_id=project_id,
        host_id=host_id,
        repo_id=repo.id,
        workspace=r"C:\work\p",
    )
    assignment = _seed_waiting(
        stores["assignment"],
        "happy",
        project_id=project_id,
        owner=ALICE,
        requested_host_id=host_id,
        task="Fix the widget\nsecond line",
        inputs=[_input("root", revision=repo.revision)],
        model_override="model-x",
        harness_override="harness-y",
    )
    prepared_dir = r"C:\work\p\.omnigent\worktrees\happy\root"
    fake_prepare = _prepare_ok({"root": prepared_dir})
    monkeypatch.setattr(assignments_mod, "prepare_assignment_on_host", fake_prepare)
    order: list[str] = []
    dispatch_texts: list[str] = []
    orig_grant = FakePermissionStore.grant

    perms = FakePermissionStore()

    def _grant_and_mark(self: FakePermissionStore, user_id: str, conv_id: str, level: int) -> None:
        orig_grant(self, user_id, conv_id, level)
        order.append("grant")

    monkeypatch.setattr(FakePermissionStore, "grant", _grant_and_mark)
    captured = _install_placement_fakes(monkeypatch, order=order, dispatch_texts=dispatch_texts)

    async def _dispatch_assert_bound(*args: Any, **kwargs: Any) -> Any:
        from omnigent.server.routes._sessions.helpers import _SessionEventDispatchResult

        captured["dispatch_calls"] = captured.get("dispatch_calls", 0) + 1
        captured["dispatch_native_ready"] = kwargs.get("native_terminal_ready")
        body = args[2] if len(args) > 2 else kwargs.get("body")
        data = getattr(body, "data", {})
        content = (data.get("content") or [{}])[0].get("text", "")
        dispatch_texts.append(content)
        conv_id = args[0] if args else kwargs.get("session_id")
        row = stores["assignment"].get(assignment.id)
        assert row is not None and row.active_attempt_id is not None
        attempt = stores["assignment"].get_attempt(assignment.id, row.active_attempt_id)
        assert attempt is not None
        assert attempt.session_id == conv_id
        assert attempt.runner_id == "runner_1"
        return _SessionEventDispatchResult(item_id="item_x", pending_id=None)

    import omnigent.server.routes.sessions as sessions_routes

    monkeypatch.setattr(
        sessions_routes, "_dispatch_session_event_to_runner", _dispatch_assert_bound
    )

    registry = FakeHostRegistry({host_id: _conn(host_id, owner=ALICE, assignments=True)})
    host_store = FakeHostStore([_FakeHost(host_id, ALICE)])
    coordinator = _coordinator(
        stores, registry=registry, host_store=host_store, permission_store=perms
    )
    coordinator.trigger(assignment.id)
    await coordinator.wait_for_idle()
    await coordinator.shutdown()

    frame = fake_prepare.captured  # type: ignore[attr-defined]
    assert frame.assignment_id == assignment.id
    # No project entry on this host: the frame carries none.
    assert frame.entry is None
    assert [r.repository_name for r in frame.repositories] == ["root"]
    assert frame.repositories[0].source_directory == r"C:\work\p"
    assert frame.repositories[0].remote_url == "https://example.com/org/repo.git"
    assert frame.repositories[0].input_commit == "a" * 40
    assert frame.repositories[0].input_ref == "refs/omnigent/assignments/x/input/root"
    assert frame.repositories[0].context_manifest_path == ".agents/project/manifest.json"
    assert frame.repositories[0].manifest_digest == "d" * 64

    row = stores["assignment"].get(assignment.id)
    assert row is not None
    assert row.state == "running"
    assert row.wait_reason is None
    assert row.active_attempt_id is not None
    attempt = stores["assignment"].get_attempt(assignment.id, row.active_attempt_id)
    assert attempt is not None
    assert attempt.event_dispatched_at is not None
    assert attempt.runner_id == "runner_1"

    expected_conv = hashlib.sha256(f"assignment-attempt:{attempt.id}".encode()).hexdigest()[:32]
    assert attempt.session_id == expected_conv
    conv = stores["conversation"].get_conversation(expected_conv)
    assert conv is not None
    assert conv.workspace == prepared_dir
    # No project entry on this host (R-ASSIGN): the execution root is both
    # the launch directory and the recorded worktree, unchanged from
    # pre-XHO04 behavior.
    assert conv.worktree == prepared_dir
    assert conv.host_id == host_id
    assert conv.project_id == project_id
    assert conv.title == "Assignment: Fix the widget"
    assert conv.model_override == "model-x"
    assert conv.harness_override == "harness-y"

    assert perms.grants and perms.grants[0] == (ALICE, expected_conv, LEVEL_OWNER)
    assert order.index("grant") < order.index("resolve")
    assert captured.get("dispatch_calls") == 1
    assert captured.get("ensure_require_success") is True
    text = dispatch_texts[0]
    assert assignment.id in text and attempt.id in text
    assert "Fix the widget" in text
    assert "root" in text and prepared_dir in text
    assert "(execution root)" in text
    assert f"{prepared_dir}/.agents/project/manifest.json" in text
    assert "sys_assignment_complete" in text
    assert f'sys_assignment_complete with assignment_id "{assignment.id}"' in text
    assert "`outputs`" in text and "`summary`" in text
    assert 'attempt_id "' not in text


@pytest.mark.asyncio
async def test_entry_within_boundary_redirects_launch_to_entry(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-ASSIGN Scenario 12: an entry passing the boundary check becomes the
    launch directory; the execution root is recorded as the worktree."""
    stores = _stores(db_uri)
    host_id = _uid("host-entry")
    project_id = _uid("entry-proj")
    _make_project(stores["project"], project_id, owner=ALICE, enabled=True)
    repo = _make_repo(stores["repository"], project_id, "root")
    _make_binding(
        stores["binding"],
        project_id=project_id,
        host_id=host_id,
        repo_id=repo.id,
        workspace="/entry/checkout",
    )
    entry_path = "/entry"
    stores["binding"].put_entry(project_id, host_id, entry_path)
    assignment = _seed_waiting(
        stores["assignment"],
        "entry-redirect",
        project_id=project_id,
        owner=ALICE,
        requested_host_id=host_id,
        inputs=[_input("root", revision=repo.revision)],
    )
    prepared_dir = "/entry/.omnigent/worktrees/entry-redirect/root"
    fake_prepare = _prepare_ok({"root": prepared_dir})
    monkeypatch.setattr(assignments_mod, "prepare_assignment_on_host", fake_prepare)

    async def _fake_validate_ok(**kwargs: Any) -> str:
        return kwargs["workspace"]

    async def _fake_spec_cwd(_agent_id: str | None) -> str | None:
        return None

    monkeypatch.setattr(assignments_mod, "validate_workspace", _fake_validate_ok)
    _install_placement_fakes(monkeypatch)
    registry = FakeHostRegistry({host_id: _conn(host_id, owner=ALICE, assignments=True)})
    host_store = FakeHostStore([_FakeHost(host_id, ALICE)])
    perms = FakePermissionStore()
    coordinator = _coordinator(
        stores,
        registry=registry,
        host_store=host_store,
        permission_store=perms,
        agent_store=SimpleNamespace(),
        agent_cache=SimpleNamespace(),
    )
    coordinator._resolve_target_agent_spec_cwd = _fake_spec_cwd  # type: ignore[method-assign]
    coordinator.trigger(assignment.id)
    await coordinator.wait_for_idle()
    await coordinator.shutdown()

    row = stores["assignment"].get(assignment.id)
    assert row is not None and row.state == "running", row
    assert row.active_attempt_id is not None
    attempt = stores["assignment"].get_attempt(assignment.id, row.active_attempt_id)
    assert attempt is not None and attempt.session_id is not None
    conv = stores["conversation"].get_conversation(attempt.session_id)
    assert conv is not None
    assert conv.workspace == entry_path
    assert conv.worktree == prepared_dir
    # The prepare frame carries the entry so the host nests the root under it.
    assert fake_prepare.captured.entry == entry_path  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_entry_outside_boundary_launches_at_execution_root(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-ASSIGN: an entry that fails the boundary check is not used — the
    session launches at the execution root, as if there were no entry."""
    from omnigent.server.routes._workspace_validation import WorkspaceValidationError

    stores = _stores(db_uri)
    host_id = _uid("host-entry-out")
    project_id = _uid("entry-out-proj")
    _make_project(stores["project"], project_id, owner=ALICE, enabled=True)
    repo = _make_repo(stores["repository"], project_id, "root")
    _make_binding(
        stores["binding"],
        project_id=project_id,
        host_id=host_id,
        repo_id=repo.id,
        workspace="/entry/checkout",
    )
    entry_path = "/entry"
    stores["binding"].put_entry(project_id, host_id, entry_path)
    assignment = _seed_waiting(
        stores["assignment"],
        "entry-out",
        project_id=project_id,
        owner=ALICE,
        requested_host_id=host_id,
        inputs=[_input("root", revision=repo.revision)],
    )
    prepared_dir = "/entry/.omnigent/worktrees/entry-out/root"
    fake_prepare = _prepare_ok({"root": prepared_dir})
    monkeypatch.setattr(assignments_mod, "prepare_assignment_on_host", fake_prepare)

    async def _fake_validate_fails(**_kwargs: Any) -> str:
        raise WorkspaceValidationError("outside the agent's boundary")

    async def _fake_spec_cwd(_agent_id: str | None) -> str | None:
        return None

    monkeypatch.setattr(assignments_mod, "validate_workspace", _fake_validate_fails)
    _install_placement_fakes(monkeypatch)
    registry = FakeHostRegistry({host_id: _conn(host_id, owner=ALICE, assignments=True)})
    host_store = FakeHostStore([_FakeHost(host_id, ALICE)])
    perms = FakePermissionStore()
    coordinator = _coordinator(
        stores,
        registry=registry,
        host_store=host_store,
        permission_store=perms,
        agent_store=SimpleNamespace(),
        agent_cache=SimpleNamespace(),
    )
    coordinator._resolve_target_agent_spec_cwd = _fake_spec_cwd  # type: ignore[method-assign]
    coordinator.trigger(assignment.id)
    await coordinator.wait_for_idle()
    await coordinator.shutdown()

    row = stores["assignment"].get(assignment.id)
    assert row is not None and row.state == "running", row
    assert row.active_attempt_id is not None
    attempt = stores["assignment"].get_attempt(assignment.id, row.active_attempt_id)
    assert attempt is not None and attempt.session_id is not None
    conv = stores["conversation"].get_conversation(attempt.session_id)
    assert conv is not None
    assert conv.workspace == prepared_dir
    assert conv.worktree == prepared_dir


@pytest.mark.asyncio
async def test_place_spec_load_failure_launches_at_execution_root(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A spec store/cache error is a failed boundary, never a failed placement.

    The entry exists, but loading the target agent's spec raises: the
    session must still be placed, at the execution root, exactly as when
    the entry fails the boundary check.
    """
    stores = _stores(db_uri)
    host_id = _uid("host-spec-fail")
    project_id = _uid("spec-fail-proj")
    _make_project(stores["project"], project_id, owner=ALICE, enabled=True)
    repo = _make_repo(stores["repository"], project_id, "root")
    _make_binding(
        stores["binding"],
        project_id=project_id,
        host_id=host_id,
        repo_id=repo.id,
        workspace="/entry/checkout",
    )
    entry_path = "/entry"
    stores["binding"].put_entry(project_id, host_id, entry_path)
    assignment = _seed_waiting(
        stores["assignment"],
        "spec-fail",
        project_id=project_id,
        owner=ALICE,
        requested_host_id=host_id,
        inputs=[_input("root", revision=repo.revision)],
    )
    prepared_dir = "/entry/.worktrees/myrepo/spec-fail"
    fake_prepare = _prepare_ok({"root": prepared_dir})
    monkeypatch.setattr(assignments_mod, "prepare_assignment_on_host", fake_prepare)

    async def _must_not_validate(**_kwargs: Any) -> str:
        raise AssertionError("validate_workspace must not run after a spec-load failure")

    monkeypatch.setattr(assignments_mod, "validate_workspace", _must_not_validate)
    _install_placement_fakes(monkeypatch)
    registry = FakeHostRegistry({host_id: _conn(host_id, owner=ALICE, assignments=True)})
    host_store = FakeHostStore([_FakeHost(host_id, ALICE)])
    perms = FakePermissionStore()
    coordinator = _coordinator(
        stores,
        registry=registry,
        host_store=host_store,
        permission_store=perms,
        agent_store=SimpleNamespace(),
        agent_cache=SimpleNamespace(),
    )

    async def _boom(_agent_id: str | None) -> str | None:
        raise RuntimeError("agent store down")

    coordinator._resolve_target_agent_spec_cwd = _boom  # type: ignore[method-assign]
    coordinator.trigger(assignment.id)
    await coordinator.wait_for_idle()
    await coordinator.shutdown()

    row = stores["assignment"].get(assignment.id)
    assert row is not None and row.state == "running", row
    assert row.active_attempt_id is not None
    attempt = stores["assignment"].get_attempt(assignment.id, row.active_attempt_id)
    assert attempt is not None and attempt.session_id is not None
    conv = stores["conversation"].get_conversation(attempt.session_id)
    assert conv is not None
    assert conv.workspace == prepared_dir
    assert conv.worktree == prepared_dir


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", [True, False])
async def test_placement_initializes_receiver_with_flag(
    db_uri: str, monkeypatch: pytest.MonkeyPatch, flag: bool
) -> None:
    """Placement's own init handshake carries the initializer's flag value."""
    from omnigent.server.runner_session_init import RunnerSessionInitializer

    stores = _stores(db_uri)
    host_id = _uid("host-flag")
    project_id = _uid("flag-proj")
    _make_project(stores["project"], project_id, owner=ALICE, enabled=True)
    repo = _make_repo(stores["repository"], project_id, "root")
    _make_binding(
        stores["binding"],
        project_id=project_id,
        host_id=host_id,
        repo_id=repo.id,
        workspace="/work/flag",
    )
    assignment = _seed_waiting(
        stores["assignment"],
        "flag",
        project_id=project_id,
        owner=ALICE,
        requested_host_id=host_id,
        inputs=[_input("root", revision=repo.revision)],
    )
    monkeypatch.setattr(
        assignments_mod, "prepare_assignment_on_host", _prepare_ok({"root": "/prepared/flag"})
    )
    init_bodies: list[dict[str, Any]] = []
    _install_placement_fakes(monkeypatch, init_bodies=init_bodies)
    import omnigent.server.routes.sessions as sessions_routes

    _fake_launch = sessions_routes._launch_runner_on_host

    async def _launch_and_bind(conv: Any, conversation_store: Any, *args: Any) -> Any:
        launched = await _fake_launch(conv, conversation_store, *args)
        if launched.error is None:
            conversation_store.set_runner_id(conv.id, launched.runner_id)
        return launched

    monkeypatch.setattr(sessions_routes, "_launch_runner_on_host", _launch_and_bind)
    initializer = RunnerSessionInitializer(
        FakeHostRegistry(),
        server_version="0.6.0.dev0",
        project_assignments_enabled=flag,
    )
    coordinator = _coordinator(
        stores,
        registry=FakeHostRegistry({host_id: _conn(host_id, owner=ALICE, assignments=True)}),
        host_store=FakeHostStore([_FakeHost(host_id, ALICE)]),
        permission_store=FakePermissionStore(),
        runner_session_initializer=initializer,
    )
    coordinator.trigger(assignment.id)
    await coordinator.wait_for_idle()
    await coordinator.shutdown()

    assert init_bodies, "placement skipped the runner session-init handshake"
    snapshot = init_bodies[0]["session_init"]["snapshot"]
    assert snapshot["project_assignments_enabled"] is flag
    row = stores["assignment"].get(assignment.id)
    assert row is not None and row.state == "running"


# ── 3. scenario 3: offline past deadline ──────────────────────────────────


@pytest.mark.asyncio
async def test_offline_past_deadline_expires_with_host_reason(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A named offline host past its deadline expires naming that host."""
    stores = _stores(db_uri)
    requested = _uid("host-off")
    other = _uid("host-on")
    project_id = _uid("exp-proj")
    _make_project(stores["project"], project_id, owner=ALICE, enabled=True)
    repo = _make_repo(stores["repository"], project_id, "root")
    _make_binding(
        stores["binding"], project_id=project_id, host_id=other, repo_id=repo.id, workspace="/w"
    )
    past = now_epoch() - 10
    assignment = _seed_waiting(
        stores["assignment"],
        "expired",
        project_id=project_id,
        owner=ALICE,
        requested_host_id=requested,
        start_deadline=past,
        inputs=[_input("root", revision=repo.revision)],
    )

    async def _no_prepare(**kwargs: Any) -> Any:
        raise AssertionError("prepare must not run for an expired row")

    monkeypatch.setattr(assignments_mod, "prepare_assignment_on_host", _no_prepare)
    registry = FakeHostRegistry({other: _conn(other, owner=ALICE, assignments=True)})
    host_store = FakeHostStore([_FakeHost(other, ALICE), _FakeHost(requested, ALICE)])
    coordinator = _coordinator(
        stores, registry=registry, host_store=host_store, permission_store=FakePermissionStore()
    )
    coordinator.trigger(assignment.id)
    await coordinator.wait_for_idle()
    await coordinator.shutdown()

    row = stores["assignment"].get(assignment.id)
    assert row is not None
    assert row.state == "expired"
    assert row.wait_reason == f"host_offline:{requested}"
    assert row.active_attempt_id is None


# ── 4. waiting reasons ────────────────────────────────────────────────────


def _waiting_reason_setup(
    db_uri: str, kind: str
) -> tuple[dict[str, Any], Assignment, FakeHostRegistry, FakeHostStore, str]:
    stores = _stores(db_uri)
    host_id = _uid("host-a")
    project_id = _uid(f"reason-{kind}-proj")
    enabled = kind != "collaboration_disabled"
    _make_project(stores["project"], project_id, owner=ALICE, enabled=enabled)
    repo = _make_repo(stores["repository"], project_id, "root")
    inputs: list[AssignmentInputEntry] = [_input("root", revision=repo.revision)]
    requested: str | None = host_id
    registry_conns: dict[str, Any] = {}
    hosts = [_FakeHost(host_id, ALICE)]
    expected = ""
    if kind == "collaboration_disabled":
        expected = "collaboration_disabled"
        registry_conns[host_id] = _conn(host_id, owner=ALICE, assignments=True)
        _make_binding(
            stores["binding"],
            project_id=project_id,
            host_id=host_id,
            repo_id=repo.id,
            workspace="/w",
        )
    elif kind == "host_offline":
        expected = f"host_offline:{host_id}"
    elif kind == "host_unsupported":
        expected = f"host_unsupported:{host_id}"
        registry_conns[host_id] = _conn(host_id, owner=ALICE, assignments=False)
        _make_binding(
            stores["binding"],
            project_id=project_id,
            host_id=host_id,
            repo_id=repo.id,
            workspace="/w",
        )
    elif kind == "binding_missing":
        expected = "binding_missing:primary"
        registry_conns[host_id] = _conn(host_id, owner=ALICE, assignments=True)
    elif kind == "repository_changed":
        expected = "repository_changed:root"
        registry_conns[host_id] = _conn(host_id, owner=ALICE, assignments=True)
        _make_binding(
            stores["binding"],
            project_id=project_id,
            host_id=host_id,
            repo_id=repo.id,
            workspace="/w",
        )
        stores["repository"].upsert(
            project_id=project_id,
            name="root",
            remote_url="https://example.com/org/other.git",
            default_branch="main",
        )
    elif kind == "second_binding_missing":
        expected = "binding_missing:extra"
        second = _make_repo(stores["repository"], project_id, "extra")
        inputs = [
            _input("root", revision=repo.revision),
            _input("extra", revision=second.revision, root=False),
        ]
        registry_conns[host_id] = _conn(host_id, owner=ALICE, assignments=True)
        _make_binding(
            stores["binding"],
            project_id=project_id,
            host_id=host_id,
            repo_id=repo.id,
            workspace="/w",
        )
    else:  # pragma: no cover
        raise AssertionError(kind)
    assignment = _seed_waiting(
        stores["assignment"],
        f"reason-{kind}",
        project_id=project_id,
        owner=ALICE,
        requested_host_id=requested,
        inputs=inputs,
    )
    return stores, assignment, FakeHostRegistry(registry_conns), FakeHostStore(hosts), expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind",
    [
        "collaboration_disabled",
        "host_offline",
        "host_unsupported",
        "binding_missing",
        "repository_changed",
        "second_binding_missing",
    ],
)
async def test_waiting_reasons_leave_row_waiting(
    db_uri: str, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """Each blocking prerequisite waits visibly without claiming."""
    stores, assignment, registry, host_store, expected = _waiting_reason_setup(db_uri, kind)

    async def _no_prepare(**kwargs: Any) -> Any:
        raise AssertionError("prepare must not run while blocked")

    monkeypatch.setattr(assignments_mod, "prepare_assignment_on_host", _no_prepare)
    before = int(time.time())
    coordinator = _coordinator(
        stores, registry=registry, host_store=host_store, permission_store=FakePermissionStore()
    )
    coordinator.trigger(assignment.id)
    await coordinator.wait_for_idle()
    await coordinator.shutdown()
    after = int(time.time())

    row = stores["assignment"].get(assignment.id)
    assert row is not None
    assert row.state == "waiting"
    assert row.wait_reason == expected
    assert row.active_attempt_id is None
    assert row.next_check_at is not None
    low = next_check_at(row.created_at, before)
    high = next_check_at(row.created_at, after)
    assert low <= row.next_check_at <= high


# ── 5. prepare failures ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_prepare_failed_returns_to_waiting(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host-reported failure waits naming the path; nothing is created."""
    stores = _stores(db_uri)
    host_id = _uid("host-a")
    project_id = _uid("prep-fail-proj")
    _make_project(stores["project"], project_id, owner=ALICE, enabled=True)
    repo = _make_repo(stores["repository"], project_id, "root")
    _make_binding(
        stores["binding"], project_id=project_id, host_id=host_id, repo_id=repo.id, workspace="/w"
    )
    assignment = _seed_waiting(
        stores["assignment"],
        "prep-fail",
        project_id=project_id,
        owner=ALICE,
        requested_host_id=host_id,
        inputs=[_input("root", revision=repo.revision)],
    )

    async def _failed(**kwargs: Any) -> HostAssignmentPrepareResultFrame:
        frame = kwargs.get("frame")
        return HostAssignmentPrepareResultFrame(
            request_id=frame.request_id,
            status="failed",
            directories={},
            error_code="context_missing",
            error="required context missing: root:AGENTS.md",
            repository_name="root",
        )

    monkeypatch.setattr(assignments_mod, "prepare_assignment_on_host", _failed)
    _install_placement_fakes(monkeypatch)
    coordinator = _coordinator(
        stores,
        registry=FakeHostRegistry({host_id: _conn(host_id, owner=ALICE, assignments=True)}),
        host_store=FakeHostStore([_FakeHost(host_id, ALICE)]),
        permission_store=FakePermissionStore(),
    )
    coordinator.trigger(assignment.id)
    await coordinator.wait_for_idle()
    await coordinator.shutdown()

    row = stores["assignment"].get(assignment.id)
    assert row is not None
    assert row.state == "waiting"
    assert row.wait_reason is not None and "AGENTS.md" in row.wait_reason
    assert row.active_attempt_id is None
    assert row.resolved_binding_id is not None
    # One finished attempt, no session bound, no conversation created.
    import sqlalchemy as _sa

    from omnigent.db.db_models import SqlAssignmentAttempt

    with stores["assignment"]._engine.connect() as conn:
        rows = list(
            conn.execute(
                _sa.select(SqlAssignmentAttempt).where(
                    SqlAssignmentAttempt.assignment_id == assignment.id
                )
            )
            .mappings()
            .all()
        )
    assert len(rows) == 1
    assert rows[0]["state"] == "finished"
    assert rows[0]["error_code"] == "context_missing"
    assert rows[0]["session_id"] is None
    derived = hashlib.sha256(f"assignment-attempt:{rows[0]['id']}".encode()).hexdigest()[:32]
    assert stores["conversation"].get_conversation(derived) is None


@pytest.mark.asyncio
async def test_prepare_unavailable_returns_to_waiting(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dropped prepare waits as host_unavailable."""
    stores = _stores(db_uri)
    host_id = _uid("host-a")
    project_id = _uid("prep-unavail-proj")
    _make_project(stores["project"], project_id, owner=ALICE, enabled=True)
    repo = _make_repo(stores["repository"], project_id, "root")
    _make_binding(
        stores["binding"], project_id=project_id, host_id=host_id, repo_id=repo.id, workspace="/w"
    )
    assignment = _seed_waiting(
        stores["assignment"],
        "prep-unavail",
        project_id=project_id,
        owner=ALICE,
        requested_host_id=host_id,
        inputs=[_input("root", revision=repo.revision)],
    )

    async def _boom(**kwargs: Any) -> Any:
        raise AssignmentHostUnavailableError("connection lost")

    monkeypatch.setattr(assignments_mod, "prepare_assignment_on_host", _boom)
    coordinator = _coordinator(
        stores,
        registry=FakeHostRegistry({host_id: _conn(host_id, owner=ALICE, assignments=True)}),
        host_store=FakeHostStore([_FakeHost(host_id, ALICE)]),
        permission_store=FakePermissionStore(),
    )
    coordinator.trigger(assignment.id)
    await coordinator.wait_for_idle()
    await coordinator.shutdown()

    row = stores["assignment"].get(assignment.id)
    assert row is not None
    assert row.state == "waiting"
    assert row.wait_reason == f"host_unavailable:{host_id}"


# ── 6. scenario 8: binding_changed then refresh ───────────────────────────


@pytest.mark.asyncio
async def test_binding_changed_then_refresh_repins(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pin survives a failed prepare; a moved binding waits until refreshed."""
    import sqlalchemy as _sa
    from sqlalchemy import func as _func

    from omnigent.db.db_models import SqlAssignmentAttempt

    stores = _stores(db_uri)
    host_id = _uid("host-a")
    project_id = _uid("changed-proj")
    _make_project(stores["project"], project_id, owner=ALICE, enabled=True)
    repo = _make_repo(stores["repository"], project_id, "root")
    binding = _make_binding(
        stores["binding"], project_id=project_id, host_id=host_id, repo_id=repo.id, workspace="/w"
    )
    assignment = _seed_waiting(
        stores["assignment"],
        "changed",
        project_id=project_id,
        owner=ALICE,
        requested_host_id=host_id,
        inputs=[_input("root", revision=repo.revision)],
    )

    async def _failed(**kwargs: Any) -> HostAssignmentPrepareResultFrame:
        frame = kwargs.get("frame")
        return HostAssignmentPrepareResultFrame(
            request_id=frame.request_id,
            status="failed",
            directories={},
            error_code="context_missing",
            error="required context missing: root:AGENTS.md",
            repository_name="root",
        )

    monkeypatch.setattr(assignments_mod, "prepare_assignment_on_host", _failed)
    registry = FakeHostRegistry({host_id: _conn(host_id, owner=ALICE, assignments=True)})
    host_store = FakeHostStore([_FakeHost(host_id, ALICE)])
    coordinator = _coordinator(
        stores, registry=registry, host_store=host_store, permission_store=FakePermissionStore()
    )
    coordinator.trigger(assignment.id)
    await coordinator.wait_for_idle()

    def _attempts() -> int:
        with stores["assignment"]._engine.connect() as conn:
            return int(
                conn.execute(_sa.select(_func.count()).select_from(SqlAssignmentAttempt)).scalar()
                or 0
            )

    assert _attempts() == 1
    pinned = stores["assignment"].get(assignment.id)
    assert pinned is not None and pinned.resolved_binding_id == binding.id

    stores["binding"].upsert(
        project_id=project_id,
        host_id=host_id,
        name="primary",
        repository_id=repo.id,
        workspace="/w2",
        is_primary=True,
    )
    prepare_calls = {"n": 0}

    async def _must_not_prepare(**kwargs: Any) -> Any:
        prepare_calls["n"] += 1
        raise AssertionError("prepare must not run while binding_changed")

    monkeypatch.setattr(assignments_mod, "prepare_assignment_on_host", _must_not_prepare)
    coordinator.trigger(assignment.id)
    await coordinator.wait_for_idle()
    row = stores["assignment"].get(assignment.id)
    assert row is not None
    assert row.wait_reason == "binding_changed"
    assert _attempts() == 1
    assert prepare_calls["n"] == 0

    refreshed = stores["assignment"].refresh_waiting(
        assignment.id,
        inputs=list(row.inputs),
        project_revision=row.project_revision,
        now=now_epoch(),
    )
    assert refreshed is not None
    prepared_dir = "/w2-prepared"
    fake_ok = _prepare_ok({"root": prepared_dir})
    monkeypatch.setattr(assignments_mod, "prepare_assignment_on_host", fake_ok)
    _install_placement_fakes(monkeypatch)
    coordinator.trigger(assignment.id)
    await coordinator.wait_for_idle()
    await coordinator.shutdown()
    assert _attempts() == 2
    assert getattr(fake_ok, "calls", 0) == 1
    final = stores["assignment"].get(assignment.id)
    assert final is not None
    assert final.state == "running"


# ── 7. scenario 10: two coordinators race ─────────────────────────────────


@pytest.mark.asyncio
async def test_two_coordinators_single_flight(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two replicas racing one row produce one attempt and one prepare."""
    import asyncio as _asyncio
    import threading as _threading

    import sqlalchemy as _sa
    from sqlalchemy import func as _func

    from omnigent.db.db_models import SqlAssignmentAttempt

    stores = _stores(db_uri)
    host_id = _uid("host-a")
    project_id = _uid("race-proj")
    _make_project(stores["project"], project_id, owner=ALICE, enabled=True)
    repo = _make_repo(stores["repository"], project_id, "root")
    _make_binding(
        stores["binding"], project_id=project_id, host_id=host_id, repo_id=repo.id, workspace="/w"
    )
    assignment = _seed_waiting(
        stores["assignment"],
        "race",
        project_id=project_id,
        owner=ALICE,
        requested_host_id=host_id,
        inputs=[_input("root", revision=repo.revision)],
    )
    calls = {"n": 0, "launch": 0, "dispatch": 0}

    async def _counted_prepare(**kwargs: Any) -> HostAssignmentPrepareResultFrame:
        calls["n"] += 1
        await _asyncio.sleep(0.01)
        frame = kwargs.get("frame")
        return HostAssignmentPrepareResultFrame(
            request_id=frame.request_id, status="ok", directories={"root": "/prepared"}
        )

    monkeypatch.setattr(assignments_mod, "prepare_assignment_on_host", _counted_prepare)
    captured = _install_placement_fakes(monkeypatch)
    import omnigent.server.routes.sessions as sessions_routes

    orig_launch = sessions_routes._launch_runner_on_host
    orig_dispatch = sessions_routes._dispatch_session_event_to_runner

    async def _counted_launch(*args: Any, **kwargs: Any) -> Any:
        calls["launch"] += 1
        return await orig_launch(*args, **kwargs)

    async def _counted_dispatch(*args: Any, **kwargs: Any) -> Any:
        calls["dispatch"] += 1
        return await orig_dispatch(*args, **kwargs)

    monkeypatch.setattr(sessions_routes, "_launch_runner_on_host", _counted_launch)
    monkeypatch.setattr(sessions_routes, "_dispatch_session_event_to_runner", _counted_dispatch)
    # Hold both evaluations at claim_attempt so neither claim runs first.
    barrier = _threading.Barrier(2)
    real_claim = stores["assignment"].claim_attempt

    def _barrier_claim(*args: Any, **kwargs: Any) -> Any:
        barrier.wait(timeout=10)
        return real_claim(*args, **kwargs)

    monkeypatch.setattr(stores["assignment"], "claim_attempt", _barrier_claim)
    registry = FakeHostRegistry({host_id: _conn(host_id, owner=ALICE, assignments=True)})
    host_store = FakeHostStore([_FakeHost(host_id, ALICE)])
    first = _coordinator(
        stores, registry=registry, host_store=host_store, permission_store=FakePermissionStore()
    )
    second = _coordinator(
        stores, registry=registry, host_store=host_store, permission_store=FakePermissionStore()
    )
    first.trigger(assignment.id)
    second.trigger(assignment.id)
    await _asyncio.gather(first.wait_for_idle(), second.wait_for_idle())
    await first.shutdown()
    await second.shutdown()

    with stores["assignment"]._engine.connect() as conn:
        count = int(
            conn.execute(_sa.select(_func.count()).select_from(SqlAssignmentAttempt)).scalar() or 0
        )
    assert count == 1
    assert calls["n"] == 1
    assert calls["launch"] == 1
    assert calls["dispatch"] == 1
    assert captured.get("dispatch_calls") == 1


# ── 8. scenario 14: scan guard ────────────────────────────────────────────


class _SpyAssignmentStore:
    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Any:
        inner = self._inner

        def _wrap(*args: Any, **kwargs: Any) -> Any:
            if name in (
                "get",
                "transition",
                "reschedule",
                "claim_attempt",
                "select_due",
                "select_waiting_for_host",
                "select_for_host",
                "list",
                "update_attempt",
                "mark_event_dispatched",
                "get_attempt",
                "set_lease",
                "append_message",
            ):
                self.calls.append((name, dict(kwargs)))
            return getattr(inner, name)(*args, **kwargs)

        return _wrap


@pytest.mark.asyncio
async def test_scan_guard_uses_scoped_queries(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Triggers never sweep; the due pass is capped and never lists."""
    stores = _stores(db_uri)
    host_id = _uid("host-a")
    project_id = _uid("guard-proj")
    _make_project(stores["project"], project_id, owner=ALICE, enabled=True)
    repo = _make_repo(stores["repository"], project_id, "root")
    _make_binding(
        stores["binding"], project_id=project_id, host_id=host_id, repo_id=repo.id, workspace="/w"
    )
    assignment = _seed_waiting(
        stores["assignment"],
        "guard",
        project_id=project_id,
        owner=ALICE,
        requested_host_id=host_id,
        inputs=[_input("root", revision=999)],
    )
    spy = _SpyAssignmentStore(stores["assignment"])
    registry = FakeHostRegistry({host_id: _conn(host_id, owner=ALICE, assignments=True)})
    host_store = FakeHostStore([_FakeHost(host_id, ALICE)])

    async def _no_prepare(**kwargs: Any) -> Any:
        raise AssertionError("unreachable")

    monkeypatch.setattr(assignments_mod, "prepare_assignment_on_host", _no_prepare)
    coordinator = AssignmentCoordinator(
        assignment_store=spy,
        project_store=stores["project"],
        repository_store=stores["repository"],
        binding_store=stores["binding"],
        host_store=host_store,
        host_registry=registry,
        conversation_store=stores["conversation"],
        permission_store=FakePermissionStore(),
        runner_router=SimpleNamespace(),
        tunnel_registry=SimpleNamespace(),
        runner_exit_reports=SimpleNamespace(),
        file_store=SimpleNamespace(),
        artifact_store=SimpleNamespace(),
        scan_interval_seconds=3600.0,
        due_batch_limit=50,
    )
    coordinator.trigger(assignment.id)
    await coordinator.wait_for_idle()
    await coordinator.shutdown()
    names = [name for name, _ in spy.calls]
    assert "get" in names
    assert "select_due" not in names
    assert "select_waiting_for_host" not in names
    assert "list" not in names

    spy.calls.clear()
    coordinator2 = AssignmentCoordinator(
        assignment_store=spy,
        project_store=stores["project"],
        repository_store=stores["repository"],
        binding_store=stores["binding"],
        host_store=host_store,
        host_registry=registry,
        conversation_store=stores["conversation"],
        permission_store=FakePermissionStore(),
        runner_router=SimpleNamespace(),
        tunnel_registry=SimpleNamespace(),
        runner_exit_reports=SimpleNamespace(),
        file_store=SimpleNamespace(),
        artifact_store=SimpleNamespace(),
        scan_interval_seconds=3600.0,
        due_batch_limit=7,
    )
    coordinator2.trigger_host(host_id)
    await coordinator2.wait_for_idle()
    await coordinator2.shutdown()
    waiting_calls = [kw for name, kw in spy.calls if name == "select_waiting_for_host"]
    assert len(waiting_calls) == 1
    assert waiting_calls[0].get("limit") == 7
    for_host_calls = [kw for name, kw in spy.calls if name == "select_for_host"]
    assert len(for_host_calls) == 1
    assert for_host_calls[0].get("limit") == 7
    assert "select_due" not in [name for name, _ in spy.calls]
    assert "list" not in [name for name, _ in spy.calls]


@pytest.mark.asyncio
async def test_due_pass_respects_row_cap(db_uri: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """With three due rows and limit two, exactly two are evaluated."""
    stores = _stores(db_uri)
    host_id = _uid("host-a")
    project_id = _uid("cap-proj")
    _make_project(stores["project"], project_id, owner=ALICE, enabled=True)
    repo = _make_repo(stores["repository"], project_id, "root")
    _make_binding(
        stores["binding"], project_id=project_id, host_id=host_id, repo_id=repo.id, workspace="/w"
    )
    ids: list[str] = []
    for seed in ("cap1", "cap2", "cap3"):
        row = _seed_waiting(
            stores["assignment"],
            seed,
            project_id=project_id,
            owner=ALICE,
            requested_host_id=host_id,
            inputs=[_input("root", revision=999)],
        )
        past = now_epoch() - 100
        stores["assignment"].reschedule(row.id, expected_state="waiting", next_check_at=past)
        ids.append(row.id)
    spy = _SpyAssignmentStore(stores["assignment"])

    async def _no_prepare(**kwargs: Any) -> Any:
        raise AssertionError("blocked rows never prepare")

    monkeypatch.setattr(assignments_mod, "prepare_assignment_on_host", _no_prepare)
    coordinator = AssignmentCoordinator(
        assignment_store=spy,
        project_store=stores["project"],
        repository_store=stores["repository"],
        binding_store=stores["binding"],
        host_store=FakeHostStore([_FakeHost(host_id, ALICE)]),
        host_registry=FakeHostRegistry({host_id: _conn(host_id, owner=ALICE, assignments=True)}),
        conversation_store=stores["conversation"],
        permission_store=FakePermissionStore(),
        runner_router=SimpleNamespace(),
        tunnel_registry=SimpleNamespace(),
        runner_exit_reports=SimpleNamespace(),
        file_store=SimpleNamespace(),
        artifact_store=SimpleNamespace(),
        scan_interval_seconds=3600.0,
        due_batch_limit=2,
    )
    await coordinator.start()
    await coordinator.wait_for_idle()
    await coordinator.shutdown()
    due_calls = [kw for name, kw in spy.calls if name == "select_due"]
    assert due_calls and due_calls[0].get("limit") == 2
    reschedules = [c for c in spy.calls if c[0] == "reschedule"]
    assert len(reschedules) == 2
    assert "list" not in [name for name, _ in spy.calls]


# ── 9. scenario 16: restart recovery ────────────────────────────────────────


@pytest.mark.asyncio
async def test_startup_due_pass_runs_without_trigger(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh coordinator picks up a due waiting row on start."""
    stores = _stores(db_uri)
    host_id = _uid("host-a")
    project_id = _uid("restart-proj")
    _make_project(stores["project"], project_id, owner=ALICE, enabled=True)
    repo = _make_repo(stores["repository"], project_id, "root")
    _make_binding(
        stores["binding"], project_id=project_id, host_id=host_id, repo_id=repo.id, workspace="/w"
    )
    assignment = _seed_waiting(
        stores["assignment"],
        "restart",
        project_id=project_id,
        owner=ALICE,
        requested_host_id=host_id,
        inputs=[_input("root", revision=repo.revision)],
    )
    past = now_epoch() - 100
    stores["assignment"].reschedule(assignment.id, expected_state="waiting", next_check_at=past)
    monkeypatch.setattr(
        assignments_mod, "prepare_assignment_on_host", _prepare_ok({"root": "/prepared"})
    )
    _install_placement_fakes(monkeypatch)
    coordinator = _coordinator(
        stores,
        registry=FakeHostRegistry({host_id: _conn(host_id, owner=ALICE, assignments=True)}),
        host_store=FakeHostStore([_FakeHost(host_id, ALICE)]),
        permission_store=FakePermissionStore(),
    )
    await coordinator.start()
    await coordinator.wait_for_idle()
    await coordinator.shutdown()
    row = stores["assignment"].get(assignment.id)
    assert row is not None
    assert row.state == "running"


# ── 10. launch and dispatch failures ───────────────────────────────────────


@pytest.mark.asyncio
async def test_launch_error_returns_to_waiting(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host launch refusal waits with launch_failed and ends the attempt."""
    stores = _stores(db_uri)
    host_id = _uid("host-a")
    project_id = _uid("launch-fail-proj")
    _make_project(stores["project"], project_id, owner=ALICE, enabled=True)
    repo = _make_repo(stores["repository"], project_id, "root")
    _make_binding(
        stores["binding"], project_id=project_id, host_id=host_id, repo_id=repo.id, workspace="/w"
    )
    assignment = _seed_waiting(
        stores["assignment"],
        "launch-fail",
        project_id=project_id,
        owner=ALICE,
        requested_host_id=host_id,
        inputs=[_input("root", revision=repo.revision)],
    )
    monkeypatch.setattr(
        assignments_mod, "prepare_assignment_on_host", _prepare_ok({"root": "/prepared"})
    )
    _install_placement_fakes(monkeypatch, launch_error="harness not configured")
    coordinator = _coordinator(
        stores,
        registry=FakeHostRegistry({host_id: _conn(host_id, owner=ALICE, assignments=True)}),
        host_store=FakeHostStore([_FakeHost(host_id, ALICE)]),
        permission_store=FakePermissionStore(),
    )
    coordinator.trigger(assignment.id)
    await coordinator.wait_for_idle()
    await coordinator.shutdown()

    row = stores["assignment"].get(assignment.id)
    assert row is not None
    assert row.state == "waiting"
    assert row.wait_reason is not None and row.wait_reason.startswith("launch_failed:")
    assert row.active_attempt_id is None
    import sqlalchemy as _sa

    from omnigent.db.db_models import SqlAssignmentAttempt

    with stores["assignment"]._engine.connect() as conn:
        attempts = list(
            conn.execute(
                _sa.select(SqlAssignmentAttempt).where(
                    SqlAssignmentAttempt.assignment_id == assignment.id
                )
            )
            .mappings()
            .all()
        )
    assert len(attempts) == 1
    assert attempts[0]["state"] == "finished"
    assert attempts[0]["error_code"] == "launch_failed"


@pytest.mark.asyncio
async def test_dispatch_failure_interrupts_after_runner_recorded(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dispatch raise after launch is unknown: active attempt, one delivery."""
    stores = _stores(db_uri)
    host_id = _uid("host-a")
    project_id = _uid("dispatch-fail-proj")
    _make_project(stores["project"], project_id, owner=ALICE, enabled=True)
    repo = _make_repo(stores["repository"], project_id, "root")
    _make_binding(
        stores["binding"], project_id=project_id, host_id=host_id, repo_id=repo.id, workspace="/w"
    )
    assignment = _seed_waiting(
        stores["assignment"],
        "dispatch-fail",
        project_id=project_id,
        owner=ALICE,
        requested_host_id=host_id,
        inputs=[_input("root", revision=repo.revision)],
    )
    monkeypatch.setattr(
        assignments_mod, "prepare_assignment_on_host", _prepare_ok({"root": "/prepared"})
    )
    captured = _install_placement_fakes(monkeypatch, dispatch_raises=True)
    coordinator = _coordinator(
        stores,
        registry=FakeHostRegistry({host_id: _conn(host_id, owner=ALICE, assignments=True)}),
        host_store=FakeHostStore([_FakeHost(host_id, ALICE)]),
        permission_store=FakePermissionStore(),
    )
    coordinator.trigger(assignment.id)
    await coordinator.wait_for_idle()
    await coordinator.shutdown()

    row = stores["assignment"].get(assignment.id)
    assert row is not None
    assert row.state == "interrupted"
    assert row.wait_reason is not None and row.wait_reason.startswith("dispatch:")
    assert row.next_check_at is not None
    assert captured.get("dispatch_calls") == 1
    import sqlalchemy as _sa

    from omnigent.db.db_models import SqlAssignmentAttempt

    with stores["assignment"]._engine.connect() as conn:
        attempts = list(
            conn.execute(
                _sa.select(SqlAssignmentAttempt).where(
                    SqlAssignmentAttempt.assignment_id == assignment.id
                )
            )
            .mappings()
            .all()
        )
    assert len(attempts) == 1
    assert attempts[0]["state"] == "active"
    assert attempts[0]["ended_at"] is None
    assert attempts[0]["runner_id"] == "runner_1"
    assert attempts[0]["session_id"] is not None
    # Interrupted rows keep the attempt link for /retry and /cancel.
    assert row.active_attempt_id == attempts[0]["id"]
    kept = stores["assignment"].get_attempt(assignment.id, row.active_attempt_id)
    assert kept is not None
    assert kept.state == "active"
    assert kept.ended_at is None


@pytest.mark.asyncio
async def test_native_dispatch_without_forward_interrupts(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A native return with no forward is a failed delivery, never running."""
    import omnigent.server.routes.sessions as sessions_routes

    stores = _stores(db_uri)
    host_id = _uid("host-a")
    project_id = _uid("native-fail-proj")
    _make_project(stores["project"], project_id, owner=ALICE, enabled=True)
    repo = _make_repo(stores["repository"], project_id, "root")
    _make_binding(
        stores["binding"], project_id=project_id, host_id=host_id, repo_id=repo.id, workspace="/w"
    )
    assignment = _seed_waiting(
        stores["assignment"],
        "native-fail",
        project_id=project_id,
        owner=ALICE,
        requested_host_id=host_id,
        inputs=[_input("root", revision=repo.revision)],
    )
    monkeypatch.setattr(
        assignments_mod, "prepare_assignment_on_host", _prepare_ok({"root": "/prepared"})
    )
    captured = _install_placement_fakes(monkeypatch)
    monkeypatch.setattr(sessions_routes, "_is_native_terminal_session", lambda conv: True)
    coordinator = _coordinator(
        stores,
        registry=FakeHostRegistry({host_id: _conn(host_id, owner=ALICE, assignments=True)}),
        host_store=FakeHostStore([_FakeHost(host_id, ALICE)]),
        permission_store=FakePermissionStore(),
    )
    coordinator.trigger(assignment.id)
    await coordinator.wait_for_idle()
    await coordinator.shutdown()

    row = stores["assignment"].get(assignment.id)
    assert row is not None
    assert row.state == "interrupted"
    assert row.wait_reason is not None and row.wait_reason.startswith("dispatch:")
    assert captured.get("dispatch_calls") == 1
    assert row.active_attempt_id is not None
    assert row.next_check_at is not None
    attempt = stores["assignment"].get_attempt(assignment.id, row.active_attempt_id)
    assert attempt is not None
    assert attempt.state == "active"
    assert attempt.ended_at is None


@pytest.mark.asyncio
async def test_ensure_failure_interrupts_without_dispatch(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An init handshake failure (require_success) never reaches dispatch."""
    import omnigent.server.routes.sessions as sessions_routes

    stores = _stores(db_uri)
    host_id = _uid("host-a")
    project_id = _uid("ensure-fail-proj")
    _make_project(stores["project"], project_id, owner=ALICE, enabled=True)
    repo = _make_repo(stores["repository"], project_id, "root")
    _make_binding(
        stores["binding"], project_id=project_id, host_id=host_id, repo_id=repo.id, workspace="/w"
    )
    assignment = _seed_waiting(
        stores["assignment"],
        "ensure-fail",
        project_id=project_id,
        owner=ALICE,
        requested_host_id=host_id,
        inputs=[_input("root", revision=repo.revision)],
    )
    monkeypatch.setattr(
        assignments_mod, "prepare_assignment_on_host", _prepare_ok({"root": "/prepared"})
    )
    captured = _install_placement_fakes(monkeypatch)

    async def _boom(*args: Any, **kwargs: Any) -> bool:
        assert kwargs.get("require_success") is True
        raise RuntimeError("init boom")

    monkeypatch.setattr(sessions_routes, "_ensure_runner_session_initialized", _boom)
    coordinator = _coordinator(
        stores,
        registry=FakeHostRegistry({host_id: _conn(host_id, owner=ALICE, assignments=True)}),
        host_store=FakeHostStore([_FakeHost(host_id, ALICE)]),
        permission_store=FakePermissionStore(),
    )
    coordinator.trigger(assignment.id)
    await coordinator.wait_for_idle()
    await coordinator.shutdown()

    row = stores["assignment"].get(assignment.id)
    assert row is not None
    assert row.state == "interrupted"
    assert row.wait_reason is not None and row.wait_reason.startswith("init:")
    assert captured.get("dispatch_calls", 0) == 0


@pytest.mark.asyncio
async def test_failure_backoff_uses_failure_time(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slow prepare stamps ended_at and backoff at the failure moment."""
    from omnigent.server.assignments import next_check_at as _next_check_at

    stores = _stores(db_uri)
    host_id = _uid("host-a")
    project_id = _uid("backoff-proj")
    _make_project(stores["project"], project_id, owner=ALICE, enabled=True)
    repo = _make_repo(stores["repository"], project_id, "root")
    _make_binding(
        stores["binding"], project_id=project_id, host_id=host_id, repo_id=repo.id, workspace="/w"
    )
    assignment = _seed_waiting(
        stores["assignment"],
        "backoff",
        project_id=project_id,
        owner=ALICE,
        requested_host_id=host_id,
        inputs=[_input("root", revision=repo.revision)],
    )
    clock = {"now": 1000}

    async def _slow_failed(**kwargs: Any) -> HostAssignmentPrepareResultFrame:
        clock["now"] = 1300
        frame = kwargs.get("frame")
        return HostAssignmentPrepareResultFrame(
            request_id=frame.request_id,
            status="failed",
            directories={},
            error_code="context_missing",
            error="required context missing",
            repository_name="root",
        )

    monkeypatch.setattr(assignments_mod, "prepare_assignment_on_host", _slow_failed)
    monkeypatch.setattr(assignments_mod.time, "time", lambda: clock["now"])
    coordinator = _coordinator(
        stores,
        registry=FakeHostRegistry({host_id: _conn(host_id, owner=ALICE, assignments=True)}),
        host_store=FakeHostStore([_FakeHost(host_id, ALICE)]),
        permission_store=FakePermissionStore(),
    )
    coordinator.trigger(assignment.id)
    await coordinator.wait_for_idle()
    await coordinator.shutdown()

    row = stores["assignment"].get(assignment.id)
    assert row is not None
    assert row.state == "waiting"
    import sqlalchemy as _sa

    from omnigent.db.db_models import SqlAssignmentAttempt

    with stores["assignment"]._engine.connect() as conn:
        attempts = list(
            conn.execute(
                _sa.select(SqlAssignmentAttempt).where(
                    SqlAssignmentAttempt.assignment_id == assignment.id
                )
            )
            .mappings()
            .all()
        )
    assert len(attempts) == 1
    assert attempts[0]["ended_at"] == 1300
    assert row.next_check_at == _next_check_at(row.created_at, 1300)


@pytest.mark.asyncio
async def test_host_scan_maps_local_owner_to_ownerless(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 'local' tunnel owner still evaluates ownerless waiting rows."""
    stores = _stores(db_uri)
    host_id = _uid("host-a")
    project_id = _uid("local-scan-proj")
    _make_project(stores["project"], project_id, owner=None, enabled=True)
    repo = _make_repo(stores["repository"], project_id, "root")
    _make_binding(
        stores["binding"], project_id=project_id, host_id=host_id, repo_id=repo.id, workspace="/w"
    )
    assignment = _seed_waiting(
        stores["assignment"],
        "local-scan",
        project_id=project_id,
        owner=None,
        requested_host_id=None,
        inputs=[_input("root", revision=repo.revision)],
    )
    monkeypatch.setattr(
        assignments_mod, "prepare_assignment_on_host", _prepare_ok({"root": "/prepared"})
    )
    _install_placement_fakes(monkeypatch)
    coordinator = _coordinator(
        stores,
        registry=FakeHostRegistry({host_id: _conn(host_id, owner="local", assignments=True)}),
        host_store=FakeHostStore([_FakeHost(host_id, "local")]),
        permission_store=FakePermissionStore(),
    )
    coordinator.trigger_host(host_id)
    await coordinator.wait_for_idle()
    await coordinator.shutdown()

    row = stores["assignment"].get(assignment.id)
    assert row is not None
    assert row.state == "running"


# ── 11. other states only move next_check_at ───────────────────────────────


@pytest.mark.asyncio
async def test_running_row_only_moves_next_check(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A due running row reschedules without touching state or reason."""
    stores = _stores(db_uri)
    host_id = _uid("host-a")
    project_id = _uid("running-proj")
    _make_project(stores["project"], project_id, owner=ALICE, enabled=True)
    repo = _make_repo(stores["repository"], project_id, "root")
    _make_binding(
        stores["binding"], project_id=project_id, host_id=host_id, repo_id=repo.id, workspace="/w"
    )
    assignment = _seed_waiting(
        stores["assignment"],
        "running-row",
        project_id=project_id,
        owner=ALICE,
        requested_host_id=host_id,
        inputs=[_input("root", revision=repo.revision)],
    )
    attempt = stores["assignment"].claim_attempt(assignment.id, host_id=host_id, now=now_epoch())
    assert attempt is not None
    moved = stores["assignment"].transition(
        assignment.id, from_state="starting", to_state="running"
    )
    assert moved is not None
    before = stores["assignment"].get(assignment.id)
    assert before is not None
    past = now_epoch() - 100
    rescheduled = stores["assignment"].reschedule(
        assignment.id, expected_state="running", next_check_at=past
    )
    assert rescheduled is not None
    old_next = rescheduled.next_check_at

    async def _no_prepare(**kwargs: Any) -> Any:
        raise AssertionError("running rows never prepare")

    monkeypatch.setattr(assignments_mod, "prepare_assignment_on_host", _no_prepare)
    coordinator = _coordinator(
        stores,
        registry=FakeHostRegistry({host_id: _conn(host_id, owner=ALICE, assignments=True)}),
        host_store=FakeHostStore([_FakeHost(host_id, ALICE)]),
        permission_store=FakePermissionStore(),
    )
    await coordinator.start()
    await coordinator.wait_for_idle()
    await coordinator.shutdown()

    after = stores["assignment"].get(assignment.id)
    assert after is not None
    assert after.state == "running"
    assert after.wait_reason == before.wait_reason
    assert after.next_check_at is not None and after.next_check_at != old_next


# ── 12. lifespan ──────────────────────────────────────────────────────────


def _lifespan_app(db_uri: str, tmp_path: Path, *, enabled: bool):  # type: ignore[no-untyped-def]
    from omnigent.runtime.agent_cache import AgentCache
    from omnigent.server.app import create_app
    from omnigent.server.feature_flags import resolve_feature_flags
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
    from omnigent.stores.artifact_store.local import LocalArtifactStore
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore as _Conv,
    )
    from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
    from omnigent.stores.host_store import HostStore

    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    flags = resolve_feature_flags({"OMNIGENT_FEATURES": "project_assignments"} if enabled else {})
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=_Conv(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        host_store=HostStore(db_uri),
        project_store=SqlAlchemyProjectStore(db_uri),
        project_repository_store=SqlAlchemyProjectRepositoryStore(db_uri),
        project_host_binding_store=SqlAlchemyProjectHostBindingStore(db_uri),
        assignment_store=SqlAlchemyAssignmentStore(db_uri),
        feature_flags=flags,
    )


@pytest.mark.asyncio
async def test_lifespan_flag_off_has_no_coordinator(
    db_uri: str, tmp_path: Path, runtime_init: None
) -> None:
    """With the flag off the coordinator is explicitly None."""
    app = _lifespan_app(db_uri, tmp_path, enabled=False)
    async with app.router.lifespan_context(app):
        assert app.state.assignment_coordinator is None


@pytest.mark.asyncio
async def test_lifespan_flag_on_starts_and_stops_coordinator(
    db_uri: str, tmp_path: Path, runtime_init: None
) -> None:
    """With the flag on and stores wired the coordinator runs and stops."""
    app = _lifespan_app(db_uri, tmp_path, enabled=True)
    async with app.router.lifespan_context(app):
        coordinator = app.state.assignment_coordinator
        assert coordinator is not None
        assert coordinator._scan_task is not None
    assert coordinator._scan_task is None


# ── 13. routes trigger ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_published_triggers_coordinator_once(db_uri: str, tmp_path: Path) -> None:
    """A successful /published schedules one evaluation."""
    import httpx

    from omnigent.runtime.agent_cache import AgentCache
    from omnigent.server.app import create_app
    from omnigent.server.feature_flags import resolve_feature_flags
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
    from omnigent.stores.artifact_store.local import LocalArtifactStore
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore as _Conv,
    )
    from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
    from omnigent.stores.host_store import HostStore

    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    flags = resolve_feature_flags({"OMNIGENT_FEATURES": "project_assignments"})
    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=_Conv(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        host_store=HostStore(db_uri),
        project_store=SqlAlchemyProjectStore(db_uri),
        project_repository_store=SqlAlchemyProjectRepositoryStore(db_uri),
        project_host_binding_store=SqlAlchemyProjectHostBindingStore(db_uri),
        assignment_store=SqlAlchemyAssignmentStore(db_uri),
        feature_flags=flags,
    )

    class _FakeCoordinator:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def trigger(self, assignment_id: str) -> None:
            self.calls.append(assignment_id)

    fake = _FakeCoordinator()
    app.state.assignment_coordinator = fake  # type: ignore[attr-defined]

    agent_store = SqlAlchemyAgentStore(db_uri)
    if agent_store.get(AGENT_ID) is None:
        agent_store.create(
            agent_id=AGENT_ID, name="test-agent", bundle_location=f"{AGENT_ID}/bundle"
        )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/v1/projects", json={"name": "RouteProj"})
        assert resp.status_code == 200, resp.text
        project_id = resp.json()["id"]
        resp = await client.patch(
            f"/v1/projects/{project_id}/collaboration",
            json={"enabled": True, "expected_revision": 0},
        )
        assert resp.status_code == 200, resp.text
        resp = await client.put(
            f"/v1/projects/{project_id}/repositories/root",
            json={"remote_url": "https://example.com/org/repo.git", "default_branch": "main"},
        )
        assert resp.status_code == 200, resp.text
        conv = SqlAlchemyConversationStore(db_uri).create_conversation(
            title="src", agent_id=AGENT_ID, project_id=project_id
        )
        assignment_id = _uid("route-pub")
        resp = await client.post(
            "/v1/assignments",
            json={
                "id": assignment_id,
                "source_session_id": conv.id,
                "target_agent_id": AGENT_ID,
                "task": "route task",
                "repositories": [
                    {
                        "repository_name": "root",
                        "commit": "a" * 40,
                        "manifest_digest": "d" * 64,
                    }
                ],
                "idempotency_key": "key-route",
            },
        )
        assert resp.status_code == 201, resp.text
        resp = await client.post(
            f"/v1/assignments/{assignment_id}/published",
            json={"refs": [{"repository_name": "root", "commit": "a" * 40}]},
        )
        assert resp.status_code == 200, resp.text
    assert fake.calls == [assignment_id]


def _revision6_release_case(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    *,
    seed: str,
    ended_at: int | None,
) -> tuple[dict[str, Any], AssignmentCoordinator, Assignment, Any, list[str]]:
    import omnigent.server.routes.sessions as sessions_routes
    from omnigent.host.frames import HostAssignmentReleaseResultFrame

    stores = _stores(db_uri)
    host_id = _uid(f"{seed}-host")
    project_id = _uid(f"{seed}-project")
    _make_project(stores["project"], project_id)
    repo = _make_repo(stores["repository"], project_id)
    _make_binding(stores["binding"], project_id=project_id, host_id=host_id, repo_id=repo.id)
    assignment, attempt = _seed_succeeded_with_session(
        stores,
        seed,
        project_id=project_id,
        host_id=host_id,
        clock_now=10_000,
        attempt_runner="runner_1",
        session_runner="runner_1",
    )
    if ended_at is not None:
        assert (
            stores["assignment"].update_attempt(assignment.id, attempt.id, ended_at=ended_at)
            is not None
        )
    else:
        assert (
            stores["assignment"].update_attempt(assignment.id, attempt.id, ended_at=None)
            is not None
        )
    calls: list[str] = []

    async def _stop(*_args: Any, **_kwargs: Any) -> str:
        calls.append("stop")
        return "acked"

    async def _release(**kwargs: Any) -> HostAssignmentReleaseResultFrame:
        calls.append("release")
        frame = kwargs["frame"]
        return HostAssignmentReleaseResultFrame(
            request_id=frame.request_id, status="ok", removed=["root"], failures={}
        )

    monkeypatch.setattr(sessions_routes, "_stop_session_host_runner_outcome", _stop)
    monkeypatch.setattr(assignments_mod, "release_assignment_on_host", _release)
    coordinator = _coordinator(
        stores,
        registry=FakeHostRegistry({host_id: _conn(host_id, owner=ALICE)}),
        host_store=FakeHostStore([_FakeHost(host_id, ALICE)]),
        permission_store=FakePermissionStore(),
    )
    stored_attempt = stores["assignment"].get_attempt(assignment.id, attempt.id)
    assert stored_attempt is not None
    return stores, coordinator, assignment, stored_attempt, calls


@pytest.mark.asyncio
async def test_succeeded_release_waits_for_two_idle_looks(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.server.routes._sessions import orchestration
    from omnigent.server.routes._sessions.common import (
        _LAST_TASK_ERROR_CODE_LABEL_KEY,
        _intentional_stop_sessions,
        _session_status_cache,
    )
    from omnigent.server.schemas import ErrorDetail
    from omnigent.util.session_lifecycle import CLOSED_LABEL_KEY, CLOSED_LABEL_VALUE

    clock = {"now": 10_000}
    monkeypatch.setattr(assignments_mod.time, "time", lambda: clock["now"])
    stores, coordinator, assignment, attempt, calls = _revision6_release_case(
        db_uri, monkeypatch, seed="rev6-turn", ended_at=10_000
    )
    session_id = attempt.session_id
    assert session_id is not None
    try:
        stores["conversation"].set_session_live_status(session_id, "running")
        await coordinator._evaluate_terminal_release(assignment)
        row = stores["assignment"].get(assignment.id)
        assert row is not None and row.next_check_at == 10_030
        assert calls == []
        assert CLOSED_LABEL_KEY not in stores["conversation"].get_conversation(session_id).labels

        clock["now"] = 10_030
        stores["conversation"].set_session_live_status(session_id, "idle")
        await coordinator._evaluate_terminal_release(row)
        row = stores["assignment"].get(assignment.id)
        assert row is not None and row.next_check_at == 10_060
        assert calls == []

        clock["now"] = 10_060
        await coordinator._evaluate_terminal_release(row)
        row = stores["assignment"].get(assignment.id)
        assert row is not None and row.next_check_at is None
        conv = stores["conversation"].get_conversation(session_id)
        assert conv is not None and conv.labels[CLOSED_LABEL_KEY] == CLOSED_LABEL_VALUE
        assert conv.live_status != "failed"
        assert calls == ["stop", "release"]
        assert session_id in _intentional_stop_sessions
        _session_status_cache[session_id] = "running"
        error = ErrorDetail(code="runner_disconnected", message="runner went offline")
        with patch.object(orchestration, "_publish_status") as publish:
            await orchestration._mark_runner_sessions_offline_impl(
                [conv], error, stores["conversation"]
            )
        publish.assert_not_called()
        conv = stores["conversation"].get_conversation(session_id)
        assert conv is not None and conv.live_status != "failed"
        assert conv.labels.get(_LAST_TASK_ERROR_CODE_LABEL_KEY) != "runner_disconnected"
    finally:
        _session_status_cache.pop(session_id, None)
        _intentional_stop_sessions.discard(session_id)
        await coordinator.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("ended_at", [9_399, None])
async def test_succeeded_release_grace_is_bounded(
    db_uri: str, monkeypatch: pytest.MonkeyPatch, ended_at: int | None
) -> None:
    from omnigent.server.routes._sessions.common import _intentional_stop_sessions
    from omnigent.util.session_lifecycle import CLOSED_LABEL_KEY, CLOSED_LABEL_VALUE

    clock = {"now": 10_000}
    monkeypatch.setattr(assignments_mod.time, "time", lambda: clock["now"])
    stores, coordinator, assignment, attempt, calls = _revision6_release_case(
        db_uri, monkeypatch, seed=f"rev6-grace-{ended_at}", ended_at=ended_at
    )
    session_id = attempt.session_id
    assert session_id is not None
    try:
        stores["conversation"].set_session_live_status(session_id, "running")
        if ended_at is None:
            await coordinator._evaluate_terminal_release(assignment)
            row = stores["assignment"].get(assignment.id)
            assert row is not None and row.next_check_at == 10_030
            assert calls == []
            clock["now"] = 10_600
        else:
            row = assignment
        await coordinator._evaluate_terminal_release(row)
        row = stores["assignment"].get(assignment.id)
        assert row is not None and row.next_check_at is None
        conv = stores["conversation"].get_conversation(session_id)
        assert conv is not None and conv.labels[CLOSED_LABEL_KEY] == CLOSED_LABEL_VALUE
        assert calls == ["stop", "release"]
    finally:
        _intentional_stop_sessions.discard(session_id)
        await coordinator.shutdown()


@pytest.mark.asyncio
async def test_succeeded_release_retries_stop_without_restarting_grace(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import omnigent.server.routes.sessions as sessions_routes
    from omnigent.server.routes._sessions.common import _intentional_stop_sessions

    clock = {"now": 10_000}
    monkeypatch.setattr(assignments_mod.time, "time", lambda: clock["now"])
    stores, coordinator, assignment, attempt, calls = _revision6_release_case(
        db_uri, monkeypatch, seed="rev6-retry-stop", ended_at=None
    )
    session_id = attempt.session_id
    assert session_id is not None
    outcomes = iter(("unavailable", "acked"))

    async def _stop(*_args: Any, **_kwargs: Any) -> str:
        calls.append("stop")
        return next(outcomes)

    monkeypatch.setattr(sessions_routes, "_stop_session_host_runner_outcome", _stop)
    try:
        stores["conversation"].set_session_live_status(session_id, "running")
        await coordinator._evaluate_terminal_release(assignment)
        row = stores["assignment"].get(assignment.id)
        assert row is not None and row.next_check_at == 10_030

        clock["now"] = 10_600
        await coordinator._evaluate_terminal_release(row)
        row = stores["assignment"].get(assignment.id)
        assert row is not None and row.next_check_at == 10_030
        assert calls == ["stop"]

        clock["now"] = 10_630
        await coordinator._evaluate_terminal_release(row)
        row = stores["assignment"].get(assignment.id)
        assert row is not None and row.next_check_at is None
        assert calls == ["stop", "stop", "release"]
        assert assignment.id not in coordinator._release_holds
    finally:
        _intentional_stop_sessions.discard(session_id)
        await coordinator.shutdown()


@pytest.mark.asyncio
async def test_terminal_row_without_pending_check_clears_release_hold(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = {"now": 10_000}
    monkeypatch.setattr(assignments_mod.time, "time", lambda: clock["now"])
    stores, coordinator, assignment, attempt, _calls = _revision6_release_case(
        db_uri, monkeypatch, seed="rev6-finished-hold", ended_at=None
    )
    session_id = attempt.session_id
    assert session_id is not None
    try:
        stores["conversation"].set_session_live_status(session_id, "running")
        await coordinator._evaluate_terminal_release(assignment)
        assert assignment.id in coordinator._release_holds
        assert (
            stores["assignment"].reschedule(
                assignment.id,
                expected_state="succeeded",
                expected_active_attempt_id=attempt.id,
                next_check_at=None,
            )
            is not None
        )
        await coordinator._evaluate(assignment.id)
        assert assignment.id not in coordinator._release_holds
    finally:
        await coordinator.shutdown()


@pytest.mark.asyncio
async def test_runner_offline_sweep_fails_unmarked_running_session(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.server.routes._sessions import orchestration
    from omnigent.server.routes._sessions.common import (
        _LAST_TASK_ERROR_CODE_LABEL_KEY,
        _intentional_stop_sessions,
        _session_status_cache,
    )
    from omnigent.server.schemas import ErrorDetail

    stores, coordinator, _assignment, attempt, _calls = _revision6_release_case(
        db_uri, monkeypatch, seed="rev6-sweep-control", ended_at=None
    )
    session_id = attempt.session_id
    assert session_id is not None
    try:
        stores["conversation"].set_session_live_status(session_id, "running")
        _session_status_cache[session_id] = "running"
        _intentional_stop_sessions.discard(session_id)
        conv = stores["conversation"].get_conversation(session_id)
        assert conv is not None
        error = ErrorDetail(code="runner_disconnected", message="runner went offline")
        with patch.object(
            orchestration, "_publish_status", wraps=orchestration._publish_status
        ) as publish:
            await orchestration._mark_runner_sessions_offline_impl(
                [conv], error, stores["conversation"]
            )
        publish.assert_called_once_with(
            session_id, "failed", error, failure_origin="runner_offline_sweep"
        )
        assert _session_status_cache[session_id] == "failed"
        conv = stores["conversation"].get_conversation(session_id)
        assert conv is not None
        assert conv.labels.get(_LAST_TASK_ERROR_CODE_LABEL_KEY) == "runner_disconnected"
    finally:
        _session_status_cache.pop(session_id, None)
        _intentional_stop_sessions.discard(session_id)
        await coordinator.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["acked", "unknown_runner", "unavailable", "raises"])
@pytest.mark.parametrize("preexisting", [False, True])
async def test_coordinator_stop_preserves_marker_ownership(
    db_uri: str, monkeypatch: pytest.MonkeyPatch, outcome: str, preexisting: bool
) -> None:
    import omnigent.server.routes.sessions as sessions_routes
    from omnigent.server.routes._sessions.common import _intentional_stop_sessions

    assert sessions_routes._intentional_stop_sessions is _intentional_stop_sessions
    _stores_unused, coordinator, assignment, attempt, _calls = _revision6_release_case(
        db_uri, monkeypatch, seed=f"rev6-marker-{outcome}-{preexisting}", ended_at=9_000
    )
    session_id = attempt.session_id
    assert session_id is not None
    _intentional_stop_sessions.discard(session_id)
    if preexisting:
        _intentional_stop_sessions.add(session_id)

    async def _stop(*_args: Any, **_kwargs: Any) -> str:
        assert session_id in _intentional_stop_sessions
        if outcome == "raises":
            raise RuntimeError("transport lost")
        return outcome

    monkeypatch.setattr(sessions_routes, "_stop_session_host_runner_outcome", _stop)
    try:
        confirmed = await coordinator._stop_confirmed(assignment, attempt, "runner_1", session_id)
        assert confirmed is (outcome in ("acked", "unknown_runner"))
        assert (session_id in _intentional_stop_sessions) is (preexisting or outcome == "acked")
    finally:
        _intentional_stop_sessions.discard(session_id)
        await coordinator.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("second_outcome", ["acked", "unknown_runner"])
async def test_cancel_stops_mid_turn_then_closes_without_erasing_marker(
    db_uri: str, monkeypatch: pytest.MonkeyPatch, second_outcome: str
) -> None:
    import omnigent.server.routes.sessions as sessions_routes
    from omnigent.host.frames import HostAssignmentReleaseResultFrame
    from omnigent.server.routes._sessions import orchestration
    from omnigent.server.routes._sessions.common import (
        _LAST_TASK_ERROR_CODE_LABEL_KEY,
        _intentional_stop_sessions,
        _session_status_cache,
    )
    from omnigent.server.schemas import ErrorDetail
    from omnigent.util.session_lifecycle import CLOSED_LABEL_KEY, CLOSED_LABEL_VALUE

    stores = _stores(db_uri)
    host_id = _uid(f"rev6-cancel-{second_outcome}-host")
    project_id = _uid(f"rev6-cancel-{second_outcome}-project")
    _make_project(stores["project"], project_id)
    repo = _make_repo(stores["repository"], project_id)
    binding = _make_binding(
        stores["binding"], project_id=project_id, host_id=host_id, repo_id=repo.id
    )
    assignment = _seed_waiting(
        stores["assignment"],
        f"rev6-cancel-{second_outcome}",
        project_id=project_id,
        requested_host_id=host_id,
    )
    attempt = stores["assignment"].claim_attempt(
        assignment.id,
        host_id=host_id,
        now=10_000,
        resolved_binding_id=binding.id,
        resolved_binding_revision=binding.revision,
        next_check_at=10_000,
    )
    assert attempt is not None
    assert (
        stores["assignment"].transition(assignment.id, from_state="starting", to_state="running")
        is not None
    )
    assert (
        stores["assignment"].transition(
            assignment.id,
            from_state="running",
            to_state="stopping",
            expected_active_attempt_id=attempt.id,
        )
        is not None
    )
    session_id = hashlib.sha256(f"assignment-attempt:{attempt.id}".encode()).hexdigest()[:32]
    assert (
        stores["assignment"].update_attempt(
            assignment.id, attempt.id, session_id=session_id, runner_id="runner_1"
        )
        is not None
    )
    _make_session_with_runner(
        stores,
        session_id,
        host_id=host_id,
        workspace="/work/p",
        runner_id="runner_1",
        project_id=project_id,
    )
    stores["conversation"].set_session_live_status(session_id, "running")
    clock = {"now": 10_000}
    monkeypatch.setattr(assignments_mod.time, "time", lambda: clock["now"])
    outcomes = iter(("acked", second_outcome))
    stops: list[str] = []

    async def _stop(*_args: Any, **_kwargs: Any) -> str:
        assert session_id in _intentional_stop_sessions
        stops.append("stop")
        return next(outcomes)

    async def _release(**kwargs: Any) -> HostAssignmentReleaseResultFrame:
        frame = kwargs["frame"]
        return HostAssignmentReleaseResultFrame(
            request_id=frame.request_id, status="ok", removed=["root"], failures={}
        )

    monkeypatch.setattr(sessions_routes, "_stop_session_host_runner_outcome", _stop)
    monkeypatch.setattr(assignments_mod, "release_assignment_on_host", _release)
    coordinator = _coordinator(
        stores,
        registry=FakeHostRegistry({host_id: _conn(host_id, owner=ALICE)}),
        host_store=FakeHostStore([_FakeHost(host_id, ALICE)]),
        permission_store=FakePermissionStore(),
    )
    try:
        await coordinator._evaluate_stopping(stores["assignment"].get(assignment.id))
        row = stores["assignment"].get(assignment.id)
        assert row is not None and row.state == "cancelled" and row.next_check_at is None
        assert stops == ["stop", "stop"]
        assert session_id in _intentional_stop_sessions
        conv = stores["conversation"].get_conversation(session_id)
        assert conv is not None and conv.labels[CLOSED_LABEL_KEY] == CLOSED_LABEL_VALUE
        assert conv.live_status != "failed"
        _session_status_cache[session_id] = "running"
        error = ErrorDetail(code="runner_disconnected", message="runner went offline")
        with patch.object(orchestration, "_publish_status") as publish:
            await orchestration._mark_runner_sessions_offline_impl(
                [conv], error, stores["conversation"]
            )
        publish.assert_not_called()
        conv = stores["conversation"].get_conversation(session_id)
        assert conv is not None and conv.live_status != "failed"
        assert conv.labels.get(_LAST_TASK_ERROR_CODE_LABEL_KEY) != "runner_disconnected"
    finally:
        _session_status_cache.pop(session_id, None)
        _intentional_stop_sessions.discard(session_id)
        await coordinator.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("requested", "primary_name"),
    [
        ("primary", "omnigent"),
        ("extra", "omnigent"),
        ("primary", "omnigent-with-named-primary"),
    ],
)
async def test_binding_selector_places_on_resolved_binding(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    requested: str,
    primary_name: str,
) -> None:
    stores = _stores(db_uri)
    host_id = _uid(f"rev6-binding-{requested}-{primary_name}-host")
    project_id = _uid(f"rev6-binding-{requested}-{primary_name}-project")
    _make_project(stores["project"], project_id)
    repo = _make_repo(stores["repository"], project_id)
    primary = _make_binding(
        stores["binding"],
        project_id=project_id,
        host_id=host_id,
        repo_id=repo.id,
        name=primary_name,
        is_primary=True,
    )
    if primary_name == "omnigent-with-named-primary":
        _make_binding(
            stores["binding"],
            project_id=project_id,
            host_id=host_id,
            repo_id=repo.id,
            name="primary",
            is_primary=False,
        )
    expected_binding_id = primary.id
    if requested == "extra":
        extra = _make_binding(
            stores["binding"],
            project_id=project_id,
            host_id=host_id,
            repo_id=repo.id,
            name="extra",
            is_primary=False,
        )
        expected_binding_id = extra.id
    assignment = _seed_waiting(
        stores["assignment"],
        f"rev6-binding-{requested}-{primary_name}",
        project_id=project_id,
        requested_host_id=host_id,
        binding_name=requested,
        inputs=[_input(revision=repo.revision)],
    )
    monkeypatch.setattr(assignments_mod, "prepare_assignment_on_host", _prepare_ok({"root": "/w"}))
    _install_placement_fakes(monkeypatch)
    coordinator = _coordinator(
        stores,
        registry=FakeHostRegistry({host_id: _conn(host_id, owner=ALICE)}),
        host_store=FakeHostStore([_FakeHost(host_id, ALICE)]),
        permission_store=FakePermissionStore(),
    )
    try:
        coordinator.trigger(assignment.id)
        await coordinator.wait_for_idle()
        row = stores["assignment"].get(assignment.id)
        assert row is not None and row.state in ("starting", "running")
        assert row.resolved_binding_id == expected_binding_id
    finally:
        await coordinator.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("named_primary", [True, False])
async def test_pinned_host_without_enabled_primary_waits(
    db_uri: str, monkeypatch: pytest.MonkeyPatch, named_primary: bool
) -> None:
    stores = _stores(db_uri)
    host_id = _uid(f"rev6-missing-{named_primary}-host")
    project_id = _uid(f"rev6-missing-{named_primary}-project")
    _make_project(stores["project"], project_id)
    repo = _make_repo(stores["repository"], project_id)
    _make_binding(
        stores["binding"],
        project_id=project_id,
        host_id=host_id,
        repo_id=repo.id,
        name="primary",
        is_primary=not named_primary,
        enabled=named_primary,
    )
    assignment = _seed_waiting(
        stores["assignment"],
        f"rev6-missing-{named_primary}",
        project_id=project_id,
        requested_host_id=host_id,
    )
    coordinator = _coordinator(
        stores,
        registry=FakeHostRegistry({host_id: _conn(host_id, owner=ALICE)}),
        host_store=FakeHostStore([_FakeHost(host_id, ALICE)]),
        permission_store=FakePermissionStore(),
    )
    try:
        coordinator.trigger(assignment.id)
        await coordinator.wait_for_idle()
        row = stores["assignment"].get(assignment.id)
        assert row is not None and row.state == "waiting"
        assert row.wait_reason == "binding_missing:primary"
        assert row.active_attempt_id is None
    finally:
        await coordinator.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("second_usable", [True, False])
async def test_hostless_selection_skips_unusable_primary(
    db_uri: str, monkeypatch: pytest.MonkeyPatch, second_usable: bool
) -> None:
    stores = _stores(db_uri)
    host_a, host_b = _uid("rev6-hostless-a"), _uid("rev6-hostless-b")
    project_id = _uid(f"rev6-hostless-{second_usable}-project")
    _make_project(stores["project"], project_id)
    repo_x = _make_repo(stores["repository"], project_id, "X")
    repo_y = _make_repo(stores["repository"], project_id, "Y")
    _make_binding(
        stores["binding"],
        project_id=project_id,
        host_id=host_a,
        repo_id=repo_x.id,
        name="a",
        is_primary=True,
    )
    binding_b = _make_binding(
        stores["binding"],
        project_id=project_id,
        host_id=host_b,
        repo_id=repo_y.id if second_usable else repo_x.id,
        name="b",
        is_primary=True,
    )
    assignment = _seed_waiting(
        stores["assignment"],
        f"rev6-hostless-{second_usable}",
        project_id=project_id,
        inputs=[_input("Y", revision=repo_y.revision)],
    )
    monkeypatch.setattr(assignments_mod, "prepare_assignment_on_host", _prepare_ok({"Y": "/w"}))
    _install_placement_fakes(monkeypatch)
    coordinator = _coordinator(
        stores,
        registry=FakeHostRegistry(
            {
                host_a: _conn(host_a, owner=ALICE),
                host_b: _conn(host_b, owner=ALICE),
            }
        ),
        host_store=FakeHostStore([_FakeHost(host_a, ALICE), _FakeHost(host_b, ALICE)]),
        permission_store=FakePermissionStore(),
    )
    try:
        coordinator.trigger(assignment.id)
        await coordinator.wait_for_idle()
        row = stores["assignment"].get(assignment.id)
        assert row is not None
        if second_usable:
            assert row.resolved_host_id == host_b
            assert row.resolved_binding_id == binding_b.id
        else:
            assert row.state == "waiting"
            assert row.wait_reason == "binding_mismatch:primary"
    finally:
        await coordinator.shutdown()


async def test_initial_assignment_event_says_complete_is_last_step() -> None:
    from omnigent.server.assignments import build_initial_event_text

    assignment = Assignment(
        id=_uid("rev6-event"),
        project_id=_uid("rev6-event-project"),
        source_session_id=_uid("rev6-event-source"),
        target_agent_id=AGENT_ID,
        task="Do work",
        inputs=[_input()],
        idempotency_key="rev6-event",
        request_digest="e" * 64,
    )
    text = build_initial_event_text(assignment, "attempt", {"root": "/w"})
    assert "sys_assignment_complete" in text
    assert "last work step" in text
    assert "sys_assignment_dispatch" in text
    assert "before" in text
    assert "session is closed after" in text


async def test_initial_assignment_event_names_the_worktree_when_launched_at_entry() -> None:
    """R-ASSIGN: launching at E != T adds a Worktree line naming T, and keeps
    the "work only in the directories above" sentence (multi-repository
    assignments still need it)."""
    from omnigent.server.assignments import build_initial_event_text

    assignment = Assignment(
        id=_uid("wt-event"),
        project_id=_uid("wt-event-project"),
        source_session_id=_uid("wt-event-source"),
        target_agent_id=AGENT_ID,
        task="Do work",
        inputs=[_input()],
        idempotency_key="wt-event",
        request_digest="e" * 64,
    )
    text = build_initial_event_text(
        assignment, "attempt", {"root": "/w"}, workspace="/entry", worktree="/entry/wt/root"
    )
    assert "Worktree: /entry/wt/root" in text
    assert "/entry" in text
    assert "Work only in the directories above, commit the work there" in text

    # Launched at the execution root itself (workspace == worktree, the
    # no-entry case): no Worktree line.
    same_text = build_initial_event_text(
        assignment, "attempt", {"root": "/w"}, workspace="/w", worktree="/w"
    )
    assert "Worktree:" not in same_text


# ── 14. active liveness ───────────────────────────────────────────────────


def _make_session_with_runner(
    stores: dict[str, Any],
    conv_id: str,
    *,
    host_id: str,
    workspace: str,
    runner_id: str,
    project_id: str,
) -> Any:
    stores["conversation"].create_conversation(
        agent_id=AGENT_ID,
        title="active",
        host_id=host_id,
        workspace=workspace,
        conversation_id=conv_id,
        project_id=project_id,
    )
    stores["conversation"].replace_runner_id(conv_id, runner_id)
    updated = stores["conversation"].get_conversation(conv_id)
    assert updated is not None
    return updated


@pytest.mark.asyncio
async def test_running_online_clears_lease(db_uri: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """An online runner stays running with a short recheck and no lease."""
    stores = _stores(db_uri)
    host_id = _uid("host-a")
    project_id = _uid("live-proj")
    _make_project(stores["project"], project_id, owner=ALICE, enabled=True)
    repo = _make_repo(stores["repository"], project_id, "root")
    _make_binding(
        stores["binding"], project_id=project_id, host_id=host_id, repo_id=repo.id, workspace="/w"
    )
    clock = {"now": 5000}
    monkeypatch.setattr(assignments_mod.time, "time", lambda: clock["now"])
    assignment = _seed_waiting(
        stores["assignment"],
        "live",
        project_id=project_id,
        owner=ALICE,
        requested_host_id=host_id,
        inputs=[_input("root", revision=repo.revision)],
    )
    attempt = stores["assignment"].claim_attempt(assignment.id, host_id=host_id, now=clock["now"])
    assert attempt is not None
    assert (
        stores["assignment"].transition(assignment.id, from_state="starting", to_state="running")
        is not None
    )
    session_id = hashlib.sha256(f"assignment-attempt:{attempt.id}".encode()).hexdigest()[:32]
    assert (
        stores["assignment"].update_attempt(
            assignment.id, attempt.id, session_id=session_id, runner_id="runner_1"
        )
        is not None
    )
    _make_session_with_runner(
        stores,
        session_id,
        host_id=host_id,
        workspace="/w",
        runner_id="runner_1",
        project_id=project_id,
    )
    stores["assignment"].set_lease(attempt.id, clock["now"] + 90)
    assert (
        stores["assignment"].reschedule(
            assignment.id, expected_state="running", next_check_at=clock["now"] - 10
        )
        is not None
    )
    coordinator = _coordinator(
        stores,
        registry=FakeHostRegistry({host_id: _conn(host_id, owner=ALICE, assignments=True)}),
        host_store=FakeHostStore([_FakeHost(host_id, ALICE)]),
        permission_store=FakePermissionStore(),
        runner_router=FakeRunnerRouter({"runner_1"}),
        runner_exit_reports=FakeExitReports(),
    )
    coordinator.trigger(assignment.id)
    await coordinator.wait_for_idle()
    await coordinator.shutdown()

    row = stores["assignment"].get(assignment.id)
    assert row is not None
    assert row.state == "running"
    assert row.wait_reason is None
    assert row.next_check_at == clock["now"] + 30
    kept = stores["assignment"].get_attempt(assignment.id, attempt.id)
    assert kept is not None
    assert kept.lease_expires_at is None


@pytest.mark.asyncio
async def test_running_offline_leases_then_interrupts_then_recovers(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An offline runner leases, interrupts with the link kept, then retires."""
    import omnigent.server.routes.sessions as sessions_routes

    stores = _stores(db_uri)
    host_id = _uid("host-a")
    project_id = _uid("offline-proj")
    _make_project(stores["project"], project_id, owner=ALICE, enabled=True)
    repo = _make_repo(stores["repository"], project_id, "root")
    _make_binding(
        stores["binding"], project_id=project_id, host_id=host_id, repo_id=repo.id, workspace="/w"
    )
    clock = {"now": 6000}
    monkeypatch.setattr(assignments_mod.time, "time", lambda: clock["now"])
    assignment = _seed_waiting(
        stores["assignment"],
        "offline",
        project_id=project_id,
        owner=ALICE,
        requested_host_id=host_id,
        inputs=[_input("root", revision=repo.revision)],
    )
    attempt = stores["assignment"].claim_attempt(assignment.id, host_id=host_id, now=clock["now"])
    assert attempt is not None
    assert (
        stores["assignment"].transition(assignment.id, from_state="starting", to_state="running")
        is not None
    )
    session_id = hashlib.sha256(f"assignment-attempt:{attempt.id}".encode()).hexdigest()[:32]
    assert (
        stores["assignment"].update_attempt(
            assignment.id, attempt.id, session_id=session_id, runner_id="runner_1"
        )
        is not None
    )
    _make_session_with_runner(
        stores,
        session_id,
        host_id=host_id,
        workspace="/w",
        runner_id="runner_1",
        project_id=project_id,
    )
    assert (
        stores["assignment"].reschedule(
            assignment.id, expected_state="running", next_check_at=clock["now"] - 10
        )
        is not None
    )
    registry = FakeHostRegistry({})
    coordinator = _coordinator(
        stores,
        registry=registry,
        host_store=FakeHostStore([_FakeHost(host_id, ALICE)]),
        permission_store=FakePermissionStore(),
        runner_router=FakeRunnerRouter(),
        runner_exit_reports=FakeExitReports(),
    )
    coordinator.trigger(assignment.id)
    await coordinator.wait_for_idle()
    row = stores["assignment"].get(assignment.id)
    assert row is not None
    assert row.state == "running"
    kept = stores["assignment"].get_attempt(assignment.id, attempt.id)
    assert kept is not None
    assert kept.lease_expires_at == clock["now"] + 90
    assert row.next_check_at == clock["now"] + 90

    clock["now"] += 91
    coordinator.trigger(assignment.id)
    await coordinator.wait_for_idle()
    row = stores["assignment"].get(assignment.id)
    assert row is not None
    assert row.state == "interrupted"
    assert row.wait_reason is not None and row.wait_reason.startswith("runner_lost:")
    assert "runner_1" in row.wait_reason
    kept = stores["assignment"].get_attempt(assignment.id, attempt.id)
    assert kept is not None
    assert kept.state == "active"
    assert kept.ended_at is None
    assert row.next_check_at is not None

    async def _unknown_runner(*args: Any, **kwargs: Any) -> str:
        return "unknown_runner"

    monkeypatch.setattr(sessions_routes, "_stop_session_host_runner_outcome", _unknown_runner)
    registry._conns[host_id] = _conn(host_id, owner=ALICE, assignments=True)
    clock["now"] += 20
    # Re-arm the check so the due pass would pick it up.
    assert (
        stores["assignment"].reschedule(
            assignment.id,
            expected_state="interrupted",
            expected_active_attempt_id=attempt.id,
            next_check_at=clock["now"] - 1,
        )
        is not None
    )
    coordinator.trigger(assignment.id)
    await coordinator.wait_for_idle()
    await coordinator.shutdown()
    row = stores["assignment"].get(assignment.id)
    assert row is not None
    assert row.state == "interrupted"
    kept = stores["assignment"].get_attempt(assignment.id, attempt.id)
    assert kept is not None
    assert kept.state == "lost"
    assert kept.ended_at is not None
    assert row.next_check_at is None
    retried = stores["assignment"].transition(
        assignment.id,
        from_state="interrupted",
        to_state="waiting",
        expected_active_attempt_id=kept.id,
        active_attempt_id=None,
        next_check_at=kept.ended_at,
    )
    assert retried is not None


@pytest.mark.asyncio
async def test_relaunched_session_counts_as_not_alive(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session pointing at a newer runner leaves the old attempt for dead."""
    stores = _stores(db_uri)
    host_id = _uid("host-a")
    project_id = _uid("relaunch-proj")
    _make_project(stores["project"], project_id, owner=ALICE, enabled=True)
    repo = _make_repo(stores["repository"], project_id, "root")
    _make_binding(
        stores["binding"], project_id=project_id, host_id=host_id, repo_id=repo.id, workspace="/w"
    )
    clock = {"now": 7000}
    monkeypatch.setattr(assignments_mod.time, "time", lambda: clock["now"])
    assignment = _seed_waiting(
        stores["assignment"],
        "relaunch",
        project_id=project_id,
        owner=ALICE,
        requested_host_id=host_id,
        inputs=[_input("root", revision=repo.revision)],
    )
    attempt = stores["assignment"].claim_attempt(assignment.id, host_id=host_id, now=clock["now"])
    assert attempt is not None
    assert (
        stores["assignment"].transition(assignment.id, from_state="starting", to_state="running")
        is not None
    )
    session_id = hashlib.sha256(f"assignment-attempt:{attempt.id}".encode()).hexdigest()[:32]
    assert (
        stores["assignment"].update_attempt(
            assignment.id, attempt.id, session_id=session_id, runner_id="runner_old"
        )
        is not None
    )
    _make_session_with_runner(
        stores,
        session_id,
        host_id=host_id,
        workspace="/w",
        runner_id="runner_new",
        project_id=project_id,
    )
    assert (
        stores["assignment"].reschedule(
            assignment.id, expected_state="running", next_check_at=clock["now"] - 10
        )
        is not None
    )
    coordinator = _coordinator(
        stores,
        registry=FakeHostRegistry({host_id: _conn(host_id, owner=ALICE, assignments=True)}),
        host_store=FakeHostStore([_FakeHost(host_id, ALICE)]),
        permission_store=FakePermissionStore(),
        runner_router=FakeRunnerRouter({"runner_old", "runner_new"}),
        runner_exit_reports=FakeExitReports(),
    )
    coordinator.trigger(assignment.id)
    await coordinator.wait_for_idle()
    await coordinator.shutdown()

    kept = stores["assignment"].get_attempt(assignment.id, attempt.id)
    assert kept is not None
    assert kept.lease_expires_at == clock["now"] + 90
    row = stores["assignment"].get(assignment.id)
    assert row is not None
    assert row.state == "running"


@pytest.mark.asyncio
async def test_disabled_project_keeps_running_online(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The switch never pauses an attempt that already started."""
    stores = _stores(db_uri)
    host_id = _uid("host-a")
    project_id = _uid("switch-proj")
    _make_project(stores["project"], project_id, owner=ALICE, enabled=True)
    repo = _make_repo(stores["repository"], project_id, "root")
    _make_binding(
        stores["binding"], project_id=project_id, host_id=host_id, repo_id=repo.id, workspace="/w"
    )
    clock = {"now": 8000}
    monkeypatch.setattr(assignments_mod.time, "time", lambda: clock["now"])
    assignment = _seed_waiting(
        stores["assignment"],
        "switch",
        project_id=project_id,
        owner=ALICE,
        requested_host_id=host_id,
        inputs=[_input("root", revision=repo.revision)],
    )
    attempt = stores["assignment"].claim_attempt(assignment.id, host_id=host_id, now=clock["now"])
    assert attempt is not None
    assert (
        stores["assignment"].transition(assignment.id, from_state="starting", to_state="running")
        is not None
    )
    session_id = hashlib.sha256(f"assignment-attempt:{attempt.id}".encode()).hexdigest()[:32]
    assert (
        stores["assignment"].update_attempt(
            assignment.id, attempt.id, session_id=session_id, runner_id="runner_1"
        )
        is not None
    )
    _make_session_with_runner(
        stores,
        session_id,
        host_id=host_id,
        workspace="/w",
        runner_id="runner_1",
        project_id=project_id,
    )
    assert (
        stores["assignment"].reschedule(
            assignment.id, expected_state="running", next_check_at=clock["now"] - 10
        )
        is not None
    )
    proj = stores["project"].get(project_id, user_id=ALICE)
    assert proj is not None
    assert (
        stores["project"].set_collaboration(
            project_id,
            user_id=ALICE,
            enabled=False,
            expected_revision=proj.collaboration_revision,
        )
        is not None
    )
    coordinator = _coordinator(
        stores,
        registry=FakeHostRegistry({host_id: _conn(host_id, owner=ALICE, assignments=True)}),
        host_store=FakeHostStore([_FakeHost(host_id, ALICE)]),
        permission_store=FakePermissionStore(),
        runner_router=FakeRunnerRouter({"runner_1"}),
        runner_exit_reports=FakeExitReports(),
    )
    coordinator.trigger(assignment.id)
    await coordinator.wait_for_idle()
    await coordinator.shutdown()

    row = stores["assignment"].get(assignment.id)
    assert row is not None
    assert row.state == "running"
    assert row.wait_reason is None


# ── 15. orphaned placements ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_orphaned_starting_with_runner_interrupts(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A starting row that launched but never reported stays never-starting."""
    import omnigent.server.routes.sessions as sessions_routes

    stores = _stores(db_uri)
    host_id = _uid("host-a")
    project_id = _uid("orphan-proj")
    _make_project(stores["project"], project_id, owner=ALICE, enabled=True)
    repo = _make_repo(stores["repository"], project_id, "root")
    _make_binding(
        stores["binding"], project_id=project_id, host_id=host_id, repo_id=repo.id, workspace="/w"
    )
    clock = {"now": 9000}
    monkeypatch.setattr(assignments_mod.time, "time", lambda: clock["now"])
    assignment = _seed_waiting(
        stores["assignment"],
        "orphan",
        project_id=project_id,
        owner=ALICE,
        requested_host_id=host_id,
        inputs=[_input("root", revision=repo.revision)],
    )
    attempt = stores["assignment"].claim_attempt(assignment.id, host_id=host_id, now=clock["now"])
    assert attempt is not None
    session_id = hashlib.sha256(f"assignment-attempt:{attempt.id}".encode()).hexdigest()[:32]
    assert (
        stores["assignment"].update_attempt(
            assignment.id, attempt.id, session_id=session_id, runner_id="runner_1"
        )
        is not None
    )
    _make_session_with_runner(
        stores,
        session_id,
        host_id=host_id,
        workspace="/w",
        runner_id="runner_1",
        project_id=project_id,
    )

    async def _unavailable(*args: Any, **kwargs: Any) -> str:
        return "unavailable"

    monkeypatch.setattr(sessions_routes, "_stop_session_host_runner_outcome", _unavailable)
    clock["now"] += 421
    coordinator = _coordinator(
        stores,
        registry=FakeHostRegistry({host_id: _conn(host_id, owner=ALICE, assignments=True)}),
        host_store=FakeHostStore([_FakeHost(host_id, ALICE)]),
        permission_store=FakePermissionStore(),
        runner_router=FakeRunnerRouter(),
        runner_exit_reports=FakeExitReports(),
    )
    coordinator.trigger(assignment.id)
    await coordinator.wait_for_idle()
    await coordinator.shutdown()

    row = stores["assignment"].get(assignment.id)
    assert row is not None
    assert row.state == "interrupted"
    assert row.wait_reason == "placement_abandoned"


@pytest.mark.asyncio
async def test_orphaned_starting_without_session_returns_to_waiting(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A starting row that never launched goes back to waiting as abandoned."""
    stores = _stores(db_uri)
    host_id = _uid("host-a")
    project_id = _uid("orphan-wait-proj")
    _make_project(stores["project"], project_id, owner=ALICE, enabled=True)
    repo = _make_repo(stores["repository"], project_id, "root")
    _make_binding(
        stores["binding"], project_id=project_id, host_id=host_id, repo_id=repo.id, workspace="/w"
    )
    clock = {"now": 9100}
    monkeypatch.setattr(assignments_mod.time, "time", lambda: clock["now"])
    assignment = _seed_waiting(
        stores["assignment"],
        "orphan-wait",
        project_id=project_id,
        owner=ALICE,
        requested_host_id=host_id,
        inputs=[_input("root", revision=repo.revision)],
    )
    attempt = stores["assignment"].claim_attempt(assignment.id, host_id=host_id, now=clock["now"])
    assert attempt is not None
    clock["now"] += 421
    coordinator = _coordinator(
        stores,
        registry=FakeHostRegistry({host_id: _conn(host_id, owner=ALICE, assignments=True)}),
        host_store=FakeHostStore([_FakeHost(host_id, ALICE)]),
        permission_store=FakePermissionStore(),
        runner_router=FakeRunnerRouter(),
        runner_exit_reports=FakeExitReports(),
    )
    coordinator.trigger(assignment.id)
    await coordinator.wait_for_idle()
    await coordinator.shutdown()

    row = stores["assignment"].get(assignment.id)
    assert row is not None
    assert row.state == "waiting"
    assert row.wait_reason == "placement_abandoned"
    assert row.active_attempt_id is None
    kept = stores["assignment"].get_attempt(assignment.id, attempt.id)
    assert kept is not None
    assert kept.state == "finished"
    assert kept.error_code == "placement_abandoned"


@pytest.mark.asyncio
async def test_starting_in_flight_skipped_by_due_pass(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The due pass never double-runs an id already being evaluated."""
    import asyncio as _asyncio

    stores = _stores(db_uri)
    host_id = _uid("host-a")
    project_id = _uid("inflight-proj")
    _make_project(stores["project"], project_id, owner=ALICE, enabled=True)
    repo = _make_repo(stores["repository"], project_id, "root")
    _make_binding(
        stores["binding"], project_id=project_id, host_id=host_id, repo_id=repo.id, workspace="/w"
    )
    assignment = _seed_waiting(
        stores["assignment"],
        "inflight",
        project_id=project_id,
        owner=ALICE,
        requested_host_id=host_id,
        inputs=[_input("root", revision=repo.revision)],
    )
    fake_prepare = _prepare_ok({"root": "/prepared"})
    monkeypatch.setattr(assignments_mod, "prepare_assignment_on_host", fake_prepare)
    parked: _asyncio.Event = _asyncio.Event()
    release: _asyncio.Event = _asyncio.Event()
    _install_placement_fakes(monkeypatch)

    import omnigent.server.routes.sessions as sessions_routes

    async def _parked_launch(*args: Any, **kwargs: Any) -> Any:
        parked.set()
        await release.wait()
        return SimpleNamespace(error=None, runner_id="runner_1")

    monkeypatch.setattr(sessions_routes, "_launch_runner_on_host", _parked_launch)
    coordinator = _coordinator(
        stores,
        registry=FakeHostRegistry({host_id: _conn(host_id, owner=ALICE, assignments=True)}),
        host_store=FakeHostStore([_FakeHost(host_id, ALICE)]),
        permission_store=FakePermissionStore(),
        runner_router=FakeRunnerRouter(),
        runner_exit_reports=FakeExitReports(),
    )
    coordinator.trigger(assignment.id)
    await _asyncio.wait_for(parked.wait(), timeout=10)
    row = stores["assignment"].get(assignment.id)
    assert row is not None and row.state == "starting"
    # Force the row due so the pass would take it without the guard.
    past = now_epoch() - 100
    assert (
        stores["assignment"].reschedule(
            assignment.id, expected_state="starting", next_check_at=past
        )
        is not None
    )
    await coordinator._due_pass_and_schedule()
    row = stores["assignment"].get(assignment.id)
    assert row is not None
    assert row.next_check_at == past
    assert getattr(fake_prepare, "calls", 0) == 1
    release.set()
    await coordinator.wait_for_idle()
    await coordinator.shutdown()
    final = stores["assignment"].get(assignment.id)
    assert final is not None
    assert final.state == "running"


# ── 16. cancellation ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stopping_acked_cancels_and_releases(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A confirmed stop cancels the attempt and releases the worktree."""
    import omnigent.server.routes.sessions as sessions_routes
    from omnigent.host.frames import HostAssignmentReleaseResultFrame

    stores = _stores(db_uri)
    host_id = _uid("host-a")
    project_id = _uid("stop-proj")
    _make_project(stores["project"], project_id, owner=ALICE, enabled=True)
    repo = _make_repo(stores["repository"], project_id, "root")
    binding = _make_binding(
        stores["binding"],
        project_id=project_id,
        host_id=host_id,
        repo_id=repo.id,
        workspace="/w",
    )
    clock = {"now": 10000}
    monkeypatch.setattr(assignments_mod.time, "time", lambda: clock["now"])
    assignment = _seed_waiting(
        stores["assignment"],
        "stopping",
        project_id=project_id,
        owner=ALICE,
        requested_host_id=host_id,
        inputs=[_input("root", revision=repo.revision)],
    )
    attempt = stores["assignment"].claim_attempt(
        assignment.id,
        host_id=host_id,
        now=clock["now"],
        resolved_binding_id=binding.id,
        resolved_binding_revision=binding.revision,
        next_check_at=clock["now"],
    )
    assert attempt is not None
    assert (
        stores["assignment"].transition(assignment.id, from_state="starting", to_state="running")
        is not None
    )
    assert (
        stores["assignment"].transition(
            assignment.id,
            from_state="running",
            to_state="stopping",
            expected_active_attempt_id=attempt.id,
        )
        is not None
    )
    session_id = hashlib.sha256(f"assignment-attempt:{attempt.id}".encode()).hexdigest()[:32]
    assert (
        stores["assignment"].update_attempt(
            assignment.id, attempt.id, session_id=session_id, runner_id="runner_1"
        )
        is not None
    )
    _make_session_with_runner(
        stores,
        session_id,
        host_id=host_id,
        workspace="/w",
        runner_id="runner_1",
        project_id=project_id,
    )
    # Pin the release snapshot the claim wrote.
    row = stores["assignment"].get(assignment.id)
    assert row is not None and row.resolved_binding_id == binding.id

    async def _acked(*args: Any, **kwargs: Any) -> str:
        return "acked"

    monkeypatch.setattr(sessions_routes, "_stop_session_host_runner_outcome", _acked)
    released: dict[str, Any] = {}

    async def _ok(**kwargs: Any) -> HostAssignmentReleaseResultFrame:
        frame = kwargs.get("frame")
        released["frame"] = frame
        return HostAssignmentReleaseResultFrame(
            request_id=frame.request_id, status="ok", removed=["root"], failures={}
        )

    monkeypatch.setattr(assignments_mod, "release_assignment_on_host", _ok)
    coordinator = _coordinator(
        stores,
        registry=FakeHostRegistry({host_id: _conn(host_id, owner=ALICE, assignments=True)}),
        host_store=FakeHostStore([_FakeHost(host_id, ALICE)]),
        permission_store=FakePermissionStore(),
        runner_router=FakeRunnerRouter(),
        runner_exit_reports=FakeExitReports(),
    )
    coordinator.trigger(assignment.id)
    await coordinator.wait_for_idle()
    await coordinator.shutdown()

    row = stores["assignment"].get(assignment.id)
    assert row is not None
    assert row.state == "cancelled"
    kept = stores["assignment"].get_attempt(assignment.id, attempt.id)
    assert kept is not None
    assert kept.state == "finished"
    assert kept.error_code == "cancelled"
    assert row.next_check_at is None
    frame = released.get("frame")
    assert frame is not None
    assert [r.repository_name for r in frame.repositories] == ["root"]
    assert frame.repositories[0].source_directory == "/w"


@pytest.mark.asyncio
async def test_stopping_unavailable_leases_then_interrupts(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unconfirmed stop retries on a lease, then gives up as interrupted."""
    import omnigent.server.routes.sessions as sessions_routes

    stores = _stores(db_uri)
    host_id = _uid("host-a")
    project_id = _uid("stop-retry-proj")
    _make_project(stores["project"], project_id, owner=ALICE, enabled=True)
    repo = _make_repo(stores["repository"], project_id, "root")
    _make_binding(
        stores["binding"], project_id=project_id, host_id=host_id, repo_id=repo.id, workspace="/w"
    )
    clock = {"now": 10100}
    monkeypatch.setattr(assignments_mod.time, "time", lambda: clock["now"])
    assignment = _seed_waiting(
        stores["assignment"],
        "stop-retry",
        project_id=project_id,
        owner=ALICE,
        requested_host_id=host_id,
        inputs=[_input("root", revision=repo.revision)],
    )
    attempt = stores["assignment"].claim_attempt(assignment.id, host_id=host_id, now=clock["now"])
    assert attempt is not None
    assert (
        stores["assignment"].transition(assignment.id, from_state="starting", to_state="running")
        is not None
    )
    assert (
        stores["assignment"].transition(
            assignment.id,
            from_state="running",
            to_state="stopping",
            expected_active_attempt_id=attempt.id,
        )
        is not None
    )
    session_id = hashlib.sha256(f"assignment-attempt:{attempt.id}".encode()).hexdigest()[:32]
    assert (
        stores["assignment"].update_attempt(
            assignment.id, attempt.id, session_id=session_id, runner_id="runner_1"
        )
        is not None
    )
    _make_session_with_runner(
        stores,
        session_id,
        host_id=host_id,
        workspace="/w",
        runner_id="runner_1",
        project_id=project_id,
    )

    async def _unavailable(*args: Any, **kwargs: Any) -> str:
        return "unavailable"

    monkeypatch.setattr(sessions_routes, "_stop_session_host_runner_outcome", _unavailable)
    coordinator = _coordinator(
        stores,
        registry=FakeHostRegistry({host_id: _conn(host_id, owner=ALICE, assignments=True)}),
        host_store=FakeHostStore([_FakeHost(host_id, ALICE)]),
        permission_store=FakePermissionStore(),
        runner_router=FakeRunnerRouter(),
        runner_exit_reports=FakeExitReports(),
    )
    coordinator.trigger(assignment.id)
    await coordinator.wait_for_idle()
    row = stores["assignment"].get(assignment.id)
    assert row is not None
    assert row.state == "stopping"
    kept = stores["assignment"].get_attempt(assignment.id, attempt.id)
    assert kept is not None
    assert kept.lease_expires_at == clock["now"] + 90
    assert row.next_check_at == clock["now"] + 30

    clock["now"] += 91
    assert (
        stores["assignment"].reschedule(
            assignment.id,
            expected_state="stopping",
            expected_active_attempt_id=attempt.id,
            next_check_at=clock["now"] - 1,
        )
        is not None
    )
    coordinator.trigger(assignment.id)
    await coordinator.wait_for_idle()
    await coordinator.shutdown()
    row = stores["assignment"].get(assignment.id)
    assert row is not None
    assert row.state == "interrupted"
    assert row.wait_reason == "stop_unconfirmed"


# ── 17. terminal release ────────────────────────────────────────────────


def _seed_terminal(
    stores: dict[str, Any],
    seed: str,
    *,
    project_id: str,
    host_id: str | None,
    to_state: str,
    clock_now: int,
) -> Assignment:
    assignment = _seed_waiting(
        stores["assignment"],
        seed,
        project_id=project_id,
        owner=ALICE,
        requested_host_id=host_id,
        inputs=[_input("root", revision=1)],
    )
    repo = stores["repository"].get_by_name(project_id=project_id, name="root")
    assert repo is not None
    if host_id is None:
        assert to_state == "expired"
        assert (
            stores["assignment"].transition(
                assignment.id, from_state="waiting", to_state="expired"
            )
            is not None
        )
        assert (
            stores["assignment"].reschedule(
                assignment.id, expected_state="expired", next_check_at=clock_now
            )
            is not None
        )
        row = stores["assignment"].get(assignment.id)
        assert row is not None
        return row
    binding = stores["binding"].get_by_name(project_id=project_id, host_id=host_id, name="primary")
    assert binding is not None
    attempt = stores["assignment"].claim_attempt(
        assignment.id,
        host_id=host_id,
        now=clock_now,
        resolved_binding_id=binding.id,
        resolved_binding_revision=binding.revision,
        next_check_at=clock_now,
    )
    assert attempt is not None
    assert (
        stores["assignment"].transition(assignment.id, from_state="starting", to_state="running")
        is not None
    )
    if to_state == "succeeded":
        assert (
            stores["assignment"].update_attempt(
                assignment.id, attempt.id, ended_at=clock_now - 601
            )
            is not None
        )
        assert (
            stores["assignment"].transition(
                assignment.id, from_state="running", to_state="publishing"
            )
            is not None
        )
        from omnigent.entities import AssignmentOutputEntry

        assert (
            stores["assignment"].transition(
                assignment.id,
                from_state="publishing",
                to_state="succeeded",
                expected_active_attempt_id=attempt.id,
                outputs=[
                    AssignmentOutputEntry(
                        repository_name="root",
                        commit="b" * 40,
                        ref="refs/omnigent/assignments/x/output/att/root",
                    )
                ],
            )
            is not None
        )
    elif to_state == "cancelled":
        assert (
            stores["assignment"].transition(
                assignment.id,
                from_state="running",
                to_state="stopping",
                expected_active_attempt_id=attempt.id,
            )
            is not None
        )
        assert (
            stores["assignment"].transition(
                assignment.id,
                from_state="stopping",
                to_state="cancelled",
                expected_active_attempt_id=attempt.id,
            )
            is not None
        )
    else:  # pragma: no cover
        raise AssertionError(to_state)
    assert (
        stores["assignment"].reschedule(
            assignment.id, expected_state=to_state, next_check_at=clock_now
        )
        is not None
    )
    row = stores["assignment"].get(assignment.id)
    assert row is not None
    return row


@pytest.mark.asyncio
async def test_terminal_release_ok_clears(db_uri: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """A due succeeded row releases once and clears its check."""
    from omnigent.host.frames import HostAssignmentReleaseResultFrame

    stores = _stores(db_uri)
    host_id = _uid("host-a")
    project_id = _uid("rel-ok-proj")
    _make_project(stores["project"], project_id, owner=ALICE, enabled=True)
    repo = _make_repo(stores["repository"], project_id, "root")
    _make_binding(
        stores["binding"], project_id=project_id, host_id=host_id, repo_id=repo.id, workspace="/w"
    )
    clock = {"now": 11000}
    monkeypatch.setattr(assignments_mod.time, "time", lambda: clock["now"])
    assignment = _seed_terminal(
        stores,
        "rel-ok",
        project_id=project_id,
        host_id=host_id,
        to_state="succeeded",
        clock_now=clock["now"],
    )
    calls = {"n": 0}

    async def _ok(**kwargs: Any) -> HostAssignmentReleaseResultFrame:
        calls["n"] += 1
        frame = kwargs.get("frame")
        assert [r.repository_name for r in frame.repositories] == ["root"]
        assert frame.repositories[0].source_directory == "/w"
        return HostAssignmentReleaseResultFrame(
            request_id=frame.request_id, status="ok", removed=["root"], failures={}
        )

    monkeypatch.setattr(assignments_mod, "release_assignment_on_host", _ok)
    coordinator = _coordinator(
        stores,
        registry=FakeHostRegistry({host_id: _conn(host_id, owner=ALICE, assignments=True)}),
        host_store=FakeHostStore([_FakeHost(host_id, ALICE)]),
        permission_store=FakePermissionStore(),
        runner_router=FakeRunnerRouter(),
        runner_exit_reports=FakeExitReports(),
    )
    coordinator.trigger(assignment.id)
    await coordinator.wait_for_idle()
    await coordinator.shutdown()
    assert calls["n"] == 1
    row = stores["assignment"].get(assignment.id)
    assert row is not None
    assert row.next_check_at is None


@pytest.mark.asyncio
async def test_terminal_release_partial_messages_once(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partial release records one message and never retries."""
    from omnigent.host.frames import HostAssignmentReleaseResultFrame

    stores = _stores(db_uri)
    host_id = _uid("host-a")
    project_id = _uid("rel-partial-proj")
    _make_project(stores["project"], project_id, owner=ALICE, enabled=True)
    repo = _make_repo(stores["repository"], project_id, "root")
    _make_binding(
        stores["binding"], project_id=project_id, host_id=host_id, repo_id=repo.id, workspace="/w"
    )
    clock = {"now": 11100}
    monkeypatch.setattr(assignments_mod.time, "time", lambda: clock["now"])
    assignment = _seed_terminal(
        stores,
        "rel-partial",
        project_id=project_id,
        host_id=host_id,
        to_state="succeeded",
        clock_now=clock["now"],
    )
    calls = {"n": 0}

    async def _partial(**kwargs: Any) -> HostAssignmentReleaseResultFrame:
        calls["n"] += 1
        frame = kwargs.get("frame")
        return HostAssignmentReleaseResultFrame(
            request_id=frame.request_id,
            status="partial",
            removed=[],
            failures={"root": "worktree contains modified files"},
        )

    monkeypatch.setattr(assignments_mod, "release_assignment_on_host", _partial)
    coordinator = _coordinator(
        stores,
        registry=FakeHostRegistry({host_id: _conn(host_id, owner=ALICE, assignments=True)}),
        host_store=FakeHostStore([_FakeHost(host_id, ALICE)]),
        permission_store=FakePermissionStore(),
        runner_router=FakeRunnerRouter(),
        runner_exit_reports=FakeExitReports(),
    )
    coordinator.trigger(assignment.id)
    await coordinator.wait_for_idle()
    row = stores["assignment"].get(assignment.id)
    assert row is not None
    assert row.next_check_at is None
    messages = stores["assignment"].read_messages(assignment.id, limit=10)
    bodies = [m.body for m in messages.data]
    assert len([b for b in bodies if "worktree release incomplete" in b]) == 1
    assert any("root" in b for b in bodies if "worktree release incomplete" in b)
    clock["now"] += 100
    coordinator.trigger(assignment.id)
    await coordinator.wait_for_idle()
    await coordinator.shutdown()
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_terminal_release_host_offline_backs_off(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An offline host backs off without a release call."""
    stores = _stores(db_uri)
    host_id = _uid("host-a")
    project_id = _uid("rel-off-proj")
    _make_project(stores["project"], project_id, owner=ALICE, enabled=True)
    repo = _make_repo(stores["repository"], project_id, "root")
    _make_binding(
        stores["binding"], project_id=project_id, host_id=host_id, repo_id=repo.id, workspace="/w"
    )
    clock = {"now": 11200}
    monkeypatch.setattr(assignments_mod.time, "time", lambda: clock["now"])
    assignment = _seed_terminal(
        stores,
        "rel-off",
        project_id=project_id,
        host_id=host_id,
        to_state="succeeded",
        clock_now=clock["now"],
    )

    async def _must_not_release(**kwargs: Any) -> Any:
        raise AssertionError("release must not run while the host is offline")

    monkeypatch.setattr(assignments_mod, "release_assignment_on_host", _must_not_release)
    coordinator = _coordinator(
        stores,
        registry=FakeHostRegistry({}),
        host_store=FakeHostStore([_FakeHost(host_id, ALICE)]),
        permission_store=FakePermissionStore(),
        runner_router=FakeRunnerRouter(),
        runner_exit_reports=FakeExitReports(),
    )
    coordinator.trigger(assignment.id)
    await coordinator.wait_for_idle()
    await coordinator.shutdown()
    row = stores["assignment"].get(assignment.id)
    assert row is not None
    assert row.next_check_at is not None and row.next_check_at > clock["now"]


@pytest.mark.asyncio
async def test_terminal_release_stops_live_runner_first(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live runner is stopped before its worktree is removed."""
    import omnigent.server.routes.sessions as sessions_routes
    from omnigent.host.frames import HostAssignmentReleaseResultFrame

    stores = _stores(db_uri)
    host_id = _uid("host-a")
    project_id = _uid("rel-stop-proj")
    _make_project(stores["project"], project_id, owner=ALICE, enabled=True)
    repo = _make_repo(stores["repository"], project_id, "root")
    binding = _make_binding(
        stores["binding"], project_id=project_id, host_id=host_id, repo_id=repo.id, workspace="/w"
    )
    clock = {"now": 11300}
    monkeypatch.setattr(assignments_mod.time, "time", lambda: clock["now"])
    assignment = _seed_waiting(
        stores["assignment"],
        "rel-stop",
        project_id=project_id,
        owner=ALICE,
        requested_host_id=host_id,
        inputs=[_input("root", revision=repo.revision)],
    )
    attempt = stores["assignment"].claim_attempt(
        assignment.id,
        host_id=host_id,
        now=clock["now"],
        resolved_binding_id=binding.id,
        resolved_binding_revision=binding.revision,
        next_check_at=clock["now"],
    )
    assert attempt is not None
    assert (
        stores["assignment"].transition(assignment.id, from_state="starting", to_state="running")
        is not None
    )
    assert (
        stores["assignment"].transition(assignment.id, from_state="running", to_state="publishing")
        is not None
    )
    session_id = hashlib.sha256(f"assignment-attempt:{attempt.id}".encode()).hexdigest()[:32]
    assert (
        stores["assignment"].update_attempt(
            assignment.id,
            attempt.id,
            session_id=session_id,
            runner_id="runner_1",
            ended_at=clock["now"] - 601,
        )
        is not None
    )
    _make_session_with_runner(
        stores,
        session_id,
        host_id=host_id,
        workspace="/w",
        runner_id="runner_1",
        project_id=project_id,
    )
    from omnigent.entities import AssignmentOutputEntry

    assert (
        stores["assignment"].transition(
            assignment.id,
            from_state="publishing",
            to_state="succeeded",
            expected_active_attempt_id=attempt.id,
            outputs=[
                AssignmentOutputEntry(
                    repository_name="root",
                    commit="b" * 40,
                    ref="refs/omnigent/assignments/x/output/att/root",
                )
            ],
        )
        is not None
    )
    assert (
        stores["assignment"].reschedule(
            assignment.id, expected_state="succeeded", next_check_at=clock["now"]
        )
        is not None
    )
    order: list[str] = []

    async def _acked_stop(*args: Any, **kwargs: Any) -> str:
        order.append("stop")
        return "acked"

    async def _ok_release(**kwargs: Any) -> HostAssignmentReleaseResultFrame:
        order.append("release")
        frame = kwargs.get("frame")
        return HostAssignmentReleaseResultFrame(
            request_id=frame.request_id, status="ok", removed=["root"], failures={}
        )

    monkeypatch.setattr(sessions_routes, "_stop_session_host_runner_outcome", _acked_stop)
    monkeypatch.setattr(assignments_mod, "release_assignment_on_host", _ok_release)
    coordinator = _coordinator(
        stores,
        registry=FakeHostRegistry({host_id: _conn(host_id, owner=ALICE, assignments=True)}),
        host_store=FakeHostStore([_FakeHost(host_id, ALICE)]),
        permission_store=FakePermissionStore(),
        runner_router=FakeRunnerRouter({"runner_1"}),
        runner_exit_reports=FakeExitReports(),
    )
    coordinator.trigger(assignment.id)
    await coordinator.wait_for_idle()
    await coordinator.shutdown()
    assert order == ["stop", "release"]
    row = stores["assignment"].get(assignment.id)
    assert row is not None
    assert row.next_check_at is None


@pytest.mark.asyncio
async def test_guard_failure_skips_side_effects(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lost conditional write never reaches stop, release or messaging."""
    import omnigent.server.routes.sessions as sessions_routes

    stores = _stores(db_uri)
    host_id = _uid("host-a")
    project_id = _uid("guard-fail-proj")
    _make_project(stores["project"], project_id, owner=ALICE, enabled=True)
    repo = _make_repo(stores["repository"], project_id, "root")
    _make_binding(
        stores["binding"], project_id=project_id, host_id=host_id, repo_id=repo.id, workspace="/w"
    )
    clock = {"now": 11400}
    monkeypatch.setattr(assignments_mod.time, "time", lambda: clock["now"])
    assignment = _seed_waiting(
        stores["assignment"],
        "guard-fail",
        project_id=project_id,
        owner=ALICE,
        requested_host_id=host_id,
        inputs=[_input("root", revision=repo.revision)],
    )
    attempt = stores["assignment"].claim_attempt(assignment.id, host_id=host_id, now=clock["now"])
    assert attempt is not None
    assert (
        stores["assignment"].transition(assignment.id, from_state="starting", to_state="running")
        is not None
    )
    session_id = hashlib.sha256(f"assignment-attempt:{attempt.id}".encode()).hexdigest()[:32]
    assert (
        stores["assignment"].update_attempt(
            assignment.id, attempt.id, session_id=session_id, runner_id="runner_1"
        )
        is not None
    )
    _make_session_with_runner(
        stores,
        session_id,
        host_id=host_id,
        workspace="/w",
        runner_id="runner_1",
        project_id=project_id,
    )
    stores["assignment"].set_lease(attempt.id, clock["now"] - 1)
    assert (
        stores["assignment"].reschedule(
            assignment.id, expected_state="running", next_check_at=clock["now"] - 10
        )
        is not None
    )
    real_transition = stores["assignment"].transition
    calls = {"stop": 0, "release": 0, "message": 0}

    def _lost_transition(*args: Any, **kwargs: Any) -> Any:
        return None

    async def _count_stop(*args: Any, **kwargs: Any) -> str:
        calls["stop"] += 1
        return "acked"

    async def _count_release(**kwargs: Any) -> Any:
        calls["release"] += 1
        raise AssertionError("release must not run after a lost guard")

    monkeypatch.setattr(stores["assignment"], "transition", _lost_transition)
    monkeypatch.setattr(sessions_routes, "_stop_session_host_runner_outcome", _count_stop)
    monkeypatch.setattr(assignments_mod, "release_assignment_on_host", _count_release)
    orig_append = stores["assignment"].append_message

    def _spy_append(message: Any) -> Any:
        calls["message"] += 1
        return orig_append(message)

    monkeypatch.setattr(stores["assignment"], "append_message", _spy_append)
    coordinator = _coordinator(
        stores,
        registry=FakeHostRegistry({host_id: _conn(host_id, owner=ALICE, assignments=True)}),
        host_store=FakeHostStore([_FakeHost(host_id, ALICE)]),
        permission_store=FakePermissionStore(),
        runner_router=FakeRunnerRouter(),
        runner_exit_reports=FakeExitReports(),
    )
    coordinator.trigger(assignment.id)
    await coordinator.wait_for_idle()
    await coordinator.shutdown()
    assert real_transition is not None
    assert calls == {"stop": 0, "release": 0, "message": 0}


# ── 18. route triggers ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_running_triggers_with_next_check(
    db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancelling a running row parks a check and nudges the coordinator."""
    import httpx

    import omnigent.server.routes.assignments as routes_mod
    from omnigent.runtime.agent_cache import AgentCache
    from omnigent.server.app import create_app
    from omnigent.server.feature_flags import resolve_feature_flags
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
    from omnigent.stores.artifact_store.local import LocalArtifactStore
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore as _Conv,
    )
    from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
    from omnigent.stores.host_store import HostStore

    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    flags = resolve_feature_flags({"OMNIGENT_FEATURES": "project_assignments"})
    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=_Conv(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        host_store=HostStore(db_uri),
        project_store=SqlAlchemyProjectStore(db_uri),
        project_repository_store=SqlAlchemyProjectRepositoryStore(db_uri),
        project_host_binding_store=SqlAlchemyProjectHostBindingStore(db_uri),
        assignment_store=SqlAlchemyAssignmentStore(db_uri),
        feature_flags=flags,
    )

    class _FakeCoordinator:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def trigger(self, assignment_id: str) -> None:
            self.calls.append(assignment_id)

    fake = _FakeCoordinator()
    app.state.assignment_coordinator = fake  # type: ignore[attr-defined]
    fixed_now = 19500
    monkeypatch.setattr(routes_mod, "now_epoch", lambda: fixed_now)
    agent_store = SqlAlchemyAgentStore(db_uri)
    if agent_store.get(AGENT_ID) is None:
        agent_store.create(
            agent_id=AGENT_ID, name="test-agent", bundle_location=f"{AGENT_ID}/bundle"
        )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/v1/projects", json={"name": "CancelProj"})
        assert resp.status_code == 200, resp.text
        project_id = resp.json()["id"]
        assert (
            await client.patch(
                f"/v1/projects/{project_id}/collaboration",
                json={"enabled": True, "expected_revision": 0},
            )
        ).status_code == 200
        assert (
            await client.put(
                f"/v1/projects/{project_id}/repositories/root",
                json={
                    "remote_url": "https://example.com/org/repo.git",
                    "default_branch": "main",
                },
            )
        ).status_code == 200
        conv = _Conv(db_uri).create_conversation(
            title="src", agent_id=AGENT_ID, project_id=project_id
        )
        assignment_id = _uid("route-cancel")
        assert (
            await client.post(
                "/v1/assignments",
                json={
                    "id": assignment_id,
                    "source_session_id": conv.id,
                    "target_agent_id": AGENT_ID,
                    "task": "cancel task",
                    "repositories": [
                        {
                            "repository_name": "root",
                            "commit": "a" * 40,
                            "manifest_digest": "d" * 64,
                        }
                    ],
                    "idempotency_key": "key-cancel",
                },
            )
        ).status_code == 201
        assert (
            await client.post(
                f"/v1/assignments/{assignment_id}/published",
                json={"refs": [{"repository_name": "root", "commit": "a" * 40}]},
            )
        ).status_code == 200
        fake.calls.clear()
        store = SqlAlchemyAssignmentStore(db_uri)
        attempt = store.claim_attempt(assignment_id, host_id=_uid("host-a"), now=now_epoch())
        assert attempt is not None
        assert (
            store.transition(assignment_id, from_state="starting", to_state="running") is not None
        )
        assert (
            store.reschedule(assignment_id, expected_state="running", next_check_at=None)
            is not None
        )
        resp = await client.post(f"/v1/assignments/{assignment_id}/cancel", json={})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["state"] == "stopping"
        assert body["next_check_at"] == fixed_now
    assert fake.calls == [assignment_id]


@pytest.mark.asyncio
async def test_finish_placed_triggers_with_next_check(
    db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finishing a placed row parks a release and nudges the coordinator."""
    import secrets

    import httpx

    import omnigent.server.routes.assignments as routes_mod
    from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id
    from omnigent.runtime.agent_cache import AgentCache
    from omnigent.server.app import create_app
    from omnigent.server.feature_flags import resolve_feature_flags
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
    from omnigent.stores.artifact_store.local import LocalArtifactStore
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore as _Conv,
    )
    from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
    from omnigent.stores.host_store import HostStore

    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    flags = resolve_feature_flags({"OMNIGENT_FEATURES": "project_assignments"})
    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=_Conv(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        host_store=HostStore(db_uri),
        project_store=SqlAlchemyProjectStore(db_uri),
        project_repository_store=SqlAlchemyProjectRepositoryStore(db_uri),
        project_host_binding_store=SqlAlchemyProjectHostBindingStore(db_uri),
        assignment_store=SqlAlchemyAssignmentStore(db_uri),
        feature_flags=flags,
    )

    class _FakeCoordinator:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def trigger(self, assignment_id: str) -> None:
            self.calls.append(assignment_id)

    fake = _FakeCoordinator()
    app.state.assignment_coordinator = fake  # type: ignore[attr-defined]
    fixed_now = 19600
    monkeypatch.setattr(routes_mod, "now_epoch", lambda: fixed_now)
    agent_store = SqlAlchemyAgentStore(db_uri)
    if agent_store.get(AGENT_ID) is None:
        agent_store.create(
            agent_id=AGENT_ID, name="test-agent", bundle_location=f"{AGENT_ID}/bundle"
        )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/v1/projects", json={"name": "FinishProj"})
        assert resp.status_code == 200, resp.text
        project_id = resp.json()["id"]
        assert (
            await client.patch(
                f"/v1/projects/{project_id}/collaboration",
                json={"enabled": True, "expected_revision": 0},
            )
        ).status_code == 200
        assert (
            await client.put(
                f"/v1/projects/{project_id}/repositories/root",
                json={
                    "remote_url": "https://example.com/org/repo.git",
                    "default_branch": "main",
                },
            )
        ).status_code == 200
        conv = _Conv(db_uri).create_conversation(
            title="src", agent_id=AGENT_ID, project_id=project_id
        )
        assignment_id = _uid("route-finish")
        assert (
            await client.post(
                "/v1/assignments",
                json={
                    "id": assignment_id,
                    "source_session_id": conv.id,
                    "target_agent_id": AGENT_ID,
                    "task": "finish task",
                    "repositories": [
                        {
                            "repository_name": "root",
                            "commit": "a" * 40,
                            "manifest_digest": "d" * 64,
                        }
                    ],
                    "idempotency_key": "key-finish",
                },
            )
        ).status_code == 201
        assert (
            await client.post(
                f"/v1/assignments/{assignment_id}/published",
                json={"refs": [{"repository_name": "root", "commit": "a" * 40}]},
            )
        ).status_code == 200
        fake.calls.clear()
        token = secrets.token_hex(16)
        runner_id = token_bound_runner_id(token)
        run_conv = _Conv(db_uri).create_conversation(
            title="run", agent_id=AGENT_ID, project_id=project_id, runner_id=runner_id
        )
        store = SqlAlchemyAssignmentStore(db_uri)
        attempt = store.claim_attempt(assignment_id, host_id=_uid("host-a"), now=now_epoch())
        assert attempt is not None
        assert (
            store.update_attempt(
                assignment_id, attempt.id, session_id=run_conv.id, runner_id=runner_id
            )
            is not None
        )
        assert (
            store.transition(assignment_id, from_state="starting", to_state="running") is not None
        )
        resp = await client.post(
            f"/v1/assignments/{assignment_id}/complete",
            json={
                "session_id": run_conv.id,
                "outputs": [{"repository_name": "root", "commit": "b" * 40}],
                "summary": "done",
            },
            headers={RUNNER_TUNNEL_TOKEN_HEADER: token},
        )
        assert resp.status_code == 200, resp.text
        fake.calls.clear()
        assert (
            store.reschedule(assignment_id, expected_state="publishing", next_check_at=None)
            is not None
        )
        resp = await client.post(
            f"/v1/assignments/{assignment_id}/finish",
            json={
                "session_id": run_conv.id,
                "refs": [{"repository_name": "root", "commit": "b" * 40}],
            },
            headers={RUNNER_TUNNEL_TOKEN_HEADER: token},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["state"] == "succeeded"
        assert body["next_check_at"] == fixed_now
    assert fake.calls == [assignment_id]


# ── 19. placement window ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_second_coordinator_waits_out_live_placement(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A starting row inside its window parks instead of retiring."""
    import asyncio as _asyncio

    stores = _stores(db_uri)
    host_id = _uid("host-a")
    project_id = _uid("window-proj")
    _make_project(stores["project"], project_id, owner=ALICE, enabled=True)
    repo = _make_repo(stores["repository"], project_id, "root")
    _make_binding(
        stores["binding"], project_id=project_id, host_id=host_id, repo_id=repo.id, workspace="/w"
    )
    assignment = _seed_waiting(
        stores["assignment"],
        "window",
        project_id=project_id,
        owner=ALICE,
        requested_host_id=host_id,
        inputs=[_input("root", revision=repo.revision)],
    )
    monkeypatch.setattr(
        assignments_mod, "prepare_assignment_on_host", _prepare_ok({"root": "/prepared"})
    )
    parked: _asyncio.Event = _asyncio.Event()
    release: _asyncio.Event = _asyncio.Event()
    _install_placement_fakes(monkeypatch)

    import omnigent.server.routes.sessions as sessions_routes

    async def _parked_launch(*args: Any, **kwargs: Any) -> Any:
        parked.set()
        await release.wait()
        return SimpleNamespace(error=None, runner_id="runner_1")

    monkeypatch.setattr(sessions_routes, "_launch_runner_on_host", _parked_launch)
    registry = FakeHostRegistry({host_id: _conn(host_id, owner=ALICE, assignments=True)})
    host_store = FakeHostStore([_FakeHost(host_id, ALICE)])
    first = _coordinator(
        stores, registry=registry, host_store=host_store, permission_store=FakePermissionStore()
    )
    second = _coordinator(
        stores, registry=registry, host_store=host_store, permission_store=FakePermissionStore()
    )
    first.trigger(assignment.id)
    await _asyncio.wait_for(parked.wait(), timeout=10)
    second.trigger(assignment.id)
    await second.wait_for_idle()

    row = stores["assignment"].get(assignment.id)
    assert row is not None
    assert row.state == "starting"
    assert row.active_attempt_id is not None
    attempt = stores["assignment"].get_attempt(assignment.id, row.active_attempt_id)
    assert attempt is not None
    assert attempt.state == "active"
    assert attempt.started_at is not None
    assert row.next_check_at == attempt.started_at + 420

    release.set()
    await first.wait_for_idle()
    await first.shutdown()
    await second.shutdown()
    final = stores["assignment"].get(assignment.id)
    assert final is not None
    assert final.state == "running"


# ── 20. ended attempts ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stopping_with_ended_attempt_cancels_without_attempt_write(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancel racing a failed placement still cancels cleanly."""
    from omnigent.host.frames import HostAssignmentReleaseResultFrame

    stores = _stores(db_uri)
    host_id = _uid("host-a")
    project_id = _uid("ended-stop-proj")
    _make_project(stores["project"], project_id, owner=ALICE, enabled=True)
    repo = _make_repo(stores["repository"], project_id, "root")
    binding = _make_binding(
        stores["binding"], project_id=project_id, host_id=host_id, repo_id=repo.id, workspace="/w"
    )
    clock = {"now": 12000}
    monkeypatch.setattr(assignments_mod.time, "time", lambda: clock["now"])
    assignment = _seed_waiting(
        stores["assignment"],
        "ended-stop",
        project_id=project_id,
        owner=ALICE,
        requested_host_id=host_id,
        inputs=[_input("root", revision=repo.revision)],
    )
    attempt = stores["assignment"].claim_attempt(
        assignment.id,
        host_id=host_id,
        now=clock["now"],
        resolved_binding_id=binding.id,
        resolved_binding_revision=binding.revision,
        next_check_at=clock["now"],
    )
    assert attempt is not None
    # A failed placement ends the attempt, then the cancel lands on the
    # still-starting row before the return to waiting commits.
    assert (
        stores["assignment"].update_attempt(
            assignment.id,
            attempt.id,
            state="finished",
            ended_at=clock["now"],
            error_code="placement_abandoned",
        )
        is not None
    )
    assert (
        stores["assignment"].transition(
            assignment.id,
            from_state="starting",
            to_state="stopping",
            expected_active_attempt_id=attempt.id,
        )
        is not None
    )
    writes = {"n": 0}
    real_update = stores["assignment"].update_attempt

    def _spy_update(*args: Any, **kwargs: Any) -> Any:
        writes["n"] += 1
        return real_update(*args, **kwargs)

    monkeypatch.setattr(stores["assignment"], "update_attempt", _spy_update)

    async def _ok(**kwargs: Any) -> HostAssignmentReleaseResultFrame:
        frame = kwargs.get("frame")
        assert [r.repository_name for r in frame.repositories] == ["root"]
        assert frame.repositories[0].source_directory == "/w"
        return HostAssignmentReleaseResultFrame(
            request_id=frame.request_id, status="ok", removed=["root"], failures={}
        )

    monkeypatch.setattr(assignments_mod, "release_assignment_on_host", _ok)
    coordinator = _coordinator(
        stores,
        registry=FakeHostRegistry({host_id: _conn(host_id, owner=ALICE, assignments=True)}),
        host_store=FakeHostStore([_FakeHost(host_id, ALICE)]),
        permission_store=FakePermissionStore(),
        runner_router=FakeRunnerRouter(),
        runner_exit_reports=FakeExitReports(),
    )
    coordinator.trigger(assignment.id)
    await coordinator.wait_for_idle()
    await coordinator.shutdown()

    row = stores["assignment"].get(assignment.id)
    assert row is not None
    assert row.state == "cancelled"
    assert writes["n"] == 0
    assert row.next_check_at is None
    kept = stores["assignment"].get_attempt(assignment.id, attempt.id)
    assert kept is not None
    assert kept.state == "finished"
    assert kept.error_code == "placement_abandoned"
    assert stores["assignment"].select_due(now=clock["now"] + 100000, limit=10) == []


# ── 21. release stops the session runner ───────────────────────────────


def _seed_succeeded_with_session(
    stores: dict[str, Any],
    seed: str,
    *,
    project_id: str,
    host_id: str,
    clock_now: int,
    attempt_runner: str,
    session_runner: str,
) -> tuple[Assignment, Any]:
    assignment = _seed_waiting(
        stores["assignment"],
        seed,
        project_id=project_id,
        owner=ALICE,
        requested_host_id=host_id,
        inputs=[_input("root", revision=1)],
    )
    binding = stores["binding"].get_by_name(project_id=project_id, host_id=host_id, name="primary")
    assert binding is not None
    attempt = stores["assignment"].claim_attempt(
        assignment.id,
        host_id=host_id,
        now=clock_now,
        resolved_binding_id=binding.id,
        resolved_binding_revision=binding.revision,
        next_check_at=clock_now,
    )
    assert attempt is not None
    assert (
        stores["assignment"].transition(assignment.id, from_state="starting", to_state="running")
        is not None
    )
    assert (
        stores["assignment"].transition(assignment.id, from_state="running", to_state="publishing")
        is not None
    )
    session_id = hashlib.sha256(f"assignment-attempt:{attempt.id}".encode()).hexdigest()[:32]
    assert (
        stores["assignment"].update_attempt(
            assignment.id,
            attempt.id,
            session_id=session_id,
            runner_id=attempt_runner,
            ended_at=clock_now - 601,
        )
        is not None
    )
    _make_session_with_runner(
        stores,
        session_id,
        host_id=host_id,
        workspace="/w",
        runner_id=session_runner,
        project_id=project_id,
    )
    from omnigent.entities import AssignmentOutputEntry

    assert (
        stores["assignment"].transition(
            assignment.id,
            from_state="publishing",
            to_state="succeeded",
            expected_active_attempt_id=attempt.id,
            outputs=[
                AssignmentOutputEntry(
                    repository_name="root",
                    commit="b" * 40,
                    ref="refs/omnigent/assignments/x/output/att/root",
                )
            ],
        )
        is not None
    )
    assert (
        stores["assignment"].reschedule(
            assignment.id, expected_state="succeeded", next_check_at=clock_now
        )
        is not None
    )
    row = stores["assignment"].get(assignment.id)
    assert row is not None
    return row, attempt


@pytest.mark.asyncio
async def test_release_stops_replacement_session_runner(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stop names the session runner, not the stale attempt runner."""
    import omnigent.server.routes.sessions as sessions_routes
    from omnigent.host.frames import HostAssignmentReleaseResultFrame

    stores = _stores(db_uri)
    host_id = _uid("host-a")
    project_id = _uid("rel-replace-proj")
    _make_project(stores["project"], project_id, owner=ALICE, enabled=True)
    repo = _make_repo(stores["repository"], project_id, "root")
    _make_binding(
        stores["binding"], project_id=project_id, host_id=host_id, repo_id=repo.id, workspace="/w"
    )
    clock = {"now": 12100}
    monkeypatch.setattr(assignments_mod.time, "time", lambda: clock["now"])
    assignment, attempt = _seed_succeeded_with_session(
        stores,
        "rel-replace",
        project_id=project_id,
        host_id=host_id,
        clock_now=clock["now"],
        attempt_runner="runner_old",
        session_runner="runner_new",
    )
    session_id = hashlib.sha256(f"assignment-attempt:{attempt.id}".encode()).hexdigest()[:32]
    order: list[str] = []
    stop_calls: list[tuple[str, str, str]] = []

    async def _record_stop(
        stopped_session_id: str, stopped_host_id: str, stopped_runner_id: str, registry: Any
    ) -> str:
        stop_calls.append((stopped_session_id, stopped_host_id, stopped_runner_id))
        order.append("stop")
        return "acked"

    async def _ok_release(**kwargs: Any) -> HostAssignmentReleaseResultFrame:
        order.append("release")
        frame = kwargs.get("frame")
        return HostAssignmentReleaseResultFrame(
            request_id=frame.request_id, status="ok", removed=["root"], failures={}
        )

    monkeypatch.setattr(sessions_routes, "_stop_session_host_runner_outcome", _record_stop)
    monkeypatch.setattr(assignments_mod, "release_assignment_on_host", _ok_release)
    coordinator = _coordinator(
        stores,
        registry=FakeHostRegistry({host_id: _conn(host_id, owner=ALICE, assignments=True)}),
        host_store=FakeHostStore([_FakeHost(host_id, ALICE)]),
        permission_store=FakePermissionStore(),
        runner_router=FakeRunnerRouter({"runner_new"}),
        runner_exit_reports=FakeExitReports(),
    )
    coordinator.trigger(assignment.id)
    await coordinator.wait_for_idle()
    await coordinator.shutdown()

    assert stop_calls == [(session_id, host_id, "runner_new")]
    assert order == ["stop", "release"]
    row = stores["assignment"].get(assignment.id)
    assert row is not None
    assert row.next_check_at is None


@pytest.mark.asyncio
async def test_release_skipped_when_session_stop_unconfirmed(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unavailable session stop releases nothing and keeps its check."""
    import omnigent.server.routes.sessions as sessions_routes

    stores = _stores(db_uri)
    host_id = _uid("host-a")
    project_id = _uid("rel-unconf-proj")
    _make_project(stores["project"], project_id, owner=ALICE, enabled=True)
    repo = _make_repo(stores["repository"], project_id, "root")
    _make_binding(
        stores["binding"], project_id=project_id, host_id=host_id, repo_id=repo.id, workspace="/w"
    )
    clock = {"now": 12200}
    monkeypatch.setattr(assignments_mod.time, "time", lambda: clock["now"])
    assignment, _attempt = _seed_succeeded_with_session(
        stores,
        "rel-unconf",
        project_id=project_id,
        host_id=host_id,
        clock_now=clock["now"],
        attempt_runner="runner_1",
        session_runner="runner_1",
    )

    async def _unavailable(*args: Any, **kwargs: Any) -> str:
        return "unavailable"

    release_calls = {"n": 0}

    async def _count_release(**kwargs: Any) -> Any:
        release_calls["n"] += 1
        from omnigent.host.frames import HostAssignmentReleaseResultFrame

        frame = kwargs.get("frame")
        return HostAssignmentReleaseResultFrame(
            request_id=frame.request_id, status="ok", removed=[], failures={}
        )

    monkeypatch.setattr(sessions_routes, "_stop_session_host_runner_outcome", _unavailable)
    monkeypatch.setattr(assignments_mod, "release_assignment_on_host", _count_release)
    coordinator = _coordinator(
        stores,
        registry=FakeHostRegistry({host_id: _conn(host_id, owner=ALICE, assignments=True)}),
        host_store=FakeHostStore([_FakeHost(host_id, ALICE)]),
        permission_store=FakePermissionStore(),
        runner_router=FakeRunnerRouter({"runner_1"}),
        runner_exit_reports=FakeExitReports(),
    )
    coordinator.trigger(assignment.id)
    await coordinator.wait_for_idle()
    await coordinator.shutdown()

    row = stores["assignment"].get(assignment.id)
    assert row is not None
    assert row.state == "succeeded"
    assert row.next_check_at is not None and row.next_check_at > clock["now"]
    assert release_calls["n"] == 0


# ── 22. moved bindings ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_release_skipped_when_root_binding_moved(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bumped root binding skips release with one message and no frame."""
    stores = _stores(db_uri)
    host_id = _uid("host-a")
    project_id = _uid("rel-moved-proj")
    _make_project(stores["project"], project_id, owner=ALICE, enabled=True)
    repo = _make_repo(stores["repository"], project_id, "root")
    _make_binding(
        stores["binding"], project_id=project_id, host_id=host_id, repo_id=repo.id, workspace="/w"
    )
    clock = {"now": 12300}
    monkeypatch.setattr(assignments_mod.time, "time", lambda: clock["now"])
    assignment = _seed_terminal(
        stores,
        "rel-moved",
        project_id=project_id,
        host_id=host_id,
        to_state="succeeded",
        clock_now=clock["now"],
    )
    stores["binding"].upsert(
        project_id=project_id,
        host_id=host_id,
        name="primary",
        repository_id=repo.id,
        workspace="/w2",
        is_primary=True,
    )

    async def _must_not_release(**kwargs: Any) -> Any:
        raise AssertionError("release must not run against a moved binding")

    monkeypatch.setattr(assignments_mod, "release_assignment_on_host", _must_not_release)
    coordinator = _coordinator(
        stores,
        registry=FakeHostRegistry({host_id: _conn(host_id, owner=ALICE, assignments=True)}),
        host_store=FakeHostStore([_FakeHost(host_id, ALICE)]),
        permission_store=FakePermissionStore(),
        runner_router=FakeRunnerRouter(),
        runner_exit_reports=FakeExitReports(),
    )
    coordinator.trigger(assignment.id)
    await coordinator.wait_for_idle()
    await coordinator.shutdown()

    row = stores["assignment"].get(assignment.id)
    assert row is not None
    assert row.next_check_at is None
    messages = stores["assignment"].read_messages(assignment.id, limit=10)
    assert [m.body for m in messages.data] == [
        "worktree release skipped: binding for root changed since placement"
    ]


@pytest.mark.asyncio
async def test_release_skipped_when_second_binding_moved(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bumped non-root binding skips release with one message and no frame."""
    stores = _stores(db_uri)
    host_id = _uid("host-a")
    project_id = _uid("rel-moved2-proj")
    _make_project(stores["project"], project_id, owner=ALICE, enabled=True)
    root_repo = _make_repo(stores["repository"], project_id, "root")
    extra_repo = _make_repo(stores["repository"], project_id, "extra")
    root_binding = _make_binding(
        stores["binding"],
        project_id=project_id,
        host_id=host_id,
        repo_id=root_repo.id,
        workspace="/w",
    )
    _make_binding(
        stores["binding"],
        project_id=project_id,
        host_id=host_id,
        repo_id=extra_repo.id,
        workspace="/x",
        name="extra",
    )
    clock = {"now": 12400}
    monkeypatch.setattr(assignments_mod.time, "time", lambda: clock["now"])
    created = _seed_waiting(
        stores["assignment"],
        "rel-moved2",
        project_id=project_id,
        owner=ALICE,
        requested_host_id=host_id,
        inputs=[
            _input("root", revision=root_repo.revision),
            _input("extra", revision=extra_repo.revision, root=False),
        ],
    )
    attempt = stores["assignment"].claim_attempt(
        created.id,
        host_id=host_id,
        now=clock["now"],
        resolved_binding_id=root_binding.id,
        resolved_binding_revision=root_binding.revision,
        next_check_at=clock["now"],
    )
    assert attempt is not None
    assert (
        stores["assignment"].update_attempt(created.id, attempt.id, ended_at=clock["now"] - 601)
        is not None
    )
    assert (
        stores["assignment"].transition(created.id, from_state="starting", to_state="running")
        is not None
    )
    assert (
        stores["assignment"].transition(created.id, from_state="running", to_state="publishing")
        is not None
    )
    from omnigent.entities import AssignmentOutputEntry

    assert (
        stores["assignment"].transition(
            created.id,
            from_state="publishing",
            to_state="succeeded",
            expected_active_attempt_id=attempt.id,
            outputs=[
                AssignmentOutputEntry(
                    repository_name="root",
                    commit="b" * 40,
                    ref="refs/omnigent/assignments/x/output/att/root",
                ),
                AssignmentOutputEntry(
                    repository_name="extra",
                    commit="c" * 40,
                    ref="refs/omnigent/assignments/x/output/att/extra",
                ),
            ],
        )
        is not None
    )
    assert (
        stores["assignment"].reschedule(
            created.id, expected_state="succeeded", next_check_at=clock["now"]
        )
        is not None
    )
    assignment = stores["assignment"].get(created.id)
    assert assignment is not None
    clock["now"] += 5
    stores["binding"].upsert(
        project_id=project_id,
        host_id=host_id,
        name="extra",
        repository_id=extra_repo.id,
        workspace="/x2",
    )

    async def _must_not_release(**kwargs: Any) -> Any:
        raise AssertionError("release must not run against a moved binding")

    monkeypatch.setattr(assignments_mod, "release_assignment_on_host", _must_not_release)
    coordinator = _coordinator(
        stores,
        registry=FakeHostRegistry({host_id: _conn(host_id, owner=ALICE, assignments=True)}),
        host_store=FakeHostStore([_FakeHost(host_id, ALICE)]),
        permission_store=FakePermissionStore(),
        runner_router=FakeRunnerRouter(),
        runner_exit_reports=FakeExitReports(),
    )
    coordinator.trigger(assignment.id)
    await coordinator.wait_for_idle()
    await coordinator.shutdown()

    row = stores["assignment"].get(assignment.id)
    assert row is not None
    assert row.next_check_at is None
    messages = stores["assignment"].read_messages(assignment.id, limit=10)
    assert [m.body for m in messages.data] == [
        "worktree release skipped: binding for extra changed since placement"
    ]


# ── 23. retry route ─────────────────────────────────────────────────────


def _retry_app(db_uri: str, tmp_path: Any):  # type: ignore[no-untyped-def]
    from omnigent.runtime.agent_cache import AgentCache
    from omnigent.server.app import create_app
    from omnigent.server.feature_flags import resolve_feature_flags
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
    from omnigent.stores.artifact_store.local import LocalArtifactStore
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore as _Conv,
    )
    from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
    from omnigent.stores.host_store import HostStore

    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    flags = resolve_feature_flags({"OMNIGENT_FEATURES": "project_assignments"})
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=_Conv(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        host_store=HostStore(db_uri),
        project_store=SqlAlchemyProjectStore(db_uri),
        project_repository_store=SqlAlchemyProjectRepositoryStore(db_uri),
        project_host_binding_store=SqlAlchemyProjectHostBindingStore(db_uri),
        assignment_store=SqlAlchemyAssignmentStore(db_uri),
        feature_flags=flags,
    )


@pytest.mark.asyncio
async def test_retry_route_rejects_active_then_accepts_after_stop(
    db_uri: str, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scenario 4 through /retry: 409 while active, 200 once stopped."""
    import httpx

    import omnigent.server.routes.assignments as routes_mod
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore as _Conv,
    )

    app = _retry_app(db_uri, tmp_path)

    class _FakeCoordinator:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def trigger(self, assignment_id: str) -> None:
            self.calls.append(assignment_id)

    fake = _FakeCoordinator()
    app.state.assignment_coordinator = fake  # type: ignore[attr-defined]
    fixed_now = 19700
    monkeypatch.setattr(routes_mod, "now_epoch", lambda: fixed_now)
    agent_store = SqlAlchemyAgentStore(db_uri)
    if agent_store.get(AGENT_ID) is None:
        agent_store.create(
            agent_id=AGENT_ID, name="test-agent", bundle_location=f"{AGENT_ID}/bundle"
        )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/v1/projects", json={"name": "RetryProj"})
        assert resp.status_code == 200, resp.text
        project_id = resp.json()["id"]
        assert (
            await client.patch(
                f"/v1/projects/{project_id}/collaboration",
                json={"enabled": True, "expected_revision": 0},
            )
        ).status_code == 200
        assert (
            await client.put(
                f"/v1/projects/{project_id}/repositories/root",
                json={
                    "remote_url": "https://example.com/org/repo.git",
                    "default_branch": "main",
                },
            )
        ).status_code == 200
        conv = _Conv(db_uri).create_conversation(
            title="src", agent_id=AGENT_ID, project_id=project_id
        )
        assignment_id = _uid("route-retry")
        assert (
            await client.post(
                "/v1/assignments",
                json={
                    "id": assignment_id,
                    "source_session_id": conv.id,
                    "target_agent_id": AGENT_ID,
                    "task": "retry task",
                    "repositories": [
                        {
                            "repository_name": "root",
                            "commit": "a" * 40,
                            "manifest_digest": "d" * 64,
                        }
                    ],
                    "idempotency_key": "key-retry",
                },
            )
        ).status_code == 201
        assert (
            await client.post(
                f"/v1/assignments/{assignment_id}/published",
                json={"refs": [{"repository_name": "root", "commit": "a" * 40}]},
            )
        ).status_code == 200
        store = SqlAlchemyAssignmentStore(db_uri)
        attempt = store.claim_attempt(assignment_id, host_id=_uid("host-a"), now=fixed_now)
        assert attempt is not None
        assert (
            store.transition(assignment_id, from_state="starting", to_state="interrupted")
            is not None
        )
        resp = await client.post(f"/v1/assignments/{assignment_id}/retry")
        assert resp.status_code == 409, resp.text
        assert (
            store.update_attempt(assignment_id, attempt.id, state="lost", ended_at=fixed_now)
            is not None
        )
        fake.calls.clear()
        assert (
            store.reschedule(assignment_id, expected_state="interrupted", next_check_at=None)
            is not None
        )
        resp = await client.post(f"/v1/assignments/{assignment_id}/retry")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["state"] == "waiting"
        assert body["active_attempt_id"] is None
        assert body["next_check_at"] == fixed_now
    assert fake.calls == [assignment_id]


@pytest.mark.asyncio
async def test_retry_route_expired_parks_release_with_next_check(
    db_uri: str, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A past-deadline /retry expires with an exact check and a trigger."""
    import httpx

    import omnigent.server.routes.assignments as routes_mod
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore as _Conv,
    )

    app = _retry_app(db_uri, tmp_path)

    class _FakeCoordinator:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def trigger(self, assignment_id: str) -> None:
            self.calls.append(assignment_id)

    fake = _FakeCoordinator()
    app.state.assignment_coordinator = fake  # type: ignore[attr-defined]
    fixed_now = 19800
    monkeypatch.setattr(routes_mod, "now_epoch", lambda: fixed_now)
    agent_store = SqlAlchemyAgentStore(db_uri)
    if agent_store.get(AGENT_ID) is None:
        agent_store.create(
            agent_id=AGENT_ID, name="test-agent", bundle_location=f"{AGENT_ID}/bundle"
        )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/v1/projects", json={"name": "RetryExpProj"})
        assert resp.status_code == 200, resp.text
        project_id = resp.json()["id"]
        assert (
            await client.patch(
                f"/v1/projects/{project_id}/collaboration",
                json={"enabled": True, "expected_revision": 0},
            )
        ).status_code == 200
        assert (
            await client.put(
                f"/v1/projects/{project_id}/repositories/root",
                json={
                    "remote_url": "https://example.com/org/repo.git",
                    "default_branch": "main",
                },
            )
        ).status_code == 200
        conv = _Conv(db_uri).create_conversation(
            title="src", agent_id=AGENT_ID, project_id=project_id
        )
        assignment_id = _uid("route-retry-exp")
        assert (
            await client.post(
                "/v1/assignments",
                json={
                    "id": assignment_id,
                    "source_session_id": conv.id,
                    "target_agent_id": AGENT_ID,
                    "task": "retry expiry task",
                    "repositories": [
                        {
                            "repository_name": "root",
                            "commit": "a" * 40,
                            "manifest_digest": "d" * 64,
                        }
                    ],
                    "idempotency_key": "key-retry-exp",
                    "start_deadline": fixed_now - 10,
                },
            )
        ).status_code == 201
        assert (
            await client.post(
                f"/v1/assignments/{assignment_id}/published",
                json={"refs": [{"repository_name": "root", "commit": "a" * 40}]},
            )
        ).status_code == 200
        store = SqlAlchemyAssignmentStore(db_uri)
        attempt = store.claim_attempt(assignment_id, host_id=_uid("host-a"), now=fixed_now)
        assert attempt is not None
        assert (
            store.transition(assignment_id, from_state="starting", to_state="interrupted")
            is not None
        )
        assert (
            store.update_attempt(assignment_id, attempt.id, state="lost", ended_at=fixed_now)
            is not None
        )
        fake.calls.clear()
        assert (
            store.reschedule(assignment_id, expected_state="interrupted", next_check_at=None)
            is not None
        )
        resp = await client.post(f"/v1/assignments/{assignment_id}/retry")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["state"] == "expired"
        assert body["next_check_at"] == fixed_now
    assert fake.calls == [assignment_id]


# ── 24. release guard ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_release_guard_failure_skips_side_effects(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row that moved after the read performs no stop, release or message."""
    import omnigent.server.routes.sessions as sessions_routes

    stores = _stores(db_uri)
    host_id = _uid("host-a")
    project_id = _uid("rel-guard-proj")
    _make_project(stores["project"], project_id, owner=ALICE, enabled=True)
    repo = _make_repo(stores["repository"], project_id, "root")
    _make_binding(
        stores["binding"], project_id=project_id, host_id=host_id, repo_id=repo.id, workspace="/w"
    )
    clock = {"now": 12500}
    monkeypatch.setattr(assignments_mod.time, "time", lambda: clock["now"])
    assignment, _attempt = _seed_succeeded_with_session(
        stores,
        "rel-guard",
        project_id=project_id,
        host_id=host_id,
        clock_now=clock["now"],
        attempt_runner="runner_1",
        session_runner="runner_1",
    )
    calls = {"stop": 0, "release": 0, "message": 0}

    def _lost_reschedule(*args: Any, **kwargs: Any) -> Any:
        return None

    async def _count_stop(*args: Any, **kwargs: Any) -> str:
        calls["stop"] += 1
        return "acked"

    async def _count_release(**kwargs: Any) -> Any:
        calls["release"] += 1
        raise AssertionError("release must not run after a lost guard")

    orig_append = stores["assignment"].append_message

    def _spy_append(message: Any) -> Any:
        calls["message"] += 1
        return orig_append(message)

    monkeypatch.setattr(stores["assignment"], "reschedule", _lost_reschedule)
    monkeypatch.setattr(sessions_routes, "_stop_session_host_runner_outcome", _count_stop)
    monkeypatch.setattr(assignments_mod, "release_assignment_on_host", _count_release)
    monkeypatch.setattr(stores["assignment"], "append_message", _spy_append)
    coordinator = _coordinator(
        stores,
        registry=FakeHostRegistry({host_id: _conn(host_id, owner=ALICE, assignments=True)}),
        host_store=FakeHostStore([_FakeHost(host_id, ALICE)]),
        permission_store=FakePermissionStore(),
        runner_router=FakeRunnerRouter({"runner_1"}),
        runner_exit_reports=FakeExitReports(),
    )
    coordinator.trigger(assignment.id)
    await coordinator.wait_for_idle()
    await coordinator.shutdown()
    assert calls == {"stop": 0, "release": 0, "message": 0}


# ── 25. partial release failure ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_partial_release_message_failure_releases_once(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed partial message keeps its backoff: one frame, two passes."""
    from omnigent.host.frames import HostAssignmentReleaseResultFrame

    stores = _stores(db_uri)
    host_id = _uid("host-a")
    project_id = _uid("rel-partial-fail-proj")
    _make_project(stores["project"], project_id, owner=ALICE, enabled=True)
    repo = _make_repo(stores["repository"], project_id, "root")
    _make_binding(
        stores["binding"], project_id=project_id, host_id=host_id, repo_id=repo.id, workspace="/w"
    )
    clock = {"now": 12600}
    monkeypatch.setattr(assignments_mod.time, "time", lambda: clock["now"])
    _seed_terminal(
        stores,
        "rel-partial-fail",
        project_id=project_id,
        host_id=host_id,
        to_state="succeeded",
        clock_now=clock["now"],
    )
    calls = {"n": 0}

    async def _partial(**kwargs: Any) -> HostAssignmentReleaseResultFrame:
        calls["n"] += 1
        frame = kwargs.get("frame")
        return HostAssignmentReleaseResultFrame(
            request_id=frame.request_id,
            status="partial",
            removed=[],
            failures={"root": "worktree contains modified files"},
        )

    def _boom_append(message: Any) -> Any:
        raise RuntimeError("message store boom")

    monkeypatch.setattr(assignments_mod, "release_assignment_on_host", _partial)
    monkeypatch.setattr(stores["assignment"], "append_message", _boom_append)
    coordinator = _coordinator(
        stores,
        registry=FakeHostRegistry({host_id: _conn(host_id, owner=ALICE, assignments=True)}),
        host_store=FakeHostStore([_FakeHost(host_id, ALICE)]),
        permission_store=FakePermissionStore(),
        runner_router=FakeRunnerRouter(),
        runner_exit_reports=FakeExitReports(),
    )
    await coordinator._due_pass_and_schedule()
    await coordinator.wait_for_idle()
    assert calls["n"] == 1
    await coordinator._due_pass_and_schedule()
    await coordinator.wait_for_idle()
    await coordinator.shutdown()
    assert calls["n"] == 1


# ── 26. nested lease ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_running_interrupted_unconfirmed_takes_lease(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unconfirmed stop after interrupt keeps its backoff across passes."""
    import omnigent.server.routes.sessions as sessions_routes

    stores = _stores(db_uri)
    host_id = _uid("host-a")
    project_id = _uid("nested-lease-proj")
    _make_project(stores["project"], project_id, owner=ALICE, enabled=True)
    repo = _make_repo(stores["repository"], project_id, "root")
    _make_binding(
        stores["binding"], project_id=project_id, host_id=host_id, repo_id=repo.id, workspace="/w"
    )
    clock = {"now": 15000}
    monkeypatch.setattr(assignments_mod.time, "time", lambda: clock["now"])
    assignment = _seed_waiting(
        stores["assignment"],
        "nested-lease",
        project_id=project_id,
        owner=ALICE,
        requested_host_id=host_id,
        inputs=[_input("root", revision=repo.revision)],
    )
    attempt = stores["assignment"].claim_attempt(assignment.id, host_id=host_id, now=clock["now"])
    assert attempt is not None
    assert (
        stores["assignment"].transition(assignment.id, from_state="starting", to_state="running")
        is not None
    )
    session_id = hashlib.sha256(f"assignment-attempt:{attempt.id}".encode()).hexdigest()[:32]
    assert (
        stores["assignment"].update_attempt(
            assignment.id, attempt.id, session_id=session_id, runner_id="runner_1"
        )
        is not None
    )
    _make_session_with_runner(
        stores,
        session_id,
        host_id=host_id,
        workspace="/w",
        runner_id="runner_1",
        project_id=project_id,
    )
    stores["assignment"].set_lease(attempt.id, clock["now"] - 1)
    assert (
        stores["assignment"].reschedule(
            assignment.id, expected_state="running", next_check_at=clock["now"] - 10
        )
        is not None
    )
    stops = {"n": 0}

    async def _unavailable(*args: Any, **kwargs: Any) -> str:
        stops["n"] += 1
        return "unavailable"

    monkeypatch.setattr(sessions_routes, "_stop_session_host_runner_outcome", _unavailable)
    coordinator = _coordinator(
        stores,
        registry=FakeHostRegistry({host_id: _conn(host_id, owner=ALICE, assignments=True)}),
        host_store=FakeHostStore([_FakeHost(host_id, ALICE)]),
        permission_store=FakePermissionStore(),
        runner_router=FakeRunnerRouter(),
        runner_exit_reports=FakeExitReports(),
    )
    coordinator.trigger(assignment.id)
    await coordinator.wait_for_idle()
    row = stores["assignment"].get(assignment.id)
    assert row is not None
    assert row.state == "interrupted"
    assert row.next_check_at == next_check_at(row.created_at, clock["now"])
    assert stops["n"] == 1
    await coordinator._due_pass_and_schedule()
    await coordinator.wait_for_idle()
    await coordinator.shutdown()
    assert stops["n"] == 1


@pytest.mark.asyncio
async def test_stopping_cancelled_partial_message_failure_releases_once(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancelled row whose partial message fails releases exactly once."""
    import omnigent.server.routes.sessions as sessions_routes
    from omnigent.host.frames import HostAssignmentReleaseResultFrame

    stores = _stores(db_uri)
    host_id = _uid("host-a")
    project_id = _uid("nested-cancel-proj")
    _make_project(stores["project"], project_id, owner=ALICE, enabled=True)
    repo = _make_repo(stores["repository"], project_id, "root")
    binding = _make_binding(
        stores["binding"], project_id=project_id, host_id=host_id, repo_id=repo.id, workspace="/w"
    )
    clock = {"now": 15100}
    monkeypatch.setattr(assignments_mod.time, "time", lambda: clock["now"])
    assignment = _seed_waiting(
        stores["assignment"],
        "nested-cancel",
        project_id=project_id,
        owner=ALICE,
        requested_host_id=host_id,
        inputs=[_input("root", revision=repo.revision)],
    )
    attempt = stores["assignment"].claim_attempt(
        assignment.id,
        host_id=host_id,
        now=clock["now"],
        resolved_binding_id=binding.id,
        resolved_binding_revision=binding.revision,
        next_check_at=clock["now"],
    )
    assert attempt is not None
    assert (
        stores["assignment"].transition(assignment.id, from_state="starting", to_state="running")
        is not None
    )
    assert (
        stores["assignment"].transition(
            assignment.id,
            from_state="running",
            to_state="stopping",
            expected_active_attempt_id=attempt.id,
        )
        is not None
    )
    session_id = hashlib.sha256(f"assignment-attempt:{attempt.id}".encode()).hexdigest()[:32]
    assert (
        stores["assignment"].update_attempt(
            assignment.id, attempt.id, session_id=session_id, runner_id="runner_1"
        )
        is not None
    )
    _make_session_with_runner(
        stores,
        session_id,
        host_id=host_id,
        workspace="/w",
        runner_id="runner_1",
        project_id=project_id,
    )

    async def _acked(*args: Any, **kwargs: Any) -> str:
        return "acked"

    frames = {"n": 0}

    async def _partial(**kwargs: Any) -> HostAssignmentReleaseResultFrame:
        frames["n"] += 1
        frame = kwargs.get("frame")
        return HostAssignmentReleaseResultFrame(
            request_id=frame.request_id,
            status="partial",
            removed=[],
            failures={"root": "worktree contains modified files"},
        )

    def _boom_append(message: Any) -> Any:
        raise RuntimeError("message store boom")

    monkeypatch.setattr(sessions_routes, "_stop_session_host_runner_outcome", _acked)
    monkeypatch.setattr(assignments_mod, "release_assignment_on_host", _partial)
    monkeypatch.setattr(stores["assignment"], "append_message", _boom_append)
    coordinator = _coordinator(
        stores,
        registry=FakeHostRegistry({host_id: _conn(host_id, owner=ALICE, assignments=True)}),
        host_store=FakeHostStore([_FakeHost(host_id, ALICE)]),
        permission_store=FakePermissionStore(),
        runner_router=FakeRunnerRouter(),
        runner_exit_reports=FakeExitReports(),
    )
    coordinator.trigger(assignment.id)
    await coordinator.wait_for_idle()
    assert frames["n"] == 1
    await coordinator._due_pass_and_schedule()
    await coordinator.wait_for_idle()
    await coordinator.shutdown()
    assert frames["n"] == 1


# ── 27. release fence ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_release_fenced_against_session_relaunch(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A runner that changed between stop and frame sends no frame."""
    import omnigent.server.routes.sessions as sessions_routes

    stores = _stores(db_uri)
    host_id = _uid("host-a")
    project_id = _uid("fence-proj")
    _make_project(stores["project"], project_id, owner=ALICE, enabled=True)
    repo = _make_repo(stores["repository"], project_id, "root")
    _make_binding(
        stores["binding"], project_id=project_id, host_id=host_id, repo_id=repo.id, workspace="/w"
    )
    clock = {"now": 15200}
    monkeypatch.setattr(assignments_mod.time, "time", lambda: clock["now"])
    assignment, _attempt = _seed_succeeded_with_session(
        stores,
        "fence",
        project_id=project_id,
        host_id=host_id,
        clock_now=clock["now"],
        attempt_runner="runner_R1",
        session_runner="runner_R1",
    )
    reads = {"n": 0}

    def _flapping(session_id: str) -> Any:
        reads["n"] += 1
        if reads["n"] == 1:
            return SimpleNamespace(runner_id="runner_R1", id=session_id)
        return SimpleNamespace(runner_id="runner_R2", id=session_id)

    monkeypatch.setattr(stores["conversation"], "get_conversation", _flapping)
    stops: list[str] = []

    async def _acked(
        stopped_session_id: str, stopped_host_id: str, stopped_runner_id: str, registry: Any
    ) -> str:
        stops.append(stopped_runner_id)
        return "acked"

    frames = {"n": 0}

    async def _count_release(**kwargs: Any) -> Any:
        frames["n"] += 1
        from omnigent.host.frames import HostAssignmentReleaseResultFrame

        frame = kwargs.get("frame")
        return HostAssignmentReleaseResultFrame(
            request_id=frame.request_id, status="ok", removed=["root"], failures={}
        )

    monkeypatch.setattr(sessions_routes, "_stop_session_host_runner_outcome", _acked)
    monkeypatch.setattr(assignments_mod, "release_assignment_on_host", _count_release)
    coordinator = _coordinator(
        stores,
        registry=FakeHostRegistry({host_id: _conn(host_id, owner=ALICE, assignments=True)}),
        host_store=FakeHostStore([_FakeHost(host_id, ALICE)]),
        permission_store=FakePermissionStore(),
        runner_router=FakeRunnerRouter(),
        runner_exit_reports=FakeExitReports(),
    )
    coordinator.trigger(assignment.id)
    await coordinator.wait_for_idle()
    await coordinator.shutdown()
    assert stops == ["runner_R1"]
    assert frames["n"] == 0
    row = stores["assignment"].get(assignment.id)
    assert row is not None
    assert row.next_check_at == next_check_at(row.created_at, clock["now"])


# ── 28. latest attempt evidence ─────────────────────────────────────────


def _seed_waiting_two_repo(
    stores: dict[str, Any],
    seed: str,
    *,
    project_id: str,
    host_id: str,
    root_repo: Any,
    extra_repo: Any,
    clock_now: int,
) -> tuple[Any, Any]:
    created = _seed_waiting(
        stores["assignment"],
        seed,
        project_id=project_id,
        owner=ALICE,
        requested_host_id=host_id,
        inputs=[
            _input("root", revision=root_repo.revision),
            _input("extra", revision=extra_repo.revision, root=False),
        ],
    )
    root_binding = stores["binding"].get_by_name(
        project_id=project_id, host_id=host_id, name="primary"
    )
    assert root_binding is not None
    attempt = stores["assignment"].claim_attempt(
        created.id,
        host_id=host_id,
        now=clock_now,
        resolved_binding_id=root_binding.id,
        resolved_binding_revision=root_binding.revision,
        next_check_at=clock_now,
    )
    assert attempt is not None
    return created, attempt


@pytest.mark.asyncio
async def test_release_with_cleared_link_skips_when_second_binding_moved(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cleared link still enforces non-root freshness via the latest attempt."""
    stores = _stores(db_uri)
    host_id = _uid("host-a")
    project_id = _uid("latest-skip-proj")
    _make_project(stores["project"], project_id, owner=ALICE, enabled=True)
    root_repo = _make_repo(stores["repository"], project_id, "root")
    extra_repo = _make_repo(stores["repository"], project_id, "extra")
    _make_binding(
        stores["binding"],
        project_id=project_id,
        host_id=host_id,
        repo_id=root_repo.id,
        workspace="/w",
    )
    _make_binding(
        stores["binding"],
        project_id=project_id,
        host_id=host_id,
        repo_id=extra_repo.id,
        workspace="/x",
        name="extra",
    )
    clock = {"now": 15300}
    monkeypatch.setattr(assignments_mod.time, "time", lambda: clock["now"])
    created, attempt = _seed_waiting_two_repo(
        stores,
        "latest-skip",
        project_id=project_id,
        host_id=host_id,
        root_repo=root_repo,
        extra_repo=extra_repo,
        clock_now=clock["now"],
    )
    assert (
        stores["assignment"].update_attempt(
            created.id,
            attempt.id,
            state="finished",
            ended_at=clock["now"],
            error_code="prepare_failed",
        )
        is not None
    )
    assert (
        stores["assignment"].transition(
            created.id,
            from_state="starting",
            to_state="waiting",
            expected_active_attempt_id=attempt.id,
            active_attempt_id=None,
            next_check_at=clock["now"],
        )
        is not None
    )
    row = stores["assignment"].get(created.id)
    assert row is not None
    assert row.active_attempt_id is None
    assert row.resolved_host_id == host_id
    clock["now"] += 5
    stores["binding"].upsert(
        project_id=project_id,
        host_id=host_id,
        name="extra",
        repository_id=extra_repo.id,
        workspace="/x2",
    )
    assert (
        stores["assignment"].transition(
            created.id, from_state="waiting", to_state="cancelled", next_check_at=clock["now"]
        )
        is not None
    )

    async def _must_not_release(**kwargs: Any) -> Any:
        frames["n"] += 1
        from omnigent.host.frames import HostAssignmentReleaseResultFrame

        frame = kwargs.get("frame")
        return HostAssignmentReleaseResultFrame(
            request_id=frame.request_id, status="ok", removed=[], failures={}
        )

    frames = {"n": 0}
    monkeypatch.setattr(assignments_mod, "release_assignment_on_host", _must_not_release)
    coordinator = _coordinator(
        stores,
        registry=FakeHostRegistry({host_id: _conn(host_id, owner=ALICE, assignments=True)}),
        host_store=FakeHostStore([_FakeHost(host_id, ALICE)]),
        permission_store=FakePermissionStore(),
        runner_router=FakeRunnerRouter(),
        runner_exit_reports=FakeExitReports(),
    )
    coordinator.trigger(created.id)
    await coordinator.wait_for_idle()
    await coordinator.shutdown()
    assert frames["n"] == 0
    final = stores["assignment"].get(created.id)
    assert final is not None
    assert final.next_check_at is None
    messages = stores["assignment"].read_messages(created.id, limit=10)
    assert [m.body for m in messages.data] == [
        "worktree release skipped: binding for extra changed since placement"
    ]


@pytest.mark.asyncio
async def test_release_with_cleared_link_releases_when_bindings_unmoved(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cleared link releases with the original directories when fresh."""
    from omnigent.host.frames import HostAssignmentReleaseResultFrame

    stores = _stores(db_uri)
    host_id = _uid("host-a")
    project_id = _uid("latest-ok-proj")
    _make_project(stores["project"], project_id, owner=ALICE, enabled=True)
    root_repo = _make_repo(stores["repository"], project_id, "root")
    extra_repo = _make_repo(stores["repository"], project_id, "extra")
    _make_binding(
        stores["binding"],
        project_id=project_id,
        host_id=host_id,
        repo_id=root_repo.id,
        workspace="/w",
    )
    _make_binding(
        stores["binding"],
        project_id=project_id,
        host_id=host_id,
        repo_id=extra_repo.id,
        workspace="/x",
        name="extra",
    )
    clock = {"now": 15400}
    monkeypatch.setattr(assignments_mod.time, "time", lambda: clock["now"])
    created, attempt = _seed_waiting_two_repo(
        stores,
        "latest-ok",
        project_id=project_id,
        host_id=host_id,
        root_repo=root_repo,
        extra_repo=extra_repo,
        clock_now=clock["now"],
    )
    assert (
        stores["assignment"].update_attempt(
            created.id,
            attempt.id,
            state="finished",
            ended_at=clock["now"],
            error_code="prepare_failed",
        )
        is not None
    )
    assert (
        stores["assignment"].transition(
            created.id,
            from_state="starting",
            to_state="waiting",
            expected_active_attempt_id=attempt.id,
            active_attempt_id=None,
            next_check_at=clock["now"],
        )
        is not None
    )
    assert (
        stores["assignment"].transition(
            created.id, from_state="waiting", to_state="cancelled", next_check_at=clock["now"]
        )
        is not None
    )
    captured: dict[str, Any] = {}

    async def _ok(**kwargs: Any) -> HostAssignmentReleaseResultFrame:
        frame = kwargs.get("frame")
        captured["frame"] = frame
        return HostAssignmentReleaseResultFrame(
            request_id=frame.request_id, status="ok", removed=["root", "extra"], failures={}
        )

    monkeypatch.setattr(assignments_mod, "release_assignment_on_host", _ok)
    coordinator = _coordinator(
        stores,
        registry=FakeHostRegistry({host_id: _conn(host_id, owner=ALICE, assignments=True)}),
        host_store=FakeHostStore([_FakeHost(host_id, ALICE)]),
        permission_store=FakePermissionStore(),
        runner_router=FakeRunnerRouter(),
        runner_exit_reports=FakeExitReports(),
    )
    coordinator.trigger(created.id)
    await coordinator.wait_for_idle()
    await coordinator.shutdown()
    frame = captured.get("frame")
    assert frame is not None
    by_name = {r.repository_name: r.source_directory for r in frame.repositories}
    assert by_name == {"root": "/w", "extra": "/x"}
    final = stores["assignment"].get(created.id)
    assert final is not None
    assert final.next_check_at is None


# ── 29. idempotent return ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_return_to_waiting_idempotent_when_attempt_ended(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An already-ended attempt still returns to waiting without raising."""
    stores = _stores(db_uri)
    host_id = _uid("host-a")
    project_id = _uid("idem-ret-proj")
    _make_project(stores["project"], project_id, owner=ALICE, enabled=True)
    repo = _make_repo(stores["repository"], project_id, "root")
    _make_binding(
        stores["binding"], project_id=project_id, host_id=host_id, repo_id=repo.id, workspace="/w"
    )
    assignment = _seed_waiting(
        stores["assignment"],
        "idem-ret",
        project_id=project_id,
        owner=ALICE,
        requested_host_id=host_id,
        inputs=[_input("root", revision=repo.revision)],
    )

    async def _fail_and_end_first(**kwargs: Any) -> HostAssignmentPrepareResultFrame:
        frame = kwargs.get("frame")
        current = stores["assignment"].get(assignment.id)
        assert current is not None and current.active_attempt_id is not None
        assert (
            stores["assignment"].update_attempt(
                assignment.id,
                current.active_attempt_id,
                state="finished",
                ended_at=9999,
                error_code="placement_abandoned",
            )
            is not None
        )
        return HostAssignmentPrepareResultFrame(
            request_id=frame.request_id,
            status="failed",
            directories={},
            error_code="context_missing",
            error="required context missing",
            repository_name="root",
        )

    monkeypatch.setattr(assignments_mod, "prepare_assignment_on_host", _fail_and_end_first)
    _install_placement_fakes(monkeypatch)
    coordinator = _coordinator(
        stores,
        registry=FakeHostRegistry({host_id: _conn(host_id, owner=ALICE, assignments=True)}),
        host_store=FakeHostStore([_FakeHost(host_id, ALICE)]),
        permission_store=FakePermissionStore(),
    )
    coordinator.trigger(assignment.id)
    await coordinator.wait_for_idle()
    await coordinator.shutdown()
    row = stores["assignment"].get(assignment.id)
    assert row is not None
    assert row.state == "waiting"
    assert row.active_attempt_id is None


# ── 30. cancel to cancelled ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_waiting_with_host_writes_next_check(
    db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancelling a placed waiting row parks a release check and triggers."""
    import httpx

    import omnigent.server.routes.assignments as routes_mod
    from omnigent.runtime.agent_cache import AgentCache
    from omnigent.server.app import create_app
    from omnigent.server.feature_flags import resolve_feature_flags
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
    from omnigent.stores.artifact_store.local import LocalArtifactStore
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore as _Conv,
    )
    from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
    from omnigent.stores.host_store import HostStore

    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    flags = resolve_feature_flags({"OMNIGENT_FEATURES": "project_assignments"})
    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=_Conv(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        host_store=HostStore(db_uri),
        project_store=SqlAlchemyProjectStore(db_uri),
        project_repository_store=SqlAlchemyProjectRepositoryStore(db_uri),
        project_host_binding_store=SqlAlchemyProjectHostBindingStore(db_uri),
        assignment_store=SqlAlchemyAssignmentStore(db_uri),
        feature_flags=flags,
    )

    class _FakeCoordinator:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def trigger(self, assignment_id: str) -> None:
            self.calls.append(assignment_id)

    fake = _FakeCoordinator()
    app.state.assignment_coordinator = fake  # type: ignore[attr-defined]
    fixed_now = 19900
    monkeypatch.setattr(routes_mod, "now_epoch", lambda: fixed_now)
    agent_store = SqlAlchemyAgentStore(db_uri)
    if agent_store.get(AGENT_ID) is None:
        agent_store.create(
            agent_id=AGENT_ID, name="test-agent", bundle_location=f"{AGENT_ID}/bundle"
        )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/v1/projects", json={"name": "CancelWaitProj"})
        assert resp.status_code == 200, resp.text
        project_id = resp.json()["id"]
        assert (
            await client.patch(
                f"/v1/projects/{project_id}/collaboration",
                json={"enabled": True, "expected_revision": 0},
            )
        ).status_code == 200
        assert (
            await client.put(
                f"/v1/projects/{project_id}/repositories/root",
                json={
                    "remote_url": "https://example.com/org/repo.git",
                    "default_branch": "main",
                },
            )
        ).status_code == 200
        conv = _Conv(db_uri).create_conversation(
            title="src", agent_id=AGENT_ID, project_id=project_id
        )
        assignment_id = _uid("route-cancel-wait")
        assert (
            await client.post(
                "/v1/assignments",
                json={
                    "id": assignment_id,
                    "source_session_id": conv.id,
                    "target_agent_id": AGENT_ID,
                    "task": "cancel waiting task",
                    "repositories": [
                        {
                            "repository_name": "root",
                            "commit": "a" * 40,
                            "manifest_digest": "d" * 64,
                        }
                    ],
                    "idempotency_key": "key-cancel-wait",
                },
            )
        ).status_code == 201
        assert (
            await client.post(
                f"/v1/assignments/{assignment_id}/published",
                json={"refs": [{"repository_name": "root", "commit": "a" * 40}]},
            )
        ).status_code == 200
        fake.calls.clear()
        store = SqlAlchemyAssignmentStore(db_uri)
        host_id = _uid("host-a")
        attempt = store.claim_attempt(assignment_id, host_id=host_id, now=now_epoch())
        assert attempt is not None
        assert (
            store.transition(
                assignment_id,
                from_state="starting",
                to_state="waiting",
                expected_active_attempt_id=attempt.id,
                active_attempt_id=None,
                next_check_at=now_epoch(),
            )
            is not None
        )
        assert (
            store.reschedule(assignment_id, expected_state="waiting", next_check_at=None)
            is not None
        )
        resp = await client.post(f"/v1/assignments/{assignment_id}/cancel", json={})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["state"] == "cancelled"
        assert body["next_check_at"] == fixed_now
    assert fake.calls == [assignment_id]
