"""Per-session check-in mutations through the authenticated REST boundary."""

import asyncio
import json
from pathlib import Path

import httpx
from fastapi import FastAPI
from starlette.requests import HTTPConnection

from omnigent.child_checkin import (
    FOLLOWUP_NEXT_KEY,
    FOLLOWUP_PREFIX,
    INSTRUCTIONS_PREFIX,
    POLICY_KEY,
    decode_followup,
)
from omnigent.runner.tool_dispatch import execute_tool
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import (
    LEVEL_EDIT,
    LEVEL_OWNER,
    LEVEL_READ,
    RESERVED_USER_LOCAL,
    AuthProvider,
)
from omnigent.server.user_preferences_store import SqlAlchemyUserPreferencesStore
from omnigent.spec import AgentSpec
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore


async def test_patch_overrides_chunks_reset_and_validation(client: httpx.AsyncClient, db_uri: str):
    store = SqlAlchemyConversationStore(db_uri)
    parent = store.create_conversation()
    path = f"/v1/sessions/{parent.id}/child-checkin"
    text = "检查后续事项" * 160
    response = await client.patch(path, json={"min_gap_seconds": 0, "instructions": text})
    assert response.status_code == 200, response.text
    labels = store.get_conversation(parent.id).labels
    assert json.loads(labels[POLICY_KEY]) == {"g": 0}
    assert "".join(labels[f"{INSTRUCTIONS_PREFIX}{index}"] for index in range(4)) == text

    assert (
        await client.patch(path, json={"coverage": "changed", "instructions": ""})
    ).status_code == 200
    labels = store.get_conversation(parent.id).labels
    assert json.loads(labels[POLICY_KEY]) == {"g": 0, "c": "changed"}
    assert {
        key: value for key, value in labels.items() if key.startswith(INSTRUCTIONS_PREFIX)
    } == {f"{INSTRUCTIONS_PREFIX}0": ""}
    for invalid in (
        {"instructions": " "},
        {"instructions": "x" * 1001},
        {"min_gap_seconds": True},
        {"min_gap_seconds": -1},
        {"min_gap_seconds": 86401},
        {"min_gap_seconds": None},
        {"coverage": "all"},
        {"instructions": None},
        {"reset": None},
        {"reset": True, "coverage": "summary"},
        {"reset_instructions": True, "instructions": ""},
        {"session_id": "other"},
    ):
        assert (await client.patch(path, json=invalid)).status_code == 422, invalid
    assert (await client.patch(path, json={"instructions": "x" * 1000 + "\0"})).status_code == 200
    reset = await client.patch(path, json={"reset": True, "reset_instructions": True})
    assert reset.status_code == 200
    labels = store.get_conversation(parent.id).labels
    assert POLICY_KEY not in labels
    assert not any(key.startswith(INSTRUCTIONS_PREFIX) for key in labels)


async def test_followup_admission_removal_and_delivery_switch(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
):
    store = SqlAlchemyConversationStore(db_uri)
    parent = store.create_conversation()
    path = f"/v1/sessions/{parent.id}/child-checkin/follow-ups"
    prefs = SqlAlchemyUserPreferencesStore(db_uri)
    app.state.user_preferences_store = prefs
    first = await client.post(path, json={"text": "检查部署", "after_minutes": 15})
    assert first.status_code == 201, first.text
    item = first.json()
    assert item["delivery"] == "off"
    labels = store.get_conversation(parent.id).labels
    stored = decode_followup(labels[f"{FOLLOWUP_PREFIX}{item['id']}"])
    assert stored.text == "检查部署"
    assert stored.due == item["due"]
    assert labels[FOLLOWUP_NEXT_KEY] == str(item["due"])
    assert (await client.delete(f"{path}/{item['id']}")).status_code == 200
    assert (await client.delete(f"{path}/{item['id']}")).status_code == 404
    assert FOLLOWUP_NEXT_KEY not in store.get_conversation(parent.id).labels

    for invalid in (
        {"text": '"' * 200, "after_minutes": 15},
        {"text": " ", "after_minutes": 15},
        {"text": "\0", "after_minutes": 15},
        {"text": "later", "after_minutes": 14},
        {"text": "later", "after_minutes": 10081},
        {"text": "later", "after_minutes": True},
        {"text": "later"},
    ):
        assert (await client.post(path, json=invalid)).status_code == 422, invalid
    prefs.patch_namespace(RESERVED_USER_LOCAL, "child_idle", {"enabled": True})
    store.set_labels(parent.id, {f"{FOLLOWUP_PREFIX}{number:08x}": "bad" for number in range(19)})
    responses = await asyncio.gather(
        *(client.post(path, json={"text": "再检查", "after_minutes": 15}) for _ in range(2))
    )
    assert sorted(response.status_code for response in responses) == [201, 422]
    admitted = next(response.json() for response in responses if response.status_code == 201)
    assert admitted["delivery"] == "on"
    assert (await client.delete(f"{path}/00000000")).status_code == 200
    assert (
        await client.post(path, json={"text": "x" * 200, "after_minutes": 15})
    ).status_code == 201


