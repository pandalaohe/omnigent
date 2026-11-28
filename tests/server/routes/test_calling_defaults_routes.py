"""Tests for the calling-defaults routes (``/v1/calling-defaults``).

Builds real stores on the per-test SQLite database and registers fake live
host connections in the app's ``host_registry``. The host round-trip itself
(``routes.hosts._proxy_model_options``) is replaced so the tests drive the
statuses the route must record (ok / failed / offline) without a tunnel.
"""

from __future__ import annotations

import asyncio
import io
import json
import tarfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
import yaml
from fastapi import FastAPI

import omnigent.server.routes.calling_defaults as calling_defaults_routes
from omnigent.db.db_models import current_workspace_id, workspace_scope
from omnigent.db.utils import now_epoch
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
from tests.server.helpers import PERMISSION_TIERS_HEADERS

HOST_A = "a1b2c3d4e5f60718293a4b5c6d7e8f01"
HOST_B = "b1b2c3d4e5f60718293a4b5c6d7e8f02"
HOST_FOREIGN = "c1b2c3d4e5f60718293a4b5c6d7e8f03"
AGENT_ID = "087b7cb7ac30abf4debfaa578d052ec6"
ALICE = "alice@example.com"
BOB = "bob@example.com"


def _as_user(user: str) -> dict[str, str]:
    """Header identifying the requesting user under header auth."""
    return {"X-Forwarded-Email": user}


