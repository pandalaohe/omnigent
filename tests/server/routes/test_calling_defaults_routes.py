"""Tests for the calling-defaults routes (``/v1/calling-defaults``).

Builds real stores on the per-test SQLite database and registers fake live
host connections in the app's ``host_registry``. The host round-trip itself
(``routes.hosts._proxy_model_options``) is replaced so the tests drive the
statuses the route must record (ok / failed / offline) without a tunnel.
"""

from __future__ import annotations

import io
import tarfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
import yaml
from fastapi import FastAPI

from omnigent.host.frames import HostHelloFrame
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.user_preferences_store import SqlAlchemyUserPreferencesStore
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_model_catalog_cache_store import HostModelCatalogCacheStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore

HOST_A = "a1b2c3d4e5f60718293a4b5c6d7e8f01"
HOST_B = "b1b2c3d4e5f60718293a4b5c6d7e8f02"
HOST_FOREIGN = "c1b2c3d4e5f60718293a4b5c6d7e8f03"
AGENT_ID = "087b7cb7ac30abf4debfaa578d052ec6"
ALICE = "alice@example.com"
BOB = "bob@example.com"


def _as_user(user: str) -> dict[str, str]:
    """Header identifying the requesting user under header auth."""
    return {"X-Forwarded-Email": user}


def _bundle(harness: str) -> bytes:
    """A minimal valid agent bundle whose executor declares *harness*."""
    config = yaml.safe_dump(
        {
            "spec_version": 1,
            "name": "calling-defaults-agent",
            "executor": {"type": "omnigent", "config": {"harness": harness}},
            "prompt": "hi",
        }
    )
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tf:
        data = config.encode()
        info = tarfile.TarInfo("config.yaml")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


@dataclass
class _Stores:
    """The app's stores, so tests can seed and inspect the same database."""

    artifact_store: LocalArtifactStore
    agent_store: SqlAlchemyAgentStore
    agent_cache: AgentCache
    host_store: HostStore
    cache: HostModelCatalogCacheStore
    project_store: SqlAlchemyProjectStore
    prefs: SqlAlchemyUserPreferencesStore


def _seed_agent(stores: _Stores, agent_id: str, harness: str) -> None:
    """Register a template agent whose spec resolves to *harness*."""
    location = f"{agent_id}/bundle"
    stores.artifact_store.put(location, _bundle(harness))
    stores.agent_store.create(agent_id, "calling-defaults-agent", location)


@pytest.fixture()
def stores(runtime_init: None, db_uri: str, tmp_path: Path) -> _Stores:
    """Real stores over one migrated SQLite database."""
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return _Stores(
        artifact_store=artifact_store,
        agent_store=SqlAlchemyAgentStore(db_uri),
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        host_store=HostStore(db_uri),
        cache=HostModelCatalogCacheStore(db_uri),
        project_store=SqlAlchemyProjectStore(db_uri),
        prefs=SqlAlchemyUserPreferencesStore(db_uri),
    )


def _build_app(stores: _Stores, *, auth: bool = False) -> FastAPI:
    return create_app(
        agent_store=stores.agent_store,
        file_store=SqlAlchemyFileStore(stores.agent_store.storage_location),
        conversation_store=SqlAlchemyConversationStore(stores.agent_store.storage_location),
        artifact_store=stores.artifact_store,
        agent_cache=stores.agent_cache,
        host_store=stores.host_store,
        host_model_catalog_cache_store=stores.cache,
        project_store=stores.project_store,
        user_preferences_store=stores.prefs,
        auth_provider=UnifiedAuthProvider(source="header", local_single_user=False)
        if auth
        else None,
    )


@pytest.fixture()
def app(stores: _Stores) -> FastAPI:
    """Single-user app (hosts are owned by the reserved ``local`` user)."""
    return _build_app(stores)


