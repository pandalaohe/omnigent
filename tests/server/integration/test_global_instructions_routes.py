"""Integration tests for the global instructions routes.

Uses real ``SqlAlchemyGlobalInstructionsStore``,
``SqlAlchemyPermissionStore`` and auth so the full request -> store ->
response pipeline is exercised.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.global_instructions_store import GLOBAL_INSTRUCTIONS_MAX_CHARS
from omnigent.stores.global_instructions_store.sqlalchemy_store import (
    SqlAlchemyGlobalInstructionsStore,
)
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

pytestmark = pytest.mark.asyncio


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
        permission_store=SqlAlchemyPermissionStore(db_uri),
        auth_provider=UnifiedAuthProvider(source="header"),
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


async def test_admin_saves_text_at_cap(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """PUT at exactly the cap saves, and GET returns the same text."""
    _make_user(db_uri, "admin@example.com", is_admin=True)
    headers = _headers("admin@example.com")
    text = "x" * GLOBAL_INSTRUCTIONS_MAX_CHARS

    resp = await auth_client.put(
        "/v1/global-instructions",
        json={"text": text},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["text"] == text
    assert body["max_chars"] == GLOBAL_INSTRUCTIONS_MAX_CHARS
    assert body["revision_id"] is not None
    assert body["updated_at"] is not None
    assert body["updated_by"] == "admin@example.com"

    get_resp = await auth_client.get("/v1/global-instructions", headers=headers)
    assert get_resp.status_code == 200
    assert get_resp.json() == body


async def test_over_cap_rejected_without_writing(
    auth_client: httpx.AsyncClient,
    db_uri: str,
    store: SqlAlchemyGlobalInstructionsStore,
) -> None:
    """PUT over the cap returns 422 naming limit and length; nothing is written."""
    _make_user(db_uri, "admin@example.com", is_admin=True)
    headers = _headers("admin@example.com")
    await auth_client.put(
        "/v1/global-instructions",
        json={"text": "keep"},
        headers=headers,
    )
    revisions_before = store.list_revisions()
    assert len(revisions_before) == 1

    resp = await auth_client.put(
        "/v1/global-instructions",
        json={"text": "x" * (GLOBAL_INSTRUCTIONS_MAX_CHARS + 1)},
        headers=headers,
    )
    assert resp.status_code == 422
    message = resp.json()["error"]["message"]
    assert str(GLOBAL_INSTRUCTIONS_MAX_CHARS) in message
    assert str(GLOBAL_INSTRUCTIONS_MAX_CHARS + 1) in message
    assert store.list_revisions() == revisions_before
    current = await auth_client.get("/v1/global-instructions", headers=headers)
    assert current.json()["text"] == "keep"


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
