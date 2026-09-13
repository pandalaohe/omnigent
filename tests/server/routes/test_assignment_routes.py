"""Tests for the assignment lifecycle routes.

Covers the gate split (only create and refresh are gated), dispatch
validation and server-derived refs, publication, refresh, owner-scoped
list/get, messages, cancel, retry, and the runner-bound complete/finish
pair. States reached only by the coordinator (starting/running) are
seeded through the stores directly.
"""

from __future__ import annotations

import dataclasses
import secrets
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.orm import Session as _SASession

from omnigent.db.db_models import SqlAssignment, current_workspace_id
from omnigent.db.utils import get_or_create_engine, now_epoch
from omnigent.entities import Assignment, AssignmentInputEntry
from omnigent.entities.assignment import inputs_to_json
from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import LEVEL_OWNER, UnifiedAuthProvider
from omnigent.server.feature_flags import resolve_feature_flags
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.assignment_store.sqlalchemy_store import SqlAlchemyAssignmentStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.project_host_binding_store.sqlalchemy_store import (
    SqlAlchemyProjectHostBindingStore,
)
from omnigent.stores.project_repository_store.sqlalchemy_store import (
    SqlAlchemyProjectRepositoryStore,
)
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore

ALICE = "alice@example.com"
BOB = "bob@example.com"
AGENT_ID = "087b7cb7ac30abf4debfaa578d052ec6"
_HOST_A = "a1b2c3d4e5f60718293a4b5c6d7e8f01"


def _as_user(user: str) -> dict[str, str]:
    """Header identifying the requesting user under header auth."""
    return {"X-Forwarded-Email": user}


def _build_app(
    db_uri: str,
    tmp_path: Path,
    *,
    enabled: bool = True,
    auth: bool = False,
) -> FastAPI:
    """Build a FastAPI app with the assignment surface wired."""
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    flags = resolve_feature_flags({"OMNIGENT_FEATURES": "project_assignments"} if enabled else {})
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(
            artifact_store=artifact_store,
            cache_dir=tmp_path / "cache",
        ),
        host_store=HostStore(db_uri),
        project_store=SqlAlchemyProjectStore(db_uri),
        project_repository_store=SqlAlchemyProjectRepositoryStore(db_uri),
        project_host_binding_store=SqlAlchemyProjectHostBindingStore(db_uri),
        assignment_store=SqlAlchemyAssignmentStore(db_uri),
        permission_store=SqlAlchemyPermissionStore(db_uri) if auth else None,
        auth_provider=UnifiedAuthProvider(source="header") if auth else None,
        feature_flags=flags,
    )


@pytest.fixture()
def app(db_uri: str, tmp_path: Path) -> FastAPI:
    """App with the assignment surface enabled (single-user)."""
    return _build_app(db_uri, tmp_path)


