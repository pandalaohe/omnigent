"""Integration tests for the global instructions routes.

Uses real ``SqlAlchemyGlobalInstructionsStore``,
``SqlAlchemyPermissionStore`` and auth so the full request -> store ->
response pipeline is exercised.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.harness_plugins import native_agents
from omnigent.runner.tool_dispatch import execute_tool
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.global_instructions_store.sqlalchemy_store import (
    SqlAlchemyGlobalInstructionsStore,
)
from omnigent.stores.host_store import HostStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests._helpers.session import bundle_files

pytestmark = pytest.mark.asyncio

_OWN_HOST = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
_OTHER_HOST = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def auth_app(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
) -> FastAPI:
    """App with auth, permission, and global instructions stores enabled.

    :param runtime_init: Fixture that initializes the runtime with a mock LLM.
    :param db_uri: Per-test SQLite URI.
    :param tmp_path: Pytest temp dir for artifacts.
    :returns: A :class:`FastAPI` instance with auth and global instructions routes.
    """
    from omnigent.server.auth import UnifiedAuthProvider

    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(
            artifact_store=artifact_store,
            cache_dir=tmp_path / "cache",
        ),
        global_instructions_store=SqlAlchemyGlobalInstructionsStore(db_uri),
        host_store=HostStore(db_uri),
        permission_store=SqlAlchemyPermissionStore(db_uri),
        auth_provider=UnifiedAuthProvider(source="header", local_single_user=False),
    )


@pytest.fixture()
def local_app(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
) -> FastAPI:
    """App with no permission store: the request identity is the local user.

    :param runtime_init: Fixture that initializes the runtime with a mock LLM.
    :param db_uri: Per-test SQLite URI.
    :param tmp_path: Pytest temp dir for artifacts.
    :returns: A :class:`FastAPI` instance in the single-user posture.
    """
    from omnigent.server.auth import UnifiedAuthProvider

    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(
            artifact_store=artifact_store,
            cache_dir=tmp_path / "cache",
        ),
        global_instructions_store=SqlAlchemyGlobalInstructionsStore(db_uri),
        host_store=HostStore(db_uri),
        auth_provider=UnifiedAuthProvider(source="header", local_single_user=True),
    )


@pytest.fixture()
def store(db_uri: str) -> SqlAlchemyGlobalInstructionsStore:
    """A store on the same per-test DB the app writes to.

    :param db_uri: Per-test SQLite URI.
    :returns: A ready-to-use :class:`SqlAlchemyGlobalInstructionsStore`.
    """
    return SqlAlchemyGlobalInstructionsStore(db_uri)


@pytest_asyncio.fixture()
async def auth_client(auth_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Async HTTP client wired to the auth-enabled app.

    :param auth_app: FastAPI app with permission and global instructions stores.
    :yields: A ready-to-use :class:`httpx.AsyncClient`.
    """
    transport = httpx.ASGITransport(app=auth_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _headers(email: str) -> dict[str, str]:
    """Return request headers simulating an authenticated user.

    :param email: The user email to present, e.g. ``"admin@example.com"``.
    :returns: Dict with ``X-Forwarded-Email`` header.
    """
    return {"X-Forwarded-Email": email}


def _make_user(db_uri: str, email: str, *, is_admin: bool) -> None:
    """Seed the permission store with one user.

    :param db_uri: SQLite URI for the per-test database.
    :param email: User email to create, e.g. ``"admin@example.com"``.
    :param is_admin: Whether the user gets admin privileges.
    """
    SqlAlchemyPermissionStore(db_uri).ensure_user(email, is_admin=is_admin)


# ── Save and read ─────────────────────────────────────────────────────────────


async def test_admin_saves_large_text_intact(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Large instructions have no product cap and round-trip intact."""
    _make_user(db_uri, "admin@example.com", is_admin=True)
    headers = _headers("admin@example.com")
    text = "全🙂" * 32_768 + "\nlast instruction"

    resp = await auth_client.put(
        "/v1/global-instructions",
        json={"text": text},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["text"] == text
    assert body["max_chars"] is None
    assert body["revision_id"] is not None
    assert body["updated_at"] is not None
    assert body["updated_by"] == "admin@example.com"

    get_resp = await auth_client.get("/v1/global-instructions", headers=headers)
    assert get_resp.status_code == 200
    assert get_resp.json() == body


async def test_large_save_keeps_full_revision(
    auth_client: httpx.AsyncClient,
    db_uri: str,
    store: SqlAlchemyGlobalInstructionsStore,
) -> None:
    """A save above the former cap adds a full revision and replaces the live text."""
    _make_user(db_uri, "admin@example.com", is_admin=True)
    headers = _headers("admin@example.com")
    await auth_client.put(
        "/v1/global-instructions",
        json={"text": "keep"},
        headers=headers,
    )
    revisions_before = store.list_revisions()
    assert len(revisions_before) == 1

    text = "x" * 8001
    resp = await auth_client.put(
        "/v1/global-instructions",
        json={"text": text},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert len(store.list_revisions()) == 2
    assert {revision.text for revision in store.list_revisions()} == {"keep", text}
    current = await auth_client.get("/v1/global-instructions", headers=headers)
    assert current.json()["text"] == text


async def test_empty_save_is_accepted(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """PUT "" is a valid save that reads back blank and keeps a revision."""
    _make_user(db_uri, "admin@example.com", is_admin=True)
    headers = _headers("admin@example.com")
    await auth_client.put(
        "/v1/global-instructions",
        json={"text": "something"},
        headers=headers,
    )

    resp = await auth_client.put(
        "/v1/global-instructions",
        json={"text": ""},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["text"] == ""

    get_resp = await auth_client.get("/v1/global-instructions", headers=headers)
    assert get_resp.json()["text"] == ""

    revisions = await auth_client.get("/v1/global-instructions/revisions", headers=headers)
    assert {r["text"] for r in revisions.json()["data"]} == {"", "something"}


async def test_non_admin_save_forbidden(
    auth_client: httpx.AsyncClient,
    db_uri: str,
    store: SqlAlchemyGlobalInstructionsStore,
) -> None:
    """A non-admin PUT is 403 and writes nothing."""
    _make_user(db_uri, "user@example.com", is_admin=False)

    resp = await auth_client.put(
        "/v1/global-instructions",
        json={"text": "sneaky"},
        headers=_headers("user@example.com"),
    )
    assert resp.status_code == 403
    assert store.list_revisions() == []


async def test_single_user_save_has_no_attribution(
    local_app: FastAPI,
    store: SqlAlchemyGlobalInstructionsStore,
) -> None:
    """A single-user PUT is not attributed to the reserved ``local`` identity."""
    transport = httpx.ASGITransport(app=local_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.put("/v1/global-instructions", json={"text": "hello"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["updated_by"] is None
    current = store.current()
    assert current is not None
    assert current.created_by is None


async def test_revisions_newest_first(
    auth_client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The revisions endpoint orders newest first and honors ``limit``.

    The clock is pinned to distinct microsecond ticks so the order is the
    assertion rather than a same-µs race.
    """
    _make_user(db_uri, "admin@example.com", is_admin=True)
    headers = _headers("admin@example.com")
    ticks = iter([100_000_000, 200_000_000, 300_000_000])
    monkeypatch.setattr(
        "omnigent.stores.global_instructions_store.sqlalchemy_store.now_epoch_us",
        lambda: next(ticks),
    )
    for text in ("first", "second", "third"):
        resp = await auth_client.put(
            "/v1/global-instructions",
            json={"text": text},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text

    resp = await auth_client.get("/v1/global-instructions/revisions", headers=headers)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert [r["text"] for r in data] == ["third", "second", "first"]
    assert data[0]["created_at"] == 300
    assert data[0]["created_by"] == "admin@example.com"
    assert len(data[0]["id"]) == 32

    limited = await auth_client.get(
        "/v1/global-instructions/revisions?limit=2",
        headers=headers,
    )
    assert [r["text"] for r in limited.json()["data"]] == ["third", "second"]


async def test_layer_routes_keep_independent_clear_and_history(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    _make_user(db_uri, "admin@example.com", is_admin=True)
    headers = _headers("admin@example.com")
    HostStore(db_uri).upsert_on_connect(_OWN_HOST, "laptop", "admin@example.com")
    for scope, agent_key, host_id, text in (
        ("shared", "", "", "Shared"),
        ("agent", "codex", "", "Agent"),
        ("host", "", _OWN_HOST, "Host"),
        ("independent", "codex", _OWN_HOST, "Independent"),
    ):
        response = await auth_client.put(
            "/v1/instruction-layers",
            json={"scope": scope, "agent_key": agent_key, "host_id": host_id, "text": text},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        assert response.json()["text"] == text
    listing = await auth_client.get("/v1/instruction-layers", headers=headers)
    assert listing.status_code == 200, listing.text
    assert {r["scope"] for r in listing.json()["data"]} == {
        "shared",
        "agent",
        "host",
        "independent",
    }
    legacy = await auth_client.get(
        "/v1/global-instructions",
        params={"host_id": _OWN_HOST, "agent": "codex"},
        headers=headers,
    )
    assert legacy.json()["text"] == "Independent"
    await auth_client.put(
        "/v1/instruction-layers",
        json={"scope": "shared", "text": "Shared latest"},
        headers=headers,
    )
    updated = await auth_client.get("/v1/instruction-layers", headers=headers)
    independent = next(r for r in updated.json()["data"] if r["scope"] == "independent")
    assert independent["stale_layers"] == ["shared"]
    clear = await auth_client.put(
        "/v1/instruction-layers",
        json={"scope": "independent", "agent_key": "codex", "host_id": _OWN_HOST, "text": ""},
        headers=headers,
    )
    assert clear.status_code == 200, clear.text
    route = await auth_client.get(
        "/v1/global-instructions",
        params={"host_id": _OWN_HOST, "agent": "codex"},
        headers=headers,
    )
    assert route.json()["text"] == "Shared latest\n\nAgent\n\nHost"
    history = await auth_client.get(
        "/v1/instruction-layers/revisions",
        params={"scope": "independent", "agent_key": "codex", "host_id": _OWN_HOST},
        headers=headers,
    )
    assert [r["text"] for r in history.json()["data"]] == ["", "Independent"]
    HostStore(db_uri).delete_host(_OWN_HOST)
    removed = await auth_client.get("/v1/instruction-layers", headers=headers)
    host_layer = next(r for r in removed.json()["data"] if r["scope"] == "host")
    assert host_layer["host_removed"] is True
    removed_history = await auth_client.get(
        "/v1/instruction-layers/revisions",
        params={"scope": "host", "host_id": _OWN_HOST},
        headers=headers,
    )
    assert removed_history.status_code == 200, removed_history.text
    assert removed_history.json()["data"][0]["text"] == "Host"


@pytest.mark.parametrize("scope", ["shared", "agent", "host", "independent", "mother", "child"])
async def test_instruction_tools_reuse_layer_api_and_revision_guard(
    auth_client: httpx.AsyncClient, db_uri: str, scope: str
) -> None:
    _make_user(db_uri, "admin@example.test", is_admin=True)
    auth_client.headers.update(_headers("admin@example.test"))
    HostStore(db_uri).upsert_on_connect(_OWN_HOST, "laptop", "admin@example.test")
    key = {"scope": scope}
    if scope in {"agent", "independent"}:
        key["agent_key"] = "codex"
    if scope in {"host", "independent"}:
        key["host_id"] = _OWN_HOST

    async def call(name, args):
        return json.loads(
            await execute_tool(
                tool_name=name, arguments=json.dumps(args), server_client=auth_client
            )
        )

    first = await call(
        "sys_instruction_layer_update",
        {
            **key,
            "text": "First",
            "expected_revision_id": None,
        },
    )
    assert first["text"] == "First", first
    assert "over_budget_routes" in first
    listing = await call("sys_instruction_layers_read", {"mode": "layers"})
    assert listing["data"][0]["revision_id"] == first["revision_id"]
    second = await call(
        "sys_instruction_layer_update",
        {
            **key,
            "text": "Second",
            "expected_revision_id": first["revision_id"],
        },
    )
    assert second["revision_id"] != first["revision_id"], second
    for stale in (None, first["revision_id"]):
        refused = await call(
            "sys_instruction_layer_update",
            {
                **key,
                "text": "Lost update",
                "expected_revision_id": stale,
            },
        )
        assert refused["error"] == "server returned 409", refused
    history = await call("sys_instruction_layers_read", {"mode": "revisions", **key})
    assert [r["text"] for r in history["data"]] == ["Second", "First"]
    preview = await call(
        "sys_instruction_layers_read",
        {
            "mode": "preview",
            "host_id": _OWN_HOST,
            "agent_key": "codex",
            "child": scope == "child",
            "spawn": True,
        },
    )
    assert "Second" in preview["text"], preview
    _make_user(db_uri, "viewer@example.test", is_admin=False)
    auth_client.headers.update(_headers("viewer@example.test"))
    for name, args in (
        (
            "sys_instruction_layer_update",
            {**key, "text": "Forbidden", "expected_revision_id": second["revision_id"]},
        ),
        ("sys_instruction_layers_read", {"mode": "revisions", **key}),
    ):
        refused = await call(name, args)
        assert refused["error"] == "server returned 403", refused


async def test_layer_scope_and_host_authorization(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    _make_user(db_uri, "admin@example.com", is_admin=True)
    _make_user(db_uri, "other@example.com", is_admin=True)
    _make_user(db_uri, "viewer@example.com", is_admin=False)
    HostStore(db_uri).upsert_on_connect(_OTHER_HOST, "other", "other@example.com")
    admin = _headers("admin@example.com")
    assert (await auth_client.get("/v1/instruction-layers")).status_code == 401
    assert (
        await auth_client.put(
            "/v1/instruction-layers",
            json={"scope": "shared", "text": "x"},
            headers=_headers("viewer@example.com"),
        )
    ).status_code == 403
    for body in (
        {"scope": "unknown", "text": "x"},
        {"scope": "shared", "agent_key": "codex", "text": "x"},
        {"scope": "agent", "agent_key": "not-registered", "text": "x"},
    ):
        assert (
            await auth_client.put(
                "/v1/instruction-layers",
                json=body,
                headers=admin,
            )
        ).status_code in {400, 422}
    assert (
        await auth_client.put(
            "/v1/instruction-layers",
            json={"scope": "host", "host_id": _OTHER_HOST, "text": "x"},
            headers=admin,
        )
    ).status_code == 404
    assert (
        await auth_client.get(
            "/v1/instruction-layers/preview",
            params={"host_id": _OTHER_HOST, "agent_key": "codex"},
            headers=admin,
        )
    ).status_code == 404
    assert (
        await auth_client.get(
            "/v1/instruction-layers/revisions",
            params={"scope": "host", "host_id": _OTHER_HOST},
            headers=admin,
        )
    ).status_code == 404
    assert (
        await auth_client.get(
            "/v1/instruction-layers/preview",
            params={"session_id": "cccccccccccccccccccccccccccccccc"},
            headers=admin,
        )
    ).status_code == 404


async def test_large_layer_save_warns_only_newly_over_budget_routes(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    _make_user(db_uri, "admin@example.com", is_admin=True)
    HostStore(db_uri).upsert_on_connect(_OWN_HOST, "laptop", "admin@example.com")
    headers = _headers("admin@example.com")
    first = await auth_client.put(
        "/v1/instruction-layers",
        json={"scope": "shared", "text": "x" * 10_001},
        headers=headers,
    )
    assert first.status_code == 200, first.text
    assert first.json()["over_budget_routes"]
    assert all(r["total_chars"] >= 10_001 for r in first.json()["over_budget_routes"])
    second = await auth_client.put(
        "/v1/instruction-layers",
        json={"scope": "shared", "text": "y" * 10_002},
        headers=headers,
    )
    assert second.status_code == 200, second.text
    assert second.json()["over_budget_routes"] == []
    listing = await auth_client.get("/v1/instruction-layers", headers=headers)
    assert listing.json()["size_reminder_chars"] == 10_000
    assert any(size["over_budget"] for size in listing.json()["route_sizes"])


@pytest.mark.parametrize("is_admin", [False, True])
async def test_session_read_grant_does_not_grant_foreign_host_instruction_access(
    auth_client: httpx.AsyncClient,
    db_uri: str,
    store: SqlAlchemyGlobalInstructionsStore,
    is_admin: bool,
) -> None:
    _make_user(db_uri, "owner@example.com", is_admin=True)
    _make_user(db_uri, "reader@example.com", is_admin=is_admin)
    HostStore(db_uri).upsert_on_connect(_OTHER_HOST, "Other host", "owner@example.com")
    conv = SqlAlchemyConversationStore(db_uri).create_conversation(
        host_id=_OTHER_HOST,
        workspace="/opt/work/project",
        harness_override="codex-native",
    )
    SqlAlchemyPermissionStore(db_uri).grant("reader@example.com", conv.id, 1)
    store.save(
        "Foreign host text", created_by="owner@example.com", scope="host", host_id=_OTHER_HOST
    )
    store.save(
        "Foreign independent text",
        created_by="owner@example.com",
        scope="independent",
        host_id=_OTHER_HOST,
        agent_key="codex",
    )
    for path, params in (
        ("/v1/instruction-layers/preview", {"host_id": _OTHER_HOST, "agent_key": "codex"}),
        ("/v1/instruction-layers/preview", {"session_id": conv.id}),
        ("/v1/global-instructions", {"session_id": conv.id}),
    ):
        response = await auth_client.get(
            path, params=params, headers=_headers("reader@example.com")
        )
        assert response.status_code == 404, response.text
        assert "Foreign" not in response.text


async def test_preview_loads_real_builtin_bundle_and_session_route(
    auth_client: httpx.AsyncClient,
    auth_app: FastAPI,
    db_uri: str,
    tmp_path: Path,
    store: SqlAlchemyGlobalInstructionsStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _make_user(db_uri, "admin@example.com", is_admin=True)
    HostStore(db_uri).upsert_on_connect(_OWN_HOST, "Laptop", "admin@example.com")
    descriptor = next(a for a in native_agents() if a.key == "codex")
    agent_id = "dddddddddddddddddddddddddddddddd"
    bundle_key = f"{agent_id}/bundle"
    LocalArtifactStore(str(tmp_path / "artifacts")).put(
        bundle_key,
        bundle_files(
            {
                "config.yaml": (
                    b"spec_version: 1\nname: test-terminal\ninstructions: Author marker\n"
                    b"spawn: true\nexecutor:\n  type: omnigent\n  config:\n"
                    b"    harness: codex-native\n"
                )
            }
        ),
    )
    agents = SqlAlchemyAgentStore(db_uri)
    agents.create(agent_id, descriptor.agent_name, bundle_key)
    monkeypatch.setattr("omnigent.server.instruction_layers.get_agent_store", lambda: agents)
    monkeypatch.setattr(
        "omnigent.server.instruction_layers.get_agent_cache", lambda: auth_app.state.agent_cache
    )
    store.save("Shared marker", created_by=None)
    store.save("Host marker", created_by=None, scope="host", host_id=_OWN_HOST)
    store.save("Mother marker", created_by=None, scope="mother")
    route = await auth_client.get(
        "/v1/instruction-layers/preview",
        params={"host_id": _OWN_HOST, "agent_key": "codex", "peer": "false"},
        headers=_headers("admin@example.com"),
    )
    assert route.status_code == 200, route.text
    body = route.json()
    assert body["framework_unavailable"] is False
    assert body["subject"]["name"] == descriptor.agent_name
    assert body["route"]["spawn"] is True
    assert body["text"].startswith("Author marker\n\n")
    assert "Shared marker\n\nHost marker\n\nMother marker" in body["text"]
    conv = SqlAlchemyConversationStore(db_uri).create_conversation(
        host_id=_OWN_HOST,
        workspace="/opt/work/project",
        agent_id=agent_id,
        harness_override="codex-native",
    )
    SqlAlchemyPermissionStore(db_uri).grant("admin@example.com", conv.id, 3)
    session = await auth_client.get(
        "/v1/instruction-layers/preview",
        params={"session_id": conv.id},
        headers=_headers("admin@example.com"),
    )
    assert session.status_code == 200, session.text
    assert session.json()["framework_unavailable"] is False
    assert session.json()["route"]["host_id"] == _OWN_HOST
    assert "Shared marker\n\nHost marker\n\nMother marker" in session.json()["text"]
    legacy = await auth_client.get(
        "/v1/global-instructions",
        params={"session_id": conv.id},
        headers=_headers("admin@example.com"),
    )
    assert legacy.status_code == 200, legacy.text
    assert legacy.json()["text"] == "Shared marker\n\nHost marker\n\nMother marker"
