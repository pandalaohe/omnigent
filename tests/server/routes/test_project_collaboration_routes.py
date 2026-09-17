"""Tests for the project-collaboration config routes.

The collaboration router is mounted only when ``create_app`` receives the
project store plus both config stores, and every route 404s unless
``Feature.PROJECT_ASSIGNMENTS`` is enabled. Binding PUT/verify validate
the path live on the host over the tunnel, so those tests connect a fake
host that answers ``host.stat`` frames (the ``test_hosts_worktrees.py``
pattern) against the full app.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from asgiref.testing import ApplicationCommunicator
from fastapi import FastAPI

from omnigent.host.frames import (
    HostHelloFrame,
    HostPostBindHookFrame,
    HostPostBindHookResultFrame,
    HostStatFrame,
    HostStatResultFrame,
    decode_host_frame,
    encode_host_frame,
)
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.feature_flags import resolve_feature_flags
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.project_host_binding_store.sqlalchemy_store import (
    SqlAlchemyProjectHostBindingStore,
)
from omnigent.stores.project_repository_store.sqlalchemy_store import (
    SqlAlchemyProjectRepositoryStore,
)
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore

ALICE = "alice@example.com"
BOB = "bob@example.com"

_HOST_A = "a1b2c3d4e5f60718293a4b5c6d7e8f01"
_HOST_B = "b1b2c3d4e5f60718293a4b5c6d7e8f02"
_HOST_OFFLINE = "c1b2c3d4e5f60718293a4b5c6d7e8f03"
_HOST_HOOKED = "d1b2c3d4e5f60718293a4b5c6d7e8f04"


def _as_user(user: str) -> dict[str, str]:
    """Header identifying the requesting user under header auth."""
    return {"X-Forwarded-Email": user}


def _websocket_scope(path: str) -> dict[str, object]:
    """Build a minimal ASGI WebSocket scope."""
    return {
        "type": "websocket",
        "asgi": {"version": "3.0"},
        "scheme": "ws",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "subprotocols": [],
    }


def _hello_text(name: str, *, post_bind_hook: bool = False) -> str:
    """Encode a hello frame for tests."""
    return encode_host_frame(
        HostHelloFrame(
            version="0.1.0-test",
            frame_protocol_version=1,
            name=name,
            post_bind_hook=post_bind_hook,
        )
    )


def _build_app(
    db_uri: str,
    tmp_path: Path,
    *,
    enabled: bool = True,
    auth: bool = False,
) -> FastAPI:
    """Build a FastAPI app with the collaboration stores wired."""
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
        auth_provider=UnifiedAuthProvider(source="header") if auth else None,
        feature_flags=flags,
    )


@pytest.fixture()
def collab_app(db_uri: str, tmp_path: Path) -> FastAPI:
    """App with the collaboration surface enabled (single-user)."""
    return _build_app(db_uri, tmp_path)


@pytest_asyncio.fixture()
async def collab_client(collab_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """HTTP client wired to the collaboration-enabled app."""
    transport = httpx.ASGITransport(app=collab_app)
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


async def _make_project(
    client: httpx.AsyncClient,
    name: str | None = None,
    headers: dict[str, str] | None = None,
) -> str:
    """Create a project and return its id.

    Names are unique per call so a flaky-marker rerun that reuses the
    test's database cannot collide with its own earlier attempt.
    """
    unique = name or f"Work-{uuid.uuid4().hex[:8]}"
    resp = await client.post("/v1/projects", json={"name": unique}, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def _connect_fake_host(
    app: FastAPI, host_id: str, name: str, *, post_bind_hook: bool = False
) -> ApplicationCommunicator:
    """Open a tunnel and complete the hello handshake."""
    comm = ApplicationCommunicator(app, _websocket_scope(f"/v1/hosts/{host_id}/tunnel"))
    await comm.send_input({"type": "websocket.connect"})
    accepted = await comm.receive_output(timeout=5.0)
    assert accepted["type"] == "websocket.accept"
    await comm.send_input(
        {"type": "websocket.receive", "text": _hello_text(name, post_bind_hook=post_bind_hook)}
    )
    registry = app.state.host_registry
    for _ in range(500):
        if registry.get(host_id) is not None:
            break
        await asyncio.sleep(0.01)
    assert registry.get(host_id) is not None
    return comm


def _start_stat_drain(
    comm: ApplicationCommunicator,
    replies: dict[str, dict[str, Any]],
    hooks: dict[str, Any] | None = None,
) -> asyncio.Task[None]:
    """Answer outbound ``host.stat`` frames from the registered replies.

    When *hooks* is given, ``host.post_bind_hook`` frames are also answered
    from ``hooks["replies"][binding_name]`` (default ``status="ok"``) and
    appended to ``hooks["seen"]``; ``hooks["drop"] = True`` leaves them
    unanswered so a test can exercise the server's wait.

    The receive timeout is deliberately long: asgiref cancels the served
    application task when a ``receive_output`` timeout fires, so a short
    timeout would kill the tunnel in any gap between test steps. Teardown
    cancels the drain task instead of waiting out the timeout.
    """

    async def _drain() -> None:
        while True:
            try:
                output = await comm.receive_output(timeout=30.0)
            except asyncio.TimeoutError:
                continue
            if output.get("type") != "websocket.send":
                continue
            text = output.get("text")
            if not isinstance(text, str):
                continue
            frame = decode_host_frame(text)
            if isinstance(frame, HostPostBindHookFrame):
                if hooks is None:
                    continue
                hooks["seen"].append(frame)
                if hooks["drop"]:
                    continue
                reply = hooks["replies"].get(frame.binding_name, {"status": "ok", "exit_code": 0})
                result = HostPostBindHookResultFrame(request_id=frame.request_id, **reply)
                await comm.send_input(
                    {"type": "websocket.receive", "text": encode_host_frame(result)}
                )
                continue
            if not isinstance(frame, HostStatFrame):
                continue
            reply = replies.get(frame.path)
            if reply is None:
                result = HostStatResultFrame(
                    request_id=frame.request_id, status="ok", exists=False
                )
            else:
                result = HostStatResultFrame(request_id=frame.request_id, **reply)
            await comm.send_input({"type": "websocket.receive", "text": encode_host_frame(result)})

    return asyncio.create_task(_drain())


async def _stop_fake_host(comm: ApplicationCommunicator, drain_task: asyncio.Task[None]) -> None:
    """Stop the drain and disconnect the fake host.

    Send an explicit disconnect so the tunnel endpoint's finally-block
    calls ``host_store.set_offline()`` and ``registry.deregister()``
    before the fixture returns. Without this, those calls happen whenever
    the comm is GC'd — potentially during the next test's setup window.
    Swallow CancelledError: the asgiref communicator may already be done
    if the event loop cancelled its internal future during teardown.
    """
    drain_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await drain_task
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await comm.send_input({"type": "websocket.disconnect", "code": 1000})


@pytest_asyncio.fixture()
async def live_host(
    collab_app: FastAPI,
) -> AsyncIterator[dict[str, Any]]:
    """Connect a fake host answering ``host.stat`` and yield its controls.

    Tests register stat replies as ``replies[typed_path]`` dicts with the
    ``HostStatResultFrame`` fields (``status``/``exists``/``type``/
    ``canonical_path``). An unregistered path stats as missing.
    """
    comm = await _connect_fake_host(collab_app, _HOST_A, "fake-a")
    replies: dict[str, dict[str, Any]] = {}
    drain_task = _start_stat_drain(comm, replies)
    try:
        yield {"host_id": _HOST_A, "replies": replies, "app": collab_app}
    finally:
        await _stop_fake_host(comm, drain_task)


@pytest_asyncio.fixture()
async def hooked_host(
    collab_app: FastAPI,
) -> AsyncIterator[dict[str, Any]]:
    """Connect a fake host that advertises and answers ``host.post_bind_hook``.

    Tests register per-binding hook results as
    ``hooks["replies"][binding_name]`` dicts with the
    ``HostPostBindHookResultFrame`` fields; a missing entry answers
    ``status="ok"``. Every received frame is appended to ``hooks["seen"]``,
    and ``hooks["drop"] = True`` leaves hook frames unanswered.
    """
    comm = await _connect_fake_host(collab_app, _HOST_HOOKED, "fake-hooked", post_bind_hook=True)
    replies: dict[str, dict[str, Any]] = {}
    hooks: dict[str, Any] = {"replies": {}, "seen": [], "drop": False}
    drain_task = _start_stat_drain(comm, replies, hooks=hooks)
    try:
        yield {
            "host_id": _HOST_HOOKED,
            "replies": replies,
            "hooks": hooks,
            "app": collab_app,
        }
    finally:
        await _stop_fake_host(comm, drain_task)


async def _register_repo(
    client: httpx.AsyncClient,
    project_id: str,
    name: str = "root",
    remote_url: str = "https://example.com/org/repo.git",
) -> dict[str, Any]:
    """Register a repository via the route and return its body."""
    resp = await client.put(
        f"/v1/projects/{project_id}/repositories/{name}",
        json={"remote_url": remote_url, "default_branch": "main"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ── Flag gate ─────────────────────────────────────────────


async def test_flag_disabled_every_route_404(
    disabled_client: httpx.AsyncClient,
) -> None:
    """With the flag off, the whole configuration surface is dark."""
    pid, hid = "0" * 32, "1" * 32
    routes = [
        ("GET", f"/v1/projects/{pid}/collaboration", None),
        ("PATCH", f"/v1/projects/{pid}/collaboration", {"enabled": True, "expected_revision": 0}),
        (
            "PUT",
            f"/v1/projects/{pid}/repositories/root",
            {"remote_url": "https://example.com/r.git", "default_branch": "main"},
        ),
        ("DELETE", f"/v1/projects/{pid}/repositories/root", None),
        (
            "PUT",
            f"/v1/projects/{pid}/hosts/{hid}/bindings/primary",
            {"workspace": "/tmp/x", "repository_name": "root"},
        ),
        ("DELETE", f"/v1/projects/{pid}/hosts/{hid}/bindings/primary", None),
        ("POST", f"/v1/projects/{pid}/hosts/{hid}/bindings/primary/verify", None),
    ]
    for method, path, body in routes:
        resp = await disabled_client.request(method, path, json=body)
        assert resp.status_code == 404, f"{method} {path}: {resp.status_code}"


async def test_flag_disabled_malformed_body_still_404(
    disabled_client: httpx.AsyncClient,
) -> None:
    """With the flag off, the gate runs before body validation rejects."""
    pid, hid = "0" * 32, "1" * 32
    resp = await disabled_client.patch(f"/v1/projects/{pid}/collaboration", json={})
    assert resp.status_code == 404, resp.text
    resp = await disabled_client.put(f"/v1/projects/{pid}/repositories/root", json={})
    assert resp.status_code == 404, resp.text
    resp = await disabled_client.put(f"/v1/projects/{pid}/hosts/{hid}/bindings/primary", json={})
    assert resp.status_code == 404, resp.text


# ── Ownership ─────────────────────────────────────────────


async def test_not_owned_project_404(
    multi_user_client: httpx.AsyncClient,
) -> None:
    """One user can never read or flip another user's collaboration config."""
    bob_project = await _make_project(multi_user_client, "Bob private", headers=_as_user(BOB))
    resp = await multi_user_client.get(
        f"/v1/projects/{bob_project}/collaboration", headers=_as_user(ALICE)
    )
    assert resp.status_code == 404
    resp = await multi_user_client.patch(
        f"/v1/projects/{bob_project}/collaboration",
        json={"enabled": True, "expected_revision": 0},
        headers=_as_user(ALICE),
    )
    assert resp.status_code == 404


