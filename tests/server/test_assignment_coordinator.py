"""Tests for the assignment coordinator.

Uses real SQLAlchemy stores for assignment, project, repository, binding
and conversation rows, with fakes for the host registry, host listing,
permissions and the runner placement seam.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

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
) -> Any:
    return binding_store.upsert(
        project_id=project_id,
        host_id=host_id,
        name=name,
        repository_id=repo_id,
        workspace=workspace,
        enabled=enabled,
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


def _coordinator(
    stores: dict[str, Any],
    *,
    registry: FakeHostRegistry,
    host_store: FakeHostStore,
    permission_store: FakePermissionStore,
    scan_interval_seconds: float = 3600.0,
    due_batch_limit: int = 50,
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
        runner_router=SimpleNamespace(),
        tunnel_registry=SimpleNamespace(),
        runner_exit_reports=SimpleNamespace(),
        file_store=SimpleNamespace(),
        artifact_store=SimpleNamespace(),
        scan_interval_seconds=scan_interval_seconds,
        due_batch_limit=due_batch_limit,
    )


def _install_placement_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    order: list[str] | None = None,
    dispatch_texts: list[str] | None = None,
    launch_error: str | None = None,
    wait_none: bool = False,
    dispatch_raises: bool = False,
) -> dict[str, Any]:
    import omnigent.server.routes._host_launch as host_launch
    import omnigent.server.routes.sessions as sessions_routes
    from omnigent.server.routes._sessions.helpers import _SessionEventDispatchResult

    captured: dict[str, Any] = {}

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
        return object()

    async def _ensure_runner_session_initialized(*args: Any, **kwargs: Any) -> bool:
        captured["ensure_require_success"] = kwargs.get("require_success")
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
                "list",
                "update_attempt",
                "mark_event_dispatched",
                "get_attempt",
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
    """A dispatch raise after launch is unknown: lost attempt, one delivery."""
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
    assert row.next_check_at is None
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
    assert attempts[0]["state"] == "lost"
    assert attempts[0]["runner_id"] == "runner_1"
    assert attempts[0]["session_id"] is not None
    # Interrupted rows keep the attempt link for /retry and /cancel.
    assert row.active_attempt_id == attempts[0]["id"]
    kept = stores["assignment"].get_attempt(assignment.id, row.active_attempt_id)
    assert kept is not None
    assert kept.state == "lost"
    assert kept.ended_at is not None
    # The retry route's write then succeeds and a new claim bumps the number.
    now = kept.ended_at
    retried = stores["assignment"].transition(
        assignment.id,
        from_state="interrupted",
        to_state="waiting",
        expected_active_attempt_id=kept.id,
        active_attempt_id=None,
        next_check_at=now,
    )
    assert retried is not None
    assert retried.active_attempt_id is None
    second = stores["assignment"].claim_attempt(assignment.id, host_id=host_id, now=now)
    assert second is not None
    assert second.number == 2


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
    attempt = stores["assignment"].get_attempt(assignment.id, row.active_attempt_id)
    assert attempt is not None
    assert attempt.state == "lost"
    assert attempt.ended_at is not None


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