def _bundle(harness: str, effort: str | None = None) -> bytes:
    """A minimal valid agent bundle whose executor declares *harness*."""
    config = yaml.safe_dump(
        {
            "spec_version": 1,
            "name": "calling-defaults-agent",
            "executor": {
                "type": "omnigent",
                "config": {"harness": harness},
                **({"reasoning_effort": effort} if effort else {}),
            },
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


def _seed_agent(
    stores: _Stores, agent_id: str, harness: str, *, name: str = "calling-defaults-agent"
) -> None:
    """Register a template agent whose spec resolves to *harness*."""
    location = f"{agent_id}/bundle"
    stores.artifact_store.put(location, _bundle(harness))
    stores.agent_store.create(agent_id, name, location)


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


def _build_app(stores: _Stores, *, auth: bool = False, permission_store=None) -> FastAPI:
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
        permission_store=permission_store,
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
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers=PERMISSION_TIERS_HEADERS
    ) as c:
        yield c


@pytest.fixture()
def multi_user_app(stores: _Stores) -> FastAPI:
    """Header-auth app, for ownership tests."""
    return _build_app(stores, auth=True)


@pytest_asyncio.fixture()
async def multi_user_client(multi_user_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """HTTP client wired to the header-auth app."""
    transport = httpx.ASGITransport(app=multi_user_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers=PERMISSION_TIERS_HEADERS
    ) as c:
        yield c


def _register_host(app: FastAPI, host_id: str) -> None:
    """Mark *host_id* online on this replica by registering a live connection."""
    app.state.host_registry.register(
        host_id=host_id,
        ws=AsyncMock(),
        hello=HostHelloFrame(version="test", frame_protocol_version=1, name="test"),
        owner=None,
    )


@pytest.mark.parametrize("surface", ["catalog", "create", "model-refresh"])
@pytest.mark.parametrize("status", ["ok", "failed"])
async def test_acp_delivery_metadata_without_manual_sync(
    client, app, stores, monkeypatch, surface, status
):
    stores.host_store.upsert_on_connect(HOST_A, "box-a", "local")
    stores.host_store.update_harness_readiness(HOST_A, {"acp:neutral": True})
    _register_host(app, HOST_A)
    proxy = AsyncMock(
        return_value={
            "status": status,
            "models": [{"id": "available"}] if status == "ok" else [],
            "error": "probe failed" if status == "failed" else None,
            "injects_prompt": False,
        }
    )
    monkeypatch.setattr("omnigent.server.routes.calling_defaults._proxy_model_options", proxy)
    monkeypatch.setattr("omnigent.server.routes.hosts._proxy_model_options", proxy)
    if surface == "catalog":
        response = await client.get("/v1/calling-defaults/catalogs")
        assert response.status_code == 200, response.text
        assert response.json()["rows"][0]["injects_prompt"] is False
    elif surface == "create":
        from starlette.requests import Request

        from omnigent.entities import Conversation
        from omnigent.server.routes._session_create_validation import session_prompt_warnings

        request = Request({"type": "http", "app": app})
        warnings = await session_prompt_warnings(
            request,
            Conversation(
                id="test",
                created_at=1,
                updated_at=1,
                root_conversation_id="test",
                host_id=HOST_A,
                session_prompt="Keep this",
            ),
            "acp:neutral",
        )
        assert warnings == ["session_prompt not sent: this agent builds its own prompt"]
    else:
        response = await client.get(
            f"/v1/hosts/{HOST_A}/harnesses/acp:neutral/model-options?refresh=force"
        )
        assert response.status_code == (200 if status == "ok" else 502), response.text
    assert stores.cache.list([HOST_A])[0]["injects_prompt"] is False
    proxy.assert_awaited_once()


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


async def test_catalogs_filter_reads_one_cached_row_without_probing(
    app: FastAPI,
    client: httpx.AsyncClient,
    stores: _Stores,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host / harness filter reads the cache only; another host's pending ACP probe waits."""
    stores.host_store.upsert_on_connect(HOST_A, "box-a", "local")
    stores.host_store.upsert_on_connect(HOST_B, "box-b", "local")
    stores.host_store.upsert_on_connect(HOST_FOREIGN, "box-x", "someone-else")
    stores.host_store.update_harness_readiness(HOST_B, {"acp:neutral": True})
    _register_host(app, HOST_A)
    _register_host(app, HOST_B)
    stores.cache.upsert(HOST_A, "codex", [{"id": "row-1", "model": "relay/model-x"}], 5)
    stores.cache.upsert(HOST_A, "claude-native", [{"id": "other-harness"}], 5)
    proxy = AsyncMock(return_value={"status": "ok", "models": [], "injects_prompt": False})
    monkeypatch.setattr("omnigent.server.routes.calling_defaults._proxy_model_options", proxy)

    resp = await client.get(
        "/v1/calling-defaults/catalogs", params={"host_id": HOST_A, "harness": "codex"}
    )
    assert resp.status_code == 200, resp.text
    assert [(row["host_id"], row["harness"], row["models"]) for row in resp.json()["rows"]] == [
        (HOST_A, "codex", [{"id": "row-1", "model": "relay/model-x"}])
    ]
    proxy.assert_not_awaited()

    foreign = await client.get("/v1/calling-defaults/catalogs", params={"host_id": HOST_FOREIGN})
    assert foreign.status_code == 404, foreign.text

    # The unfiltered read still fills B's first-use ACP metadata.
    full = await client.get("/v1/calling-defaults/catalogs")
    assert full.status_code == 200, full.text
    proxy.assert_awaited_once()
    assert proxy.await_args.kwargs["host_conn"].host_id == HOST_B


@pytest.mark.parametrize("seed", ["missing", "expired"])
async def test_agy_catalog_refreshes_without_manual_sync(client, app, stores, monkeypatch, seed):
    harness = "antigravity-native"
    stores.host_store.upsert_on_connect(HOST_A, "box-a", "local")
    stores.host_store.update_harness_readiness(HOST_A, {harness: True})
    _register_host(app, HOST_A)
    if seed == "expired":
        stores.cache.upsert(HOST_A, harness, [{"id": "retired"}], now_epoch() - 301)
    models = [{"id": model} for model in ("gemini-4-neon", "gemini-4-hydrogen", "gemini-4-carbon")]
    proxy = AsyncMock(return_value={"status": "ok", "models": models})
    monkeypatch.setattr("omnigent.server.routes.calling_defaults._proxy_model_options", proxy)
    params = {"host_id": HOST_A, "harness": harness}

    started_at = now_epoch()
    response = await client.get("/v1/calling-defaults/catalogs", params=params)
    assert response.status_code == 200, response.text
    row = response.json()["rows"][0]
    assert row["models"] == models
    assert row["fetched_at"] >= started_at
    assert row["stale"] is False
    repeated = await client.get("/v1/calling-defaults/catalogs", params=params)
    assert repeated.json()["rows"] == [row]
    proxy.assert_awaited_once()


@pytest.mark.parametrize("online", [True, False])
async def test_agy_catalog_preserves_old_rows_when_refresh_unavailable(
    client, app, stores, monkeypatch, online
):
    harness = "antigravity-native"
    stores.host_store.upsert_on_connect(HOST_A, "box-a", "local")
    stores.host_store.update_harness_readiness(HOST_A, {harness: True})
    if online:
        _register_host(app, HOST_A)
    fetched_at = now_epoch() - 301
    models = [{"id": "last-known"}]
    stores.cache.upsert(HOST_A, harness, models, fetched_at)
    proxy = AsyncMock(return_value={"status": "failed", "error": "probe unavailable"})
    monkeypatch.setattr("omnigent.server.routes.calling_defaults._proxy_model_options", proxy)

    response = await client.get(
        "/v1/calling-defaults/catalogs", params={"host_id": HOST_A, "harness": harness}
    )
    assert response.status_code == 200, response.text
    row = response.json()["rows"][0]
    assert row["models"] == models
    assert row["fetched_at"] == fetched_at
    assert row["stale"] is True
    assert row["error"] == ("probe unavailable" if online else None)
    assert proxy.await_count == int(online)
    assert (
        await client.get(
            "/v1/calling-defaults/catalogs", params={"host_id": HOST_A, "harness": harness}
        )
    ).json()["rows"] == [row]
    assert proxy.await_count == int(online)
    if online:
        recovered = [{"id": "available-again"}]
        proxy.return_value = {"status": "ok", "models": recovered}
        synced = await client.post(
            "/v1/calling-defaults/sync", json={"host_id": HOST_A, "harness": harness}
        )
        assert synced.json()["rows"][0]["models"] == recovered
        assert proxy.await_count == 2


async def test_slow_agy_refresh_does_not_hold_catalog_readers(client, app, stores, monkeypatch):
    harness = "antigravity-native"
    acp = "acp:example"
    stores.host_store.upsert_on_connect(HOST_A, "box-a", "local")
    stores.host_store.update_harness_readiness(HOST_A, {harness: True, acp: True})
    _register_host(app, HOST_A)
    fetched_at = now_epoch() - 301
    stores.cache.upsert(HOST_A, harness, [{"id": "last-known"}], fetched_at)
    stores.cache.upsert(HOST_A, "codex", [{"id": "codex-cached"}], now_epoch())
    started = asyncio.Event()
    finish = asyncio.Event()

    async def proxy(*, harness, **_kwargs):
        if harness == acp:
            return {"status": "ok", "models": [], "injects_prompt": True}
        assert harness == "antigravity-native"
        started.set()
        await finish.wait()
        return {"status": "ok", "models": [{"id": "current-model"}]}

    lookup = AsyncMock(side_effect=proxy)
    monkeypatch.setattr(calling_defaults_routes, "_proxy_model_options", lookup)
    monkeypatch.setattr(calling_defaults_routes, "_AGY_FILTERED_READ_WAIT_S", 0.01)
    params = {"host_id": HOST_A, "harness": harness}

    try:
        async with asyncio.timeout(2):
            full = await client.get("/v1/calling-defaults/catalogs")
            await started.wait()
            rows = {row["harness"]: row for row in full.json()["rows"]}
            assert rows["codex"]["models"] == [{"id": "codex-cached"}]
            assert rows[acp]["injects_prompt"] is True
            assert rows[harness]["stale"] is True

            filtered = await client.get("/v1/calling-defaults/catalogs", params=params)
            assert filtered.json()["rows"] == [rows[harness]]
            assert rows[harness]["models"] == [{"id": "last-known"}]
            assert rows[harness]["fetched_at"] == fetched_at
    finally:
        finish.set()
    monkeypatch.setattr(calling_defaults_routes, "_AGY_FILTERED_READ_WAIT_S", 2)
    synced = await client.get("/v1/calling-defaults/catalogs", params=params)
    row = synced.json()["rows"][0]
    assert row["models"] == [{"id": "current-model"}]
    assert row["stale"] is False
    assert [call.kwargs["harness"] for call in lookup.await_args_list].count(harness) == 1


async def test_same_agy_host_id_refreshes_each_workspace(client, app, stores, monkeypatch):
    harness = "antigravity-native"
    for workspace in (1, 2):
        with workspace_scope(workspace):
            stores.host_store.upsert_on_connect(HOST_A, "box-a", "local")
            stores.host_store.update_harness_readiness(HOST_A, {harness: True})
            _register_host(app, HOST_A)
    active = set()
    both_started = asyncio.Event()

    async def proxy(**_kwargs):
        workspace = current_workspace_id()
        active.add(workspace)
        if active == {1, 2}:
            both_started.set()
        await both_started.wait()
        return {"status": "ok", "models": [{"id": f"model-{workspace}"}]}

    monkeypatch.setattr(calling_defaults_routes, "_proxy_model_options", proxy)

    async def read(workspace):
        with workspace_scope(workspace):
            return await client.get(
                "/v1/calling-defaults/catalogs", params={"host_id": HOST_A, "harness": harness}
            )

    async with asyncio.timeout(2):
        first, second = await asyncio.gather(read(1), read(2))
    assert first.json()["rows"][0]["models"] == [{"id": "model-1"}]
    assert second.json()["rows"][0]["models"] == [{"id": "model-2"}]


async def test_resolve_exposes_unavailable_defaults(client, stores, monkeypatch):
    stores.host_store.upsert_on_connect(HOST_A, "box-a", "local")
    _seed_agent(stores, AGENT_ID, harness="codex")

    def unavailable(owner):
        raise OSError("preferences unavailable")

    monkeypatch.setattr(stores.prefs, "get", unavailable)
    response = await client.get(
        "/v1/calling-defaults/resolve", params={"host_id": HOST_A, "agent_id": AGENT_ID}
    )
    assert response.status_code == 200, response.text
    assert "storage_unavailable" in response.json()["warnings"][0]


@pytest.mark.parametrize("harness", ["codex", "claude-native", "claude-sdk"])
async def test_resolve_reads_the_master_table_through_the_agents_harness(
    client: httpx.AsyncClient, stores: _Stores, harness: str
) -> None:
    """No project: the master table applies under the agent's resolved harness."""
    stores.host_store.upsert_on_connect(HOST_A, "box-a", "local")
    _seed_agent(stores, AGENT_ID, harness=harness)
    stores.prefs.patch_namespace(
        "local",
        "calling_defaults",
        {
            HOST_A: {
                harness: {
                    "model": "gpt-6-sol",
                    "effort": "high",
                    "speed": "fast",
                    "permission": "auto" if harness.startswith("claude") else "approve-for-me",
                }
            }
        },
    )
    stores.cache.upsert(
        HOST_A,
        harness,
        [
            {
                "id": "gpt-6-sol",
                "supportedReasoningEfforts": ["high"],
                **(
                    {"serviceTiers": [{"id": "priority", "name": "Fast"}]}
                    if harness == "codex"
                    else {}
                ),
            }
        ],
        10,
    )

    resp = await client.get(
        "/v1/calling-defaults/resolve",
        params={"host_id": HOST_A, "agent_id": AGENT_ID},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["agent_id"] == AGENT_ID
    assert body["harness"] == harness
    assert body["model"] == "gpt-6-sol"
    assert body["effort"] == "high"
    assert body["speed"] == "fast"
    assert body["permission"] == ("auto" if harness.startswith("claude") else "approve-for-me")
    assert body["sources"] == {
        "agent": "explicit",
        "model": "master",
        "effort": "master",
        "speed": "master",
        "permission": "master",
    }
    assert body["level"] == dict.fromkeys(("model", "effort", "speed", "permission"), "cell")
    assert body["problems"] == []


async def test_resolve_reports_host_level_and_unsupported_host_falls_back(
    client: httpx.AsyncClient,
    stores: _Stores,
) -> None:
    stores.host_store.upsert_on_connect(HOST_A, "box-a", "local")
    _seed_agent(stores, AGENT_ID, harness="codex")
    stores.prefs.patch_namespace(
        "local",
        "calling_defaults",
        {
            "*": {"codex": {"permission": "approve-for-me"}},
            HOST_A: {"*": {"permission": "read-only", "host_wins": True}},
        },
    )
    params = {"host_id": HOST_A, "agent_id": AGENT_ID}
    result = await client.get("/v1/calling-defaults/resolve", params=params)
    assert result.status_code == 200
    assert (
        result.json()["permission"],
        result.json()["sources"]["permission"],
        result.json()["level"]["permission"],
    ) == ("read-only", "master", "host")
    stores.prefs.patch_namespace(
        "local",
        "calling_defaults",
        {
            HOST_A: {"*": {"permission": "edit-automatic", "host_wins": True}},
        },
    )
    result = await client.get("/v1/calling-defaults/resolve", params=params)
    assert result.status_code == 200
    assert (result.json()["permission"], result.json()["level"]["permission"]) == (
        "approve-for-me",
        "row",
    )


async def test_resolve_a_named_member_with_an_explicit_harness(
    client: httpx.AsyncClient, stores: _Stores
) -> None:
    """A built-in joint member is named, not an id: the chain still resolves."""
    stores.host_store.upsert_on_connect(HOST_A, "box-a", "local")
    stores.prefs.patch_namespace(
        "local",
        "calling_defaults",
        {HOST_A: {"codex-native": {"model": "gpt-6-astra", "effort": "high"}}},
    )

    resp = await client.get(
        "/v1/calling-defaults/resolve",
        params={"host_id": HOST_A, "agent_id": "researcher", "harness": "codex-native"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["model"] == "gpt-6-astra"
    assert body["effort"] == "high"


async def test_resolve_project_default_keeps_catalog_miss_unknown_and_warns(
    client: httpx.AsyncClient, stores: _Stores
) -> None:
    """A catalog miss warns and keeps the saved model; an errored catalog is unknown."""
    stores.host_store.upsert_on_connect(HOST_A, "box-a", "local")
    _seed_agent(stores, AGENT_ID, harness="codex")
    project = (
        await client.post(
            "/v1/projects",
            json={
                "name": "P",
                "config": {
                    "calling_defaults": {
                        HOST_A: {
                            "agent_id": AGENT_ID,
                            "harnesses": {"codex": {"model": "gpt-6-sol", "effort": "high"}},
                        }
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

    params = {
        "project_id": project["id"],
        "host_id": HOST_A,
        "agent_id": AGENT_ID,
        "harness": "codex",
    }
    body = (await client.get("/v1/calling-defaults/resolve", params=params)).json()
    assert body["model"] == "gpt-6-sol"
    assert body["sources"]["model"] == "project_host"
    assert body["problems"] == []
    assert body["notes"][0]["outcome"] == "missing"
    assert body["notes"][0]["saved"] == "gpt-6-sol"
    assert any("passing it to the host unchanged" in warning for warning in body["warnings"])

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


async def test_resolve_reports_default_agent_readiness_and_library_problems(
    client: httpx.AsyncClient, stores: _Stores
) -> None:
    """The preview reports the create helper's K5 checks as problems.

    A default agent whose host reports its harness unready, and a default
    naming a saved joint agent, both surface here instead of raising so the
    web picker sees what an agent-omitted create would refuse.
    """
    stores.host_store.upsert_on_connect(HOST_A, "box-a", "local")
    stores.host_store.update_harness_readiness(HOST_A, {"codex": False})
    _seed_agent(stores, AGENT_ID, harness="codex")

    project = (
        await client.post(
            "/v1/projects",
            json={
                "name": "Unready",
                "config": {"calling_defaults": {HOST_A: {"agent_id": AGENT_ID}}},
            },
        )
    ).json()
    body = (
        await client.get(
            "/v1/calling-defaults/resolve",
            params={"project_id": project["id"], "host_id": HOST_A},
        )
    ).json()
    assert body["agent_id"] == AGENT_ID
    (problem,) = body["problems"]
    assert problem["field"] == "agent"
    assert problem["setting"] == "project_host"
    assert "not ready on host 'box-a'" in problem["message"]
    assert "harness 'codex' is False" in problem["message"]

    library_project = (
        await client.post(
            "/v1/projects",
            json={
                "name": "Joint",
                "config": {"calling_defaults": {HOST_A: {"agent_id": "ca_polly"}}},
            },
        )
    ).json()
    body = (
        await client.get(
            "/v1/calling-defaults/resolve",
            params={"project_id": library_project["id"], "host_id": HOST_A},
        )
    ).json()
    assert body["agent_id"] == "ca_polly"
    (problem,) = body["problems"]
    assert problem["field"] == "agent"
    assert "ca_polly" in problem["message"]
    assert "saved joint agent" in problem["message"]
    assert "server-side creates" in problem["message"]


@pytest.mark.asyncio
async def test_acp_defaults_keep_agent_namespace_and_literal_effort(
    client: httpx.AsyncClient, stores: _Stores
) -> None:
    stores.host_store.upsert_on_connect(HOST_A, "box-a", "local")
    stores.prefs.patch_namespace(
        "local", "calling_defaults", {HOST_A: {"acp:neutral": {"effort": "default"}}}
    )
    response = await client.get(
        "/v1/calling-defaults/resolve", params={"host_id": HOST_A, "harness": "acp:neutral"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["harness"] == "acp:neutral"
    assert response.json()["effort"] == "default"


@pytest.mark.asyncio
async def test_acp_session_patch_preserves_literal_values_and_null_reset(
    client: httpx.AsyncClient, stores: _Stores
) -> None:
    _seed_agent(stores, AGENT_ID, "acp:neutral")
    created = await client.post("/v1/sessions", json={"agent_id": AGENT_ID})
    assert created.status_code == 201, created.text
    sid = created.json()["id"]
    for effort in ("default", "off", "deliberate", None):
        response = await client.patch(
            f"/v1/sessions/{sid}", json={"reasoning_effort": effort, "silent": True}
        )
        assert response.status_code == 200, response.text
        assert response.json()["reasoning_effort"] == effort


async def test_acp_host_tier_defaults_on_delegated_create(client, app, stores):
    from omnigent.server.session_open_rate import CREATE_ORIGIN_AGENT, CREATE_ORIGIN_HEADER
    from omnigent.session_default_modes import ACP_SESSION_MODE_LABEL_KEY

    stores.host_store.upsert_on_connect(HOST_A, "box-a", "local")
    stores.host_store.update_harness_readiness(HOST_A, {"acp": True, "acp:ag": True})
    _register_host(app, HOST_A)
    _seed_agent(stores, AGENT_ID, "acp:ag")
    app.state.host_registry.get(HOST_A).hello.capabilities.append("agy_acp_permissions:acp:ag")
    conversations = SqlAlchemyConversationStore(stores.agent_store.storage_location)
    parent = conversations.create_conversation(
        agent_id=AGENT_ID, host_id=HOST_A, workspace="/neutral/project", runner_id="test-runner"
    )
    request = {"agent_id": AGENT_ID, "parent_session_id": parent.id}
    stores.cache.upsert(
        HOST_A,
        "acp:ag",
        [],
        1,
        permission_options=[{"value": "plan", "label": "Plan", "concept": "read-only"}],
    )
    stores.prefs.patch_namespace(
        "local", "calling_defaults", {"*": {"*": {"permission": "read-only"}}}
    )
    legacy = await client.post("/v1/sessions", json=request)
    assert legacy.status_code == 201, legacy.text
    assert legacy.json()["permission_tier"] is None
    assert legacy.json()["labels"][ACP_SESSION_MODE_LABEL_KEY] == "plan"

    delegated = await client.post(
        "/v1/sessions",
        json=request,
        headers={**PERMISSION_TIERS_HEADERS, CREATE_ORIGIN_HEADER: CREATE_ORIGIN_AGENT},
    )
    assert delegated.status_code == 201, delegated.text
    assert delegated.json()["permission_tier"] == "plan"
    assert delegated.json()["permission_tier_state"] == "blocked"

    connection = app.state.host_registry.get(HOST_A)
    connection.hello.acp_permission_tiers = {"acp:ag": "auto"}
    defaulted = await client.post("/v1/sessions", json=request, headers=PERMISSION_TIERS_HEADERS)
    assert defaulted.status_code == 201, defaulted.text
    assert defaulted.json()["permission_tier"] == "auto"

    explicit = await client.post(
        "/v1/sessions",
        json={**request, "permission_tier": "accept-edits"},
        headers=PERMISSION_TIERS_HEADERS,
    )
    assert explicit.status_code == 201, explicit.text
    assert explicit.json()["permission_tier"] == "accept-edits"
    assert explicit.json()["permission_tier_state"] == "blocked"

    # User create accepts `full` for ACP; agent-originated create still refuses it.
    explicit_full = await client.post(
        "/v1/sessions",
        json={**request, "permission_tier": "full"},
        headers=PERMISSION_TIERS_HEADERS,
    )
    assert explicit_full.status_code == 201, explicit_full.text
    assert explicit_full.json()["permission_tier"] == "full"

    delegated_full = await client.post(
        "/v1/sessions",
        json={**request, "permission_tier": "full"},
        headers={**PERMISSION_TIERS_HEADERS, CREATE_ORIGIN_HEADER: CREATE_ORIGIN_AGENT},
    )
    assert delegated_full.status_code == 400, delegated_full.text

    # Host config default `full` applies on user create.
    connection.hello.acp_permission_tiers = {"acp:ag": "full"}
    defaulted_full = await client.post(
        "/v1/sessions", json=request, headers=PERMISSION_TIERS_HEADERS
    )
    assert defaulted_full.status_code == 201, defaulted_full.text
    assert defaulted_full.json()["permission_tier"] == "full"

    # Calling default `host-tier:full` resolves and applies on create, and validates for ACP only.
    from omnigent.calling_defaults import validate_project_calling_defaults
    from omnigent.errors import OmnigentError

    assert validate_project_calling_defaults(
        {HOST_A: {"harnesses": {"acp:ag": {"permission": "host-tier:full"}}}}
    ) == {HOST_A: {"harnesses": {"acp:ag": {"permission": "host-tier:full"}}}}
    with pytest.raises(OmnigentError):
        validate_project_calling_defaults(
            {HOST_A: {"harnesses": {"codex-native": {"permission": "host-tier:full"}}}}
        )
    connection.hello.acp_permission_tiers = {}
    stores.prefs.patch_namespace(
        "local", "calling_defaults", {HOST_A: {"acp:ag": {"permission": "host-tier:full"}}}
    )
    calling_full = await client.post(
        "/v1/sessions", json=request, headers=PERMISSION_TIERS_HEADERS
    )
    assert calling_full.status_code == 201, calling_full.text
    assert calling_full.json()["permission_tier"] == "full"

    # Bundle upload create rejects explicit `full` on agent-originated creates too.
    agent_headers = {**PERMISSION_TIERS_HEADERS, CREATE_ORIGIN_HEADER: CREATE_ORIGIN_AGENT}
    bundle_denied = await client.post(
        "/v1/sessions",
        data={
            "metadata": json.dumps(
                {"host_id": HOST_A, "workspace": "/neutral/project", "permission_tier": "full"}
            )
        },
        files={"bundle": ("agent.tar.gz", _bundle("acp:ag"), "application/gzip")},
        headers=agent_headers,
    )
    assert bundle_denied.status_code == 400, bundle_denied.text

    # Tiered parents never propagate `full` or widen past their tier despite a `full` default.
    from omnigent.server.permission_tiers import (
        PERMISSION_CAPABILITY_LABEL_KEY,
        PERMISSION_CAPABILITY_VALUE,
        ConfirmedPermissionState,
    )

    conversations = SqlAlchemyConversationStore(stores.agent_store.storage_location)
    for parent_tier, expected_child_tier in (("full", "auto"), ("plan", "plan")):
        tiered_parent = conversations.create_conversation(
            agent_id=AGENT_ID,
            host_id=HOST_A,
            workspace="/neutral/project",
            runner_id="test-runner",
            labels={
                **ConfirmedPermissionState(parent_tier, 1, "ready").labels("acp:ag"),
                PERMISSION_CAPABILITY_LABEL_KEY: PERMISSION_CAPABILITY_VALUE,
            },
        )
        child = await client.post(
            "/v1/sessions",
            json={**request, "parent_session_id": tiered_parent.id},
            headers=agent_headers,
        )
        assert child.status_code == 201, child.text
        assert child.json()["permission_tier"] == expected_child_tier

    # User bundle upload also picks up the saved `host-tier:full` calling default.
    bundle_default = await client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({"host_id": HOST_A, "workspace": "/neutral/project"})},
        files={"bundle": ("agent.tar.gz", _bundle("acp:ag"), "application/gzip")},
        headers=PERMISSION_TIERS_HEADERS,
    )
    assert bundle_default.status_code == 201, bundle_default.text
    bundle_snapshot = await client.get(f"/v1/sessions/{bundle_default.json()['session_id']}")
    assert bundle_snapshot.json()["permission_tier"] == "full"

    # An untiered orchestrator omitting `permission_tier` honors the user's saved `full` default.
    _seed_agent(stores, "parent-claude", "claude-native", name="parent-claude-agent")
    untiered_parent = conversations.create_conversation(
        agent_id="parent-claude",
        host_id=HOST_A,
        workspace="/neutral/project",
        runner_id="test-runner",
    )
    orchestrated = await client.post(
        "/v1/sessions",
        json={**request, "parent_session_id": untiered_parent.id},
        headers=agent_headers,
    )
    assert orchestrated.status_code == 201, orchestrated.text
    assert orchestrated.json()["permission_tier"] == "full"


@pytest.mark.parametrize("bundled", [False, True], ids=["registered", "upload"])
@pytest.mark.parametrize(
    "mode,calling_default,expected",
    [
        ("plan", False, "plan"),
        ("review", False, "plan"),
        (None, True, "plan"),
        (None, False, "auto"),
        (None, False, None),
    ],
)
async def test_delegated_acp_respects_unmanaged_source_restrictions(
    client, app, stores, bundled, mode, calling_default, expected
):
    from omnigent.sdk_permission_modes import ACP_SESSION_MODE_LABEL_KEY
    from omnigent.server.session_open_rate import CREATE_ORIGIN_AGENT, CREATE_ORIGIN_HEADER

    harness = "acp:opencode" if expected is None else "acp:ag"
    _seed_agent(stores, AGENT_ID, harness)
    stores.host_store.upsert_on_connect(HOST_A, "box-a", "local")
    stores.host_store.update_harness_readiness(HOST_A, {"acp": True, harness: True})
    _register_host(app, HOST_A)
    app.state.host_registry.get(HOST_A).hello.capabilities.append("agy_acp_permissions:acp:ag")
    if mode == "review":
        stores.cache.upsert(
            HOST_A,
            harness,
            [],
            1,
            permission_options=[{"value": "review", "label": "Review", "concept": "read-only"}],
        )
    if calling_default:
        stores.prefs.patch_namespace(
            "local", "calling_defaults", {"*": {"*": {"permission": "read-only"}}}
        )
    conversations = SqlAlchemyConversationStore(stores.agent_store.storage_location)
    parent = conversations.create_conversation(
        agent_id=AGENT_ID,
        host_id=HOST_A,
        workspace="/neutral/project",
        runner_id="test-runner",
        labels={ACP_SESSION_MODE_LABEL_KEY: mode} if mode else {},
    )
    headers = {**PERMISSION_TIERS_HEADERS, CREATE_ORIGIN_HEADER: CREATE_ORIGIN_AGENT}

    async def create(**overrides):
        metadata = {"parent_session_id": parent.id, **overrides}
        if bundled:
            return await client.post(
                "/v1/sessions",
                data={"metadata": json.dumps(metadata)},
                files={"bundle": ("agent.tar.gz", _bundle(harness), "application/gzip")},
                headers=headers,
            )
        return await client.post(
            "/v1/sessions", json={"agent_id": AGENT_ID, **metadata}, headers=headers
        )

    created = await create()
    assert created.status_code == 201, created.text
    snapshot = created
    if bundled:
        snapshot = await client.get(f"/v1/sessions/{created.json()['session_id']}")
    assert snapshot.json()["permission_tier"] == expected
    if expected == "plan":
        denied = await create(permission_tier="auto")
        assert denied.status_code == 403, denied.text


async def test_delegated_acp_bundle_inherits_the_creator_tier(client, app, stores, monkeypatch):
    from omnigent.server.permission_tiers import (
        PERMISSION_CAPABILITY_LABEL_KEY,
        PERMISSION_CAPABILITY_VALUE,
        ConfirmedPermissionState,
    )
    from omnigent.server.routes.sessions import routes_core
    from omnigent.server.session_open_rate import CREATE_ORIGIN_AGENT, CREATE_ORIGIN_HEADER

    _seed_agent(stores, AGENT_ID, "antigravity-native")
    stores.host_store.upsert_on_connect(HOST_A, "box-a", "local")
    _register_host(app, HOST_A)
    app.state.host_registry.get(HOST_A).hello.capabilities.append("agy_acp_permissions:acp:ag")
    conversations = SqlAlchemyConversationStore(stores.agent_store.storage_location)
    parent = conversations.create_conversation(
        agent_id=AGENT_ID,
        host_id=HOST_A,
        workspace="/neutral/project",
        runner_id="test-runner",
        labels={
            **ConfirmedPermissionState("plan", 1, "ready").labels("antigravity-native"),
            PERMISSION_CAPABILITY_LABEL_KEY: PERMISSION_CAPABILITY_VALUE,
        },
    )
    metadata = {"parent_session_id": parent.id}
    headers = {**PERMISSION_TIERS_HEADERS, CREATE_ORIGIN_HEADER: CREATE_ORIGIN_AGENT}
    bundle = {"bundle": ("agent.tar.gz", _bundle("acp:ag"), "application/gzip")}
    created = await client.post(
        "/v1/sessions", data={"metadata": json.dumps(metadata)}, files=bundle, headers=headers
    )
    assert created.status_code == 201, created.text
    snapshot = await client.get(f"/v1/sessions/{created.json()['session_id']}")
    assert snapshot.json()["permission_tier"] == "plan"
    assert snapshot.json()["permission_tier_state"] == "blocked"

    denied = await client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({**metadata, "permission_tier": "auto"})},
        files=bundle,
        headers=headers,
    )
    assert denied.status_code == 403, denied.text

    original_member_labels = routes_core._member_snapshot_labels

    async def change_source_tier(*args, **kwargs):
        conversations.set_labels(
            parent.id, ConfirmedPermissionState("ask", 2, "blocked").labels("antigravity-native")
        )
        return await original_member_labels(*args, **kwargs)

    monkeypatch.setattr(routes_core, "_member_snapshot_labels", change_source_tier)
    stale = await client.post(
        "/v1/sessions", data={"metadata": json.dumps(metadata)}, files=bundle, headers=headers
    )
    assert stale.status_code == 400, stale.text
    assert "Source runtime permission state is not ready" in stale.text


async def test_named_acp_child_uses_its_own_host_tier(client, app, stores):
    from omnigent.server.session_open_rate import CREATE_ORIGIN_AGENT, CREATE_ORIGIN_HEADER
    from tests.server.helpers import create_test_agent

    stores.host_store.upsert_on_connect(HOST_A, "box-a", "local")
    stores.host_store.update_harness_readiness(HOST_A, {"acp": True, "acp:neutral": True})
    _register_host(app, HOST_A)
    app.state.host_registry.get(HOST_A).hello.acp_permission_tiers = {"acp:neutral": "plan"}
    agent = await create_test_agent(
        client,
        sub_agents=[
            {
                "name": "worker",
                "prompt": "Read the workspace",
                "executor": {"type": "omnigent", "config": {"harness": "acp:neutral"}},
            }
        ],
    )
    conversations = SqlAlchemyConversationStore(stores.agent_store.storage_location)
    parent = conversations.create_conversation(
        agent_id=agent["id"], host_id=HOST_A, workspace="/neutral/project", runner_id="test-runner"
    )
    created = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "sub_agent_name": "worker", "parent_session_id": parent.id},
        headers={**PERMISSION_TIERS_HEADERS, CREATE_ORIGIN_HEADER: CREATE_ORIGIN_AGENT},
    )
    assert created.status_code == 201, created.text
    assert created.json()["permission_tier"] == "plan"


@pytest.mark.parametrize("harness", ["acp:ag", "pi", "pi-native", "antigravity-native"])
async def test_agent_created_child_cannot_widen_the_source_tier(client, app, stores, harness):
    from omnigent.server.permission_tiers import (
        PERMISSION_CAPABILITY_LABEL_KEY,
        PERMISSION_CAPABILITY_VALUE,
        ConfirmedPermissionState,
    )
    from omnigent.server.session_open_rate import CREATE_ORIGIN_AGENT, CREATE_ORIGIN_HEADER

    _seed_agent(stores, AGENT_ID, harness)
    conversations = SqlAlchemyConversationStore(stores.agent_store.storage_location)
    labels = ConfirmedPermissionState("plan", 2, "ready").labels(harness)
    labels[PERMISSION_CAPABILITY_LABEL_KEY] = PERMISSION_CAPABILITY_VALUE
    placement = {}
    if harness == "acp:ag":
        stores.host_store.upsert_on_connect(HOST_A, "box-a", "local")
        _register_host(app, HOST_A)
        app.state.host_registry.get(HOST_A).hello.capabilities.append("agy_acp_permissions:acp:ag")
        placement = {
            "host_id": HOST_A,
            "workspace": "/neutral/project",
            "runner_id": "test-runner",
        }
    parent = conversations.create_conversation(agent_id=AGENT_ID, labels=labels, **placement)

    request = {"agent_id": AGENT_ID, "parent_session_id": parent.id}
    agent_headers = {**PERMISSION_TIERS_HEADERS, CREATE_ORIGIN_HEADER: CREATE_ORIGIN_AGENT}
    denied = await client.post(
        "/v1/sessions", json={**request, "permission_tier": "auto"}, headers=agent_headers
    )
    assert denied.status_code == 403, denied.text
    allowed = await client.post("/v1/sessions", json=request, headers=agent_headers)
    assert allowed.status_code == 201, allowed.text
    assert allowed.json()["permission_tier"] == "plan"
    user_selected = await client.post(
        "/v1/sessions",
        json={**request, "permission_tier": "auto"},
        headers=PERMISSION_TIERS_HEADERS,
    )
    assert user_selected.status_code == 201, user_selected.text
    assert user_selected.json()["permission_tier"] == "auto"


async def test_expired_acp_model_catalog_refreshes_once_and_preserves_last_success(
    client, app, stores, monkeypatch
):
    import asyncio

    from tests._helpers.polling import poll_until_async

    stores.host_store.upsert_on_connect(HOST_A, "box-a", "local")
    stores.host_store.update_harness_readiness(HOST_A, {"acp:neutral": True})
    _register_host(app, HOST_A)
    previous = [{"id": "previous-model"}]
    stores.cache.upsert(HOST_A, "acp:neutral", previous, 100)
    started, release = asyncio.Event(), asyncio.Event()

    async def failed_probe(**kwargs):
        assert kwargs["refresh"] == "force"
        started.set()
        await release.wait()
        return {"status": "failed", "error": "ACP probe timed out"}

    proxy = AsyncMock(side_effect=failed_probe)
    monkeypatch.setattr("omnigent.server.routes.calling_defaults._proxy_model_options", proxy)
    monkeypatch.setattr("omnigent.server.routes.calling_defaults.now_epoch", lambda: 1000)
    params = {"host_id": HOST_A, "harness": "acp:neutral"}
    try:
        first = await client.get("/v1/calling-defaults/catalogs", params=params)
        row = first.json()["rows"][0]
        assert row["stale"] is True
        assert row["models"] == previous
        await asyncio.wait_for(started.wait(), timeout=2)
        second = await client.get("/v1/calling-defaults/catalogs", params=params)
        assert second.json()["rows"][0]["models"] == previous
        proxy.assert_awaited_once()
    finally:
        release.set()
    await poll_until_async(
        lambda: stores.cache.list([HOST_A])[0]["error"] is not None, timeout_s=2
    )
    row = stores.cache.list([HOST_A])[0]
    assert row["models"] == previous
    assert row["fetched_at"] == 100
    assert row["error"] == "ACP probe timed out"


@pytest.mark.parametrize("surface", ["sync", "model-refresh"])
async def test_empty_acp_refresh_cannot_replace_the_server_catalog(
    client, app, stores, monkeypatch, surface
):
    harness = "acp:neutral"
    stores.host_store.upsert_on_connect(HOST_A, "box-a", "local")
    stores.host_store.update_harness_readiness(HOST_A, {harness: True})
    _register_host(app, HOST_A)
    previous = [{"id": "previous-model"}]
    previous_modes = [{"value": "default", "label": "Default"}]
    stores.cache.upsert(HOST_A, harness, previous, 100, permission_options=previous_modes)
    proxy = AsyncMock(return_value={"status": "ok", "models": [], "injects_prompt": False})
    monkeypatch.setattr("omnigent.server.routes.calling_defaults._proxy_model_options", proxy)
    monkeypatch.setattr("omnigent.server.routes.hosts._proxy_model_options", proxy)

    if surface == "sync":
        response = await client.post(
            "/v1/calling-defaults/sync", json={"host_id": HOST_A, "harness": harness}
        )
        assert response.status_code == 200, response.text
    else:
        response = await client.get(
            f"/v1/hosts/{HOST_A}/harnesses/{harness}/model-options?refresh=force"
        )
        assert response.status_code == 502, response.text
        assert response.json()["detail"] == "ACP probe returned an empty model catalog"
    row = next(row for row in stores.cache.list([HOST_A]) if row["harness"] == harness)
    assert row["models"] == previous
    assert row["permission_options"] == previous_modes
    assert row["fetched_at"] == 100
    assert row["error"] == "ACP probe returned an empty model catalog"


async def test_acp_modes_sync_resolve_create_and_offline_patch(client, app, stores, monkeypatch):
    from omnigent.sdk_permission_modes import ACP_SESSION_MODE_LABEL_KEY

    stores.host_store.upsert_on_connect(HOST_A, "box-a", "local")
    stores.host_store.update_harness_readiness(HOST_A, {"acp": True, "acp:neutral": True})
    _register_host(app, HOST_A)
    options = [
        {"value": "review", "label": "Review"},
        {"value": "go", "label": "Go", "concept": "automatic"},
    ]
    proxy = AsyncMock(
        return_value={
            "status": "ok",
            "models": [{"id": "model-a", "isDefault": True, "start_efforts": ["high"]}],
            "permission_options": options,
        }
    )
    monkeypatch.setattr("omnigent.server.routes.calling_defaults._proxy_model_options", proxy)
    synced = await client.post(
        "/v1/calling-defaults/sync", json={"host_id": HOST_A, "harness": "acp:neutral"}
    )
    assert synced.status_code == 200, synced.text
    assert synced.json()["rows"][0]["permission_options"] == options
    assert proxy.await_args.kwargs["refresh"] == "force"
    stores.prefs.patch_namespace(
        "local", "calling_defaults", {"*": {"*": {"permission": "automatic"}}}
    )
    resolved = await client.get(
        "/v1/calling-defaults/resolve", params={"host_id": HOST_A, "harness": "acp:neutral"}
    )
    assert resolved.json()["permission"] == "go", resolved.text
    _seed_agent(stores, AGENT_ID, "acp:neutral")
    conversations = SqlAlchemyConversationStore(stores.agent_store.storage_location)
    parent = conversations.create_conversation(
        agent_id=AGENT_ID, host_id=HOST_A, workspace="/neutral/project", runner_id="test-runner"
    )
    created = await client.post(
        "/v1/sessions",
        json={"agent_id": AGENT_ID, "parent_session_id": parent.id, "permission_mode": "review"},
    )
    assert created.status_code == 201, created.text
    sid = created.json()["id"]
    assert created.json()["labels"][ACP_SESSION_MODE_LABEL_KEY] == "review"
    changed = await client.patch(f"/v1/sessions/{sid}", json={"permission_mode": "go"})
    assert changed.status_code == 200, changed.text
    assert changed.json()["labels"][ACP_SESSION_MODE_LABEL_KEY] == "go"
    refused = await client.patch(f"/v1/sessions/{sid}", json={"permission_mode": "plan"})
    assert refused.status_code == 400, refused.text
    assert "Press Sync models" in refused.text
    assert conversations.get_conversation(sid).labels[ACP_SESSION_MODE_LABEL_KEY] == "go"


async def test_acp_unclassified_modes_never_supply_permission_totals(client, stores):
    stores.host_store.upsert_on_connect(HOST_A, "box-a", "local")
    stores.cache.upsert(
        HOST_A,
        "acp:neutral",
        [],
        1,
        permission_options=[
            {"value": "plan", "label": "Plan"},
            {"value": "default", "label": "Default"},
        ],
    )
    stores.prefs.patch_namespace(
        "local", "calling_defaults", {"*": {"*": {"permission": "read-only"}}}
    )
    params = {"host_id": HOST_A, "harness": "acp:neutral"}
    result = await client.get("/v1/calling-defaults/resolve", params=params)
    assert result.json()["permission"] is None
    stores.prefs.patch_namespace(
        "local", "calling_defaults", {HOST_A: {"acp:neutral": {"permission": "plan"}}}
    )
    result = await client.get("/v1/calling-defaults/resolve", params=params)
    assert result.json()["permission"] == "plan"


async def test_acp_mode_label_survives_fork_and_continue(client, stores):
    from omnigent.sdk_permission_modes import ACP_SESSION_MODE_LABEL_KEY

    _seed_agent(stores, AGENT_ID, "acp:neutral")
    conversations = SqlAlchemyConversationStore(stores.agent_store.storage_location)
    source = conversations.create_conversation(
        agent_id=AGENT_ID, labels={ACP_SESSION_MODE_LABEL_KEY: "review"}
    )
    for operation in ("fork", "continue"):
        if operation == "continue":
            archived = await client.patch(f"/v1/sessions/{source.id}", json={"archived": True})
            assert archived.status_code == 200, archived.text
        response = await client.post(f"/v1/sessions/{source.id}/{operation}", json={})
        assert response.status_code == 201, response.text
        assert response.json()["labels"][ACP_SESSION_MODE_LABEL_KEY] == "review"


@pytest.mark.parametrize("operation", ["fork", "continue", "switch"])
async def test_managed_acp_derivation_builds_fresh_permission_state(client, stores, operation):
    from omnigent.server.permission_tiers import (
        PERMISSION_CAPABILITY_LABEL_KEY,
        PERMISSION_CAPABILITY_VALUE,
        PERMISSION_RUNTIME_ID_LABEL_KEY,
        PERMISSION_STATE_LABEL_KEY,
        ConfirmedPermissionState,
        read_permission_state,
    )

    harness = "acp:neutral"
    _seed_agent(stores, AGENT_ID, harness)
    conversations = SqlAlchemyConversationStore(stores.agent_store.storage_location)
    labels = ConfirmedPermissionState("plan", 7, "ready").labels(harness)
    labels[PERMISSION_CAPABILITY_LABEL_KEY] = PERMISSION_CAPABILITY_VALUE
    labels[PERMISSION_RUNTIME_ID_LABEL_KEY] = "source-runtime"
    source = conversations.create_conversation(agent_id=AGENT_ID, labels=labels)
    body = {}
    if operation == "switch":
        _seed_agent(stores, HOST_B, "codex", name="other-agent")
        body = {"agent_id": HOST_B}
    if operation == "continue":
        archived = await client.patch(
            f"/v1/sessions/{source.id}", json={"archived": True}, headers=PERMISSION_TIERS_HEADERS
        )
        assert archived.status_code == 200, archived.text
    response = await client.post(
        f"/v1/sessions/{source.id}/{'continue' if operation == 'continue' else 'fork'}",
        json=body,
        headers=PERMISSION_TIERS_HEADERS,
    )
    assert response.status_code == 201, response.text
    derived = conversations.get_conversation(response.json()["id"])
    assert PERMISSION_RUNTIME_ID_LABEL_KEY not in derived.labels
    assert PERMISSION_CAPABILITY_LABEL_KEY not in derived.labels
    if operation == "switch":
        assert PERMISSION_STATE_LABEL_KEY not in derived.labels
        assert response.json()["permission_tier"] is None
    else:
        assert read_permission_state(derived.labels, harness=harness) == ConfirmedPermissionState(
            "plan", 1, "blocked"
        )


async def test_acp_mode_unreported_by_an_older_host_is_refused(client, stores):
    _seed_agent(stores, AGENT_ID, "acp:neutral")
    stores.host_store.upsert_on_connect(HOST_A, "box-a", "local")
    stores.cache.upsert(HOST_A, "acp:neutral", [{"id": "model-a"}], 1)
    conversations = SqlAlchemyConversationStore(stores.agent_store.storage_location)
    parent = conversations.create_conversation(
        agent_id=AGENT_ID, host_id=HOST_A, workspace="/neutral/project", runner_id="test-runner"
    )
    response = await client.post(
        "/v1/sessions",
        json={"agent_id": AGENT_ID, "parent_session_id": parent.id, "permission_mode": "plan"},
    )
    assert response.status_code == 400, response.text
    assert "Press Sync models" in response.text


@pytest.mark.parametrize(
    "key",
    [
        "omnigent.acp.session_mode",
        "OMNIGENT.ACP.SESSION_MODE",
        "omnigent.acp.session_mode_selection",
        "OMNIGENT.ACP.SESSION_MODE_SELECTION",
    ],
)
async def test_acp_permission_label_cannot_bypass_selection_validation(client, stores, key):
    _seed_agent(stores, AGENT_ID, "acp:neutral")
    created = await client.post("/v1/sessions", json={"agent_id": AGENT_ID, "labels": {key: "go"}})
    assert created.status_code == 400, created.text
    conversations = SqlAlchemyConversationStore(stores.agent_store.storage_location)
    conv = conversations.create_conversation(agent_id=AGENT_ID)
    changed = await client.patch(f"/v1/sessions/{conv.id}", json={"labels": {key: "go"}})
    assert changed.status_code == 400, changed.text
    assert "use permission_mode" in changed.text
    assert not conversations.get_conversation(conv.id).labels.get("omnigent.acp.session_mode")


async def test_acp_permission_editor_cannot_set_internal_label(stores):
    from omnigent.server.auth import LEVEL_EDIT
    from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

    conv = SqlAlchemyConversationStore(stores.agent_store.storage_location).create_conversation(
        harness_override="acp:neutral"
    )
    permissions = SqlAlchemyPermissionStore(stores.agent_store.storage_location)
    permissions.grant(BOB, conv.id, LEVEL_EDIT)
    app = _build_app(stores, auth=True, permission_store=permissions)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers=PERMISSION_TIERS_HEADERS,
    ) as editor:
        for body, status in (
            ({"permission_mode": "go"}, 403),
            ({"labels": {"omnigent.acp.session_mode": "go"}}, 400),
        ):
            response = await editor.patch(
                f"/v1/sessions/{conv.id}", json=body, headers=_as_user(BOB)
            )
            assert response.status_code == status, response.text


@pytest.mark.parametrize("current,expected", [("go", "review"), ("new-choice", "new-choice")])
async def test_acp_readback_relay_persists_actual_mode_without_overwriting_new_choice(
    stores, monkeypatch, current, expected
):
    from omnigent.sdk_permission_modes import ACP_SESSION_MODE_LABEL_KEY
    from omnigent.server.routes._sessions import orchestration as orch

    store = SqlAlchemyConversationStore(stores.agent_store.storage_location)
    conv = store.create_conversation(
        harness_override="acp:neutral", labels={ACP_SESSION_MODE_LABEL_KEY: current}
    )
    event = {
        "type": "session.permission_mode",
        "harness": "acp",
        "conversation_id": conv.id,
        "requested_mode": "go",
        "permission_mode": "review",
    }
    published = []
    monkeypatch.setattr(
        "omnigent.runtime.session_stream.publish",
        lambda sid, payload: published.append(payload),
    )
    stream = "data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text=stream)),
        base_url="http://runner",
    ) as runner:
        await orch._relay_runner_stream_once(conv.id, runner, store)
    assert store.get_conversation(conv.id).labels[ACP_SESSION_MODE_LABEL_KEY] == expected
    assert (event in published) is (current == "go")


async def test_model_only_usage_reports_model_without_creating_a_usage_bucket(client, stores):
    from omnigent.server.routes._sessions import orchestration

    _seed_agent(stores, AGENT_ID, "acp:neutral")
    conversations = SqlAlchemyConversationStore(stores.agent_store.storage_location)
    source = conversations.create_conversation(agent_id=AGENT_ID)
    response = {"usage": {"model": "model-a"}}
    await orchestration._persist_relay_reported_model(response, source.id, conversations)
    orchestration._accumulate_session_usage(response, source.id, conversations)
    saved = conversations.get_conversation(source.id)
    assert saved.reported_model == "model-a"
    assert not saved.session_usage


@pytest.mark.parametrize("legacy", [False, True])
async def test_acp_readback_selection_accepts_repeated_reports_and_fences_new_choice(
    stores, monkeypatch, legacy
):
    from omnigent.sdk_permission_modes import ACP_SESSION_MODE_LABEL_KEY as mode
    from omnigent.sdk_permission_modes import ACP_SESSION_MODE_SELECTION_LABEL_KEY as selection
    from omnigent.server.routes._sessions import orchestration as orch

    store = SqlAlchemyConversationStore(stores.agent_store.storage_location)
    selection_id = "go" if legacy else "first"
    labels = {mode: "go", **({selection: selection_id} if not legacy else {})}
    conv = store.create_conversation(harness_override="acp:neutral", labels=labels)
    events = [
        {
            "type": "session.permission_mode",
            "harness": "acp",
            "conversation_id": conv.id,
            "requested_mode": "go",
            "selection_id": selection_id,
            "permission_mode": actual,
        }
        for actual in ("review", "manual")
    ]
    published = []
    monkeypatch.setattr(
        "omnigent.runtime.session_stream.publish",
        lambda sid, payload: published.append(payload),
    )

    async def relay():
        stream = (
            "".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n"
        )
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, text=stream)),
            base_url="http://runner",
        ) as runner:
            await orch._relay_runner_stream_once(conv.id, runner, store)

    await relay()
    assert store.get_conversation(conv.id).labels[mode] == "manual"
    assert len(published) == 2
    store.set_labels(conv.id, {mode: "go", selection: "second"})
    await relay()
    assert store.get_conversation(conv.id).labels[mode] == "go"
    assert len(published) == 2


async def test_acp_unrequested_mode_readback_cannot_replace_an_explicit_selection(stores):
    from omnigent.sdk_permission_modes import (
        ACP_SESSION_MODE_LABEL_KEY,
        ACP_SESSION_MODE_SELECTION_LABEL_KEY,
    )
    from omnigent.server.routes._sessions import orchestration as orch

    store = SqlAlchemyConversationStore(stores.agent_store.storage_location)
    labels = {ACP_SESSION_MODE_LABEL_KEY: "go", ACP_SESSION_MODE_SELECTION_LABEL_KEY: "new-choice"}
    conv = store.create_conversation(harness_override="acp:neutral", labels=labels)
    event = {
        "type": "session.permission_mode",
        "harness": "acp",
        "conversation_id": conv.id,
        "permission_mode": "plan",
    }
    stream = "data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text=stream)),
        base_url="http://runner",
    ) as runner:
        await orch._relay_runner_stream_once(conv.id, runner, store)
    assert store.get_conversation(conv.id).labels == labels


async def test_snapshot_uses_active_agent_model_until_the_turn_end_report(stores):
    from omnigent.server.routes._sessions import orchestration as orch

    _seed_agent(stores, AGENT_ID, "acp:neutral")
    store = SqlAlchemyConversationStore(stores.agent_store.storage_location)
    conv = store.create_conversation(agent_id=AGENT_ID, harness_override="acp:neutral")
    orch._model_options_cache[conv.id] = []
    orch._acp_effort_options_cache[conv.id] = [
        {"id": "model-a", "isActive": False},
        {"id": "model-b", "isActive": True},
    ]
    for reported, expected in ((None, "model-b"), ("model-c", "model-c")):
        if reported:
            store.update_conversation(conv.id, reported_model=reported)
        response = await orch._get_session_snapshot(
            store,
            conv.id,
            agent_store=stores.agent_store,
            agent_cache=stores.agent_cache,
            include_live_status=False,
            include_items=False,
        )
        assert response.llm_model == expected


@pytest.mark.parametrize("curated", [[], [{"id": "model-a"}, {"id": "model-b"}]])
async def test_acp_running_model_refresh_keeps_catalog_sources_separate(
    client, stores, monkeypatch, curated
):
    from omnigent.server.routes._sessions import orchestration as orch

    _seed_agent(stores, AGENT_ID, "acp:neutral")
    store = SqlAlchemyConversationStore(stores.agent_store.storage_location)
    conv = store.create_conversation(agent_id=AGENT_ID)
    orch._model_options_cache[conv.id] = list(curated)
    orch._acp_effort_options_cache[conv.id] = [{"id": "model-c", "isActive": True}]
    initial = await client.get(f"/v1/sessions/{conv.id}/model-options")
    assert [row["id"] for row in initial.json()["models"]] == (
        ["model-a", "model-b"] if curated else ["model-c"]
    )
    live = [{"id": "model-d", "isActive": True}]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"models": live})),
        base_url="http://runner",
    ) as runner:
        monkeypatch.setattr(
            "omnigent.server.routes.sessions.routes_core._get_runner_client",
            AsyncMock(return_value=runner),
        )
        refreshed = await client.get(
            f"/v1/sessions/{conv.id}/model-options", params={"refresh": "force"}
        )
    assert refreshed.status_code == 200, refreshed.text
    assert [row["id"] for row in refreshed.json()["models"]] == (
        ["model-a", "model-b"] if curated else ["model-d"]
    )
    assert orch._model_options_cache[conv.id] == curated