# ── GET config + problems ─────────────────────────────────


def _insert_dangling_binding(
    db_uri: str, *, project_id: str, host_id: str, repository_id: str
) -> str:
    """Insert a binding row pointing at an unknown repository, bypassing the store.

    The store rejects such rows; only legacy data can dangle, so the
    fixture writes the row directly to exercise the GET problems surface.
    """
    import uuid as _uuid

    from sqlalchemy.orm import Session as _Session

    from omnigent.db.db_models import SqlProjectHostBinding, current_workspace_id
    from omnigent.db.utils import get_or_create_engine, now_epoch

    binding_id = _uuid.uuid4().hex
    engine = get_or_create_engine(db_uri)
    with _Session(engine) as session:
        session.add(
            SqlProjectHostBinding(
                workspace_id=current_workspace_id(),
                id=binding_id,
                project_id=project_id,
                host_id=host_id,
                name="primary",
                is_primary=True,
                repository_id=repository_id,
                workspace="/data/dangling",
                enabled=True,
                revision=1,
                path_verified_at=None,
                created_at=now_epoch(),
                updated_at=None,
            )
        )
        session.commit()
    return binding_id


async def test_get_returns_config_and_problems(
    collab_client: httpx.AsyncClient, db_uri: str
) -> None:
    """GET returns the switch, rows, and flagged problems with offending ids."""
    project_id = await _make_project(collab_client)
    repo = await _register_repo(collab_client, project_id)
    bindings = SqlAlchemyProjectHostBindingStore(db_uri)
    # An enabled non-primary binding with no primary on its host.
    lonely = bindings.upsert(
        project_id=project_id,
        host_id=_HOST_A,
        name="extra",
        repository_id=repo["id"],
        workspace="/data/extra",
        is_primary=False,
        enabled=True,
    )
    # A binding pointing at a repository that was never registered.
    dangling_id = _insert_dangling_binding(
        db_uri, project_id=project_id, host_id=_HOST_B, repository_id="f" * 32
    )
    resp = await collab_client.get(f"/v1/projects/{project_id}/collaboration")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["enabled"] is False
    assert body["revision"] == 0
    assert [r["name"] for r in body["repositories"]] == ["root"]
    assert {b["name"] for b in body["bindings"]} == {"extra", "primary"}
    problems = body["problems"]
    missing = [p for p in problems if p["code"] == "missing_primary"]
    assert len(missing) == 1 and missing[0]["host_id"] == _HOST_A
    dangling_problems = [p for p in problems if p["code"] == "dangling_repository"]
    assert len(dangling_problems) == 1
    assert dangling_problems[0]["binding_id"] == dangling_id
    assert dangling_problems[0]["repository_id"] == "f" * 32
    assert lonely.id  # the flagged host's binding exists


