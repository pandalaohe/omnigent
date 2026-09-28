"""Scheduled tasks bound to a saved library Agent (``ca_`` id).

Covers the server surface of scheduled library-Agent support: create
validation against the task owner's custom-agents store (owner-only, the same
404 as an unknown agent), the sandbox-target rejection, the per-harness
permission gate, and the fire path's bundle-copy launch — a session-scoped
``ag_`` agent carrying the template label and the task's overrides.

The runner launch is injected as a seam, so the fire tests exercise the real
stores and launch operation without a live host.
"""

from __future__ import annotations

import io
import tarfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.member_snapshot import member_label_key, parse_member_entry
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.custom_agents_store import CustomAgentsStore
from omnigent.server.routes import scheduled_tasks as scheduled_tasks_routes
from omnigent.server.scheduled.fire import FireDeps, build_on_fire
from omnigent.server.user_preferences_store import SqlAlchemyUserPreferencesStore
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore
from omnigent.stores.scheduled_task_store.sqlalchemy_store import (
    SqlAlchemyScheduledTaskStore,
)
from tests.server.scheduled.test_fire import _drain

pytestmark = pytest.mark.asyncio

_OWNER = "alice@example.com"
_HOST_ID = "4b653f6031f35d168cc0b37caa1306d1"
_WORKER_HOST_ID = "8e7d6c5b4a39281706f5e4d3c2b1a09f"
_VALID_RRULE = "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=9;BYMINUTE=0"
_TEMPLATE_LABEL = "omnigent:agent-template-id"


def _bundle(*, harness: str = "claude-native", name: str = "library-runner") -> bytes:
    config = f"""spec_version: 1
name: {name}
executor:
  type: omnigent
  model: test-model
  config:
    harness: {harness}
"""
    entries = {"config.yaml": config.encode(), "prompts/custom.md": b"do the thing"}
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as archive:
        for entry, data in entries.items():
            info = tarfile.TarInfo(entry)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return out.getvalue()


def _joint_bundle() -> bytes:
    """A 2-member saved Agent: a claude-native lead plus a codex worker."""
    entries = {
        "config.yaml": b"""spec_version: 1
name: library-runner
executor: {type: omnigent, model: lead-model, config: {harness: claude-native}}
""",
        "agents/researcher/config.yaml": b"""spec_version: 1
name: researcher
executor: {type: omnigent, model: worker-model, config: {harness: codex}}
""",
    }
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as archive:
        for entry, data in entries.items():
            info = tarfile.TarInfo(entry)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return out.getvalue()


@dataclass
class _LibraryServer:
    app: FastAPI
    artifacts: LocalArtifactStore
    agents: SqlAlchemyAgentStore
    conversations: SqlAlchemyConversationStore
    permissions: SqlAlchemyPermissionStore
    tasks: SqlAlchemyScheduledTaskStore
    hosts: HostStore
    custom: CustomAgentsStore
    projects: SqlAlchemyProjectStore
    prefs: SqlAlchemyUserPreferencesStore


@pytest.fixture()
def library_server(runtime_init: None, db_uri: str, tmp_path: Path) -> _LibraryServer:
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    agents = SqlAlchemyAgentStore(db_uri)
    conversations = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    tasks = SqlAlchemyScheduledTaskStore(db_uri)
    hosts = HostStore(db_uri)
    custom = CustomAgentsStore(db_uri)
    projects = SqlAlchemyProjectStore(db_uri)
    prefs = SqlAlchemyUserPreferencesStore(db_uri)
    app = create_app(
        agents,
        SqlAlchemyFileStore(db_uri),
        conversations,
        artifacts,
        AgentCache(artifact_store=artifacts, cache_dir=tmp_path / "cache"),
        permission_store=permissions,
        scheduled_task_store=tasks,
        host_store=hosts,
        auth_provider=UnifiedAuthProvider(source="header"),
        custom_agents_store=custom,
        project_store=projects,
        user_preferences_store=prefs,
    )
    permissions.ensure_user(_OWNER, is_admin=False)
    permissions.ensure_user("bob@example.com", is_admin=False)
    # A local row is all the pinned-host ownership check resolves against; the
    # host never has to be online for create validation.
    hosts.upsert_on_connect(_HOST_ID, "alice-laptop", _OWNER)
    return _LibraryServer(
        app, artifacts, agents, conversations, permissions, tasks, hosts, custom, projects, prefs
    )


