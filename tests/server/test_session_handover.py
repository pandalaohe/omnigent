"""Server handover-endpoint tests.

``POST /v1/sessions/{id}/handover`` persists the calling session's handover
as a visible assistant message, points ``omnigent.handover_item`` at it, and
(with ``rotate``) arms ``omnigent.rotate_requested`` plus a visible notice.
Only a top-level claude-native / codex-native session may call it.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent._wrapper_labels import (
    CLAUDE_NATIVE_WRAPPER_VALUE,
    WRAPPER_LABEL_KEY,
)
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.routes import sessions as sessions_facade
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
from omnigent.stores.conversation_store import (
    HANDOVER_ITEM_LABEL_KEY,
    ROTATE_REQUESTED_LABEL_KEY,
)
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore

pytestmark = pytest.mark.asyncio


@pytest.fixture()
def store(db_uri: str) -> SqlAlchemyConversationStore:
    """A conversation store backed by the per-test SQLite database."""
    return SqlAlchemyConversationStore(db_uri)


@pytest.fixture()
def published(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    """Capture every SSE event published on a session stream."""
    events: list[tuple[str, dict[str, Any]]] = []

    def _capture(
        conversation_id: str,
        event: dict[str, Any],
        track_pending: bool = True,
    ) -> None:
        del track_pending
        events.append((conversation_id, event))

    monkeypatch.setattr(sessions_facade.session_stream, "publish", _capture)
    return events


@pytest.fixture()
def handover_app(
    runtime_init: None,
    db_uri: str,
    tmp_path: Any,
) -> FastAPI:
    """App with real stores and no permission store (single-user posture)."""
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
        comment_store=SqlAlchemyCommentStore(db_uri),
    )


@pytest_asyncio.fixture()
async def client(handover_app: FastAPI) -> Any:
    """Async client wired to the handover app."""
    transport = httpx.ASGITransport(app=handover_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _create(
    store: SqlAlchemyConversationStore,
    *,
    parent: str | None = None,
    native: bool = True,
) -> str:
    """Create a conversation, optionally stamped as a claude-native wrapper."""
    conversation = store.create_conversation(parent_conversation_id=parent, title="session")
    if native:
        store.set_labels(
            conversation.id,
            {WRAPPER_LABEL_KEY: CLAUDE_NATIVE_WRAPPER_VALUE},
        )
    return conversation.id


def _handover_messages(store: SqlAlchemyConversationStore, session_id: str) -> list[Any]:
    """All persisted message items on a session."""
    page = store.list_items(session_id, limit=200, order="desc", type="message")
    return list(page.data)


def _rotation_notices(store: SqlAlchemyConversationStore, session_id: str) -> list[Any]:
    """All persisted rotation notices on a session."""
    page = store.list_items(session_id, limit=200, order="desc", type="error")
    return [item for item in page.data if getattr(item.data, "code", None) == "session_rotation"]


async def test_handover_records_item_and_labels(
    client: httpx.AsyncClient,
    store: SqlAlchemyConversationStore,
    published: list[tuple[str, dict[str, Any]]],
) -> None:
    """One note item, both labels, and a rotation notice land on the session."""
    session_id = _create(store)

    response = await client.post(
        f"/v1/sessions/{session_id}/handover",
        json={"handover": "Continue the migration."},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["rotate"] is True
    assert isinstance(body["item_id"], str) and body["item_id"]

    messages = _handover_messages(store, session_id)
    assert len(messages) == 1
    assert messages[0].id == body["item_id"]
    assert messages[0].data.role == "assistant"
    assert messages[0].data.content == [{"type": "output_text", "text": "Continue the migration."}]

    row = store.get_conversation(session_id)
    assert row is not None
    assert row.labels[HANDOVER_ITEM_LABEL_KEY] == body["item_id"]
    assert row.labels[ROTATE_REQUESTED_LABEL_KEY] == str(row.archive_revision)

    notices = _rotation_notices(store, session_id)
    assert len(notices) == 1
    assert notices[0].data.message == (
        "This session will continue in a new session when the current turn ends."
    )

    published_item_ids = [
        event.get("item", {}).get("id")
        for session, event in published
        if session == session_id and event.get("type") == "response.output_item.done"
    ]
    assert body["item_id"] in published_item_ids
    assert notices[0].id in published_item_ids


async def test_handover_rotate_false_records_only_the_note(
    client: httpx.AsyncClient,
    store: SqlAlchemyConversationStore,
) -> None:
    """``rotate=false`` records the note but arms nothing."""
    session_id = _create(store)

    response = await client.post(
        f"/v1/sessions/{session_id}/handover",
        json={"handover": "Context for later.", "rotate": False},
    )
    assert response.status_code == 200, response.text
    assert response.json()["rotate"] is False

    row = store.get_conversation(session_id)
    assert row is not None
    assert row.labels[HANDOVER_ITEM_LABEL_KEY] == response.json()["item_id"]
    assert ROTATE_REQUESTED_LABEL_KEY not in row.labels
    assert _rotation_notices(store, session_id) == []


async def test_handover_same_text_does_not_duplicate(
    client: httpx.AsyncClient,
    store: SqlAlchemyConversationStore,
) -> None:
    """A retry with the same text reuses the stable item and notice ids."""
    session_id = _create(store)

    first = await client.post(
        f"/v1/sessions/{session_id}/handover",
        json={"handover": "Same note."},
    )
    second = await client.post(
        f"/v1/sessions/{session_id}/handover",
        json={"handover": "Same note."},
    )
    assert first.status_code == 200 and second.status_code == 200
    assert second.json()["item_id"] == first.json()["item_id"]
    assert len(_handover_messages(store, session_id)) == 1
    assert len(_rotation_notices(store, session_id)) == 1


async def test_handover_child_session_refused(
    client: httpx.AsyncClient,
    store: SqlAlchemyConversationStore,
) -> None:
    """A sub-agent session cannot record a handover."""
    parent_id = _create(store)
    child_id = _create(store, parent=parent_id)

    response = await client.post(
        f"/v1/sessions/{child_id}/handover",
        json={"handover": "Child note."},
    )
    assert response.status_code == 400, response.text
    assert "not_top_level" in response.text
    assert _handover_messages(store, child_id) == []


async def test_handover_non_native_session_refused(
    client: httpx.AsyncClient,
    store: SqlAlchemyConversationStore,
) -> None:
    """A session that is not claude-native / codex-native cannot rotate."""
    session_id = _create(store, native=False)

    response = await client.post(
        f"/v1/sessions/{session_id}/handover",
        json={"handover": "SDK note."},
    )
    assert response.status_code == 400, response.text
    assert "not_native" in response.text
    assert _handover_messages(store, session_id) == []


async def test_handover_empty_text_refused(
    client: httpx.AsyncClient,
    store: SqlAlchemyConversationStore,
) -> None:
    """Whitespace-only handover text is rejected."""
    session_id = _create(store)

    response = await client.post(
        f"/v1/sessions/{session_id}/handover",
        json={"handover": "   \n"},
    )
    assert response.status_code == 400, response.text
    assert "invalid_input" in response.text
    assert _handover_messages(store, session_id) == []