@pytest.mark.parametrize("protocol", ["mode-config", "mode-legacy"])
@pytest.mark.parametrize(
    "failure", ["", "mode-notify-refusal", "mode-multiple-readbacks", "initial-plan"]
)
async def test_acp_mode_crosses_server_runner_and_scripted_agent(
    client, app, stores, tmp_path, protocol, failure, monkeypatch
):
    import asyncio
    import shlex
    import sys
    import tempfile

    from starlette.websockets import WebSocketDisconnect

    from omnigent.host.frames import (
        HostModelOptionsResultFrame,
        decode_host_frame,
        encode_host_frame,
    )
    from omnigent.runner import create_runner_app
    from omnigent.runner.session_init_protocol import build_runner_session_init_payload
    from omnigent.runtime.harnesses.process_manager import HarnessProcessManager
    from omnigent.sdk_permission_modes import (
        ACP_SESSION_MODE_LABEL_KEY,
        ACP_SESSION_MODE_SELECTION_LABEL_KEY,
    )
    from omnigent.server.routes.host_tunnel import _receive_loop
    from omnigent.server.session_open_rate import CREATE_ORIGIN_AGENT, CREATE_ORIGIN_HEADER
    from tests._helpers.polling import poll_until_async
    from tests.runner.conftest import _runner_client
    from tests.runtime.harnesses.test_acp_model_switching import _FAKE_ACP_AGENT

    script = tmp_path / "agent.py"
    script.write_text(_FAKE_ACP_AGENT)
    record = tmp_path / "requests.jsonl"
    entry = {
        "name": "Neutral",
        "command": shlex.join([sys.executable, str(script), str(record), protocol, failure]),
        "omnigent_mcp": False,
        "permission_concepts": {"go": "automatic"},
    }
    location = f"{AGENT_ID}/bundle"
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"acp": {"agents": [entry]}}))
    stores.artifact_store.put(location, _bundle("acp:neutral"))
    stores.agent_store.create(AGENT_ID, "calling-defaults-agent", location)
    stores.host_store.upsert_on_connect(HOST_A, "box-a", "local")
    options = [
        {"value": "review", "label": "Review"},
        {"value": "go", "label": "Go", "concept": "automatic"},
    ]
    _register_host(app, HOST_A)
    conn = app.state.host_registry.get(HOST_A)
    conn.hello.capabilities.append("agy_acp_permissions:acp:ag")
    requested_mode = None if failure == "initial-plan" else "go"

    async def receive_host_reply():
        outgoing = await conn.outbound_queue.get()
        request = decode_host_frame(outgoing.data)
        response = HostModelOptionsResultFrame(
            request_id=request.request_id,
            status="ok",
            models=[{"id": "model-a", "start_efforts": ["low", "high"]}],
            permission_options=options,
            injects_prompt=False,
        )
        ws = AsyncMock()
        ws.receive.side_effect = [
            {"type": "websocket.receive", "text": encode_host_frame(response)},
            {"type": "websocket.disconnect"},
        ]
        with pytest.raises(WebSocketDisconnect):
            await _receive_loop(
                ws,
                conn,
                HOST_A,
                stores.host_store,
                app.state.host_registry,
                None,
                None,
                None,
                None,
                None,
                None,
            )

    reply = asyncio.create_task(receive_host_reply())
    synced = await client.post(
        "/v1/calling-defaults/sync", json={"host_id": HOST_A, "harness": "acp:neutral"}
    )
    await reply
    assert synced.status_code == 200, synced.text
    assert synced.json()["rows"][0]["permission_options"] == options
    catalogs = await client.get("/v1/calling-defaults/catalogs")
    assert catalogs.status_code == 200, catalogs.text
    assert catalogs.json()["rows"][0]["permission_options"] == options
    assert catalogs.json()["rows"][0]["injects_prompt"] is False
    conversations = SqlAlchemyConversationStore(stores.agent_store.storage_location)
    parent = conversations.create_conversation(
        agent_id=AGENT_ID, host_id=HOST_A, workspace=str(tmp_path), runner_id="test-runner"
    )
    created = await client.post(
        "/v1/sessions",
        json={
            "agent_id": AGENT_ID,
            "parent_session_id": parent.id,
            **({"permission_mode": requested_mode} if requested_mode else {}),
            "reasoning_effort": "high",
            "session_prompt": "Kept for this chat",
        },
    )
    assert created.status_code == 201, created.text
    saved = conversations.get_conversation(created.json()["id"])
    assert saved.labels.get(ACP_SESSION_MODE_LABEL_KEY) == requested_mode
    assert saved.session_prompt == "Kept for this chat"
    assert created.json()["warnings"] == [
        "session_prompt not sent: this agent builds its own prompt"
    ]
    assert created.json()["harness"] == "acp"
    assert created.json()["harness_namespace"] == "acp:neutral"
    assert created.json()["effective_host_id"] == HOST_A

    async def resolve_spec(_agent_id, _session_id=None):
        return stores.agent_cache.load(AGENT_ID, location).spec

    def requests():
        return (
            [json.loads(line) for line in record.read_text().splitlines()]
            if record.exists()
            else []
        )

    with tempfile.TemporaryDirectory(
        prefix="acp-mode-", dir="/tmp" if sys.platform != "win32" else None
    ) as sockets:
        manager = HarnessProcessManager(tmp_parent=Path(sockets))
        await manager.start(sweep_orphans=False)
        runner = create_runner_app(
            process_manager=manager, spec_resolver=resolve_spec, server_client=client
        )
        try:
            async with _runner_client(runner) as runner_client:
                initialized = await runner_client.post(
                    "/v1/sessions",
                    json=build_runner_session_init_payload(
                        saved, server_version="test", suppress_recovery_turn=True
                    ),
                )
                assert initialized.status_code == 201, initialized.text
                sent = await runner_client.post(
                    f"/v1/sessions/{saved.id}/events",
                    json={
                        "type": "message",
                        "role": "user",
                        "model": "calling-defaults-agent",
                        "harness": "acp:neutral",
                        "content": "hello",
                    },
                )
                assert sent.status_code == 202, sent.text
                await poll_until_async(
                    lambda: any(row.get("method") == "session/prompt" for row in requests()),
                    timeout_s=15,
                )
                await poll_until_async(lambda: not manager.has_active_turn(saved.id))

                async def mode_readback():
                    queue = runner.state.session_event_queues[saved.id]
                    while True:
                        event = await queue.get()
                        if event and event.get("type") == "session.permission_mode":
                            return event

                observed = await asyncio.wait_for(mode_readback(), timeout=10)
                assert observed["harness"] == "acp"
                assert observed.get("requested_mode") == requested_mode
                assert observed["permission_mode"] == (
                    "plan" if requested_mode is None else "review" if failure else "go"
                )
                assert observed.get("selection_id") == saved.labels.get(
                    ACP_SESSION_MODE_SELECTION_LABEL_KEY
                )
                from omnigent.server.routes._sessions import orchestration as orch

                monkeypatch.setattr("omnigent.runtime._globals._agent_store", stores.agent_store)
                monkeypatch.setattr("omnigent.runtime.get_agent_cache", lambda: stores.agent_cache)
                observations = [observed]
                if failure == "mode-multiple-readbacks":
                    final_readback = await asyncio.wait_for(mode_readback(), timeout=10)
                    assert final_readback["permission_mode"] == "manual"
                    assert final_readback["selection_id"] == observed["selection_id"]
                    observations.append(final_readback)
                stream = (
                    "".join("data: " + json.dumps(item) + "\n\n" for item in observations)
                    + "data: [DONE]\n\n"
                )
                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(lambda _: httpx.Response(200, text=stream)),
                    base_url="http://runner",
                ) as readback_client:
                    await orch._relay_runner_stream_once(saved.id, readback_client, conversations)
                assert (
                    conversations.get_conversation(saved.id).labels[ACP_SESSION_MODE_LABEL_KEY]
                    == observations[-1]["permission_mode"]
                )
                if requested_mode is None:
                    stores.prefs.patch_namespace(
                        "local", "session_collab", {"sessionRestrictions": "off"}
                    )
                    headers = {
                        **PERMISSION_TIERS_HEADERS,
                        CREATE_ORIGIN_HEADER: CREATE_ORIGIN_AGENT,
                    }
                    metadata = {"parent_session_id": saved.id}
                    for tier, expected_status in [(None, 201), ("auto", 403)]:
                        child = await client.post(
                            "/v1/sessions",
                            json={
                                **metadata,
                                "agent_id": AGENT_ID,
                                "harness_override": "acp:ag",
                                **({"permission_tier": tier} if tier else {}),
                            },
                            headers=headers,
                        )
                        assert child.status_code == expected_status, child.text
                        if tier is None:
                            assert child.json()["permission_tier"] == "plan"
                        else:
                            assert "cannot exceed its creator's permission tier" in child.text
                    assert not any(
                        row["method"] in ("session/set_mode", "session/set_config_option")
                        for row in requests()
                    )
                    return
                monkeypatch.setattr("omnigent.runtime.get_runner_client", lambda: runner_client)
                changed = await client.patch(
                    f"/v1/sessions/{saved.id}", json={"permission_mode": "review"}
                )
                assert changed.status_code == 200, changed.text
                sent = await runner_client.post(
                    f"/v1/sessions/{saved.id}/events",
                    json={
                        "type": "message",
                        "role": "user",
                        "model": "calling-defaults-agent",
                        "harness": "acp:neutral",
                        "content": "again",
                    },
                )
                assert sent.status_code == 202, sent.text
                await poll_until_async(
                    lambda: sum(row.get("method") == "session/prompt" for row in requests()) == 2
                )
                await poll_until_async(lambda: not manager.has_active_turn(saved.id))
        finally:
            await manager.shutdown()
    recorded = requests()
    handshake = next(row for row in recorded if row.get("method") == "initialize")
    assert handshake["params"]["_meta"] == {"omnigent": {"start_effort": "high"}}
    method = "session/set_config_option" if protocol == "mode-config" else "session/set_mode"
    switch = next(row for row in recorded if row.get("method") == method)
    assert switch["params"].get("value", switch["params"].get("modeId")) == "go"
    assert recorded.index(switch) < next(
        i for i, row in enumerate(recorded) if row.get("method") == "session/prompt"
    )
    switches = [row for row in recorded if row.get("method") == method]
    assert switches[-1]["params"].get("value", switches[-1]["params"].get("modeId")) == (
        "go" if failure == "mode-notify-refusal" else "review"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("effort", ["default", "deliberate"])
async def test_acp_json_and_multipart_create_accept_literal_efforts(client, stores, effort):
    _seed_agent(stores, AGENT_ID, "acp:neutral")
    created = await client.post(
        "/v1/sessions", json={"agent_id": AGENT_ID, "reasoning_effort": effort}
    )
    assert created.status_code == 201, created.text
    assert created.json()["reasoning_effort"] == effort
    uploaded = await client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({"reasoning_effort": effort})},
        files={"bundle": ("agent.tar.gz", _bundle("acp:neutral"), "application/gzip")},
    )
    assert uploaded.status_code == 201, uploaded.text
    snapshot = await client.get(f"/v1/sessions/{uploaded.json()['session_id']}")
    assert snapshot.json()["reasoning_effort"] == effort


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "seeded,explicit,expected",
    [(None, False, "deliberate"), ("default", False, "default"), ("default", True, None)],
)
async def test_acp_bundle_inherits_its_row_without_overwriting_explicit_values(
    client, stores, seeded, explicit, expected
):
    stores.host_store.upsert_on_connect(HOST_A, "box-a", "local")
    stores.prefs.patch_namespace(
        "local",
        "calling_defaults",
        {HOST_A: {"acp:neutral": {"model": "model-b", "effort": "deliberate"}}},
    )
    _seed_agent(stores, AGENT_ID, "acp:neutral")
    parent = await client.post("/v1/sessions", json={"agent_id": AGENT_ID})
    assert parent.status_code == 201, parent.text
    SqlAlchemyConversationStore(stores.agent_store.storage_location).set_host_id(
        parent.json()["id"], HOST_A, workspace="/neutral/project"
    )
    metadata = {"parent_session_id": parent.json()["id"]}
    if explicit:
        metadata["reasoning_effort"] = None
    uploaded = await client.post(
        "/v1/sessions",
        data={"metadata": json.dumps(metadata)},
        files={"bundle": ("agent.tar.gz", _bundle("acp:neutral", seeded), "application/gzip")},
    )
    assert uploaded.status_code == 201, uploaded.text
    snapshot = await client.get(f"/v1/sessions/{uploaded.json()['session_id']}")
    assert snapshot.json()["reasoning_effort"] == expected
    assert snapshot.json()["model_override"] == "model-b"