# ── PATCH revision ────────────────────────────────────────


async def test_patch_bumps_revision_and_rejects_stale(
    collab_client: httpx.AsyncClient,
) -> None:
    """PATCH flips the switch with optimistic concurrency."""
    project_id = await _make_project(collab_client)
    resp = await collab_client.patch(
        f"/v1/projects/{project_id}/collaboration",
        json={"enabled": True, "expected_revision": 0},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"enabled": True, "revision": 1}
    stale = await collab_client.patch(
        f"/v1/projects/{project_id}/collaboration",
        json={"enabled": False, "expected_revision": 0},
    )
    assert stale.status_code == 409


@pytest.mark.flaky(reruns=2, reruns_delay=1)
async def test_repo_and_binding_put_leave_collaboration_revision(
    collab_client: httpx.AsyncClient,
    live_host: dict[str, Any],
) -> None:
    """Repository and binding edits carry their own revision, not the switch's."""
    project_id = await _make_project(collab_client)
    await collab_client.patch(
        f"/v1/projects/{project_id}/collaboration",
        json={"enabled": True, "expected_revision": 0},
    )
    await _register_repo(collab_client, project_id)
    live_host["replies"]["/data/work"] = {
        "status": "ok",
        "exists": True,
        "type": "directory",
        "canonical_path": "/data/work",
    }
    resp = await collab_client.put(
        f"/v1/projects/{project_id}/hosts/{live_host['host_id']}/bindings/primary",
        json={
            "workspace": "/data/work",
            "repository_name": "root",
            "is_primary": True,
            "enabled": True,
        },
    )
    assert resp.status_code == 200, resp.text
    config = (await collab_client.get(f"/v1/projects/{project_id}/collaboration")).json()
    assert config["revision"] == 1


