"""Server-owned Pi successor creation and retry boundaries."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from omnigent._wrapper_labels import UI_MODE_LABEL_KEY, WRAPPER_LABEL_KEY
from omnigent.permission_tiers import PermissionTier
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import LEVEL_EDIT, LEVEL_READ, UnifiedAuthProvider
from omnigent.server.permission_tiers import (
    PERMISSION_CAPABILITY_LABEL_KEY,
    PERMISSION_CAPABILITY_VALUE,
    ConfirmedPermissionState,
)
from omnigent.server.routes._host_worktree import WORKTREE_ROOT_LABEL_KEY
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store import (
    HANDOVER_ITEM_LABEL_KEY,
    ROTATION_SUCCESSOR_LABEL_KEY,
    SUCCEEDED_BY_LABEL_KEY,
)
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

pytestmark = pytest.mark.asyncio

AGENT_ID = "a1b2c3d4e5f60718293a4b5c6d7e8f01"
RUNNER_ID = "b1b2c3d4e5f60718293a4b5c6d7e8f02"


def _source(
    store: SqlAlchemyConversationStore,
    *,
    harness: str = "pi",
    tier: PermissionTier = "ask",
    status: str = "ready",
    parent_id: str | None = None,
) -> str:
    labels = {
        **ConfirmedPermissionState(tier, 2, status).labels(harness),
        PERMISSION_CAPABILITY_LABEL_KEY: PERMISSION_CAPABILITY_VALUE,
        UI_MODE_LABEL_KEY: "terminal",
        WRAPPER_LABEL_KEY: "pi-native",
        WORKTREE_ROOT_LABEL_KEY: "f" * 64,
        HANDOVER_ITEM_LABEL_KEY: "c" * 32,
        "guardrail.customer": "enabled",
        "omnigent.pi.bridge_id": "old-bridge",
        "cost_control.current": "old-cost",
        "omnigent.mcp_service.disabled": "0",
    }
    return store.create_conversation(
        agent_id=AGENT_ID,
        runner_id=RUNNER_ID,
        parent_conversation_id=parent_id,
        harness_override=harness,
        title="Pi mother",
        workspace="/opt/work/sample-app",
        git_branch="feature/pi",
        terminal_launch_args=["--model", "test", "--session", "old.json", "--continue"],
        labels=labels,
        model_override="test-model",
        reasoning_effort="high",
    ).id


@pytest.fixture()
def store(db_uri: str) -> SqlAlchemyConversationStore:
    return SqlAlchemyConversationStore(db_uri)


@pytest.fixture()
def app(runtime_init: None, db_uri: str, tmp_path: Any) -> FastAPI:
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifacts,
        agent_cache=AgentCache(artifact_store=artifacts, cache_dir=tmp_path / "cache"),
    )


@pytest.mark.parametrize("harness", ["pi", "pi-native"])
@pytest.mark.parametrize("tier", ["ask", "plan", "full"])
async def test_successor_is_stable_and_copies_only_configuration(
    app: FastAPI, store: SqlAlchemyConversationStore, harness: str, tier: PermissionTier
) -> None:
    source_id = _source(store, harness=harness, tier=tier)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        responses = await asyncio.gather(
            *(client.post(f"/v1/sessions/{source_id}/successor") for _ in range(4))
        )
    assert all(response.status_code == 200 for response in responses), [
        response.text for response in responses
    ]
    successor_ids = {response.json()["id"] for response in responses}
    assert len(successor_ids) == 1
    target = store.get_conversation(successor_ids.pop())
    assert target is not None
    assert target.agent_id == AGENT_ID and target.runner_id == RUNNER_ID
    assert target.parent_conversation_id is None and target.external_session_id is None
    assert target.workspace == "/opt/work/sample-app" and target.git_branch == "feature/pi"
    assert target.model_override == "test-model" and target.reasoning_effort == "high"
    assert target.terminal_launch_args == ["--model", "test"]
    assert target.labels[UI_MODE_LABEL_KEY] == "terminal"
    assert target.labels[WORKTREE_ROOT_LABEL_KEY] == "f" * 64
    assert target.labels["guardrail.customer"] == "enabled"
    assert HANDOVER_ITEM_LABEL_KEY not in target.labels
    assert "omnigent.pi.bridge_id" not in target.labels
    assert "cost_control.current" not in target.labels
    assert target.labels["omnigent.mcp_service.disabled"] == "0"
    assert ROTATION_SUCCESSOR_LABEL_KEY not in target.labels
    assert json.loads(store.get_conversation(source_id).labels[ROTATION_SUCCESSOR_LABEL_KEY]) == {
        "id": target.id,
        "revision": 0,
    }
    assert (
        ConfirmedPermissionState(tier, 1, "blocked").labels(harness).items()
        <= target.labels.items()
    )
    assert PERMISSION_CAPABILITY_LABEL_KEY not in target.labels


async def test_metadata_failure_resumes_same_successor(
    app: FastAPI, store: SqlAlchemyConversationStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.stores.conversation_store import sqlalchemy_store

    source_id = _source(store)
    original = sqlalchemy_store._new_session_metadata_row
    failures = 1

    def fail_once(*args: Any, **kwargs: Any) -> Any:
        nonlocal failures
        if failures:
            failures -= 1
            raise RuntimeError("metadata unavailable")
        return original(*args, **kwargs)

    monkeypatch.setattr(sqlalchemy_store, "_new_session_metadata_row", fail_once)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        with pytest.raises(RuntimeError, match="metadata unavailable"):
            await client.post(f"/v1/sessions/{source_id}/successor")
        response = await client.post(f"/v1/sessions/{source_id}/successor")
        again = await client.post(f"/v1/sessions/{source_id}/successor")
    assert response.status_code == again.status_code == 200
    assert response.json() == again.json()
    assert store.get_conversation(response.json()["id"]).runner_id == RUNNER_ID


@pytest.mark.parametrize("status", ["switching", "blocked"])
async def test_nonready_source_refused_without_successor(
    app: FastAPI, store: SqlAlchemyConversationStore, status: str
) -> None:
    source_id = _source(store, status="ready")
    store.set_labels(
        source_id,
        ConfirmedPermissionState("ask", 3, status, "tx" if status == "switching" else None).labels(
            "pi"
        ),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(f"/v1/sessions/{source_id}/successor")
    assert response.status_code == 409


async def test_child_and_archived_sources_refused(
    app: FastAPI, store: SqlAlchemyConversationStore
) -> None:
    parent = _source(store)
    child = _source(store, parent_id=parent)
    archived = _source(store)
    store.update_conversation(archived, archived=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        child_response = await client.post(f"/v1/sessions/{child}/successor")
        archived_response = await client.post(f"/v1/sessions/{archived}/successor")
    assert child_response.status_code == archived_response.status_code == 409


async def test_completed_archive_recovers_the_same_successor(
    app: FastAPI, store: SqlAlchemyConversationStore
) -> None:
    source_id = _source(store)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post(f"/v1/sessions/{source_id}/successor")
        assert created.status_code == 200, created.text
        target_id = created.json()["id"]
        store.set_labels(source_id, {SUCCEEDED_BY_LABEL_KEY: target_id})
        store.update_conversation(source_id, archived=True)
        repeated = await client.post(f"/v1/sessions/{source_id}/successor")
    assert repeated.status_code == 200, repeated.text
    assert repeated.json() == created.json()


async def test_successor_inherits_source_grants_only(
    runtime_init: None, db_uri: str, tmp_path: Any, store: SqlAlchemyConversationStore
) -> None:
    permissions = SqlAlchemyPermissionStore(db_uri)
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifacts,
        agent_cache=AgentCache(artifact_store=artifacts, cache_dir=tmp_path / "cache"),
        auth_provider=UnifiedAuthProvider(source="header"),
        permission_store=permissions,
    )
    source_id = _source(store)
    permissions.grant("bob@example.com", source_id, LEVEL_EDIT)
    permissions.grant("reader@example.com", source_id, LEVEL_READ)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/v1/sessions/{source_id}/successor",
            headers={"X-Forwarded-Email": "bob@example.com"},
        )
    assert response.status_code == 200, response.text
    target_id = response.json()["id"]
    assert permissions.get("bob@example.com", target_id).level == LEVEL_EDIT
    assert permissions.get("reader@example.com", target_id).level == LEVEL_READ
    assert permissions.get("stranger@example.com", target_id) is None


async def test_grant_failure_rolls_back_metadata_and_resumes_same_target(
    app: FastAPI, store: SqlAlchemyConversationStore, db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.db.db_models import SqlSessionPermission

    source_id = _source(store)
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.grant("reader@example.com", source_id, LEVEL_READ)
    original = SqlSessionPermission.__init__
    fail = True

    def fail_once(self: Any, **kwargs: Any) -> None:
        nonlocal fail
        if kwargs.get("conversation_id") != source_id and fail:
            fail = False
            raise RuntimeError("grant unavailable")
        original(self, **kwargs)

    monkeypatch.setattr(SqlSessionPermission, "__init__", fail_once)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        with pytest.raises(RuntimeError, match="grant unavailable"):
            await client.post(f"/v1/sessions/{source_id}/successor")
        prepared = json.loads(
            store.get_conversation(source_id).labels[ROTATION_SUCCESSOR_LABEL_KEY]
        )
        target_id = prepared["id"]
        assert store.get_conversation(target_id).runner_id is None
        assert permissions.get("reader@example.com", target_id) is None
        retry = await client.post(f"/v1/sessions/{source_id}/successor")
        assert retry.status_code == 200, retry.text
        assert retry.json()["id"] == target_id
        assert permissions.get("reader@example.com", target_id).level == LEVEL_READ
        permissions.revoke("reader@example.com", target_id)
        again = await client.post(f"/v1/sessions/{source_id}/successor")
        assert again.status_code == 200
        assert permissions.get("reader@example.com", target_id) is None