@pytest.mark.parametrize("metadata", [{}, {"model_override": None}, {"model_override": "model-b"}])
def test_bundle_metadata_preserves_model_field_presence(metadata):
    from omnigent.server.routes._sessions.helpers import _parse_session_create_metadata

    parsed = _parse_session_create_metadata(json.dumps(metadata))
    assert ("model_override" in parsed.model_fields_set) == ("model_override" in metadata)
    assert parsed.model_override == metadata.get("model_override")


@pytest.mark.asyncio
async def test_non_acp_bundle_still_rejects_opaque_effort(client):
    uploaded = await client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({"reasoning_effort": "deliberate"})},
        files={"bundle": ("agent.tar.gz", _bundle("claude-sdk"), "application/gzip")},
    )
    assert uploaded.status_code == 400, uploaded.text


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["claude-sdk", "codex", "pi-native"])
async def test_non_acp_bundle_preserves_model_override(client, harness):
    uploaded = await client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({"model_override": "unoffered-model"})},
        files={"bundle": ("agent.tar.gz", _bundle(harness), "application/gzip")},
    )
    assert uploaded.status_code == 201, uploaded.text
    snapshot = await client.get(f"/v1/sessions/{uploaded.json()['session_id']}")
    assert snapshot.json()["model_override"] == "unoffered-model"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "requested,expected_status", [("model-b", 201), (None, 201), ("outside", 400)]
)
async def test_bound_acp_bundle_preserves_resolved_model(
    app, client, stores, requested, expected_status
):
    stores.host_store.upsert_on_connect(HOST_A, "box-a", "local")
    stores.prefs.patch_namespace(
        "local", "calling_defaults", {HOST_A: {"acp:neutral": {"model": "model-b"}}}
    )
    _seed_agent(stores, AGENT_ID, "acp:neutral")
    catalog = {
        "status": "ready",
        "default_model": "model-a",
        "models": [{"id": "model-a"}, {"id": "model-b"}],
    }
    snapshot = {
        "harness": "acp:neutral",
        "runtime_config": {
            "providers": {
                "neutral": {
                    "kind": "gateway",
                    "openai": {
                        "base_url": "https://gateway.example.invalid/v1",
                        "api_key_ref": "env:NEUTRAL_TEST_TOKEN",
                    },
                }
            },
            "inference": {
                "harnesses": {
                    "acp:neutral": {
                        "provider": "neutral",
                        "default_model": "model-a",
                        "model_allowlist": ["model-a", "model-b"],
                    }
                }
            },
        },
    }
    conversations = SqlAlchemyConversationStore(stores.agent_store.storage_location)
    parent = conversations.create_conversation(
        agent_id=AGENT_ID,
        inference_snapshot=snapshot,
        host_id=HOST_A,
        workspace="/neutral/project",
    )
    app.state.inference_catalog = SimpleNamespace(catalog=AsyncMock(return_value=catalog))
    metadata = {"parent_session_id": parent.id}
    if requested is not None:
        metadata["model_override"] = requested
    response = await client.post(
        "/v1/sessions",
        data={"metadata": json.dumps(metadata)},
        files={"bundle": ("agent.tar.gz", _bundle("acp:neutral"), "application/gzip")},
    )
    assert response.status_code == expected_status, response.text
    if expected_status == 201:
        saved = conversations.get_conversation(response.json()["session_id"])
        assert saved is not None
        assert saved.model_override == "model-b"
    else:
        assert "not available" in response.text


