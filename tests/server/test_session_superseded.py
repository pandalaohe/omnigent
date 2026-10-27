"""Superseded sessions reject controls and route messages without starting old work."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import LEVEL_EDIT, LEVEL_READ, UnifiedAuthProvider
from omnigent.server.routes import sessions as sessions_facade
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store import SUCCEEDED_BY_LABEL_KEY
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests.server.test_session_succession import (
    _create,
)
from tests.server.test_session_succession import (
    client as client,
)
from tests.server.test_session_succession import (
    runners as runners,
)
from tests.server.test_session_succession import (
    store as store,
)
from tests.server.test_session_succession import (
    succession_app as succession_app,
)

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize(
    "kind",
    [
        "interrupt",
        "compact",
        "approval",
        "stop_session",
        "retry_session",
        "/clear",
        "/new",
        "/model next",
        "/permissions",
    ],
)
async def test_superseded_controls_never_lookup_runner(
    client: httpx.AsyncClient,
    store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    old = _create(store, title="old")
    new = _create(store, title="new")
    store.set_labels(old, {SUCCEEDED_BY_LABEL_KEY: new})
    store.set_runner_id(old, None)
    lookup = AsyncMock(return_value=None)
    launch = AsyncMock()
    monkeypatch.setattr(sessions_facade, "_get_runner_client", lookup)
    monkeypatch.setattr(sessions_facade, "_launch_runner_on_host", launch)
    event = {"type": kind, "data": {}}
    if kind.startswith("/"):
        event = {
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": kind}]},
        }
    response = await client.post(f"/v1/sessions/{old}/events", json=event)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "session_superseded"
    assert response.json()["error"]["successor_id"] == new
    lookup.assert_not_awaited()
    launch.assert_not_awaited()


async def test_superseded_message_reaches_final_successor_with_attachments(
    client: httpx.AsyncClient,
    store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
    db_uri: str,
    runners: object,
) -> None:
    old = _create(store, title="old")
    middle = _create(store, title="middle")
    new = _create(store, title="new")
    store.set_labels(old, {SUCCEEDED_BY_LABEL_KEY: middle})
    store.set_labels(middle, {SUCCEEDED_BY_LABEL_KEY: new})
    store.set_runner_id(old, None)
    dispatch = AsyncMock(
        return_value=SimpleNamespace(forwarded=True, item_id="accepted", pending_id=None)
    )
    monkeypatch.setattr(sessions_facade, "_dispatch_session_event_to_runner", dispatch)
    monkeypatch.setattr(sessions_facade, "_ensure_runner_relay_ready", AsyncMock())
    launch = AsyncMock()
    monkeypatch.setattr(sessions_facade, "_launch_runner_on_host", launch)
    attachment = {"type": "input_image", "image_url": "data:image/png;base64,aGVsbG8="}
    files = SqlAlchemyFileStore(db_uri)
    uploaded = files.create(
        "note.txt", 5, "text/plain", session_id=old, blob_key="1234567890abcdef1234567890abcdef"
    )
    file_block = {"type": "input_file", "file_id": uploaded.id}
    response = await client.post(
        f"/v1/sessions/{old}/events",
        json={
            "type": "message",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": "continue"}, attachment, file_block],
            },
        },
    )
    assert response.status_code == 202, response.text
    assert response.json()["redirected_to"] == new
    assert dispatch.await_args.args[0] == new
    content = dispatch.await_args.args[2].data["content"]
    assert attachment in content
    assert any(old in block.get("text", "") for block in content)
    copied_id = next(block["file_id"] for block in content if block.get("type") == "input_file")
    assert copied_id != uploaded.id
    assert files.get(copied_id, session_id=new).blob_key == "1234567890abcdef1234567890abcdef"
    launch.assert_not_awaited()


@pytest.mark.parametrize(
    "kind",
    ["external_assistant_message", "external_session_status", "external_session_superseded"],
)
async def test_superseded_forwarder_bookkeeping_stays_on_old(
    client: httpx.AsyncClient,
    store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    old = _create(store, title="old")
    new = _create(store, title="new")
    store.set_labels(old, {SUCCEEDED_BY_LABEL_KEY: new})
    lookup = AsyncMock(return_value=None)
    monkeypatch.setattr(sessions_facade, "_get_runner_client", lookup)
    launch = AsyncMock()
    monkeypatch.setattr(sessions_facade, "_launch_runner_on_host", launch)
    data = (
        {"text": "This session continues elsewhere", "agent": "test"}
        if kind == "external_assistant_message"
        else {"status": "idle"}
        if kind == "external_session_status"
        else {"target_conversation_id": new, "reason": "clear"}
    )
    response = await client.post(f"/v1/sessions/{old}/events", json={"type": kind, "data": data})
    assert response.status_code == 202, response.text
    assert "redirected_to" not in response.json()
    launch.assert_not_awaited()
    if kind == "external_assistant_message":
        assert store.list_items(old).data
        assert not store.list_items(new).data


async def test_undo_accepts_input_on_the_restored_session(
    client: httpx.AsyncClient,
    store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
    runners: object,
) -> None:
    old = _create(store, title="old")
    new = _create(store, title="new")
    _create(store, parent=old, title="child")
    store.reassign_live_children(old, new, "forward")
    store.reassign_live_children(new, old, "reverse", reverse_of=True)
    dispatch = AsyncMock(
        return_value=SimpleNamespace(forwarded=True, item_id="accepted", pending_id=None)
    )
    monkeypatch.setattr(sessions_facade, "_dispatch_session_event_to_runner", dispatch)
    monkeypatch.setattr(sessions_facade, "_ensure_runner_relay_ready", AsyncMock())
    result = await client.post(
        f"/v1/sessions/{old}/events",
        json={
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": "restored"}]},
        },
    )
    assert result.status_code == 202, result.text
    assert "redirected_to" not in result.json()
    assert dispatch.await_args.args[0] == old


async def test_redirect_requires_edit_on_the_final_receiver(
    store: SqlAlchemyConversationStore,
    db_uri: str,
    tmp_path,
    runtime_init: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = _create(store, title="old")
    new = _create(store, title="new")
    store.set_labels(old, {SUCCEEDED_BY_LABEL_KEY: new})
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.grant("bob@example.com", old, LEVEL_EDIT)
    permissions.grant("bob@example.com", new, LEVEL_READ)
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    app = create_app(
        conversation_store=store,
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        artifact_store=artifacts,
        agent_cache=AgentCache(artifact_store=artifacts, cache_dir=tmp_path / "cache"),
        auth_provider=UnifiedAuthProvider(source="header"),
        permission_store=permissions,
    )
    dispatch = AsyncMock()
    launch = AsyncMock()
    monkeypatch.setattr(sessions_facade, "_dispatch_session_event_to_runner", dispatch)
    monkeypatch.setattr(sessions_facade, "_launch_runner_on_host", launch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as requester:
        response = await requester.post(
            f"/v1/sessions/{old}/events",
            headers={"X-Forwarded-Email": "bob@example.com"},
            json={
                "type": "message",
                "data": {"role": "user", "content": [{"type": "input_text", "text": "hello"}]},
            },
        )
    assert response.status_code == 403, response.text
    dispatch.assert_not_awaited()
    launch.assert_not_awaited()
