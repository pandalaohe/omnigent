"""Tests for the project-entries routes.

The entries router is mounted whenever ``create_app`` receives the project
store and the binding store (entries share it), and — unlike the
collaboration surface — is never feature-flag gated. Entry PUT/GET validate
the path live on the host over the tunnel, so those tests connect a fake
host that answers ``host.stat`` frames, reusing the fakes from
``test_project_collaboration_routes.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.stores.host_store import HostStore
from tests.server.routes.test_project_collaboration_routes import (
    _HOST_A,
    _HOST_OFFLINE,
    _as_user,
    _build_app,
    _connect_fake_host,
    _make_project,
    _start_stat_drain,
    _stop_fake_host,
)

ALICE = "alice@example.com"
BOB = "bob@example.com"


@pytest.fixture()
def entries_app(db_uri: str, tmp_path: Path) -> FastAPI:
    """App with the entries surface enabled (collaboration flag on)."""
    return _build_app(db_uri, tmp_path)


@pytest.fixture()
def disabled_app(db_uri: str, tmp_path: Path) -> FastAPI:
    """App with the collaboration flag off — entries must still be served."""
    return _build_app(db_uri, tmp_path, enabled=False)


@pytest.fixture()
def multi_user_app(db_uri: str, tmp_path: Path) -> FastAPI:
    """App with header auth, for ownership tests."""
    return _build_app(db_uri, tmp_path, auth=True)


@pytest_asyncio.fixture()
async def entries_client(entries_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """HTTP client wired to the entries-enabled app."""
    transport = httpx.ASGITransport(app=entries_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest_asyncio.fixture()
async def disabled_client(disabled_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """HTTP client wired to the flag-disabled app."""
    transport = httpx.ASGITransport(app=disabled_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest_asyncio.fixture()
async def multi_user_client(multi_user_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """HTTP client wired to the header-auth app."""
    transport = httpx.ASGITransport(app=multi_user_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest_asyncio.fixture()
async def live_host(entries_app: FastAPI) -> AsyncIterator[dict[str, Any]]:
    """Connect a fake host answering ``host.stat`` and yield its controls."""
    comm = await _connect_fake_host(entries_app, _HOST_A, "fake-a")
    replies: dict[str, dict[str, Any]] = {}
    drain_task = _start_stat_drain(comm, replies)
    try:
        yield {"host_id": _HOST_A, "replies": replies, "app": entries_app}
    finally:
        await _stop_fake_host(comm, drain_task)


async def test_entries_crud_round_trip(
    entries_client: httpx.AsyncClient,
    live_host: dict[str, Any],
) -> None:
    """PUT stores the canonical path, GET lists it, DELETE removes it (404 after)."""
    project_id = await _make_project(entries_client)
    live_host["replies"]["/data/link"] = {
        "status": "ok",
        "exists": True,
        "type": "directory",
        "canonical_path": "/private/data/entry",
    }
    created = await entries_client.put(
        f"/v1/projects/{project_id}/entries/{live_host['host_id']}",
        json={"workspace": "/data/link"},
    )
    assert created.status_code == 200, created.text
    assert created.json() == {
        "host_id": live_host["host_id"],
        "workspace": "/private/data/entry",
        "updated_at": None,
    }

    listed = await entries_client.get(f"/v1/projects/{project_id}/entries")
    assert listed.status_code == 200, listed.text
    assert listed.json() == {"entries": [created.json()]}

    same = await entries_client.put(
        f"/v1/projects/{project_id}/entries/{live_host['host_id']}",
        json={"workspace": "/data/link"},
    )
    assert same.status_code == 200, same.text
    assert same.json()["updated_at"] is None

    live_host["replies"]["/data/moved"] = {
        "status": "ok",
        "exists": True,
        "type": "directory",
        "canonical_path": "/private/data/moved",
    }
    moved = await entries_client.put(
        f"/v1/projects/{project_id}/entries/{live_host['host_id']}",
        json={"workspace": "/data/moved"},
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["workspace"] == "/private/data/moved"
    assert moved.json()["updated_at"] is not None

    deleted = await entries_client.delete(
        f"/v1/projects/{project_id}/entries/{live_host['host_id']}"
    )
    assert deleted.status_code == 204, deleted.text
    assert deleted.content == b""
    assert (await entries_client.get(f"/v1/projects/{project_id}/entries")).json() == {
        "entries": []
    }
    absent = await entries_client.delete(
        f"/v1/projects/{project_id}/entries/{live_host['host_id']}"
    )
    assert absent.status_code == 404
    assert absent.json()["error"]["message"] == "Entry not found"


async def test_entries_are_owner_scoped(
    multi_user_client: httpx.AsyncClient,
) -> None:
    """One user can never read or write another user's entries."""
    bob_project = await _make_project(multi_user_client, "Bob private", headers=_as_user(BOB))
    host_id = "1" * 32
    for method, path, body in [
        ("GET", f"/v1/projects/{bob_project}/entries", None),
        ("PUT", f"/v1/projects/{bob_project}/entries/{host_id}", {"workspace": "/w"}),
        ("DELETE", f"/v1/projects/{bob_project}/entries/{host_id}", None),
    ]:
        response = await multi_user_client.request(
            method, path, json=body, headers=_as_user(ALICE)
        )
        assert response.status_code == 404, f"{method} {path}: {response.text}"
        assert response.json()["error"]["message"] == "Project not found"


