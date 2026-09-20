"""``GET /v1/sessions`` preview + project fields (T5, R1/D8).

``include_preview=1`` fills ``last_message_preview`` from the session's
newest visible message via the shared excerpt function; without it the
key is absent (``exclude_none``). ``project_id`` rides every row from
the conversation row.
"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio

from omnigent.db.utils import generate_agent_id
from omnigent.entities.conversation import ConversationItem, MessageData
from omnigent.entities.conversation import NewConversationItem
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)


@pytest_asyncio.fixture()
async def seeded(db_uri: str) -> dict[str, str]:
    """Two sessions: one with a project + message, one bare."""
    from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore

    agent_store = SqlAlchemyAgentStore(db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    agent_id = generate_agent_id()
    agent_store.create(agent_id, name="test-agent", bundle_location="test:///bundle")
    project = SqlAlchemyProjectStore(db_uri).create(
        "c" * 32, "peer-proj", None
    )
    with_text = conv_store.create_conversation(agent_id=agent_id, project_id=project.id)
    bare = conv_store.create_conversation(agent_id=agent_id)
    conv_store.append(
        with_text.id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_1",
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": "hello from the peer session"}],
                ),
            )
        ],
    )
    return {"with_text": with_text.id, "bare": bare.id, "project_id": project.id}


async def test_preview_absent_by_default(
    client: httpx.AsyncClient, seeded: dict[str, str]
) -> None:
    """Without ``include_preview`` rows carry no preview key."""
    resp = await client.get("/v1/sessions?kind=any")
    assert resp.status_code == 200
    rows = {row["id"]: row for row in resp.json()["data"]}
    assert "last_message_preview" not in rows[seeded["with_text"]]
    assert "last_message_preview" not in rows[seeded["bare"]]


async def test_preview_present_when_requested(
    client: httpx.AsyncClient, seeded: dict[str, str]
) -> None:
    """``include_preview=1`` fills the excerpt only where messages exist."""
    resp = await client.get("/v1/sessions?kind=any&include_preview=1")
    assert resp.status_code == 200
    rows = {row["id"]: row for row in resp.json()["data"]}
    assert rows[seeded["with_text"]]["last_message_preview"] == "hello from the peer session"
    assert rows[seeded["bare"]].get("last_message_preview") is None


async def test_project_id_on_rows(
    client: httpx.AsyncClient, seeded: dict[str, str]
) -> None:
    """``project_id`` comes from the conversation row, on or off preview."""
    for query in ("/v1/sessions?kind=any", "/v1/sessions?kind=any&include_preview=1"):
        resp = await client.get(query)
        assert resp.status_code == 200
        rows = {row["id"]: row for row in resp.json()["data"]}
        assert rows[seeded["with_text"]]["project_id"] == seeded["project_id"]
        assert rows[seeded["bare"]].get("project_id") is None


async def test_preview_skips_meta_messages(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    """Hidden meta messages never surface as the preview excerpt."""
    agent_store = SqlAlchemyAgentStore(db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    agent_id = generate_agent_id()
    agent_store.create(agent_id, name="meta-agent", bundle_location="test:///bundle")
    conv = conv_store.create_conversation(agent_id=agent_id)
    conv_store.append(
        conv.id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_1",
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": "hidden runner context"}],
                    is_meta=True,
                ),
            )
        ],
    )
    resp = await client.get("/v1/sessions?kind=any&include_preview=1")
    assert resp.status_code == 200
    rows = {row["id"]: row for row in resp.json()["data"]}
    assert rows[conv.id].get("last_message_preview") is None