# ── Repository validation ─────────────────────────────────


async def test_repository_put_empty_remote_400_names_remote(
    collab_client: httpx.AsyncClient,
) -> None:
    """A repository without a remote cannot join, and the error says which."""
    project_id = await _make_project(collab_client)
    resp = await collab_client.put(
        f"/v1/projects/{project_id}/repositories/root",
        json={"remote_url": "   ", "default_branch": "main"},
    )
    assert resp.status_code == 400, resp.text
    message = resp.json()["error"]["message"]
    assert "root" in message and "remote" in message


async def test_repository_put_credentialed_url_400(
    collab_client: httpx.AsyncClient,
) -> None:
    """Credentials in the remote are refused, never stored."""
    project_id = await _make_project(collab_client)
    resp = await collab_client.put(
        f"/v1/projects/{project_id}/repositories/root",
        json={
            "remote_url": "https://user:secret@example.com/org/repo.git",
            "default_branch": "main",
        },
    )
    assert resp.status_code == 400, resp.text
    config = (await collab_client.get(f"/v1/projects/{project_id}/collaboration")).json()
    assert config["repositories"] == []


@pytest.mark.parametrize(
    "remote_url",
    [
        "https://ghp_secrettoken123@example.com/org/repo.git",
        "https://user:@example.com/org/repo.git",
        "https://user@example.com/org/repo.git",
    ],
)
async def test_repository_put_http_userinfo_400_without_echo(
    collab_client: httpx.AsyncClient, remote_url: str
) -> None:
    """Any userinfo on an http(s) remote is refused, and never echoed back."""
    project_id = await _make_project(collab_client)
    resp = await collab_client.put(
        f"/v1/projects/{project_id}/repositories/root",
        json={"remote_url": remote_url, "default_branch": "main"},
    )
    assert resp.status_code == 400, resp.text
    message = resp.json()["error"]["message"]
    assert "must not contain credentials" in message
    assert remote_url not in message
    for secret in ("ghp_secrettoken123", "user:secret", "user:@"):
        assert secret not in message