def test_facade_attr_proxy_monkeypatch_does_not_pin_instance_attribute() -> None:
    from omnigent.runtime import session_stream as runtime_session_stream
    from omnigent.server.routes._sessions import common

    original = runtime_session_stream.publish
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(common.session_stream, "publish", lambda *_args: 0)
        assert "publish" not in vars(common.session_stream)
        assert runtime_session_stream.publish is not original
    assert "publish" not in vars(common.session_stream)
    assert runtime_session_stream.publish is original


async def test_sdk_session_catalog_recovers_502_empty_cache_and_host_change(
    client, app, stores, monkeypatch
):
    from omnigent.server.routes._sessions import common

    _seed_agent(stores, AGENT_ID, "codex")
    store = SqlAlchemyConversationStore(stores.agent_store.storage_location)
    conv = store.create_conversation(agent_id=AGENT_ID)
    store.set_host_id(conv.id, HOST_A, workspace="/opt/work")
    store.update_conversation(conv.id, model_override="retired/model")
    _register_host(app, HOST_A)
    _register_host(app, HOST_B)
    common._model_options_cache[conv.id] = []
    responses = [
        {"status": "failed", "models": []},
        {"status": "ok", "models": [{"id": "host-model"}]},
        {"status": "ok", "models": []},
        {"status": "ok", "models": [{"id": "other-host-model"}]},
    ]
    seen = []

    async def probe(**kwargs):
        seen.append(kwargs["host_conn"].host_id)
        return responses.pop(0)

    monkeypatch.setattr("omnigent.server.routes.hosts._proxy_model_options", probe)
    url = f"/v1/sessions/{conv.id}/model-options?refresh=auto"
    assert (await client.get(url)).status_code == 502
    assert common._model_catalog_error[conv.id]
    recovered = await client.get(url)
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["models"] == [{"id": "host-model"}]
    assert conv.id not in common._model_catalog_error
    empty = await client.get(url.replace("auto", "force"))
    assert empty.status_code == 502
    assert common._model_options_cache[conv.id] == [{"id": "host-model"}]
    store.set_host_id(conv.id, HOST_B, workspace="/opt/work")
    changed = await client.get(url)
    assert changed.status_code == 200, changed.text
    assert changed.json()["models"] == [{"id": "other-host-model"}]
    assert seen == [HOST_A, HOST_A, HOST_A, HOST_B]