async def test_entry_put_offline_host_409_and_nothing_stored(
    entries_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A registered but disconnected host refuses the entry; no row lands."""
    project_id = await _make_project(entries_client)
    hosts = HostStore(db_uri)
    hosts.upsert_on_connect(_HOST_OFFLINE, "offline-box", "local")
    hosts.set_offline(_HOST_OFFLINE)
    response = await entries_client.put(
        f"/v1/projects/{project_id}/entries/{_HOST_OFFLINE}",
        json={"workspace": "/data/work"},
    )
    assert response.status_code == 409, response.text
    listed = await entries_client.get(f"/v1/projects/{project_id}/entries")
    assert listed.json() == {"entries": []}


async def test_entry_put_bad_path_400(
    entries_client: httpx.AsyncClient,
    live_host: dict[str, Any],
) -> None:
    """A path the host does not see as a directory is refused untouched."""
    project_id = await _make_project(entries_client)
    live_host["replies"]["/data/missing"] = {"status": "ok", "exists": False}
    response = await entries_client.put(
        f"/v1/projects/{project_id}/entries/{live_host['host_id']}",
        json={"workspace": "/data/missing"},
    )
    assert response.status_code == 400, response.text
    listed = await entries_client.get(f"/v1/projects/{project_id}/entries")
    assert listed.json() == {"entries": []}


async def test_entry_put_sandbox_400(entries_client: httpx.AsyncClient) -> None:
    """The sandbox sentinel has no directory and is refused before any host call."""
    project_id = await _make_project(entries_client)
    response = await entries_client.put(
        f"/v1/projects/{project_id}/entries/__sandbox__",
        json={"workspace": "/data/work"},
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "invalid_input"


async def test_entries_surface_is_not_flag_gated(
    disabled_client: httpx.AsyncClient,
) -> None:
    """With collaboration disabled the entries routes still serve."""
    project_id = await _make_project(disabled_client)
    listed = await disabled_client.get(f"/v1/projects/{project_id}/entries")
    assert listed.status_code == 200, listed.text
    assert listed.json() == {"entries": []}
    sandbox = await disabled_client.put(
        f"/v1/projects/{project_id}/entries/__sandbox__",
        json={"workspace": "/data/work"},
    )
    assert sandbox.status_code == 400, sandbox.text
    absent = await disabled_client.delete(f"/v1/projects/{project_id}/entries/{_HOST_A}")
    assert absent.status_code == 404
    assert absent.json()["error"]["message"] == "Entry not found"