@pytest_asyncio.fixture()
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """HTTP client wired to the enabled app."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture()
def disabled_app(db_uri: str, tmp_path: Path) -> FastAPI:
    """App with the collaboration flag off."""
    return _build_app(db_uri, tmp_path, enabled=False)


@pytest_asyncio.fixture()
async def disabled_client(disabled_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """HTTP client wired to the flag-disabled app."""
    transport = httpx.ASGITransport(app=disabled_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture()
def multi_user_app(db_uri: str, tmp_path: Path) -> FastAPI:
    """App with header auth, for ownership tests."""
    return _build_app(db_uri, tmp_path, auth=True)


@pytest_asyncio.fixture()
async def multi_user_client(
    multi_user_app: FastAPI,
) -> AsyncIterator[httpx.AsyncClient]:
    """HTTP client wired to the header-auth app."""
    transport = httpx.ASGITransport(app=multi_user_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _ensure_agent(db_uri: str) -> None:
    """Register the template agent dispatch tests target."""
    agent_store = SqlAlchemyAgentStore(db_uri)
    if agent_store.get(AGENT_ID) is None:
        agent_store.create(
            agent_id=AGENT_ID,
            name="test-agent",
            bundle_location=f"{AGENT_ID}/bundle",
        )


def _uid(seed: str) -> str:
    """Deterministic bare 32-char hex id from a readable seed."""
    return uuid.uuid5(uuid.NAMESPACE_DNS, seed).hex


def _make_session(
    db_uri: str,
    *,
    owner: str | None = None,
    project_id: str | None = None,
    runner_id: str | None = None,
    title: str = "s",
) -> str:
    """Create a session, optionally owned, filed and runner-bound."""
    conv = SqlAlchemyConversationStore(db_uri).create_conversation(
        title=f"{title}-{uuid.uuid4().hex[:6]}",
        agent_id=AGENT_ID,
        runner_id=runner_id,
        project_id=project_id,
    )
    if owner is not None:
        perms = SqlAlchemyPermissionStore(db_uri)
        perms.ensure_user(owner)
        perms.grant(owner, conv.id, LEVEL_OWNER)
    return conv.id


async def _make_project(
    client: httpx.AsyncClient,
    name: str | None = None,
    headers: dict[str, str] | None = None,
) -> str:
    """Create a project and return its id."""
    unique = name or f"Work-{uuid.uuid4().hex[:8]}"
    resp = await client.post("/v1/projects", json={"name": unique}, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def _enable_collaboration(
    client: httpx.AsyncClient,
    project_id: str,
    headers: dict[str, str] | None = None,
) -> None:
    """Flip the collaboration switch on from revision 0."""
    resp = await client.patch(
        f"/v1/projects/{project_id}/collaboration",
        json={"enabled": True, "expected_revision": 0},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text


async def _register_repo(
    client: httpx.AsyncClient,
    project_id: str,
    name: str = "root",
    remote_url: str = "https://example.com/org/repo.git",
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Register a repository via the route and return its body."""
    resp = await client.put(
        f"/v1/projects/{project_id}/repositories/{name}",
        json={"remote_url": remote_url, "default_branch": "main"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _repos(*names: str, commits: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """One minimal repository entry per name."""
    return [
        {
            "repository_name": name,
            "commit": (commits or {}).get(name, "a" * 40),
            "manifest_digest": "d" * 64,
        }
        for name in names
    ]


def _create_payload(
    source_session_id: str,
    *,
    assignment_id: str | None = None,
    target_agent_id: str = AGENT_ID,
    repositories: list[dict[str, Any]] | None = None,
    task: str = "Do the thing",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A minimal valid create body with fresh id and idempotency key."""
    payload: dict[str, Any] = {
        "id": assignment_id or uuid.uuid4().hex,
        "source_session_id": source_session_id,
        "target_agent_id": target_agent_id,
        "task": task,
        "repositories": repositories if repositories is not None else _repos("root"),
        "idempotency_key": f"key-{uuid.uuid4().hex[:8]}",
    }
    if extra:
        payload.update(extra)
    return payload


def _runner_pair() -> tuple[str, str]:
    """A (tunnel token, bound runner id) pair for runner-bound calls."""
    token = secrets.token_hex(16)
    return token, token_bound_runner_id(token)


def _runner_headers(token: str | None) -> dict[str, str]:
    """Tunnel-token header, or no header when the token is missing."""
    return {RUNNER_TUNNEL_TOKEN_HEADER: token} if token else {}


def _seed_input(name: str = "root", commit: str = "a" * 40) -> AssignmentInputEntry:
    """One minimal input entry for store-seeded rows."""
    return AssignmentInputEntry(
        repository_name=name,
        repository_revision=1,
        remote_url="https://example.com/org/repo.git",
        input_commit=commit,
        input_ref=f"refs/omnigent/assignments/x/input/{name}",
        context_manifest_path=".agents/project/manifest.json",
        manifest_digest="d" * 64,
        artifact_paths=[],
        is_execution_root=name == "root",
    )


def _seed_assignment(
    db_uri: str,
    seed: str,
    *,
    owner: str | None = None,
    to_state: str | None = None,
) -> Assignment:
    """Create a store-level row, optionally moved out of ``preparing``."""
    store = SqlAlchemyAssignmentStore(db_uri)
    created = store.create(
        Assignment(
            id=_uid(f"{seed}-id"),
            project_id=_uid(f"{seed}-proj"),
            source_session_id=_uid(f"{seed}-sess"),
            target_agent_id=AGENT_ID,
            task=f"task {seed}",
            inputs=[_seed_input()],
            idempotency_key=f"key-{seed}",
            request_digest="e" * 64,
            owner_user_id=owner,
        )
    )
    if to_state is not None and to_state != "preparing":
        moved = store.transition(created.id, from_state="preparing", to_state=to_state)
        assert moved is not None
        return moved
    return created


def _to_running(
    db_uri: str,
    assignment_id: str,
    *,
    host_id: str,
    session_id: str,
    runner_id: str,
) -> str:
    """Claim an attempt and drive the row to ``running``; return attempt id."""
    store = SqlAlchemyAssignmentStore(db_uri)
    now = now_epoch()
    attempt = store.claim_attempt(assignment_id, host_id=host_id, now=now)
    assert attempt is not None
    updated = store.update_attempt(
        assignment_id, attempt.id, session_id=session_id, runner_id=runner_id
    )
    assert updated is not None
    moved = store.transition(assignment_id, from_state="starting", to_state="running")
    assert moved is not None
    return attempt.id


def _to_interrupted(db_uri: str, assignment_id: str, *, end_attempt: bool) -> str | None:
    """Move a ``running`` row to ``interrupted``; optionally end the attempt."""
    store = SqlAlchemyAssignmentStore(db_uri)
    moved = store.transition(assignment_id, from_state="running", to_state="interrupted")
    assert moved is not None
    assert moved.active_attempt_id is not None
    if end_attempt:
        ended = store.update_attempt(
            assignment_id,
            moved.active_attempt_id,
            state="finished",
            ended_at=now_epoch(),
        )
        assert ended is not None
    return moved.active_attempt_id


async def _setup_dispatch(client: httpx.AsyncClient, db_uri: str) -> dict[str, str]:
    """A collaboration-enabled project with a repo and an owned session."""
    _ensure_agent(db_uri)
    project_id = await _make_project(client)
    await _enable_collaboration(client, project_id)
    await _register_repo(client, project_id)
    session_id = _make_session(db_uri, project_id=project_id)
    return {"project_id": project_id, "session_id": session_id}


# ── Gate split ────────────────────────────────────────────────


async def test_flag_off_create_and_refresh_404(
    disabled_client: httpx.AsyncClient, db_uri: str
) -> None:
    """With the flag off, create and refresh are dark — even malformed."""
    assignment_id = uuid.uuid4().hex
    resp = await disabled_client.post("/v1/assignments", json={})
    assert resp.status_code == 404, resp.text
    resp = await disabled_client.post(
        "/v1/assignments",
        json=_create_payload(_uid("sess"), assignment_id=assignment_id),
    )
    assert resp.status_code == 404, resp.text
    seeded = _seed_assignment(db_uri, "flag-refresh", to_state="waiting")
    resp = await disabled_client.post(f"/v1/assignments/{seeded.id}/refresh")
    assert resp.status_code == 404, resp.text


async def test_flag_off_rest_of_surface_still_served(
    disabled_client: httpx.AsyncClient, db_uri: str
) -> None:
    """Disabling the flag lets in-flight work finish, publish and report."""
    _ensure_agent(db_uri)
    token, runner_id = _runner_pair()
    run_session = _make_session(db_uri, runner_id=runner_id)
    preparing = _seed_assignment(db_uri, "flag-preparing")
    waiting = _seed_assignment(db_uri, "flag-waiting", to_state="waiting")
    running = _seed_assignment(db_uri, "flag-running", to_state="waiting")
    _to_running(db_uri, running.id, host_id=_HOST_A, session_id=run_session, runner_id=runner_id)
    interrupted = _seed_assignment(db_uri, "flag-interrupted", to_state="waiting")
    _to_running(
        db_uri,
        interrupted.id,
        host_id=_HOST_A,
        session_id=_make_session(db_uri),
        runner_id="runner_other",
    )
    _to_interrupted(db_uri, interrupted.id, end_attempt=True)

    resp = await disabled_client.get(f"/v1/assignments/{preparing.id}")
    assert resp.status_code == 200, resp.text
    resp = await disabled_client.get("/v1/assignments")
    assert resp.status_code == 200, resp.text
    assert {row["id"] for row in resp.json()["data"]} >= {
        preparing.id,
        waiting.id,
        running.id,
    }
    resp = await disabled_client.post(
        f"/v1/assignments/{preparing.id}/published",
        json={"refs": [{"repository_name": "root", "commit": "a" * 40}]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "waiting"
    resp = await disabled_client.post(
        f"/v1/assignments/{preparing.id}/messages",
        json={
            "sender_session_id": _uid("sess"),
            "body": "hello",
            "idempotency_key": "k1",
        },
    )
    assert resp.status_code == 201, resp.text
    resp = await disabled_client.get(f"/v1/assignments/{preparing.id}/messages")
    assert resp.status_code == 200, resp.text
    resp = await disabled_client.post(f"/v1/assignments/{waiting.id}/cancel", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "cancelled"
    resp = await disabled_client.post(f"/v1/assignments/{interrupted.id}/retry")
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "waiting"
    resp = await disabled_client.post(
        f"/v1/assignments/{running.id}/complete",
        json={
            "session_id": run_session,
            "outputs": [{"repository_name": "root", "commit": "b" * 40}],
            "summary": "done",
        },
        headers=_runner_headers(token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "publishing"
    resp = await disabled_client.post(
        f"/v1/assignments/{running.id}/finish",
        json={
            "session_id": run_session,
            "refs": [{"repository_name": "root", "commit": "b" * 40}],
        },
        headers=_runner_headers(token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "succeeded"


# ── Create ────────────────────────────────────────────────────


async def test_create_happy_path_derives_refs(client: httpx.AsyncClient, db_uri: str) -> None:
    """Create stores the row in preparing with server-derived input refs."""
    setup = await _setup_dispatch(client, db_uri)
    payload = _create_payload(setup["session_id"])
    resp = await client.post("/v1/assignments", json=payload)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["state"] == "preparing"
    assert body["project_id"] == setup["project_id"]
    assert body["project_revision"] == 1
    (entry,) = body["inputs"]
    assert entry["input_ref"] == (f"refs/omnigent/assignments/{payload['id']}/input/root")
    assert entry["remote_url"] == "https://example.com/org/repo.git"
    assert entry["input_commit"] == "a" * 40
    assert entry["is_execution_root"] is True


async def test_create_switch_off_409_names_project(client: httpx.AsyncClient, db_uri: str) -> None:
    """A disabled switch refuses dispatch with the project name as reason."""
    _ensure_agent(db_uri)
    project_id = await _make_project(client, "Off project")
    await _register_repo(client, project_id)
    session_id = _make_session(db_uri, project_id=project_id)
    resp = await client.post("/v1/assignments", json=_create_payload(session_id))
    assert resp.status_code == 409, resp.text
    assert "Off project" in resp.json()["error"]["message"]


async def test_create_unknown_repository_400(client: httpx.AsyncClient, db_uri: str) -> None:
    """A repository with no registration is refused by name."""
    setup = await _setup_dispatch(client, db_uri)
    resp = await client.post(
        "/v1/assignments",
        json=_create_payload(setup["session_id"], repositories=_repos("ghost")),
    )
    assert resp.status_code == 400, resp.text
    assert "ghost" in resp.json()["error"]["message"]


async def test_create_two_repositories_need_execution_root(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    """Two repositories without an execution root are refused; with one succeed."""
    setup = await _setup_dispatch(client, db_uri)
    await _register_repo(client, setup["project_id"], name="docs")
    payload = _create_payload(setup["session_id"], repositories=_repos("root", "docs"))
    resp = await client.post("/v1/assignments", json=payload)
    assert resp.status_code == 400, resp.text
    payload = _create_payload(
        setup["session_id"],
        repositories=_repos("root", "docs"),
        extra={"execution_root": "docs"},
    )
    resp = await client.post("/v1/assignments", json=payload)
    assert resp.status_code == 201, resp.text
    roots = {e["repository_name"]: e["is_execution_root"] for e in resp.json()["inputs"]}
    assert roots == {"root": False, "docs": True}


async def test_create_idempotent_retry_and_digest_conflict(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    """Same id + digest returns the row (200); a changed task 409s."""
    setup = await _setup_dispatch(client, db_uri)
    payload = _create_payload(setup["session_id"])
    first = await client.post("/v1/assignments", json=payload)
    assert first.status_code == 201, first.text
    second = await client.post("/v1/assignments", json=payload)
    assert second.status_code == 200, second.text
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["created_at"] == first.json()["created_at"]
    changed = {**payload, "task": "A different instruction"}
    conflicted = await client.post("/v1/assignments", json=changed)
    assert conflicted.status_code == 409, conflicted.text
    other_id = {**payload, "id": uuid.uuid4().hex}
    keyed = await client.post("/v1/assignments", json=other_id)
    assert keyed.status_code == 200, keyed.text
    assert keyed.json()["id"] == first.json()["id"]


async def test_create_session_without_project_400(client: httpx.AsyncClient, db_uri: str) -> None:
    """An unfiled session cannot dispatch; the error names the session."""
    _ensure_agent(db_uri)
    session_id = _make_session(db_uri)
    resp = await client.post("/v1/assignments", json=_create_payload(session_id))
    assert resp.status_code == 400, resp.text
    assert session_id in resp.json()["error"]["message"]


async def test_create_validation_400s(client: httpx.AsyncClient, db_uri: str) -> None:
    """Bad ids, commits, agents and roots are refused before any write."""
    setup = await _setup_dispatch(client, db_uri)
    session_id = setup["session_id"]
    bad_id = _create_payload(session_id, assignment_id="not-hex")
    assert (await client.post("/v1/assignments", json=bad_id)).status_code == 400
    bad_commit = _create_payload(session_id, repositories=_repos("root", commits={"root": "zz"}))
    assert (await client.post("/v1/assignments", json=bad_commit)).status_code == 400
    bad_agent = _create_payload(session_id, target_agent_id="f" * 32)
    resp = await client.post("/v1/assignments", json=bad_agent)
    assert resp.status_code == 400, resp.text
    bad_root = _create_payload(
        session_id, repositories=_repos("root"), extra={"execution_root": "ghost"}
    )
    assert (await client.post("/v1/assignments", json=bad_root)).status_code == 400
    dupes = _create_payload(session_id, repositories=_repos("root") + _repos("root"))
    assert (await client.post("/v1/assignments", json=dupes)).status_code == 400


async def test_create_requested_host_ownership(client: httpx.AsyncClient, db_uri: str) -> None:
    """An unknown requested host 404s; a registered one is accepted."""
    setup = await _setup_dispatch(client, db_uri)
    missing = _create_payload(setup["session_id"], extra={"requested_host_id": "0" * 32})
    assert (await client.post("/v1/assignments", json=missing)).status_code == 404
    HostStore(db_uri).upsert_on_connect(_HOST_A, "box-a", "local")
    pinned = _create_payload(setup["session_id"], extra={"requested_host_id": _HOST_A})
    resp = await client.post("/v1/assignments", json=pinned)
    assert resp.status_code == 201, resp.text
    assert resp.json()["requested_host_id"] == _HOST_A


async def test_create_session_access_multi_user(
    multi_user_client: httpx.AsyncClient, db_uri: str
) -> None:
    """No access to the source session 404s; weaker access 403s."""
    _ensure_agent(db_uri)
    project_id = await _make_project(multi_user_client, "Alice work", headers=_as_user(ALICE))
    await _enable_collaboration(multi_user_client, project_id, headers=_as_user(ALICE))
    await _register_repo(multi_user_client, project_id, headers=_as_user(ALICE))
    session_id = _make_session(db_uri, owner=ALICE, project_id=project_id)
    stranger = await multi_user_client.post(
        "/v1/assignments",
        json=_create_payload(session_id),
        headers=_as_user(BOB),
    )
    assert stranger.status_code == 404, stranger.text
    perms = SqlAlchemyPermissionStore(db_uri)
    perms.grant(BOB, session_id, 2)
    editor = await multi_user_client.post(
        "/v1/assignments",
        json=_create_payload(session_id),
        headers=_as_user(BOB),
    )
    assert editor.status_code == 403, editor.text


async def test_create_requested_host_owned_by_other_user_403(
    multi_user_client: httpx.AsyncClient, db_uri: str
) -> None:
    """An assignment may not name another user's host."""
    _ensure_agent(db_uri)
    project_id = await _make_project(multi_user_client, "Alice work", headers=_as_user(ALICE))
    await _enable_collaboration(multi_user_client, project_id, headers=_as_user(ALICE))
    await _register_repo(multi_user_client, project_id, headers=_as_user(ALICE))
    session_id = _make_session(db_uri, owner=ALICE, project_id=project_id)
    HostStore(db_uri).upsert_on_connect(_HOST_A, "bob-box", BOB)
    resp = await multi_user_client.post(
        "/v1/assignments",
        json=_create_payload(session_id, extra={"requested_host_id": _HOST_A}),
        headers=_as_user(ALICE),
    )
    assert resp.status_code == 403, resp.text


# ── Published ─────────────────────────────────────────────────


async def test_published_all_refs_moves_to_waiting(client: httpx.AsyncClient, db_uri: str) -> None:
    """Every input at its advertised commit opens the wait."""
    setup = await _setup_dispatch(client, db_uri)
    payload = _create_payload(setup["session_id"])
    created = (await client.post("/v1/assignments", json=payload)).json()
    resp = await client.post(
        f"/v1/assignments/{payload['id']}/published",
        json={"refs": [{"repository_name": "root", "commit": "a" * 40}]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "waiting"
    assert resp.json()["next_check_at"] is not None
    assert created["id"] == resp.json()["id"]
    messages = (await client.get(f"/v1/assignments/{payload['id']}/messages")).json()["data"]
    assert any(m["body"] == "preparing -> waiting" for m in messages)


async def test_published_partial_lands_failed_with_landed_ref(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    """One missing ref of two fails the row and records the landed one."""
    setup = await _setup_dispatch(client, db_uri)
    await _register_repo(client, setup["project_id"], name="docs")
    payload = _create_payload(
        setup["session_id"],
        repositories=_repos("root", "docs"),
        extra={"execution_root": "root"},
    )
    assert (await client.post("/v1/assignments", json=payload)).status_code == 201
    resp = await client.post(
        f"/v1/assignments/{payload['id']}/published",
        json={"refs": [{"repository_name": "root", "commit": "a" * 40}]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["state"] == "failed"
    assert body["error_code"] == "publication_failed"
    by_name = {e["repository_name"]: e for e in body["inputs"]}
    assert by_name["root"]["observed_commit"] == "a" * 40
    assert by_name["docs"]["observed_commit"] is None


async def test_published_idempotent_repeat_and_409_on_change(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    """An identical repeat returns the waiting row; a different call 409s."""
    setup = await _setup_dispatch(client, db_uri)
    payload = _create_payload(setup["session_id"])
    assert (await client.post("/v1/assignments", json=payload)).status_code == 201
    refs = {"refs": [{"repository_name": "root", "commit": "a" * 40}]}
    first = await client.post(f"/v1/assignments/{payload['id']}/published", json=refs)
    assert first.status_code == 200
    repeat = await client.post(f"/v1/assignments/{payload['id']}/published", json=refs)
    assert repeat.status_code == 200, repeat.text
    assert repeat.json()["state"] == "waiting"
    changed = await client.post(
        f"/v1/assignments/{payload['id']}/published",
        json={"refs": [{"repository_name": "root", "commit": "b" * 40}]},
    )
    assert changed.status_code == 409, changed.text
    assert "waiting" in changed.json()["error"]["message"]


# ── Refresh ───────────────────────────────────────────────────


async def test_refresh_re_pins_revisions(client: httpx.AsyncClient, db_uri: str) -> None:
    """Refresh picks up the moved repository and collaboration revisions."""
    setup = await _setup_dispatch(client, db_uri)
    payload = _create_payload(setup["session_id"])
    assert (await client.post("/v1/assignments", json=payload)).status_code == 201
    refs = {"refs": [{"repository_name": "root", "commit": "a" * 40}]}
    assert (
        await client.post(f"/v1/assignments/{payload['id']}/published", json=refs)
    ).status_code == 200
    revised = await _register_repo(
        client, setup["project_id"], remote_url="https://example.com/org/other.git"
    )
    assert revised["revision"] == 2
    engine = get_or_create_engine(db_uri)
    with _SASession(engine) as session:
        row = session.get(SqlAssignment, (current_workspace_id(), payload["id"]))
        assert row is not None
        row.wait_reason = "host_offline"
        row.resolved_binding_id = "b" * 32
        row.resolved_binding_revision = 3
        session.commit()
    resp = await client.post(f"/v1/assignments/{payload['id']}/refresh")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["state"] == "waiting"
    (entry,) = body["inputs"]
    assert entry["repository_revision"] == 2
    assert entry["manifest_digest"] == "d" * 64
    assert body["wait_reason"] is None
    assert body["next_check_at"] is not None
    assert body["resolved_binding_id"] is None
    assert body["resolved_binding_revision"] is None


async def test_refresh_rejects_non_waiting_and_disabled_switch(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    """Refresh 409s off waiting, and 409s naming the project when off."""
    setup = await _setup_dispatch(client, db_uri)
    payload = _create_payload(setup["session_id"])
    assert (await client.post("/v1/assignments", json=payload)).status_code == 201
    early = await client.post(f"/v1/assignments/{payload['id']}/refresh")
    assert early.status_code == 409, early.text
    refs = {"refs": [{"repository_name": "root", "commit": "a" * 40}]}
    assert (
        await client.post(f"/v1/assignments/{payload['id']}/published", json=refs)
    ).status_code == 200
    config = (await client.get(f"/v1/projects/{setup['project_id']}/collaboration")).json()
    off = await client.patch(
        f"/v1/projects/{setup['project_id']}/collaboration",
        json={"enabled": False, "expected_revision": config["revision"]},
    )
    assert off.status_code == 200, off.text
    refused = await client.post(f"/v1/assignments/{payload['id']}/refresh")
    assert refused.status_code == 409, refused.text


# ── List / get owner scoping ──────────────────────────────────


async def test_list_and_get_are_owner_scoped(
    multi_user_client: httpx.AsyncClient, db_uri: str
) -> None:
    """Another user's assignment is invisible in get and list."""
    _ensure_agent(db_uri)
    for user, name in ((ALICE, "Alice work"), (BOB, "Bob work")):
        headers = _as_user(user)
        project_id = await _make_project(multi_user_client, name, headers=headers)
        await _enable_collaboration(multi_user_client, project_id, headers=headers)
        await _register_repo(multi_user_client, project_id, headers=headers)
        session_id = _make_session(db_uri, owner=user, project_id=project_id)
        resp = await multi_user_client.post(
            "/v1/assignments", json=_create_payload(session_id), headers=headers
        )
        assert resp.status_code == 201, resp.text
    listed = await multi_user_client.get("/v1/assignments", headers=_as_user(BOB))
    assert listed.status_code == 200, listed.text
    assert len(listed.json()["data"]) == 1
    alice_row = (await multi_user_client.get("/v1/assignments", headers=_as_user(ALICE))).json()[
        "data"
    ][0]
    assert (
        await multi_user_client.get(f"/v1/assignments/{alice_row['id']}", headers=_as_user(BOB))
    ).status_code == 404
    assert (
        await multi_user_client.get(f"/v1/assignments/{alice_row['id']}", headers=_as_user(ALICE))
    ).status_code == 200


async def test_list_role_filters(client: httpx.AsyncClient, db_uri: str) -> None:
    """role=sent filters by source session; role=received by attempt session."""
    _ensure_agent(db_uri)
    store = SqlAlchemyAssignmentStore(db_uri)
    second_session = _make_session(db_uri)
    first = _seed_assignment(db_uri, "role-first")
    _seed_assignment(db_uri, "role-second")
    store.transition(first.id, from_state="preparing", to_state="waiting")
    attempt = store.claim_attempt(first.id, host_id=_HOST_A, now=now_epoch())
    assert attempt is not None
    store.update_attempt(first.id, attempt.id, session_id=second_session)

    sent = await client.get(
        "/v1/assignments",
        params={"role": "sent", "source_session_id": first.source_session_id},
    )
    assert sent.status_code == 200, sent.text
    assert [row["id"] for row in sent.json()["data"]] == [first.id]
    received = await client.get(
        "/v1/assignments", params={"role": "received", "session_id": second_session}
    )
    assert received.status_code == 200, received.text
    assert [row["id"] for row in received.json()["data"]] == [first.id]
    missing = await client.get("/v1/assignments", params={"role": "sent"})
    assert missing.status_code == 400, missing.text
    missing = await client.get("/v1/assignments", params={"role": "received"})
    assert missing.status_code == 400, missing.text
    assert (await client.get("/v1/assignments", params={"role": "sideways"})).status_code == 400
    assert (await client.get("/v1/assignments", params={"state": "bogus"})).status_code == 400
    assert (await client.get("/v1/assignments", params={"limit": 101})).status_code == 422


async def test_list_paging(client: httpx.AsyncClient, db_uri: str) -> None:
    """Cursor paging walks the owner's rows and reports has_more."""
    first = _seed_assignment(db_uri, "page-first")
    second = _seed_assignment(db_uri, "page-second")
    page = await client.get("/v1/assignments", params={"limit": 1})
    assert page.status_code == 200, page.text
    body = page.json()
    assert len(body["data"]) == 1
    assert body["has_more"] is True
    rest = await client.get("/v1/assignments", params={"limit": 1, "after": body["last_id"]})
    assert rest.status_code == 200, rest.text
    assert rest.json()["has_more"] is False
    assert {body["data"][0]["id"], rest.json()["data"][0]["id"]} == {
        first.id,
        second.id,
    }


# ── Messages ──────────────────────────────────────────────────


async def test_messages_append_idempotent_and_page(client: httpx.AsyncClient, db_uri: str) -> None:
    """Notes append idempotently on the key and page by cursor."""
    setup = await _setup_dispatch(client, db_uri)
    payload = _create_payload(setup["session_id"])
    assert (await client.post("/v1/assignments", json=payload)).status_code == 201
    note = {
        "sender_session_id": setup["session_id"],
        "body": "progress",
        "idempotency_key": "m1",
    }
    first = await client.post(f"/v1/assignments/{payload['id']}/messages", json=note)
    assert first.status_code == 201, first.text
    retry = await client.post(f"/v1/assignments/{payload['id']}/messages", json=note)
    assert retry.status_code == 201, retry.text
    assert retry.json()["id"] == first.json()["id"]
    second_note = {**note, "body": "more", "idempotency_key": "m2"}
    second = await client.post(f"/v1/assignments/{payload['id']}/messages", json=second_note)
    assert second.status_code == 201, second.text
    page = await client.get(f"/v1/assignments/{payload['id']}/messages", params={"limit": 1})
    assert page.status_code == 200, page.text
    assert page.json()["has_more"] is True
    rest = await client.get(
        f"/v1/assignments/{payload['id']}/messages",
        params={"after": page.json()["last_id"]},
    )
    assert rest.status_code == 200, rest.text
    assert len(rest.json()["data"]) == 1
    assert rest.json()["has_more"] is False
    assert {
        page.json()["data"][0]["id"],
        rest.json()["data"][0]["id"],
    } == {first.json()["id"], second.json()["id"]}
    again = await client.get(f"/v1/assignments/{payload['id']}/messages")
    assert [m["id"] for m in again.json()["data"]] == [m["id"] for m in page.json()["data"]] + [
        m["id"] for m in rest.json()["data"]
    ]


async def test_messages_require_sender_access(
    multi_user_client: httpx.AsyncClient, db_uri: str
) -> None:
    """A note from a session the caller cannot own is refused."""
    _ensure_agent(db_uri)
    headers = _as_user(ALICE)
    project_id = await _make_project(multi_user_client, "Alice work", headers=headers)
    await _enable_collaboration(multi_user_client, project_id, headers=headers)
    await _register_repo(multi_user_client, project_id, headers=headers)
    session_id = _make_session(db_uri, owner=ALICE, project_id=project_id)
    created = await multi_user_client.post(
        "/v1/assignments", json=_create_payload(session_id), headers=headers
    )
    assert created.status_code == 201, created.text
    assignment_id = created.json()["id"]
    foreign_session = _make_session(db_uri, owner=BOB)
    refused = await multi_user_client.post(
        f"/v1/assignments/{assignment_id}/messages",
        json={
            "sender_session_id": foreign_session,
            "body": "hi",
            "idempotency_key": "k",
        },
        headers=headers,
    )
    assert refused.status_code in (403, 404), refused.text
    assert (
        await multi_user_client.get(
            f"/v1/assignments/{assignment_id}/messages", headers=_as_user(BOB)
        )
    ).status_code == 404


# ── Cancel ────────────────────────────────────────────────────


async def test_cancel_from_waiting_cancels(client: httpx.AsyncClient, db_uri: str) -> None:
    """Cancelling a waiting row cancels it and records the transition."""
    setup = await _setup_dispatch(client, db_uri)
    payload = _create_payload(setup["session_id"])
    assert (await client.post("/v1/assignments", json=payload)).status_code == 201
    refs = {"refs": [{"repository_name": "root", "commit": "a" * 40}]}
    assert (
        await client.post(f"/v1/assignments/{payload['id']}/published", json=refs)
    ).status_code == 200
    resp = await client.post(
        f"/v1/assignments/{payload['id']}/cancel", json={"reason": "no longer needed"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "cancelled"
    messages = (await client.get(f"/v1/assignments/{payload['id']}/messages")).json()["data"]
    assert any(
        m["body"].startswith("waiting -> cancelled")
        and "no longer needed" in m["body"]
        and m["sender_session_id"] is None
        for m in messages
    )


async def test_cancel_from_running_stops_and_stopping_idempotent(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    """A running row becomes stopping; repeating returns the row."""
    setup = await _setup_dispatch(client, db_uri)
    payload = _create_payload(setup["session_id"])
    assert (await client.post("/v1/assignments", json=payload)).status_code == 201
    refs = {"refs": [{"repository_name": "root", "commit": "a" * 40}]}
    assert (
        await client.post(f"/v1/assignments/{payload['id']}/published", json=refs)
    ).status_code == 200
    run_session = _make_session(db_uri)
    _to_running(
        db_uri,
        payload["id"],
        host_id=_HOST_A,
        session_id=run_session,
        runner_id="runner_1",
    )
    resp = await client.post(f"/v1/assignments/{payload['id']}/cancel", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "stopping"
    assert resp.json()["cancel_requested_at"] is not None
    repeat = await client.post(f"/v1/assignments/{payload['id']}/cancel", json={})
    assert repeat.status_code == 200, repeat.text
    assert repeat.json()["state"] == "stopping"


async def test_cancel_from_interrupted_needs_stopped_execution(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    """An interrupted row cancels only after the old execution stopped."""
    setup = await _setup_dispatch(client, db_uri)
    payload = _create_payload(setup["session_id"])
    assert (await client.post("/v1/assignments", json=payload)).status_code == 201
    refs = {"refs": [{"repository_name": "root", "commit": "a" * 40}]}
    assert (
        await client.post(f"/v1/assignments/{payload['id']}/published", json=refs)
    ).status_code == 200
    _to_running(
        db_uri,
        payload["id"],
        host_id=_HOST_A,
        session_id=_make_session(db_uri),
        runner_id="runner_1",
    )
    _to_interrupted(db_uri, payload["id"], end_attempt=False)
    stuck = await client.post(f"/v1/assignments/{payload['id']}/cancel", json={})
    assert stuck.status_code == 409, stuck.text
    store = SqlAlchemyAssignmentStore(db_uri)
    row = store.get(payload["id"])
    assert row is not None and row.active_attempt_id is not None
    store.update_attempt(
        payload["id"], row.active_attempt_id, state="finished", ended_at=now_epoch()
    )
    freed = await client.post(f"/v1/assignments/{payload['id']}/cancel", json={})
    assert freed.status_code == 200, freed.text
    assert freed.json()["state"] == "cancelled"


async def test_cancel_terminal_409(client: httpx.AsyncClient, db_uri: str) -> None:
    """A succeeded row cannot be cancelled."""
    token, runner_id = _runner_pair()
    setup = await _setup_dispatch(client, db_uri)
    payload = _create_payload(setup["session_id"])
    assert (await client.post("/v1/assignments", json=payload)).status_code == 201
    refs = {"refs": [{"repository_name": "root", "commit": "a" * 40}]}
    assert (
        await client.post(f"/v1/assignments/{payload['id']}/published", json=refs)
    ).status_code == 200
    run_session = _make_session(db_uri, runner_id=runner_id)
    _to_running(
        db_uri, payload["id"], host_id=_HOST_A, session_id=run_session, runner_id=runner_id
    )
    complete = await client.post(
        f"/v1/assignments/{payload['id']}/complete",
        json={
            "session_id": run_session,
            "outputs": [{"repository_name": "root", "commit": "b" * 40}],
            "summary": "done",
        },
        headers=_runner_headers(token),
    )
    assert complete.status_code == 200, complete.text
    finish = await client.post(
        f"/v1/assignments/{payload['id']}/finish",
        json={
            "session_id": run_session,
            "refs": [{"repository_name": "root", "commit": "b" * 40}],
        },
        headers=_runner_headers(token),
    )
    assert finish.status_code == 200, finish.text
    assert finish.json()["state"] == "succeeded"
    refused = await client.post(f"/v1/assignments/{payload['id']}/cancel", json={})
    assert refused.status_code == 409, refused.text


# ── Retry ─────────────────────────────────────────────────────


async def test_retry_needs_stopped_execution_then_waits(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    """Retry 409s while the attempt runs, then returns the row to waiting."""
    setup = await _setup_dispatch(client, db_uri)
    payload = _create_payload(setup["session_id"])
    assert (await client.post("/v1/assignments", json=payload)).status_code == 201
    assert (await client.post(f"/v1/assignments/{payload['id']}/retry")).status_code == 409
    refs = {"refs": [{"repository_name": "root", "commit": "a" * 40}]}
    assert (
        await client.post(f"/v1/assignments/{payload['id']}/published", json=refs)
    ).status_code == 200
    _to_running(
        db_uri,
        payload["id"],
        host_id=_HOST_A,
        session_id=_make_session(db_uri),
        runner_id="runner_1",
    )
    _to_interrupted(db_uri, payload["id"], end_attempt=False)
    stuck = await client.post(f"/v1/assignments/{payload['id']}/retry")
    assert stuck.status_code == 409, stuck.text
    store = SqlAlchemyAssignmentStore(db_uri)
    row = store.get(payload["id"])
    assert row is not None and row.active_attempt_id is not None
    store.update_attempt(payload["id"], row.active_attempt_id, state="lost", ended_at=now_epoch())
    retried = await client.post(f"/v1/assignments/{payload['id']}/retry")
    assert retried.status_code == 200, retried.text
    body = retried.json()
    assert body["state"] == "waiting"
    assert body["active_attempt_id"] is None
    assert body["next_check_at"] is not None


async def test_retry_past_deadline_expires(client: httpx.AsyncClient, db_uri: str) -> None:
    """An interrupted row past its start deadline expires on retry."""
    setup = await _setup_dispatch(client, db_uri)
    payload = _create_payload(setup["session_id"], extra={"start_deadline": now_epoch() - 10})
    assert (await client.post("/v1/assignments", json=payload)).status_code == 201
    refs = {"refs": [{"repository_name": "root", "commit": "a" * 40}]}
    assert (
        await client.post(f"/v1/assignments/{payload['id']}/published", json=refs)
    ).status_code == 200
    _to_running(
        db_uri,
        payload["id"],
        host_id=_HOST_A,
        session_id=_make_session(db_uri),
        runner_id="runner_1",
    )
    _to_interrupted(db_uri, payload["id"], end_attempt=True)
    resp = await client.post(f"/v1/assignments/{payload['id']}/retry")
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "expired"


# ── Complete / finish ─────────────────────────────────────────


async def _to_publishing(
    client: httpx.AsyncClient,
    db_uri: str,
    assignment_id: str,
    run_session: str,
    token: str,
    commit: str = "b" * 40,
) -> dict[str, Any]:
    """Drive a running row through ``complete`` and return the body."""
    resp = await client.post(
        f"/v1/assignments/{assignment_id}/complete",
        json={
            "session_id": run_session,
            "outputs": [{"repository_name": "root", "commit": commit}],
            "summary": "done",
        },
        headers=_runner_headers(token),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_complete_and_finish_happy_path(client: httpx.AsyncClient, db_uri: str) -> None:
    """running → publishing → succeeded, with derived output refs."""
    token, runner_id = _runner_pair()
    setup = await _setup_dispatch(client, db_uri)
    payload = _create_payload(setup["session_id"])
    assert (await client.post("/v1/assignments", json=payload)).status_code == 201
    refs = {"refs": [{"repository_name": "root", "commit": "a" * 40}]}
    assert (
        await client.post(f"/v1/assignments/{payload['id']}/published", json=refs)
    ).status_code == 200
    run_session = _make_session(db_uri, runner_id=runner_id)
    attempt_id = _to_running(
        db_uri, payload["id"], host_id=_HOST_A, session_id=run_session, runner_id=runner_id
    )
    publishing = await _to_publishing(client, db_uri, payload["id"], run_session, token)
    assert publishing["state"] == "publishing"
    assert publishing["result_summary"] == "done"
    (output,) = publishing["outputs"]
    assert output["ref"] == (f"refs/omnigent/assignments/{payload['id']}/output/{attempt_id}/root")
    assert output["commit"] == "b" * 40
    repeat = await client.post(
        f"/v1/assignments/{payload['id']}/complete",
        json={
            "session_id": run_session,
            "outputs": [{"repository_name": "root", "commit": "b" * 40}],
            "summary": "done",
        },
        headers=_runner_headers(token),
    )
    assert repeat.status_code == 200, repeat.text
    assert repeat.json()["state"] == "publishing"
    finish = await client.post(
        f"/v1/assignments/{payload['id']}/finish",
        json={
            "session_id": run_session,
            "refs": [{"repository_name": "root", "commit": "b" * 40}],
        },
        headers=_runner_headers(token),
    )
    assert finish.status_code == 200, finish.text
    assert finish.json()["state"] == "succeeded"
    assert finish.json()["outputs"] == publishing["outputs"]
    store = SqlAlchemyAssignmentStore(db_uri)
    attempt = store.get_attempt(payload["id"], attempt_id)
    assert attempt is not None
    assert attempt.state == "finished"
    assert attempt.ended_at is not None


async def test_complete_rejects_bad_callers(client: httpx.AsyncClient, db_uri: str) -> None:
    """Unknown sessions, missing tokens and foreign tokens all 403."""
    token, runner_id = _runner_pair()
    setup = await _setup_dispatch(client, db_uri)
    payload = _create_payload(setup["session_id"])
    assert (await client.post("/v1/assignments", json=payload)).status_code == 201
    refs = {"refs": [{"repository_name": "root", "commit": "a" * 40}]}
    assert (
        await client.post(f"/v1/assignments/{payload['id']}/published", json=refs)
    ).status_code == 200
    run_session = _make_session(db_uri, runner_id=runner_id)
    _to_running(
        db_uri, payload["id"], host_id=_HOST_A, session_id=run_session, runner_id=runner_id
    )
    good = {
        "session_id": run_session,
        "outputs": [{"repository_name": "root", "commit": "b" * 40}],
        "summary": "done",
    }
    ghost = await client.post(
        f"/v1/assignments/{payload['id']}/complete",
        json={**good, "session_id": "0" * 32},
        headers=_runner_headers(token),
    )
    assert ghost.status_code == 403, ghost.text
    missing = await client.post(f"/v1/assignments/{payload['id']}/complete", json=good)
    assert missing.status_code == 403, missing.text
    foreign_token, _ = _runner_pair()
    foreign = await client.post(
        f"/v1/assignments/{payload['id']}/complete",
        json=good,
        headers=_runner_headers(foreign_token),
    )
    assert foreign.status_code == 403, foreign.text


async def test_complete_validates_outputs(client: httpx.AsyncClient, db_uri: str) -> None:
    """Unknown repositories and doubled outputs are refused; wrong states 409."""
    token, runner_id = _runner_pair()
    setup = await _setup_dispatch(client, db_uri)
    payload = _create_payload(setup["session_id"])
    assert (await client.post("/v1/assignments", json=payload)).status_code == 201
    refs = {"refs": [{"repository_name": "root", "commit": "a" * 40}]}
    assert (
        await client.post(f"/v1/assignments/{payload['id']}/published", json=refs)
    ).status_code == 200
    run_session = _make_session(db_uri, runner_id=runner_id)
    _to_running(
        db_uri, payload["id"], host_id=_HOST_A, session_id=run_session, runner_id=runner_id
    )
    headers = _runner_headers(token)
    unknown = await client.post(
        f"/v1/assignments/{payload['id']}/complete",
        json={
            "session_id": run_session,
            "outputs": [{"repository_name": "ghost", "commit": "b" * 40}],
            "summary": "done",
        },
        headers=headers,
    )
    assert unknown.status_code == 400, unknown.text
    doubled = await client.post(
        f"/v1/assignments/{payload['id']}/complete",
        json={
            "session_id": run_session,
            "outputs": [
                {"repository_name": "root", "commit": "b" * 40},
                {"repository_name": "root", "commit": "c" * 40},
            ],
            "summary": "done",
        },
        headers=headers,
    )
    assert doubled.status_code == 400, doubled.text
    early_payload = _create_payload(setup["session_id"])
    assert (await client.post("/v1/assignments", json=early_payload)).status_code == 201
    early_session = _make_session(db_uri, runner_id=runner_id)
    early = await client.post(
        f"/v1/assignments/{early_payload['id']}/complete",
        json={
            "session_id": early_session,
            "outputs": [{"repository_name": "root", "commit": "b" * 40}],
            "summary": "done",
        },
        headers=headers,
    )
    assert early.status_code == 409, early.text


async def test_finish_mismatched_commit_fails(client: httpx.AsyncClient, db_uri: str) -> None:
    """An output observed at the wrong commit fails the publication."""
    token, runner_id = _runner_pair()
    setup = await _setup_dispatch(client, db_uri)
    payload = _create_payload(setup["session_id"])
    assert (await client.post("/v1/assignments", json=payload)).status_code == 201
    refs = {"refs": [{"repository_name": "root", "commit": "a" * 40}]}
    assert (
        await client.post(f"/v1/assignments/{payload['id']}/published", json=refs)
    ).status_code == 200
    run_session = _make_session(db_uri, runner_id=runner_id)
    _to_running(
        db_uri, payload["id"], host_id=_HOST_A, session_id=run_session, runner_id=runner_id
    )
    await _to_publishing(client, db_uri, payload["id"], run_session, token)
    mismatch = await client.post(
        f"/v1/assignments/{payload['id']}/finish",
        json={
            "session_id": run_session,
            "refs": [{"repository_name": "root", "commit": "c" * 40}],
        },
        headers=_runner_headers(token),
    )
    assert mismatch.status_code == 200, mismatch.text
    assert mismatch.json()["state"] == "failed"
    assert mismatch.json()["error_code"] == "publication_failed"


async def test_finish_with_error_fails(client: httpx.AsyncClient, db_uri: str) -> None:
    """A reported error fails even when every ref matches."""
    token, runner_id = _runner_pair()
    setup = await _setup_dispatch(client, db_uri)
    payload = _create_payload(setup["session_id"])
    assert (await client.post("/v1/assignments", json=payload)).status_code == 201
    refs = {"refs": [{"repository_name": "root", "commit": "a" * 40}]}
    assert (
        await client.post(f"/v1/assignments/{payload['id']}/published", json=refs)
    ).status_code == 200
    run_session = _make_session(db_uri, runner_id=runner_id)
    _to_running(
        db_uri, payload["id"], host_id=_HOST_A, session_id=run_session, runner_id=runner_id
    )
    await _to_publishing(client, db_uri, payload["id"], run_session, token)
    failed = await client.post(
        f"/v1/assignments/{payload['id']}/finish",
        json={
            "session_id": run_session,
            "refs": [{"repository_name": "root", "commit": "b" * 40}],
            "error": "push rejected",
        },
        headers=_runner_headers(token),
    )
    assert failed.status_code == 200, failed.text
    assert failed.json()["state"] == "failed"
    assert failed.json()["error_code"] == "publication_failed"


async def test_late_finish_from_lost_attempt_409_row_untouched(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    """A former attempt's finish 409s and changes nothing."""
    old_token, old_runner = _runner_pair()
    new_token, new_runner = _runner_pair()
    setup = await _setup_dispatch(client, db_uri)
    payload = _create_payload(setup["session_id"])
    assert (await client.post("/v1/assignments", json=payload)).status_code == 201
    refs = {"refs": [{"repository_name": "root", "commit": "a" * 40}]}
    assert (
        await client.post(f"/v1/assignments/{payload['id']}/published", json=refs)
    ).status_code == 200
    old_session = _make_session(db_uri, runner_id=old_runner)
    _to_running(
        db_uri,
        payload["id"],
        host_id=_HOST_A,
        session_id=old_session,
        runner_id=old_runner,
    )
    _to_interrupted(db_uri, payload["id"], end_attempt=True)
    retried = await client.post(f"/v1/assignments/{payload['id']}/retry")
    assert retried.status_code == 200, retried.text
    new_session = _make_session(db_uri, runner_id=new_runner)
    _to_running(
        db_uri,
        payload["id"],
        host_id=_HOST_A,
        session_id=new_session,
        runner_id=new_runner,
    )
    await _to_publishing(client, db_uri, payload["id"], new_session, new_token)
    late = await client.post(
        f"/v1/assignments/{payload['id']}/finish",
        json={
            "session_id": old_session,
            "refs": [{"repository_name": "root", "commit": "b" * 40}],
        },
        headers=_runner_headers(old_token),
    )
    assert late.status_code == 409, late.text
    row = SqlAlchemyAssignmentStore(db_uri).get(payload["id"])
    assert row is not None
    assert row.state == "publishing"
    assert row.outputs is not None
    assert [(o.repository_name, o.commit) for o in row.outputs] == [("root", "b" * 40)]


# ── Switch off mid-flight ─────────────────────────────────────


async def test_switch_off_mid_run_keeps_finish_open_blocks_create(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    """In-flight work still completes; only new dispatch is refused."""
    token, runner_id = _runner_pair()
    setup = await _setup_dispatch(client, db_uri)
    payload = _create_payload(setup["session_id"])
    assert (await client.post("/v1/assignments", json=payload)).status_code == 201
    refs = {"refs": [{"repository_name": "root", "commit": "a" * 40}]}
    assert (
        await client.post(f"/v1/assignments/{payload['id']}/published", json=refs)
    ).status_code == 200
    run_session = _make_session(db_uri, runner_id=runner_id)
    _to_running(
        db_uri, payload["id"], host_id=_HOST_A, session_id=run_session, runner_id=runner_id
    )
    config = (await client.get(f"/v1/projects/{setup['project_id']}/collaboration")).json()
    off = await client.patch(
        f"/v1/projects/{setup['project_id']}/collaboration",
        json={"enabled": False, "expected_revision": config["revision"]},
    )
    assert off.status_code == 200, off.text
    await _to_publishing(client, db_uri, payload["id"], run_session, token)
    finish = await client.post(
        f"/v1/assignments/{payload['id']}/finish",
        json={
            "session_id": run_session,
            "refs": [{"repository_name": "root", "commit": "b" * 40}],
        },
        headers=_runner_headers(token),
    )
    assert finish.status_code == 200, finish.text
    assert finish.json()["state"] == "succeeded"
    refused = await client.post("/v1/assignments", json=_create_payload(setup["session_id"]))
    assert refused.status_code == 409, refused.text


# ── Round-1 fixes ───────────────────────────────────────────────


async def test_old_finish_after_retry_into_new_publishing_409(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    """An old attempt's finish after a retry into new publishing 409s untouched."""
    old_token, old_runner = _runner_pair()
    new_token, new_runner = _runner_pair()
    setup = await _setup_dispatch(client, db_uri)
    payload = _create_payload(setup["session_id"])
    assert (await client.post("/v1/assignments", json=payload)).status_code == 201
    refs = {"refs": [{"repository_name": "root", "commit": "a" * 40}]}
    assert (
        await client.post(f"/v1/assignments/{payload['id']}/published", json=refs)
    ).status_code == 200
    old_session = _make_session(db_uri, runner_id=old_runner)
    old_attempt = _to_running(
        db_uri, payload["id"], host_id=_HOST_A, session_id=old_session, runner_id=old_runner
    )
    await _to_publishing(client, db_uri, payload["id"], old_session, old_token, commit="b" * 40)
    store = SqlAlchemyAssignmentStore(db_uri)
    assert (
        store.transition(payload["id"], from_state="publishing", to_state="interrupted")
        is not None
    )
    assert (
        store.update_attempt(payload["id"], old_attempt, state="finished", ended_at=now_epoch())
        is not None
    )
    retried = await client.post(f"/v1/assignments/{payload['id']}/retry")
    assert retried.status_code == 200, retried.text
    new_session = _make_session(db_uri, runner_id=new_runner)
    _to_running(
        db_uri, payload["id"], host_id=_HOST_A, session_id=new_session, runner_id=new_runner
    )
    await _to_publishing(client, db_uri, payload["id"], new_session, new_token, commit="c" * 40)
    before = store.get_attempt(payload["id"], old_attempt)
    assert before is not None
    late = await client.post(
        f"/v1/assignments/{payload['id']}/finish",
        json={
            "session_id": old_session,
            "refs": [{"repository_name": "root", "commit": "b" * 40}],
        },
        headers=_runner_headers(old_token),
    )
    assert late.status_code == 409, late.text
    row = store.get(payload["id"])
    assert row is not None
    assert row.state == "publishing"
    assert [(o.repository_name, o.commit) for o in row.outputs or []] == [("root", "c" * 40)]
    assert store.get_attempt(payload["id"], old_attempt) == before


async def test_old_complete_after_retry_into_new_running_409(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    """An old attempt's complete after a retry into new running 409s untouched."""
    old_token, old_runner = _runner_pair()
    new_token, new_runner = _runner_pair()
    setup = await _setup_dispatch(client, db_uri)
    payload = _create_payload(setup["session_id"])
    assert (await client.post("/v1/assignments", json=payload)).status_code == 201
    refs = {"refs": [{"repository_name": "root", "commit": "a" * 40}]}
    assert (
        await client.post(f"/v1/assignments/{payload['id']}/published", json=refs)
    ).status_code == 200
    old_session = _make_session(db_uri, runner_id=old_runner)
    old_attempt = _to_running(
        db_uri, payload["id"], host_id=_HOST_A, session_id=old_session, runner_id=old_runner
    )
    store = SqlAlchemyAssignmentStore(db_uri)
    assert (
        store.transition(payload["id"], from_state="running", to_state="interrupted") is not None
    )
    assert (
        store.update_attempt(payload["id"], old_attempt, state="lost", ended_at=now_epoch())
        is not None
    )
    assert (await client.post(f"/v1/assignments/{payload['id']}/retry")).status_code == 200
    new_session = _make_session(db_uri, runner_id=new_runner)
    new_attempt = _to_running(
        db_uri, payload["id"], host_id=_HOST_A, session_id=new_session, runner_id=new_runner
    )
    assert new_token is not None
    before = store.get_attempt(payload["id"], old_attempt)
    assert before is not None
    late = await client.post(
        f"/v1/assignments/{payload['id']}/complete",
        json={
            "session_id": old_session,
            "outputs": [{"repository_name": "root", "commit": "b" * 40}],
            "summary": "old",
        },
        headers=_runner_headers(old_token),
    )
    assert late.status_code == 409, late.text
    row = store.get(payload["id"])
    assert row is not None
    assert row.state == "running"
    assert row.active_attempt_id == new_attempt
    assert store.get_attempt(payload["id"], old_attempt) == before


async def test_stale_refresh_409_newer_inputs_survive(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    """A refresh computed from a stale read 409s and keeps the newer inputs."""
    setup = await _setup_dispatch(client, db_uri)
    payload = _create_payload(setup["session_id"])
    assert (await client.post("/v1/assignments", json=payload)).status_code == 201
    refs = {"refs": [{"repository_name": "root", "commit": "a" * 40}]}
    assert (
        await client.post(f"/v1/assignments/{payload['id']}/published", json=refs)
    ).status_code == 200
    store = SqlAlchemyAssignmentStore(db_uri)
    row = store.get(payload["id"])
    assert row is not None
    stale_blob = inputs_to_json(row.inputs)
    newer_inputs = [
        dataclasses.replace(entry, repository_revision=entry.repository_revision + 1)
        for entry in row.inputs
    ]
    landed = store.refresh_waiting(
        payload["id"],
        inputs=newer_inputs,
        project_revision=row.project_revision,
        now=now_epoch(),
        expected_inputs_json=stale_blob,
    )
    assert landed is not None
    stale = store.refresh_waiting(
        payload["id"],
        inputs=row.inputs,
        project_revision=row.project_revision,
        now=now_epoch(),
        expected_inputs_json=stale_blob,
    )
    assert stale is None
    current = store.get(payload["id"])
    assert current is not None
    assert current.inputs[0].repository_revision == row.inputs[0].repository_revision + 1


async def test_complete_repeat_from_unrelated_session_rejected(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    """An identical complete repeat from another session's runner is not 200."""
    token, runner_id = _runner_pair()
    setup = await _setup_dispatch(client, db_uri)
    payload = _create_payload(setup["session_id"])
    assert (await client.post("/v1/assignments", json=payload)).status_code == 201
    refs = {"refs": [{"repository_name": "root", "commit": "a" * 40}]}
    assert (
        await client.post(f"/v1/assignments/{payload['id']}/published", json=refs)
    ).status_code == 200
    run_session = _make_session(db_uri, runner_id=runner_id)
    _to_running(
        db_uri, payload["id"], host_id=_HOST_A, session_id=run_session, runner_id=runner_id
    )
    await _to_publishing(client, db_uri, payload["id"], run_session, token)
    other_token, other_runner = _runner_pair()
    other_session = _make_session(db_uri, runner_id=other_runner)
    repeat = await client.post(
        f"/v1/assignments/{payload['id']}/complete",
        json={
            "session_id": other_session,
            "outputs": [{"repository_name": "root", "commit": "b" * 40}],
            "summary": "done",
        },
        headers=_runner_headers(other_token),
    )
    assert repeat.status_code in (403, 409), repeat.text


async def test_complete_runner_mismatch_rejected(client: httpx.AsyncClient, db_uri: str) -> None:
    """A calling conversation on another runner than the attempt is rejected."""
    token_b, runner_b = _runner_pair()
    _, runner_a = _runner_pair()
    setup = await _setup_dispatch(client, db_uri)
    payload = _create_payload(setup["session_id"])
    assert (await client.post("/v1/assignments", json=payload)).status_code == 201
    refs = {"refs": [{"repository_name": "root", "commit": "a" * 40}]}
    assert (
        await client.post(f"/v1/assignments/{payload['id']}/published", json=refs)
    ).status_code == 200
    run_session = _make_session(db_uri, runner_id=runner_b)
    _to_running(db_uri, payload["id"], host_id=_HOST_A, session_id=run_session, runner_id=runner_a)
    resp = await client.post(
        f"/v1/assignments/{payload['id']}/complete",
        json={
            "session_id": run_session,
            "outputs": [{"repository_name": "root", "commit": "b" * 40}],
            "summary": "done",
        },
        headers=_runner_headers(token_b),
    )
    assert resp.status_code in (403, 409), resp.text
    row = SqlAlchemyAssignmentStore(db_uri).get(payload["id"])
    assert row is not None
    assert row.state == "running"


async def test_complete_and_finish_reject_runnerless_attempt(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    """An attempt with no recorded runner fails closed on complete/finish."""
    token, runner_id = _runner_pair()
    setup = await _setup_dispatch(client, db_uri)
    payload = _create_payload(setup["session_id"])
    assert (await client.post("/v1/assignments", json=payload)).status_code == 201
    refs = {"refs": [{"repository_name": "root", "commit": "a" * 40}]}
    assert (
        await client.post(f"/v1/assignments/{payload['id']}/published", json=refs)
    ).status_code == 200
    run_session = _make_session(db_uri, runner_id=runner_id)
    store = SqlAlchemyAssignmentStore(db_uri)
    attempt = store.claim_attempt(payload["id"], host_id=_HOST_A, now=now_epoch())
    assert attempt is not None
    assert store.update_attempt(payload["id"], attempt.id, session_id=run_session) is not None
    assert store.transition(payload["id"], from_state="starting", to_state="running") is not None
    complete = await client.post(
        f"/v1/assignments/{payload['id']}/complete",
        json={
            "session_id": run_session,
            "outputs": [{"repository_name": "root", "commit": "b" * 40}],
            "summary": "done",
        },
        headers=_runner_headers(token),
    )
    assert complete.status_code == 409, complete.text
    row = store.get(payload["id"])
    assert row is not None
    assert row.state == "running"
    assert store.transition(payload["id"], from_state="running", to_state="publishing") is not None
    finish = await client.post(
        f"/v1/assignments/{payload['id']}/finish",
        json={
            "session_id": run_session,
            "refs": [{"repository_name": "root", "commit": "b" * 40}],
        },
        headers=_runner_headers(token),
    )
    assert finish.status_code == 409, finish.text
    row = store.get(payload["id"])
    assert row is not None
    assert row.state == "publishing"


async def test_complete_bad_commit_400_row_stays_running(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    """A non-hex output commit is a 400 and leaves the row running."""
    token, runner_id = _runner_pair()
    setup = await _setup_dispatch(client, db_uri)
    payload = _create_payload(setup["session_id"])
    assert (await client.post("/v1/assignments", json=payload)).status_code == 201
    refs = {"refs": [{"repository_name": "root", "commit": "a" * 40}]}
    assert (
        await client.post(f"/v1/assignments/{payload['id']}/published", json=refs)
    ).status_code == 200
    run_session = _make_session(db_uri, runner_id=runner_id)
    _to_running(
        db_uri, payload["id"], host_id=_HOST_A, session_id=run_session, runner_id=runner_id
    )
    bad = await client.post(
        f"/v1/assignments/{payload['id']}/complete",
        json={
            "session_id": run_session,
            "outputs": [{"repository_name": "root", "commit": "not-a-commit"}],
            "summary": "done",
        },
        headers=_runner_headers(token),
    )
    assert bad.status_code == 400, bad.text
    row = SqlAlchemyAssignmentStore(db_uri).get(payload["id"])
    assert row is not None
    assert row.state == "running"


async def test_published_empty_refs_fails(client: httpx.AsyncClient, db_uri: str) -> None:
    """An empty publication records a failed row with no observed commits."""
    setup = await _setup_dispatch(client, db_uri)
    payload = _create_payload(setup["session_id"])
    assert (await client.post("/v1/assignments", json=payload)).status_code == 201
    resp = await client.post(f"/v1/assignments/{payload['id']}/published", json={"refs": []})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["state"] == "failed"
    assert body["error_code"] == "publication_failed"
    assert all(entry["observed_commit"] is None for entry in body["inputs"])


async def test_finish_empty_refs_with_error_fails(client: httpx.AsyncClient, db_uri: str) -> None:
    """An empty finish with an error fails the publication and ends the attempt."""
    token, runner_id = _runner_pair()
    setup = await _setup_dispatch(client, db_uri)
    payload = _create_payload(setup["session_id"])
    assert (await client.post("/v1/assignments", json=payload)).status_code == 201
    refs = {"refs": [{"repository_name": "root", "commit": "a" * 40}]}
    assert (
        await client.post(f"/v1/assignments/{payload['id']}/published", json=refs)
    ).status_code == 200
    run_session = _make_session(db_uri, runner_id=runner_id)
    attempt_id = _to_running(
        db_uri, payload["id"], host_id=_HOST_A, session_id=run_session, runner_id=runner_id
    )
    await _to_publishing(client, db_uri, payload["id"], run_session, token)
    failed = await client.post(
        f"/v1/assignments/{payload['id']}/finish",
        json={"session_id": run_session, "refs": [], "error": "push rejected"},
        headers=_runner_headers(token),
    )
    assert failed.status_code == 200, failed.text
    assert failed.json()["state"] == "failed"
    assert failed.json()["error_code"] == "publication_failed"
    attempt = SqlAlchemyAssignmentStore(db_uri).get_attempt(payload["id"], attempt_id)
    assert attempt is not None
    assert attempt.state == "finished"
    assert attempt.ended_at is not None


async def test_published_repeat_after_failed_partial_200(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    """The identical partial publication after failed returns the row unchanged."""
    setup = await _setup_dispatch(client, db_uri)
    await _register_repo(client, setup["project_id"], name="docs")
    payload = _create_payload(
        setup["session_id"],
        repositories=_repos("root", "docs"),
        extra={"execution_root": "root"},
    )
    assert (await client.post("/v1/assignments", json=payload)).status_code == 201
    partial = {"refs": [{"repository_name": "root", "commit": "a" * 40}]}
    first = await client.post(f"/v1/assignments/{payload['id']}/published", json=partial)
    assert first.status_code == 200, first.text
    assert first.json()["state"] == "failed"
    repeat = await client.post(f"/v1/assignments/{payload['id']}/published", json=partial)
    assert repeat.status_code == 200, repeat.text
    assert repeat.json() == first.json()
    changed = await client.post(
        f"/v1/assignments/{payload['id']}/published",
        json={
            "refs": [
                {"repository_name": "root", "commit": "a" * 40},
                {"repository_name": "docs", "commit": "a" * 40},
            ]
        },
    )
    assert changed.status_code == 409, changed.text


async def test_published_repeat_after_output_publication_failure_200(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    """After a failed finish, the original input publication still replays 200."""
    token, runner_id = _runner_pair()
    setup = await _setup_dispatch(client, db_uri)
    payload = _create_payload(setup["session_id"])
    assert (await client.post("/v1/assignments", json=payload)).status_code == 201
    full = {"refs": [{"repository_name": "root", "commit": "a" * 40}]}
    assert (
        await client.post(f"/v1/assignments/{payload['id']}/published", json=full)
    ).status_code == 200
    run_session = _make_session(db_uri, runner_id=runner_id)
    _to_running(
        db_uri, payload["id"], host_id=_HOST_A, session_id=run_session, runner_id=runner_id
    )
    await _to_publishing(client, db_uri, payload["id"], run_session, token)
    failed = await client.post(
        f"/v1/assignments/{payload['id']}/finish",
        json={"session_id": run_session, "refs": [], "error": "push rejected"},
        headers=_runner_headers(token),
    )
    assert failed.status_code == 200, failed.text
    assert failed.json()["state"] == "failed"
    assert failed.json()["error_code"] == "publication_failed"
    repeat = await client.post(f"/v1/assignments/{payload['id']}/published", json=full)
    assert repeat.status_code == 200, repeat.text
    assert repeat.json() == failed.json()
    changed = await client.post(f"/v1/assignments/{payload['id']}/published", json={"refs": []})
    assert changed.status_code == 409, changed.text


async def test_published_repeat_after_starting_200(client: httpx.AsyncClient, db_uri: str) -> None:
    """The identical success publication after claim returns the current row."""
    setup = await _setup_dispatch(client, db_uri)
    payload = _create_payload(setup["session_id"])
    assert (await client.post("/v1/assignments", json=payload)).status_code == 201
    full = {"refs": [{"repository_name": "root", "commit": "a" * 40}]}
    assert (
        await client.post(f"/v1/assignments/{payload['id']}/published", json=full)
    ).status_code == 200
    store = SqlAlchemyAssignmentStore(db_uri)
    assert store.claim_attempt(payload["id"], host_id=_HOST_A, now=now_epoch()) is not None
    repeat = await client.post(f"/v1/assignments/{payload['id']}/published", json=full)
    assert repeat.status_code == 200, repeat.text
    assert repeat.json()["state"] == "starting"
    changed = await client.post(
        f"/v1/assignments/{payload['id']}/published",
        json={"refs": [{"repository_name": "root", "commit": "b" * 40}]},
    )
    assert changed.status_code == 409, changed.text


async def test_retry_and_cancel_without_attempt_record_409(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    """Interrupted with no attempt record fails closed on retry and cancel."""
    setup = await _setup_dispatch(client, db_uri)
    payload = _create_payload(setup["session_id"])
    assert (await client.post("/v1/assignments", json=payload)).status_code == 201
    refs = {"refs": [{"repository_name": "root", "commit": "a" * 40}]}
    assert (
        await client.post(f"/v1/assignments/{payload['id']}/published", json=refs)
    ).status_code == 200
    _to_running(
        db_uri,
        payload["id"],
        host_id=_HOST_A,
        session_id=_make_session(db_uri),
        runner_id="runner_1",
    )
    store = SqlAlchemyAssignmentStore(db_uri)
    assert (
        store.transition(
            payload["id"],
            from_state="running",
            to_state="interrupted",
            active_attempt_id=None,
        )
        is not None
    )
    row = store.get(payload["id"])
    assert row is not None
    assert row.state == "interrupted"
    assert row.active_attempt_id is None
    retry = await client.post(f"/v1/assignments/{payload['id']}/retry")
    assert retry.status_code == 409, retry.text
    assert "no attempt record" in retry.json()["error"]["message"]
    cancel = await client.post(f"/v1/assignments/{payload['id']}/cancel", json={})
    assert cancel.status_code == 409, cancel.text
    assert "no attempt record" in cancel.json()["error"]["message"]