@pytest_asyncio.fixture()
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """HTTP client wired to the single-user app."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture()
def multi_user_app(stores: _Stores) -> FastAPI:
    """Header-auth app, for ownership tests."""
    return _build_app(stores, auth=True)


@pytest_asyncio.fixture()
async def multi_user_client(multi_user_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """HTTP client wired to the header-auth app."""
    transport = httpx.ASGITransport(app=multi_user_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _register_host(app: FastAPI, host_id: str) -> None:
    """Mark *host_id* online on this replica by registering a live connection."""
    app.state.host_registry.register(
        host_id=host_id,
        ws=AsyncMock(),
        hello=HostHelloFrame(version="test", frame_protocol_version=1, name="test"),
        owner=None,
    )


async def test_sync_records_ok_unsupported_and_offline_pairs(
    app: FastAPI,
    client: httpx.AsyncClient,
    stores: _Stores,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario 22: online host synced, offline host untouched, unsupported recorded."""
    stores.host_store.upsert_on_connect(HOST_A, "box-a", "local")
    stores.host_store.upsert_on_connect(HOST_B, "box-b", "local")
    # An explicit negative readiness drops a harness from the candidate set;
    # unknown readiness (the other keys) keeps them.
    stores.host_store.update_harness_readiness(HOST_A, {"codex-native": False})
    _register_host(app, HOST_A)
    stores.cache.upsert(HOST_B, "codex", [{"id": "old-model"}], 100)

    calls: list[tuple[str, str]] = []

    async def fake_proxy(*, host_registry: Any, host_conn: Any, harness: str) -> dict[str, Any]:
        calls.append((host_conn.host_id, harness))
        if harness == "codex":
            return {"status": "failed", "error": "unsupported"}
        return {"status": "ok", "models": [{"id": f"{harness}-model"}]}

    monkeypatch.setattr("omnigent.server.routes.calling_defaults._proxy_model_options", fake_proxy)

    resp = await client.post("/v1/calling-defaults/sync", json={})
    assert resp.status_code == 200, resp.text
    rows = {(row["host_id"], row["harness"]): row for row in resp.json()["rows"]}

    # Only the online host was asked, and only for configured harnesses.
    assert calls and all(host_id == HOST_A for host_id, _ in calls)
    assert ("codex-native",) not in {(harness,) for _, harness in calls}
    assert (HOST_A, "codex-native") not in rows

    # The host's own failure text is recorded; a first failure carries no models.
    assert rows[(HOST_A, "codex")]["error"] == "unsupported"
    assert rows[(HOST_A, "codex")]["models"] == []
    assert rows[(HOST_A, "codex")]["stale"] is True

    # A successful pair is fresh.
    assert rows[(HOST_A, "claude-native")]["models"] == [{"id": "claude-native-model"}]
    assert rows[(HOST_A, "claude-native")]["error"] is None
    assert rows[(HOST_A, "claude-native")]["stale"] is False

    # The offline host was never requested and keeps its old row, read stale.
    assert rows[(HOST_B, "codex")]["models"] == [{"id": "old-model"}]
    assert rows[(HOST_B, "codex")]["stale"] is True


async def test_sync_unowned_host_filter_404(client: httpx.AsyncClient, stores: _Stores) -> None:
    """An unknown or foreign ``host_id`` filter is a 404, never a proxied pair."""
    stores.host_store.upsert_on_connect(HOST_FOREIGN, "box-x", "someone-else")

    foreign = await client.post("/v1/calling-defaults/sync", json={"host_id": HOST_FOREIGN})
    assert foreign.status_code == 404, foreign.text
    assert stores.cache.list([HOST_FOREIGN]) == []

    missing = await client.post("/v1/calling-defaults/sync", json={"host_id": "0" * 32})
    assert missing.status_code == 404


async def test_catalogs_returns_only_the_callers_hosts(
    app: FastAPI, client: httpx.AsyncClient, stores: _Stores
) -> None:
    """The catalogs route scopes to the caller's own hosts."""
    stores.host_store.upsert_on_connect(HOST_A, "box-a", "local")
    stores.host_store.upsert_on_connect(HOST_FOREIGN, "box-x", "someone-else")
    _register_host(app, HOST_A)
    stores.cache.upsert(HOST_A, "codex", [{"id": "mine"}], 5)
    stores.cache.upsert(HOST_FOREIGN, "codex", [{"id": "theirs"}], 5)

    resp = await client.get("/v1/calling-defaults/catalogs")
    assert resp.status_code == 200, resp.text
    rows = resp.json()["rows"]
    assert [(row["host_id"], row["models"]) for row in rows] == [(HOST_A, [{"id": "mine"}])]
    assert rows[0]["stale"] is False


