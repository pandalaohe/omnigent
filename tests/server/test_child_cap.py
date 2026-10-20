"""Session child-cap HTTP behavior at the create boundary."""

from __future__ import annotations

import asyncio
import json
import threading
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from starlette.requests import HTTPConnection

from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import LEVEL_OWNER, AuthProvider
from omnigent.server.child_cap import CHILD_CAP_LABEL_KEY, ChildCapLocks
from omnigent.server.session_open_rate import CREATE_ORIGIN_AGENT, CREATE_ORIGIN_HEADER
from omnigent.server.user_preferences_store import SqlAlchemyUserPreferencesStore
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests.server.helpers import build_agent_bundle, create_test_agent


async def test_cap_precedence_validation_and_user_exemption(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = await create_test_agent(client)
    parent_id = agent["_session_id"]
    store = SqlAlchemyConversationStore(db_uri)
    prefs = SqlAlchemyUserPreferencesStore(db_uri)
    app.state.user_preferences_store = prefs
    from omnigent.server.auth import RESERVED_USER_LOCAL

    prefs.patch_namespace(RESERVED_USER_LOCAL, "session_collab", {"childCap": 1})
    path = f"/v1/sessions/{parent_id}/child-cap"
    initial = await client.get(path)
    assert initial.status_code == 200
    assert initial.json() == {"cap": 1, "source": "user", "open_children": 0}

    body = {"agent_id": agent["id"], "parent_session_id": parent_id}
    user_create = await client.post("/v1/sessions", json=body)
    assert user_create.status_code == 201, user_create.text
    assert (await client.get(path)).json()["open_children"] == 1

    from omnigent.server.routes.sessions import routes_core

    monkeypatch.setattr(
        routes_core, "admit_open", lambda *_args: pytest.fail("cap refusal spent open rate")
    )
    refused = await client.post(
        "/v1/sessions", json=body, headers={CREATE_ORIGIN_HEADER: CREATE_ORIGIN_AGENT}
    )
    assert refused.status_code == 409
    assert refused.json()["detail"] == {
        "code": "child_cap",
        "message": (
            "This session already has 1 open child sessions (cap 1). "
            "Archive a finished child or reuse an idle one, then retry."
        ),
    }
    assert store.count_counted_children(parent_id) == 1

    for invalid in (0, -1, True, 1.0, "1", None):
        response = await client.patch(path, json={"cap": invalid})
        assert response.status_code == 422, invalid
    assert (await client.patch(path, json={"cap": 2})).json() == {
        "cap": 2,
        "source": "session",
        "open_children": 1,
    }
    assert store.get_conversation(parent_id).labels[CHILD_CAP_LABEL_KEY] == "2"
    assert (await client.patch(path, json={"cap": "none"})).json()["cap"] is None
    assert (await client.get(path)).json()["source"] == "session"


async def test_concurrent_agent_creates_take_only_last_slot(
    client: httpx.AsyncClient, db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = await create_test_agent(client)
    parent_id = agent["_session_id"]
    store = SqlAlchemyConversationStore(db_uri)
    store.set_labels(parent_id, {CHILD_CAP_LABEL_KEY: "1"})
    original_count = SqlAlchemyConversationStore.count_counted_children
    entered = threading.Event()
    release = threading.Event()

    def held_count(self: SqlAlchemyConversationStore, session_id: str) -> int:
        count = original_count(self, session_id)
        if session_id == parent_id and not entered.is_set():
            entered.set()
            assert release.wait(5)
        return count

    monkeypatch.setattr(SqlAlchemyConversationStore, "count_counted_children", held_count)
    headers = {CREATE_ORIGIN_HEADER: CREATE_ORIGIN_AGENT}

    async def create(title: str) -> httpx.Response:
        return await client.post(
            "/v1/sessions",
            json={"agent_id": agent["id"], "parent_session_id": parent_id, "title": title},
            headers=headers,
        )

    first = asyncio.create_task(create("first"))
    assert await asyncio.to_thread(entered.wait, 5)
    second = asyncio.create_task(create("second"))
    await asyncio.sleep(0)
    release.set()
    responses = await asyncio.gather(first, second)
    assert sorted(response.status_code for response in responses) == [201, 409]
    assert original_count(store, parent_id) == 1


async def test_multipart_child_create_checks_cap_before_insert(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    agent = await create_test_agent(client)
    parent_id = agent["_session_id"]
    store = SqlAlchemyConversationStore(db_uri)
    store.set_labels(parent_id, {CHILD_CAP_LABEL_KEY: "1"})
    store.create_conversation(kind="sub_agent", parent_conversation_id=parent_id)

    response = await client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({"parent_session_id": parent_id})},
        files={
            "bundle": ("agent.tar.gz", build_agent_bundle(name="cap-child"), "application/gzip")
        },
        headers={CREATE_ORIGIN_HEADER: CREATE_ORIGIN_AGENT},
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "child_cap"
    assert store.count_counted_children(parent_id) == 1


@pytest.mark.parametrize("contender_mode", ["json", "multipart"])
async def test_cancelled_multipart_insert_keeps_slot_locked_until_writer_finishes(
    client, db_uri, monkeypatch, contender_mode
):
    from omnigent.server.routes.sessions import routes_core

    agent = await create_test_agent(client)
    parent_id = agent["_session_id"]
    store = SqlAlchemyConversationStore(db_uri)
    store.set_labels(parent_id, {CHILD_CAP_LABEL_KEY: "1"})
    entered, release = threading.Event(), threading.Event()
    second_attempt = asyncio.Event()
    original_insert = routes_core._create_session_from_bundle
    original_hold = ChildCapLocks.hold
    attempts = 0

    def held_insert(*args, **kwargs):
        if not entered.is_set():
            entered.set()
            assert release.wait(5)
        return original_insert(*args, **kwargs)

    @asynccontextmanager
    async def traced_hold(self, session_id):
        nonlocal attempts
        if session_id == parent_id:
            attempts += 1
            if attempts == 2:
                second_attempt.set()
        async with original_hold(self, session_id):
            yield

    monkeypatch.setattr(routes_core, "_create_session_from_bundle", held_insert)
    monkeypatch.setattr(ChildCapLocks, "hold", traced_hold)
    headers = {CREATE_ORIGIN_HEADER: CREATE_ORIGIN_AGENT}

    async def create(mode):
        if mode == "json":
            return await client.post(
                "/v1/sessions",
                headers=headers,
                json={
                    "agent_id": agent["id"],
                    "parent_session_id": parent_id,
                    "title": "contender",
                },
            )
        return await client.post(
            "/v1/sessions",
            headers=headers,
            data={"metadata": json.dumps({"parent_session_id": parent_id})},
            files={
                "bundle": (
                    "agent.tar.gz",
                    build_agent_bundle(name="cancel-child"),
                    "application/gzip",
                )
            },
        )

    first = asyncio.create_task(create("multipart"))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        first.cancel()
        second = asyncio.create_task(create(contender_mode))
        await asyncio.wait_for(second_attempt.wait(), 5)
        assert not second.done()
        assert store.count_counted_children(parent_id) == 0
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    response = await second
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "child_cap"
    assert store.count_counted_children(parent_id) == 1


class _HeaderAuth(AuthProvider):
    def get_user_id(self, request: HTTPConnection) -> str | None:
        return request.headers.get("x-test-user")


async def test_forged_parent_cannot_read_cap_or_spend_budget(
    db_uri: str, runtime_init: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    conversations = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=conversations,
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        auth_provider=_HeaderAuth(),
        permission_store=permissions,
        user_preferences_store=SqlAlchemyUserPreferencesStore(db_uri),
    )
    parent = conversations.create_conversation()
    permissions.grant("alice@example.test", parent.id, LEVEL_OWNER)
    conversations.set_labels(parent.id, {CHILD_CAP_LABEL_KEY: "1"})
    monkeypatch.setattr(
        conversations,
        "count_counted_children",
        lambda _id: pytest.fail("unauthorized request reached cap count"),
    )
    headers = {"x-test-user": "mallory@example.test"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        get = await client.get(f"/v1/sessions/{parent.id}/child-cap", headers=headers)
        patch = await client.patch(
            f"/v1/sessions/{parent.id}/child-cap", json={"cap": 2}, headers=headers
        )
        create = await client.post(
            "/v1/sessions",
            json={"agent_id": "missing", "parent_session_id": parent.id},
            headers={**headers, CREATE_ORIGIN_HEADER: CREATE_ORIGIN_AGENT},
        )
    assert get.status_code in (403, 404)
    assert patch.status_code in (403, 404)
    assert create.status_code in (403, 404)
    assert conversations.get_conversation(parent.id).labels[CHILD_CAP_LABEL_KEY] == "1"
