"""``GET /v1/sessions/{id}/collab-settings`` — the runner's read of row 8."""

from __future__ import annotations

from pathlib import Path

import httpx
from fastapi import FastAPI
from starlette.requests import HTTPConnection

from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import LEVEL_OWNER, RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.user_preferences_store import SqlAlchemyUserPreferencesStore
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore


class _HeaderAuthProvider(AuthProvider):
    """Resolve a test identity from ``X-Test-User``."""

    def get_user_id(self, request: HTTPConnection) -> str | None:
        return request.headers.get("x-test-user")


def _app(
    db_uri: str,
    tmp_path: Path,
    *,
    auth: bool,
) -> tuple[FastAPI, SqlAlchemyConversationStore, SqlAlchemyPermissionStore]:
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    conversation_store = SqlAlchemyConversationStore(db_uri)
    permission_store = SqlAlchemyPermissionStore(db_uri)
    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=conversation_store,
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        auth_provider=_HeaderAuthProvider() if auth else None,
        permission_store=permission_store if auth else None,
        user_preferences_store=SqlAlchemyUserPreferencesStore(db_uri),
    )
    return app, conversation_store, permission_store


async def test_local_mode_reads_the_local_user_row_and_defaults(
    db_uri: str, runtime_init: None, tmp_path: Path
) -> None:
    app, conversations, _perms = _app(db_uri, tmp_path, auth=False)
    session_id = conversations.create_conversation().id
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        default = await client.get(f"/v1/sessions/{session_id}/collab-settings")
        assert default.status_code == 200
        assert default.json()["flow_timer_enabled"] is True
        assert default.json()["enabled"] is True

        SqlAlchemyUserPreferencesStore(db_uri).patch_namespace(
            RESERVED_USER_LOCAL, "session_collab", {"flowTimerEnabled": False}
        )
        off = await client.get(f"/v1/sessions/{session_id}/collab-settings")
    assert off.json()["flow_timer_enabled"] is False


async def test_auth_mode_reads_the_top_level_owner_and_checks_access(
    db_uri: str, runtime_init: None, tmp_path: Path
) -> None:
    app, conversations, perms = _app(db_uri, tmp_path, auth=True)
    parent = conversations.create_conversation()
    child = conversations.create_conversation(parent_conversation_id=parent.id)
    perms.grant("alice@example.com", parent.id, LEVEL_OWNER)
    perms.grant("alice@example.com", child.id, LEVEL_OWNER)
    SqlAlchemyUserPreferencesStore(db_uri).patch_namespace(
        "alice@example.com", "session_collab", {"flowTimerEnabled": False}
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        alice = await client.get(
            f"/v1/sessions/{child.id}/collab-settings",
            headers={"x-test-user": "alice@example.com"},
        )
        mallory = await client.get(
            f"/v1/sessions/{child.id}/collab-settings",
            headers={"x-test-user": "mallory@example.com"},
        )
        missing = await client.get(
            "/v1/sessions/conv_missing/collab-settings",
            headers={"x-test-user": "alice@example.com"},
        )
    assert alice.status_code == 200
    assert alice.json()["flow_timer_enabled"] is False
    assert mallory.status_code in (403, 404)
    assert missing.status_code == 404