@pytest.fixture()
async def client(library_server: _LibraryServer) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=library_server.app), base_url="http://test"
    ) as http:
        yield http


@pytest.fixture(autouse=True)
def _stub_bundle_workspace_validation(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """Avoid the host.stat RPC; record what the validation was asked to check."""
    calls: list[dict[str, object]] = []

    async def _validate(**kwargs: object) -> str:
        calls.append(kwargs)
        workspace = kwargs["workspace"]
        if not isinstance(workspace, str) or not workspace.startswith("/"):
            raise OmnigentError(
                "workspace must be an absolute path starting with /",
                code=ErrorCode.INVALID_INPUT,
            )
        return workspace

    monkeypatch.setattr(
        scheduled_tasks_routes, "validate_uploaded_bundle_host_workspace", _validate
    )
    return calls


def _headers(email: str = _OWNER) -> dict[str, str]:
    return {"X-Forwarded-Email": email}


async def _create_agent(client: httpx.AsyncClient, *, harness: str = "claude-native") -> str:
    response = await client.post(
        "/v1/custom-agents",
        headers=_headers(),
        files={"bundle": ("agent.tar.gz", _bundle(harness=harness))},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _create_joint_agent(client: httpx.AsyncClient) -> str:
    response = await client.post(
        "/v1/custom-agents",
        headers=_headers(),
        files={"bundle": ("agent.tar.gz", _joint_bundle())},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _task_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "name": "library nightly",
        "prompt": "do it",
        "rrule": _VALID_RRULE,
        "timezone": "UTC",
    }
    body.update(overrides)
    return body


async def test_create_with_owned_library_agent(
    client: httpx.AsyncClient,
    _stub_bundle_workspace_validation: list[dict[str, object]],
) -> None:
    """An owned ``ca_`` id is a valid task target; its bundle bounds the workspace."""
    agent_id = await _create_agent(client)
    response = await client.post(
        "/v1/scheduled-tasks",
        json=_task_body(agent_id=agent_id, host_id=_HOST_ID, workspace="/repo"),
        headers=_headers(),
    )
    assert response.status_code == 200, response.text
    created = response.json()
    assert created["agent_id"] == agent_id
    assert created["workspace"] == "/repo"
    assert created["host_id"] == _HOST_ID
    assert _stub_bundle_workspace_validation[0]["spec_cwd"] is None


async def test_create_with_another_owners_library_agent_is_404(
    client: httpx.AsyncClient,
) -> None:
    """Another owner's ``ca_`` id 404s exactly like an unknown agent id."""
    agent_id = await _create_agent(client)
    response = await client.post(
        "/v1/scheduled-tasks",
        json=_task_body(agent_id=agent_id),
        headers=_headers("bob@example.com"),
    )
    assert response.status_code == 404, response.text


async def test_library_agent_on_sandbox_target_is_rejected(client: httpx.AsyncClient) -> None:
    """A saved Agent cannot run on a server-provisioned sandbox."""
    agent_id = await _create_agent(client)
    response = await client.post(
        "/v1/scheduled-tasks",
        json=_task_body(agent_id=agent_id, execution_target="managed_sandbox"),
        headers=_headers(),
    )
    assert response.status_code == 400, response.text
    assert "connected computer" in response.text


async def test_permission_mode_incompatible_with_bundle_lead_harness(
    client: httpx.AsyncClient,
) -> None:
    """A Claude mode on a Codex bundle is rejected with the same 400 as a stored agent."""
    agent_id = await _create_agent(client, harness="codex")
    response = await client.post(
        "/v1/scheduled-tasks",
        json=_task_body(agent_id=agent_id, permission_mode="acceptEdits"),
        headers=_headers(),
    )
    assert response.status_code == 400, response.text
    assert "acceptEdits" in response.text


def _fire_deps(server: _LibraryServer) -> FireDeps:
    return FireDeps(
        scheduled_task_store=server.tasks,
        agent_store=server.agents,
        conversation_store=server.conversations,
        permission_store=server.permissions,
        host_store=server.hosts,
        host_registry=None,
        artifact_store=server.artifacts,
        custom_agents_store=server.custom,
        project_store=server.projects,
        preferences_store=server.prefs,
    )


async def _create_library_task(
    client: httpx.AsyncClient, agent_id: str, **overrides: object
) -> str:
    response = await client.post(
        "/v1/scheduled-tasks",
        json=_task_body(agent_id=agent_id, host_id=_HOST_ID, workspace="/repo", **overrides),
        headers=_headers(),
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


async def test_fire_launches_session_scoped_agent_from_saved_bundle(
    client: httpx.AsyncClient, library_server: _LibraryServer
) -> None:
    """A ``ca_`` fire copies the bundle into an ``ag_`` agent with the overrides."""
    agent_id = await _create_agent(client)
    task_id = await _create_library_task(
        client,
        agent_id,
        model_override="model-one",
        reasoning_effort="high",
        permission_mode="acceptEdits",
    )
    dispatched: list[Any] = []

    async def _dispatch(conv: Any, task: Any) -> None:
        dispatched.append(conv)

    on_fire = build_on_fire(_fire_deps(library_server), launch_dispatch=_dispatch)
    await on_fire(0, task_id)
    await _drain()

    assert len(dispatched) == 1
    conv = dispatched[0]
    assert conv.agent_id != agent_id
    stored_agent = library_server.agents.get(conv.agent_id)
    assert stored_agent is not None
    assert stored_agent.session_id == conv.id

    session = library_server.conversations.get_conversation(conv.id)
    assert session is not None
    assert session.labels[_TEMPLATE_LABEL] == agent_id
    assert session.labels["omnigent.ui"] == "terminal"
    assert session.model_override == "model-one"
    assert session.reasoning_effort == "high"
    assert session.terminal_launch_args == ["--permission-mode", "acceptEdits"]
    assert session.host_id == _HOST_ID
    assert session.workspace == "/repo"
    assert session.title == "library nightly"

    runs, _ = library_server.tasks.list_runs(task_id)
    assert [run.status for run in runs] == ["running"]


async def test_fire_writes_member_snapshot_labels(
    client: httpx.AsyncClient, library_server: _LibraryServer
) -> None:
    """A 2-member saved Agent's fire freezes the member snapshot on the session.

    The scheduled launch path writes the same ``omnigent.member.<role>``
    labels the interactive multipart create does, so the runner's member lock
    works for fired sessions too.
    """
    agent_id = await _create_joint_agent(client)
    task_id = await _create_library_task(client, agent_id)
    dispatched: list[Any] = []

    async def _dispatch(conv: Any, task: Any) -> None:
        dispatched.append(conv)

    on_fire = build_on_fire(_fire_deps(library_server), launch_dispatch=_dispatch)
    await on_fire(0, task_id)
    await _drain()

    assert len(dispatched) == 1
    session = library_server.conversations.get_conversation(dispatched[0].id)
    assert session is not None
    lead = parse_member_entry(session.labels[member_label_key("library-runner")])
    worker = parse_member_entry(session.labels[member_label_key("researcher")])
    assert lead == {
        "host": _HOST_ID,
        "harness": "claude-native",
        "model": "lead-model",
        "effort": None,
        "lead": True,
    }
    assert worker == {
        "host": _HOST_ID,
        "harness": "codex",
        "model": "worker-model",
        "effort": None,
        "lead": False,
    }


@pytest.mark.asyncio
async def test_fire_member_snapshot_uses_the_tasks_project_chain(
    client: httpx.AsyncClient, library_server: _LibraryServer
) -> None:
    """A fired joint Agent's unset member effort fills from the task's project.

    The worker's saved model is kept; its unset effort takes the project's
    per-host default for the worker harness, which only resolves if the fire
    path passes the task's project into the snapshot.
    """
    project = (
        await client.post(
            "/v1/projects",
            json={
                "name": "P",
                "config": {
                    "calling_defaults": {_HOST_ID: {"harnesses": {"codex": {"effort": "xhigh"}}}}
                },
            },
            headers=_headers(),
        )
    ).json()
    agent_id = await _create_joint_agent(client)
    task_id = await _create_library_task(client, agent_id, project_id=project["id"])
    dispatched: list[Any] = []

    async def _dispatch(conv: Any, task: Any) -> None:
        dispatched.append(conv)

    on_fire = build_on_fire(_fire_deps(library_server), launch_dispatch=_dispatch)
    await on_fire(0, task_id)
    await _drain()

    assert len(dispatched) == 1
    session = library_server.conversations.get_conversation(dispatched[0].id)
    assert session is not None
    worker = parse_member_entry(session.labels[member_label_key("researcher")])
    assert worker is not None
    assert worker["model"] == "worker-model"
    assert worker["effort"] == "xhigh"
    lead = parse_member_entry(session.labels[member_label_key("library-runner")])
    assert lead is not None and lead["effort"] is None


@pytest.mark.asyncio
async def test_fire_member_snapshot_honours_saved_member_hosts(
    client: httpx.AsyncClient, library_server: _LibraryServer
) -> None:
    """A fired joint Agent freezes each member's saved library host."""
    agent_id = await _create_joint_agent(client)
    library_server.hosts.upsert_on_connect(_WORKER_HOST_ID, "worker-laptop", _OWNER)
    patched = await client.patch(
        f"/v1/custom-agents/{agent_id}",
        headers=_headers(),
        json={
            "version": 1,
            "members": [
                {
                    "name": "library-runner",
                    "description": None,
                    "harness": "claude-native",
                    "model": "lead-model",
                    "reasoning_effort": None,
                    "lead": True,
                },
                {
                    "name": "researcher",
                    "description": None,
                    "harness": "codex",
                    "model": "worker-model",
                    "reasoning_effort": None,
                    "lead": False,
                    "host_id": _WORKER_HOST_ID,
                },
            ],
        },
    )
    assert patched.status_code == 200, patched.text
    task_id = await _create_library_task(client, agent_id)
    dispatched: list[Any] = []

    async def _dispatch(conv: Any, task: Any) -> None:
        dispatched.append(conv)

    on_fire = build_on_fire(_fire_deps(library_server), launch_dispatch=_dispatch)
    await on_fire(0, task_id)
    await _drain()

    assert len(dispatched) == 1
    session = library_server.conversations.get_conversation(dispatched[0].id)
    assert session is not None
    lead = parse_member_entry(session.labels[member_label_key("library-runner")])
    worker = parse_member_entry(session.labels[member_label_key("researcher")])
    assert lead is not None and lead["host"] == _HOST_ID
    assert worker is not None and worker["host"] == _WORKER_HOST_ID


async def test_fire_after_library_agent_deleted_records_failed_run(
    client: httpx.AsyncClient, library_server: _LibraryServer
) -> None:
    """A deleted Agent fails the fire through the failed-run path, with the reason."""
    agent_id = await _create_agent(client)
    task_id = await _create_library_task(client, agent_id)
    deleted = await client.delete(f"/v1/custom-agents/{agent_id}", headers=_headers())
    assert deleted.status_code == 204, deleted.text

    dispatched: list[Any] = []

    async def _dispatch(conv: Any, task: Any) -> None:
        dispatched.append(conv)

    on_fire = build_on_fire(_fire_deps(library_server), launch_dispatch=_dispatch)
    await on_fire(0, task_id)
    await _drain()

    assert dispatched == []
    runs, _ = library_server.tasks.list_runs(task_id)
    assert [run.status for run in runs] == ["failed"]
    assert runs[0].error_code == ErrorCode.NOT_FOUND
    assert f"Agent not found: {agent_id!r}" in (runs[0].error or "")