@pytest.mark.parametrize(
    ("name", "remote_url"),
    [
        ("ssh-remote", "ssh://git@example.com/org/repo.git"),
        ("scp-remote", "git@github.com:org/repo.git"),
    ],
)
async def test_repository_put_non_http_username_accepted(
    collab_client: httpx.AsyncClient, name: str, remote_url: str
) -> None:
    """Usernames survive on ssh/scp-like remotes; only passwords are refused."""
    project_id = await _make_project(collab_client)
    resp = await collab_client.put(
        f"/v1/projects/{project_id}/repositories/{name}",
        json={"remote_url": remote_url, "default_branch": "main"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["remote_url"] == remote_url


@pytest.mark.parametrize(
    "remote_url",
    [
        "ssh://user:secret@example.com/org/repo.git",
        "git:user:secret@github.com:org/repo.git",
    ],
)
async def test_repository_put_non_http_password_400(
    collab_client: httpx.AsyncClient, remote_url: str
) -> None:
    """Passwords are refused on every scheme, scp-like included."""
    project_id = await _make_project(collab_client)
    resp = await collab_client.put(
        f"/v1/projects/{project_id}/repositories/root",
        json={"remote_url": remote_url, "default_branch": "main"},
    )
    assert resp.status_code == 400, resp.text


async def test_repository_put_fullwidth_slash_host_400_without_echo(
    collab_client: httpx.AsyncClient,
) -> None:
    """A confusable slash in the host is refused, without echoing credentials."""
    project_id = await _make_project(collab_client)
    # U+FF0F FULLWIDTH SOLIDUS in the host.
    remote_url = "https://user:SECRETMARK@exa\uff0fmple.com/r.git"
    resp = await collab_client.put(
        f"/v1/projects/{project_id}/repositories/root",
        json={"remote_url": remote_url, "default_branch": "main"},
    )
    assert resp.status_code == 400, resp.text
    assert "SECRETMARK" not in resp.text


@pytest.mark.parametrize(
    "remote_url",
    [
        "example.com:org/a@b:r.git",
        "[2001:db8::1]:org/a@b:r.git",
        "git@github.com:org/r.git",
    ],
)
async def test_repository_put_scp_at_in_path_accepted(
    collab_client: httpx.AsyncClient, remote_url: str
) -> None:
    """An @ after a / or [...] is part of the path, not userinfo."""
    project_id = await _make_project(collab_client)
    resp = await collab_client.put(
        f"/v1/projects/{project_id}/repositories/root",
        json={"remote_url": remote_url, "default_branch": "main"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["remote_url"] == remote_url


async def test_repository_put_scp_password_400_without_echo(
    collab_client: httpx.AsyncClient,
) -> None:
    """A password in scp-like userinfo is refused, without echoing it back."""
    project_id = await _make_project(collab_client)
    remote_url = "user:SECRETMARK@github.com:org/r.git"
    resp = await collab_client.put(
        f"/v1/projects/{project_id}/repositories/root",
        json={"remote_url": remote_url, "default_branch": "main"},
    )
    assert resp.status_code == 400, resp.text
    assert "SECRETMARK" not in resp.text
    assert "must not contain credentials" in resp.json()["error"]["message"]


@pytest.mark.parametrize(
    "remote_url",
    [
        "https:///user:SECRETMARK@example.com/r.git",
        "HTTPS:///user:SECRETMARK@example.com/r.git",
        "https:/user:SECRETMARK@example.com/r.git",
    ],
)
async def test_repository_put_malformed_http_authority_400_without_echo(
    collab_client: httpx.AsyncClient, remote_url: str
) -> None:
    """Empty or single-slash http(s) authority is invalid, without echo."""
    project_id = await _make_project(collab_client)
    resp = await collab_client.put(
        f"/v1/projects/{project_id}/repositories/root",
        json={"remote_url": remote_url, "default_branch": "main"},
    )
    assert resp.status_code == 400, resp.text
    assert "SECRETMARK" not in resp.text
    assert "has an invalid remote_url" in resp.json()["error"]["message"]


async def test_repository_put_plain_https_remote_accepted(
    collab_client: httpx.AsyncClient,
) -> None:
    """A plain https remote still registers."""
    project_id = await _make_project(collab_client)
    remote_url = "https://github.com/org/r.git"
    resp = await collab_client.put(
        f"/v1/projects/{project_id}/repositories/root",
        json={"remote_url": remote_url, "default_branch": "main"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["remote_url"] == remote_url


@pytest.mark.parametrize(
    "name", ["bad name", "a*b", "x" * 101, ".hidden", "foo..bar", "foo.lock", "foo."]
)
async def test_repository_put_bad_name_400(collab_client: httpx.AsyncClient, name: str) -> None:
    """Repository names must be single safe ref-path segments."""
    project_id = await _make_project(collab_client)
    resp = await collab_client.put(
        f"/v1/projects/{project_id}/repositories/{name}",
        json={"remote_url": "https://example.com/r.git", "default_branch": "main"},
    )
    assert resp.status_code == 400, resp.text


@pytest.mark.parametrize(
    "name", [".", "..", "a/b", "", "a" * 101, ".hidden", "foo..bar", "foo.lock", "foo.", "foo\n"]
)
def test_ref_name_validator_rejects_unsafe_segments(name: str) -> None:
    """Dot-segments never survive URL normalization, so unit-test the guard."""
    from omnigent.errors import ErrorCode, OmnigentError
    from omnigent.server.routes.project_collaboration import _validate_ref_name

    with pytest.raises(OmnigentError) as exc_info:
        _validate_ref_name(name, kind="repository")
    assert exc_info.value.code == ErrorCode.INVALID_INPUT


@pytest.mark.parametrize("name", ["repo-1", "my_repo.v2"])
async def test_repository_put_accepted_name_matches_git_ref_format(
    collab_client: httpx.AsyncClient, name: str
) -> None:
    """Accepted names register, and each yields a ref git itself accepts."""
    import shutil
    import subprocess

    project_id = await _make_project(collab_client)
    resp = await collab_client.put(
        f"/v1/projects/{project_id}/repositories/{name}",
        json={"remote_url": "https://example.com/r.git", "default_branch": "main"},
    )
    assert resp.status_code == 200, resp.text
    if shutil.which("git") is None:
        pytest.skip("git not available")
    ref = f"refs/omnigent/assignments/x/input/{name}"
    completed = subprocess.run(
        ["git", "check-ref-format", ref], capture_output=True, text=True, timeout=30
    )
    assert completed.returncode == 0, completed.stderr


async def test_repository_put_revision_bump_and_noop(
    collab_client: httpx.AsyncClient,
) -> None:
    """A changed registration bumps its revision; an identical one is a no-op."""
    project_id = await _make_project(collab_client)
    first = await _register_repo(collab_client, project_id)
    assert first["revision"] == 1
    same = await _register_repo(collab_client, project_id)
    assert same["revision"] == 1
    assert same["updated_at"] is None
    changed = await _register_repo(
        collab_client, project_id, remote_url="https://example.com/org/other.git"
    )
    assert changed["revision"] == 2


async def test_repository_put_bad_manifest_path_400(
    collab_client: httpx.AsyncClient,
) -> None:
    """Absolute and escaping manifest paths are refused."""
    project_id = await _make_project(collab_client)
    for bad in [
        "/etc/manifest.json",
        "../outside.json",
        "a/../../b.json",
        "C:/outside.json",
        "a\\..\\outside.json",
        "a//b.json",
        "a/./b.json",
    ]:
        resp = await collab_client.put(
            f"/v1/projects/{project_id}/repositories/root",
            json={
                "remote_url": "https://example.com/r.git",
                "default_branch": "main",
                "context_manifest_path": bad,
            },
        )
        assert resp.status_code == 400, f"{bad}: {resp.text}"


@pytest.mark.flaky(reruns=2, reruns_delay=1)
async def test_repository_delete_with_binding_409(
    collab_client: httpx.AsyncClient,
    live_host: dict[str, Any],
) -> None:
    """A repository with a referencing binding cannot be deleted."""
    project_id = await _make_project(collab_client)
    await _register_repo(collab_client, project_id)
    live_host["replies"]["/data/work"] = {
        "status": "ok",
        "exists": True,
        "type": "directory",
        "canonical_path": "/data/work",
    }
    resp = await collab_client.put(
        f"/v1/projects/{project_id}/hosts/{live_host['host_id']}/bindings/primary",
        json={
            "workspace": "/data/work",
            "repository_name": "root",
            "is_primary": True,
            "enabled": True,
        },
    )
    assert resp.status_code == 200, resp.text
    refused = await collab_client.delete(f"/v1/projects/{project_id}/repositories/root")
    assert refused.status_code == 409
    await collab_client.delete(
        f"/v1/projects/{project_id}/hosts/{live_host['host_id']}/bindings/primary"
    )
    deleted = await collab_client.delete(f"/v1/projects/{project_id}/repositories/root")
    assert deleted.status_code == 200
    missing = await collab_client.delete(f"/v1/projects/{project_id}/repositories/root")
    assert missing.status_code == 404


# ── Binding validation (live host) ────────────────────────


@pytest.mark.flaky(reruns=2, reruns_delay=1)
async def test_binding_put_stores_canonical_path(
    collab_client: httpx.AsyncClient,
    live_host: dict[str, Any],
) -> None:
    """The host's canonical path is stored, never the typed one."""
    project_id = await _make_project(collab_client)
    await _register_repo(collab_client, project_id)
    live_host["replies"]["/data/link"] = {
        "status": "ok",
        "exists": True,
        "type": "directory",
        "canonical_path": "/private/data/work",
    }
    resp = await collab_client.put(
        f"/v1/projects/{project_id}/hosts/{live_host['host_id']}/bindings/primary",
        json={
            "workspace": "/data/link",
            "repository_name": "root",
            "is_primary": True,
            "enabled": True,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["workspace"] == "/private/data/work"
    assert body["path_verified_at"] is not None


async def test_binding_put_offline_host_409(collab_client: httpx.AsyncClient, db_uri: str) -> None:
    """A registered but disconnected host fails with a 409-class error."""
    project_id = await _make_project(collab_client)
    await _register_repo(collab_client, project_id)
    hosts = HostStore(db_uri)
    hosts.upsert_on_connect(_HOST_OFFLINE, "offline-box", "local")
    # A freshly registered row reads as live; mark it offline so the route
    # sees a known-but-disconnected host rather than a wrong-replica landing.
    hosts.set_offline(_HOST_OFFLINE)
    resp = await collab_client.put(
        f"/v1/projects/{project_id}/hosts/{_HOST_OFFLINE}/bindings/primary",
        json={
            "workspace": "/data/work",
            "repository_name": "root",
            "is_primary": True,
            "enabled": True,
        },
    )
    assert resp.status_code == 409


@pytest.mark.flaky(reruns=2, reruns_delay=1)
async def test_binding_put_unknown_repository_400(
    collab_client: httpx.AsyncClient,
    live_host: dict[str, Any],
) -> None:
    """A binding cannot point at an unregistered repository."""
    project_id = await _make_project(collab_client)
    resp = await collab_client.put(
        f"/v1/projects/{project_id}/hosts/{live_host['host_id']}/bindings/primary",
        json={
            "workspace": "/data/work",
            "repository_name": "ghost",
            "is_primary": True,
            "enabled": True,
        },
    )
    assert resp.status_code == 400


@pytest.mark.flaky(reruns=2, reruns_delay=1)
async def test_binding_second_primary_same_host_409_other_host_allowed(
    collab_client: httpx.AsyncClient,
    live_host: dict[str, Any],
    db_uri: str,
) -> None:
    """One primary per (project, host); another host may have its own."""
    project_id = await _make_project(collab_client)
    await _register_repo(collab_client, project_id)
    live_host["replies"]["/data/a"] = {
        "status": "ok",
        "exists": True,
        "type": "directory",
        "canonical_path": "/data/a",
    }
    live_host["replies"]["/data/b"] = {
        "status": "ok",
        "exists": True,
        "type": "directory",
        "canonical_path": "/data/b",
    }
    host_id = live_host["host_id"]
    first = await collab_client.put(
        f"/v1/projects/{project_id}/hosts/{host_id}/bindings/primary",
        json={
            "workspace": "/data/a",
            "repository_name": "root",
            "is_primary": True,
            "enabled": True,
        },
    )
    assert first.status_code == 200, first.text
    second = await collab_client.put(
        f"/v1/projects/{project_id}/hosts/{host_id}/bindings/second",
        json={
            "workspace": "/data/b",
            "repository_name": "root",
            "is_primary": True,
            "enabled": True,
        },
    )
    assert second.status_code == 409
    # A primary on another host is a different scope and succeeds. The
    # second host row is registered directly and served by a tunneled
    # connection opened inline below.
    HostStore(db_uri).upsert_on_connect(_HOST_B, "fake-b", "local")
    app = live_host["app"]
    comm_b = await _connect_fake_host(app, _HOST_B, "fake-b")

    def _echo_b(path: str) -> dict[str, Any]:
        return {
            "status": "ok",
            "exists": True,
            "type": "directory",
            "canonical_path": path,
        }

    replies_b: dict[str, dict[str, Any]] = {"/data/b": _echo_b("/data/b")}
    drain_b = _start_stat_drain(comm_b, replies_b)
    try:
        other = await collab_client.put(
            f"/v1/projects/{project_id}/hosts/{_HOST_B}/bindings/primary",
            json={
                "workspace": "/data/b",
                "repository_name": "root",
                "is_primary": True,
                "enabled": True,
            },
        )
        assert other.status_code == 200, other.text
    finally:
        await _stop_fake_host(comm_b, drain_b)


@pytest.mark.flaky(reruns=2, reruns_delay=1)
async def test_verify_success_refreshes_timestamp(
    collab_client: httpx.AsyncClient,
    live_host: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful re-verify stamps a fresh ``path_verified_at``."""
    import itertools

    project_id = await _make_project(collab_client)
    await _register_repo(collab_client, project_id)
    live_host["replies"]["/data/work"] = {
        "status": "ok",
        "exists": True,
        "type": "directory",
        "canonical_path": "/data/work",
    }
    ticks = itertools.count(start=1_700_000_000, step=10)
    monkeypatch.setattr(
        "omnigent.server.routes.project_collaboration.now_epoch",
        lambda: next(ticks),
    )
    created = await collab_client.put(
        f"/v1/projects/{project_id}/hosts/{live_host['host_id']}/bindings/primary",
        json={
            "workspace": "/data/work",
            "repository_name": "root",
            "is_primary": True,
            "enabled": True,
        },
    )
    assert created.status_code == 200, created.text
    verified = await collab_client.post(
        f"/v1/projects/{project_id}/hosts/{live_host['host_id']}/bindings/primary/verify"
    )
    assert verified.status_code == 200, verified.text
    assert verified.json()["path_verified_at"] > created.json()["path_verified_at"]


@pytest.mark.flaky(reruns=2, reruns_delay=1)
async def test_verify_failure_leaves_row_unchanged(
    collab_client: httpx.AsyncClient,
    live_host: dict[str, Any],
) -> None:
    """A failed re-verify returns the error and touches nothing."""
    project_id = await _make_project(collab_client)
    await _register_repo(collab_client, project_id)
    live_host["replies"]["/data/work"] = {
        "status": "ok",
        "exists": True,
        "type": "directory",
        "canonical_path": "/data/work",
    }
    created = await collab_client.put(
        f"/v1/projects/{project_id}/hosts/{live_host['host_id']}/bindings/primary",
        json={
            "workspace": "/data/work",
            "repository_name": "root",
            "is_primary": True,
            "enabled": True,
        },
    )
    assert created.status_code == 200, created.text
    before = created.json()
    # The directory disappears from the host's view.
    live_host["replies"]["/data/work"] = {
        "status": "ok",
        "exists": False,
    }
    # Verify stats the stored canonical path; point the reply at it too.
    failed = await collab_client.post(
        f"/v1/projects/{project_id}/hosts/{live_host['host_id']}/bindings/primary/verify"
    )
    assert failed.status_code == 400
    config = (await collab_client.get(f"/v1/projects/{project_id}/collaboration")).json()
    after = next(b for b in config["bindings"] if b["name"] == "primary")
    assert after["workspace"] == before["workspace"]
    assert after["path_verified_at"] == before["path_verified_at"]
    assert after["revision"] == before["revision"]


# ── Post-bind hook (live host) ────────────────────────────


async def _put_binding(
    client: httpx.AsyncClient,
    host_id: str,
    project_id: str,
    *,
    workspace: str = "/data/work",
    name: str = "primary",
    enabled: bool = True,
) -> httpx.Response:
    """PUT a binding for the registered ``root`` repository."""
    return await client.put(
        f"/v1/projects/{project_id}/hosts/{host_id}/bindings/{name}",
        json={
            "workspace": workspace,
            "repository_name": "root",
            "is_primary": True,
            "enabled": enabled,
        },
    )


async def test_binding_put_legacy_host_unsupported(
    collab_client: httpx.AsyncClient,
    live_host: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hello without post_bind_hook answers unsupported and sends no frame."""
    # A short server wait makes an accidental send fail as ``unreachable``
    # instead of hanging the test, so this stays a real no-frame assertion.
    monkeypatch.setattr(
        "omnigent.server.routes.project_collaboration._POST_BIND_HOOK_TIMEOUT_S", 0.1
    )
    project_id = await _make_project(collab_client)
    await _register_repo(collab_client, project_id)
    live_host["replies"]["/data/work"] = {
        "status": "ok",
        "exists": True,
        "type": "directory",
        "canonical_path": "/data/work",
    }

    resp = await _put_binding(collab_client, live_host["host_id"], project_id)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["post_bind"] == {
        "status": "unsupported",
        "exit_code": None,
        "output": None,
        "error": None,
    }
    config = (await collab_client.get(f"/v1/projects/{project_id}/collaboration")).json()
    assert [b["name"] for b in config["bindings"]] == ["primary"]


async def test_binding_put_post_bind_runs_for_disabled_binding(
    collab_client: httpx.AsyncClient,
    hooked_host: dict[str, Any],
) -> None:
    """The hook runs for a disabled binding and its result rides the response."""
    project_id = await _make_project(collab_client)
    await _register_repo(collab_client, project_id)
    hooked_host["replies"]["/data/work"] = {
        "status": "ok",
        "exists": True,
        "type": "directory",
        "canonical_path": "/data/work",
    }
    hooked_host["hooks"]["replies"]["primary"] = {
        "status": "ok",
        "exit_code": 0,
        "output": "joined",
    }

    resp = await _put_binding(collab_client, hooked_host["host_id"], project_id, enabled=False)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["enabled"] is False
    assert body["post_bind"] == {
        "status": "ok",
        "exit_code": 0,
        "output": "joined",
        "error": None,
    }
    frames = hooked_host["hooks"]["seen"]
    assert len(frames) == 1
    frame = frames[0]
    assert frame.project_id == project_id
    assert frame.binding_name == "primary"
    assert frame.revision == body["revision"]
    assert frame.binding_id == body["id"]
    assert frame.repository_name == "root"
    assert frame.workspace == "/data/work"
    assert frame.is_primary is True
    assert frame.context_manifest_path == ".agents/project/manifest.json"


async def test_binding_put_post_bind_failure_is_carried_back(
    collab_client: httpx.AsyncClient,
    hooked_host: dict[str, Any],
) -> None:
    """A failing command reports its exit code and output without refusing."""
    project_id = await _make_project(collab_client)
    await _register_repo(collab_client, project_id)
    hooked_host["replies"]["/data/work"] = {
        "status": "ok",
        "exists": True,
        "type": "directory",
        "canonical_path": "/data/work",
    }
    hooked_host["hooks"]["replies"]["primary"] = {
        "status": "failed",
        "exit_code": 3,
        "output": "hook exploded",
        "error": "command exited 3",
    }

    resp = await _put_binding(collab_client, hooked_host["host_id"], project_id)

    assert resp.status_code == 200, resp.text
    assert resp.json()["post_bind"] == {
        "status": "failed",
        "exit_code": 3,
        "output": "hook exploded",
        "error": "command exited 3",
    }
    config = (await collab_client.get(f"/v1/projects/{project_id}/collaboration")).json()
    assert [b["name"] for b in config["bindings"]] == ["primary"]


async def test_binding_put_post_bind_unreachable_without_an_answer(
    collab_client: httpx.AsyncClient,
    hooked_host: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host that never answers costs the bounded wait; the row is stored."""
    monkeypatch.setattr(
        "omnigent.server.routes.project_collaboration._POST_BIND_HOOK_TIMEOUT_S", 0.1
    )
    project_id = await _make_project(collab_client)
    await _register_repo(collab_client, project_id)
    hooked_host["replies"]["/data/work"] = {
        "status": "ok",
        "exists": True,
        "type": "directory",
        "canonical_path": "/data/work",
    }
    hooked_host["hooks"]["drop"] = True

    resp = await _put_binding(collab_client, hooked_host["host_id"], project_id)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["post_bind"] == {
        "status": "unreachable",
        "exit_code": None,
        "output": None,
        "error": None,
    }
    assert body["revision"] == 1
    config = (await collab_client.get(f"/v1/projects/{project_id}/collaboration")).json()
    assert [b["name"] for b in config["bindings"]] == ["primary"]


async def test_verify_post_bind_reruns_with_stored_revision(
    collab_client: httpx.AsyncClient,
    hooked_host: dict[str, Any],
) -> None:
    """Verify re-runs the hook with the stored binding revision."""
    project_id = await _make_project(collab_client)
    await _register_repo(collab_client, project_id)
    hooked_host["replies"]["/data/work"] = {
        "status": "ok",
        "exists": True,
        "type": "directory",
        "canonical_path": "/data/work",
    }
    created = await _put_binding(collab_client, hooked_host["host_id"], project_id)
    assert created.status_code == 200, created.text

    verified = await collab_client.post(
        f"/v1/projects/{project_id}/hosts/{hooked_host['host_id']}/bindings/primary/verify"
    )

    assert verified.status_code == 200, verified.text
    body = verified.json()
    assert body["post_bind"]["status"] == "ok"
    frames = hooked_host["hooks"]["seen"]
    assert len(frames) == 2
    assert frames[1].revision == body["revision"] == created.json()["revision"]
    assert frames[1].binding_id == body["id"]
    assert frames[1].binding_name == "primary"


async def test_binding_delete_sends_no_post_bind_frame(
    collab_client: httpx.AsyncClient,
    hooked_host: dict[str, Any],
) -> None:
    """DELETE runs no hook; only the PUT's frame was ever sent."""
    project_id = await _make_project(collab_client)
    await _register_repo(collab_client, project_id)
    hooked_host["replies"]["/data/work"] = {
        "status": "ok",
        "exists": True,
        "type": "directory",
        "canonical_path": "/data/work",
    }
    created = await _put_binding(collab_client, hooked_host["host_id"], project_id)
    assert created.status_code == 200, created.text
    assert len(hooked_host["hooks"]["seen"]) == 1

    deleted = await collab_client.delete(
        f"/v1/projects/{project_id}/hosts/{hooked_host['host_id']}/bindings/primary"
    )

    assert deleted.status_code == 200, deleted.text
    await asyncio.sleep(0.05)
    assert len(hooked_host["hooks"]["seen"]) == 1
