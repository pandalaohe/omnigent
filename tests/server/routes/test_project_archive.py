"""Project archive API contracts through real stores and ordinary session transitions."""

from __future__ import annotations

from collections.abc import Callable
from unittest.mock import Mock

import httpx
import pytest
from fastapi import FastAPI, Request

from omnigent.entities import ScheduledTask
from omnigent.server import project_archive
from omnigent.server.project_archive import ProjectArchiveService
from omnigent.server.routes.sessions import routes_core
from omnigent.server.scheduled.scheduler import ScheduledTaskScheduler
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore
from omnigent.stores.scheduled_task_store.sqlalchemy_store import SqlAlchemyScheduledTaskStore
from tests.server.routes.test_projects_crud import multi_user_app as multi_user_app
from tests.server.routes.test_projects_crud import multi_user_client as multi_user_client
from tests.server.routes.test_projects_crud import project_app as project_app
from tests.server.routes.test_projects_crud import project_client as project_client
from tests.server.routes.test_sessions_project_membership import AGENT_ID, _ensure_agent


@pytest.fixture(autouse=True)
def no_runner_teardown(monkeypatch: pytest.MonkeyPatch) -> None:
    # These tests exercise persisted transitions; runner timing has its own integration scenarios.
    for name in ("_spawn_archive_fence", "_spawn_archive_stop", "_spawn_archive_unfence"):
        monkeypatch.setattr(routes_core, name, Mock())