async def test_ag_acp_calling_default_permission_and_proxy_timeout(
    client, app, stores, monkeypatch
):
    """Saved and explicit ag-acp permissions apply at create, and refresh honors 125s budget."""
    from omnigent.db.utils import builtin_agent_id
    from omnigent.sdk_permission_modes import ACP_SESSION_MODE_LABEL_KEY
    from omnigent.server.routes.hosts import _proxy_model_options

    ag_acp_id = builtin_agent_id("ag-acp")
    _seed_agent(stores, ag_acp_id, "ag-acp", name="ag-acp")
    stores.host_store.upsert_on_connect(HOST_A, "box-a", "local")
    stores.host_store.update_harness_readiness(HOST_A, {"ag-acp": True})
    _register_host(app, HOST_A)
    conn = app.state.host_registry.get(HOST_A)
    assert conn is not None
    stores.cache.upsert(
        HOST_A,
        "ag-acp",
        [{"id": "gemini-4-neon", "isDefault": True}],
        now_epoch(),
        permission_options=[
            {"value": "default", "label": "Default", "concept": "manual"},
            {"value": "auto_edit", "label": "Auto Edit", "concept": "edit-automatic"},
        ],
    )
    stores.prefs.patch_namespace(
        "local", "calling_defaults", {HOST_A: {"ag-acp": {"permission": "auto_edit"}}}
    )
    resolved = await client.get(
        "/v1/calling-defaults/resolve", params={"host_id": HOST_A, "harness": "ag-acp"}
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["permission"] == "auto_edit", resolved.text

    conversations = SqlAlchemyConversationStore(stores.agent_store.storage_location)
    parent = conversations.create_conversation(
        agent_id=ag_acp_id, host_id=HOST_A, workspace="/opt/work", runner_id="test-runner"
    )
    created_default = await client.post(
        "/v1/sessions",
        json={"agent_id": ag_acp_id, "parent_session_id": parent.id},
    )
    assert created_default.status_code == 201, created_default.text
    assert created_default.json()["labels"][ACP_SESSION_MODE_LABEL_KEY] == "auto_edit"

    created_explicit = await client.post(
        "/v1/sessions",
        json={
            "agent_id": ag_acp_id,
            "parent_session_id": parent.id,
            "permission_mode": "default",
        },
    )
    assert created_explicit.status_code == 201, created_explicit.text
    assert created_explicit.json()["labels"][ACP_SESSION_MODE_LABEL_KEY] == "default"

    seen_timeouts: list[float] = []

    async def fake_request_host_model_options(**kwargs: Any) -> dict[str, Any]:
        seen_timeouts.append(kwargs["timeout_s"])
        return {"status": "ok", "models": [{"id": "gemini-4-neon"}]}

    monkeypatch.setattr(
        "omnigent.server.routes._host_model_options.request_host_model_options",
        fake_request_host_model_options,
    )
    await _proxy_model_options(
        host_registry=app.state.host_registry,
        host_conn=conn,
        harness="ag-acp",
        refresh="force",
    )
    assert seen_timeouts == [125.0]