async def test_resolve_reads_the_master_table_through_the_agents_harness(
    client: httpx.AsyncClient, stores: _Stores
) -> None:
    """No project: the master table applies under the agent's resolved harness."""
    stores.host_store.upsert_on_connect(HOST_A, "box-a", "local")
    _seed_agent(stores, AGENT_ID, harness="codex")
    stores.prefs.patch_namespace(
        "local",
        "calling_defaults",
        {HOST_A: {"codex": {"model": "gpt-6-sol", "effort": "high"}}},
    )
    stores.cache.upsert(
        HOST_A,
        "codex",
        [{"id": "gpt-6-sol", "supportedReasoningEfforts": ["high"]}],
        10,
    )

    resp = await client.get(
        "/v1/calling-defaults/resolve",
        params={"host_id": HOST_A, "agent_id": AGENT_ID},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["agent_id"] == AGENT_ID
    assert body["harness"] == "codex"
    assert body["model"] == "gpt-6-sol"
    assert body["effort"] == "high"
    assert body["sources"] == {"agent": "explicit", "model": "master", "effort": "master"}
    assert body["problems"] == []


async def test_resolve_project_default_flags_a_missing_model_and_stale_skips(
    client: httpx.AsyncClient, stores: _Stores
) -> None:
    """Scenarios 11 + 13: a fresh catalog refuses the model; a stale one does not."""
    stores.host_store.upsert_on_connect(HOST_A, "box-a", "local")
    project = (
        await client.post(
            "/v1/projects",
            json={
                "name": "P",
                "config": {
                    "calling_defaults": {
                        HOST_A: {"harnesses": {"codex": {"model": "gpt-6-sol", "effort": "high"}}}
                    }
                },
            },
        )
    ).json()
    stores.cache.upsert(
        HOST_A,
        "codex",
        [{"id": "gpt-5.5", "supportedReasoningEfforts": ["high"]}],
        1700000000,
    )

    params = {"project_id": project["id"], "host_id": HOST_A, "harness": "codex"}
    body = (await client.get("/v1/calling-defaults/resolve", params=params)).json()
    assert body["model"] == "gpt-6-sol"
    assert body["sources"]["model"] == "project_host"
    (problem,) = body["problems"]
    assert problem["field"] == "model"
    assert problem["setting"] == "project_host"
    assert "project 'P' host settings" in problem["message"]
    assert f"host {HOST_A!r}" in problem["message"]
    assert "last sync" in problem["message"]

    # Once the pair is stale, the offered check is skipped entirely.
    stores.cache.mark_error(HOST_A, "codex", "unsupported")
    stale = (await client.get("/v1/calling-defaults/resolve", params=params)).json()
    assert stale["model"] == "gpt-6-sol"
    assert stale["problems"] == []


async def test_resolve_foreign_project_and_host_404(
    multi_user_client: httpx.AsyncClient, stores: _Stores
) -> None:
    """Unknown / foreign projects and unowned hosts all read as 404 for the caller."""
    bob_project = (
        await multi_user_client.post(
            "/v1/projects", json={"name": "Bob work"}, headers=_as_user(BOB)
        )
    ).json()
    stores.host_store.upsert_on_connect(HOST_FOREIGN, "bob-box", BOB)

    foreign_project = await multi_user_client.get(
        "/v1/calling-defaults/resolve",
        params={"project_id": bob_project["id"]},
        headers=_as_user(ALICE),
    )
    assert foreign_project.status_code == 404, foreign_project.text

    unknown_project = await multi_user_client.get(
        "/v1/calling-defaults/resolve",
        params={"project_id": "0" * 32},
        headers=_as_user(ALICE),
    )
    assert unknown_project.status_code == 404

    foreign_host = await multi_user_client.get(
        "/v1/calling-defaults/resolve",
        params={"host_id": HOST_FOREIGN},
        headers=_as_user(ALICE),
    )
    assert foreign_host.status_code == 404

    # The owner still resolves against their own project.
    owned = await multi_user_client.get(
        "/v1/calling-defaults/resolve",
        params={"project_id": bob_project["id"]},
        headers=_as_user(BOB),
    )
    assert owned.status_code == 200, owned.text