async def test_archive_roundtrip_and_hidden_legacy_names(
    project_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    client = project_client
    project = (
        await client.post("/v1/projects", json={"name": "Work", "config": {"icon": "W"}})
    ).json()
    other = (await client.post("/v1/projects", json={"name": "Other"})).json()
    _ensure_agent(db_uri)
    store = SqlAlchemyConversationStore(db_uri)
    root = store.create_conversation(agent_id=AGENT_ID, title="Parent", project_id=project["id"])
    child = store.create_conversation(
        agent_id=AGENT_ID, title="Child", parent_conversation_id=root.id, project_id=other["id"]
    )
    earlier = store.create_conversation(agent_id=AGENT_ID, project_id=project["id"])
    store.update_conversation(earlier.id, archived=True)
    legacy = store.create_conversation(agent_id=AGENT_ID, labels={"omni_project": "Work"})
    preview = (await client.get(f"/v1/projects/{project['id']}/archive-preview")).json()
    assert {s["id"] for s in preview["sessions"]} == {root.id, child.id, legacy.id}
    assert next(s for s in preview["sessions"] if s["id"] == child.id)[
        "via_parent_outside_project"
    ]
    archived = await client.post(f"/v1/projects/{project['id']}/archive")
    assert archived.status_code == 200, archived.text
    result = archived.json()
    assert result["status"] == "complete", result
    assert result["project"]["archive_summary"] == {"sessions": 3, "tasks": 0}
    for session_id in (root.id, child.id, legacy.id):
        session = store.get_conversation(session_id)
        assert session.archived
        assert (
            session.labels["omnigent.project_archive"]
            == f"{result['batch']}:{session.archive_revision}"
        )
    assert "omnigent.project_archive" not in store.get_conversation(earlier.id).labels
    assert [p["id"] for p in (await client.get("/v1/projects")).json()["data"]] == [other["id"]]
    assert "Work" not in {p["name"] for p in (await client.get("/v1/sessions/projects")).json()}
    assert len((await client.get("/v1/projects?include_archived=true")).json()["data"]) == 2
    assert [p["id"] for p in (await client.get("/v1/projects?archived=only")).json()["data"]] == [
        project["id"]
    ]
    duplicate = await client.post("/v1/projects", json={"name": "Work"})
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "project_name_archived"
    assert duplicate.json()["error"]["project_id"] == project["id"]
    restored = (await client.post(f"/v1/projects/{project['id']}/unarchive")).json()
    assert restored["status"] == "complete", restored
    assert set(restored["sessions"]["done"]) == {root.id, child.id, legacy.id}
    assert store.get_conversation(earlier.id).archived
    assert restored["project"]["config"] == project["config"]


async def test_partial_archive_retries_and_busy_op(
    project_client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = (await project_client.post("/v1/projects", json={"name": "Work"})).json()
    _ensure_agent(db_uri)
    store = SqlAlchemyConversationStore(db_uri)
    first = store.create_conversation(agent_id=AGENT_ID, project_id=project["id"])
    second = store.create_conversation(agent_id=AGENT_ID, project_id=project["id"])
    transition = project_archive.apply_session_archive_transition

    async def fail_one(request: Request, conv: object, *args: object, **kwargs: object) -> object:
        if conv.id == second.id:
            raise RuntimeError("injected failure")
        return await transition(request, conv, *args, **kwargs)

    monkeypatch.setattr(project_archive, "apply_session_archive_transition", fail_one)
    result = (await project_client.post(f"/v1/projects/{project['id']}/archive")).json()
    assert result["status"] == "incomplete"
    revision = store.get_conversation(first.id).archive_revision
    monkeypatch.setattr(project_archive, "apply_session_archive_transition", transition)
    retried = (await project_client.post(f"/v1/projects/{project['id']}/archive")).json()
    assert retried["status"] == "complete", retried
    assert retried["batch"] == result["batch"]
    assert store.get_conversation(first.id).archive_revision == revision
    projects = SqlAlchemyProjectStore(db_uri)
    projects.begin_archive_op(project["id"], user_id=None, kind="archive")
    busy = await project_client.post(f"/v1/projects/{project['id']}/unarchive")
    assert busy.status_code == 409
    assert busy.json()["error"]["code"] == "project_archive_busy"


async def test_restore_skips_members_handled_separately(
    project_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    project = (await project_client.post("/v1/projects", json={"name": "Work"})).json()
    other = (await project_client.post("/v1/projects", json={"name": "Other"})).json()
    _ensure_agent(db_uri)
    store = SqlAlchemyConversationStore(db_uri)
    session = store.create_conversation(agent_id=AGENT_ID, project_id=project["id"])
    await project_client.post(f"/v1/projects/{project['id']}/archive")
    refused = await project_client.patch(f"/v1/sessions/{session.id}", json={"archived": False})
    assert refused.status_code == 409
    moved = await project_client.patch(
        f"/v1/sessions/{session.id}", json={"project_id": other["id"], "archived": False}
    )
    assert moved.status_code == 200, moved.text
    await project_client.post(f"/v1/projects/{other['id']}/archive")
    restored = (await project_client.post(f"/v1/projects/{project['id']}/unarchive")).json()
    assert restored["status"] == "complete", restored
    assert session.id not in restored["sessions"]["done"]
    assert any(row["id"] == session.id for row in restored["sessions"]["skipped"])
    assert store.get_conversation(session.id).archived


async def test_archive_endpoints_remain_owner_private(
    multi_user_client: httpx.AsyncClient,
) -> None:
    alice = {"X-Forwarded-Email": "alice@example.com"}
    bob = {"X-Forwarded-Email": "bob@example.com"}
    project = (
        await multi_user_client.post("/v1/projects", json={"name": "Private"}, headers=alice)
    ).json()
    for action, method in (("archive-preview", "GET"), ("archive", "POST"), ("unarchive", "POST")):
        result = await multi_user_client.request(
            method, f"/v1/projects/{project['id']}/{action}", headers=bob
        )
        assert result.status_code == 404
    current = (await multi_user_client.get(f"/v1/projects/{project['id']}", headers=alice)).json()
    assert current["archived_at"] is None


async def test_task_resume_reconciles_after_scheduler_failure(db_uri: str) -> None:
    projects = SqlAlchemyProjectStore(db_uri)
    project = projects.create("b" * 32, "Work", None)
    conversations = SqlAlchemyConversationStore(db_uri)
    tasks = SqlAlchemyScheduledTaskStore(db_uri)
    task = tasks.create(
        scheduled_task_id="c" * 32,
        name="Nightly",
        prompt="Check",
        rrule="FREQ=DAILY",
        timezone="UTC",
        user_id=None,
        agent_id="d" * 32,
        project_id=project.id,
    )
    paused = tasks.create(
        scheduled_task_id="e" * 32,
        name="Paused",
        prompt="Check",
        rrule="FREQ=DAILY",
        timezone="UTC",
        user_id=None,
        agent_id="d" * 32,
        project_id=project.id,
    )
    tasks.update(paused.id, state="paused")
    scheduler = Mock()
    scheduler.next_run_at.return_value = "2026-10-22T09:00:00Z"
    app = FastAPI()
    app.state.scheduled_task_scheduler = scheduler
    request = Request({"type": "http", "app": app})
    service = ProjectArchiveService(projects, conversations, None, tasks, None)
    archived = await service.transition(request, project, "archive")
    assert archived["status"] == "complete", archived
    scheduler.remove.assert_called_once_with(task.id)
    scheduler.update.side_effect = RuntimeError("interrupted")
    incomplete = await service.transition(
        request, projects.get(project.id, user_id=None), "unarchive"
    )
    assert incomplete["status"] == "incomplete"
    assert tasks.get(task.id).state == "active"
    assert tasks.get(task.id).paused_by_archive_batch == archived["batch"]
    scheduler.update.side_effect = None
    restored = await service.transition(
        request, projects.get(project.id, user_id=None), "unarchive"
    )
    assert restored["status"] == "complete", restored
    assert tasks.get(task.id).paused_by_archive_batch is None
    assert tasks.get(paused.id).state == "paused"
    assert scheduler.update.call_count == 2


async def test_stale_archive_cannot_write_after_unarchive_takeover(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projects = SqlAlchemyProjectStore(db_uri)
    project = projects.create("7" * 32, "Work", None)
    conversations = SqlAlchemyConversationStore(db_uri)
    session = conversations.create_conversation(project_id=project.id)
    service = ProjectArchiveService(projects, conversations, None, None, None)
    app = FastAPI()
    request = Request({"type": "http", "app": app})
    clock = [1000]
    monkeypatch.setattr(
        "omnigent.stores.project_store.sqlalchemy_store.now_epoch", lambda: clock[0]
    )
    original = project_archive.apply_session_archive_transition
    taken_over = False

    async def transition_after_takeover(*args: object, **kwargs: object) -> object:
        nonlocal taken_over
        if not taken_over and kwargs.get("project_archive_batch"):
            taken_over = True
            clock[0] += 400
            restored = await service.transition(
                request, projects.get(project.id, user_id=None), "unarchive"
            )
            assert restored["status"] == "complete"
        return await original(*args, **kwargs)

    monkeypatch.setattr(
        project_archive, "apply_session_archive_transition", transition_after_takeover
    )
    stale = await service.transition(request, project, "archive")
    assert stale["status"] == "incomplete"
    assert not conversations.get_conversation(session.id).archived
    assert projects.get(project.id, user_id=None).archived_at is None
    assert (
        conversations.list_project_archive_sessions(
            project.id, project.name, None, batch=stale["batch"]
        )
        == []
    )


async def test_stale_restore_cannot_unarm_a_newer_restored_task(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    projects = SqlAlchemyProjectStore(db_uri)
    project = projects.create("1" * 32, "Work", None)
    tasks = SqlAlchemyScheduledTaskStore(db_uri)
    task = tasks.create(
        "2" * 32,
        "Nightly",
        "Check",
        "FREQ=DAILY",
        None,
        "3" * 32,
        "UTC",
        project_id=project.id,
    )
    clock = [1000]
    monkeypatch.setattr(
        "omnigent.stores.project_store.sqlalchemy_store.now_epoch", lambda: clock[0]
    )

    async def on_fire(workspace_id: int, task_id: str) -> None:
        pass

    scheduler = ScheduledTaskScheduler(tasks, on_fire)
    app = FastAPI()
    app.state.scheduled_task_scheduler = scheduler
    request = Request({"type": "http", "app": app})
    service = ProjectArchiveService(
        projects, SqlAlchemyConversationStore(db_uri), None, tasks, None
    )
    archived = await service.transition(request, project, "archive")
    assert archived["status"] == "complete"
    original_reconcile = tasks.reconcile_project_archive_task
    taken_over = False

    def reconcile(current: ScheduledTask | None) -> None:
        if current is not None and current.state == "active":
            scheduler.update(current)
        else:
            scheduler.remove(task.id)

    def take_over_before_reconcile(
        task_id: str,
        project_id: str,
        op_id: str,
        callback: Callable[[ScheduledTask | None], None],
    ) -> bool:
        nonlocal taken_over
        if not taken_over:
            taken_over = True
            clock[0] += 400
            newer_archive = projects.begin_archive_op(project.id, user_id=None, kind="archive")
            tasks.pause_for_project_archive(
                task.id, project.id, newer_archive.batch, op_id=newer_archive.id
            )
            assert original_reconcile(task.id, project.id, newer_archive.id, reconcile)
            assert projects.finish_archive_op(project.id, newer_archive.id, user_id=None)
            newest_restore = projects.begin_archive_op(project.id, user_id=None, kind="unarchive")
            tasks.restore_from_project_archive(
                task.id, project.id, newest_restore.batch, op_id=newest_restore.id
            )
            assert original_reconcile(task.id, project.id, newest_restore.id, reconcile)
            tasks.clear_project_archive_marker(
                task.id, newest_restore.batch, project_id=project.id, op_id=newest_restore.id
            )
            assert projects.finish_archive_op(
                project.id, newest_restore.id, user_id=None, clear_batch=True
            )
        return original_reconcile(task_id, project_id, op_id, callback)

    monkeypatch.setattr(tasks, "reconcile_project_archive_task", take_over_before_reconcile)
    try:
        stale = await service.transition(
            request, projects.get(project.id, user_id=None), "unarchive"
        )
        assert stale["status"] == "incomplete"
        assert tasks.get(task.id).state == "active"
        assert tasks.get(task.id).paused_by_archive_batch is None
        assert scheduler.job_count == 1
        assert scheduler.next_run_at(task.id) is not None
        assert projects.get(project.id, user_id=None).archive_state == {}
    finally:
        scheduler.stop()