async def test_checkin_writes_require_existing_session(client: httpx.AsyncClient):
    path = "/v1/sessions/missing/child-checkin"
    assert (await client.patch(path, json={"coverage": "summary"})).status_code == 404
    assert (
        await client.post(f"{path}/follow-ups", json={"text": "later", "after_minutes": 15})
    ).status_code == 404
    assert (await client.delete(f"{path}/follow-ups/1234abcd")).status_code == 404


async def test_checkin_tool_reaches_real_rest(client: httpx.AsyncClient, db_uri: str):
    store = SqlAlchemyConversationStore(db_uri)
    parent = store.create_conversation()
    for arguments in (
        {"action": "set", "min_gap_minutes": 30},
        {"action": "follow_up_add", "text": "检查后续", "after_minutes": 15},
    ):
        result = json.loads(
            await execute_tool(
                tool_name="sys_child_checkin",
                arguments=json.dumps(arguments),
                server_client=client,
                conversation_id=parent.id,
                agent_spec=AgentSpec(spec_version=1, spawn=True),
            )
        )
        assert "error" not in result, result
    labels = store.get_conversation(parent.id).labels
    assert json.loads(labels[POLICY_KEY]) == {"g": 1800}
    assert f"{FOLLOWUP_PREFIX}{result['id']}" in labels


class _HeaderAuth(AuthProvider):
    def get_user_id(self, request: HTTPConnection) -> str | None:
        return request.headers.get("x-test-user")


async def test_checkin_mutations_require_edit_grant(
    db_uri: str,
    runtime_init: None,
    tmp_path: Path,
):
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    store = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=store,
        artifact_store=artifacts,
        agent_cache=AgentCache(artifact_store=artifacts, cache_dir=tmp_path / "cache"),
        auth_provider=_HeaderAuth(),
        permission_store=permissions,
    )
    parent = store.create_conversation()
    permissions.grant("owner@example.test", parent.id, LEVEL_OWNER)
    permissions.grant("reader@example.test", parent.id, LEVEL_READ)
    permissions.grant("editor@example.test", parent.id, LEVEL_EDIT)
    path = f"/v1/sessions/{parent.id}/child-checkin"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for user in ("reader@example.test", "stranger@example.test"):
            headers = {"x-test-user": user}
            responses = [
                await client.patch(path, json={"coverage": "summary"}, headers=headers),
                await client.post(
                    f"{path}/follow-ups",
                    json={"text": "later", "after_minutes": 15},
                    headers=headers,
                ),
                await client.delete(f"{path}/follow-ups/1234abcd", headers=headers),
            ]
            assert all(response.status_code in (403, 404) for response in responses)
            assert not store.get_conversation(parent.id).labels
        for user in ("editor@example.test", "owner@example.test"):
            headers = {"x-test-user": user}
            assert (
                await client.patch(path, json={"coverage": "changed"}, headers=headers)
            ).status_code == 200
            added = await client.post(
                f"{path}/follow-ups", json={"text": "later", "after_minutes": 15}, headers=headers
            )
            assert added.status_code == 201, added.text
            assert (
                await client.delete(f"{path}/follow-ups/{added.json()['id']}", headers=headers)
            ).status_code == 200
