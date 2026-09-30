"""``GET /v1/sessions`` preview + project fields (T5, R1/D8).

``include_preview=1`` fills ``last_message_preview`` from the session's
newest visible message via the shared excerpt function; without it the
key is absent (``exclude_none``). The single-session form also fills
``last_message_tail`` (the newest assistant reply's end) from the same
items read. ``project_id`` rides every row from the conversation row.
"""

from __future__ import annotations

import httpx
import pytest_asyncio

from omnigent.db.utils import generate_agent_id
from omnigent.entities.conversation import MessageData, NewConversationItem
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
    project = SqlAlchemyProjectStore(db_uri).create("c" * 32, "peer-proj", None)
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


async def test_project_id_on_rows(client: httpx.AsyncClient, seeded: dict[str, str]) -> None:
    """``project_id`` comes from the conversation row, on or off preview."""
    for query in ("/v1/sessions?kind=any", "/v1/sessions?kind=any&include_preview=1"):
        resp = await client.get(query)
        assert resp.status_code == 200
        rows = {row["id"]: row for row in resp.json()["data"]}
        assert rows[seeded["with_text"]]["project_id"] == seeded["project_id"]
        assert rows[seeded["bare"]].get("project_id") is None


async def test_preview_skips_meta_messages(client: httpx.AsyncClient, db_uri: str) -> None:
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


async def test_tail_returns_newest_assistant_message(
    client: httpx.AsyncClient, seeded: dict[str, str], db_uri: str
) -> None:
    """``include_preview=true`` fills the tail from the assistant reply."""
    conv_store = SqlAlchemyConversationStore(db_uri)
    conv_store.append(
        seeded["with_text"],
        [
            NewConversationItem(
                type="message",
                response_id="resp_2",
                data=MessageData(
                    role="assistant",
                    agent="test-agent",
                    content=[{"type": "output_text", "text": "done: all tests pass"}],
                ),
            )
        ],
    )
    resp = await client.get(
        f"/v1/sessions/{seeded['with_text']}", params={"include_preview": "true"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["last_message_tail"] == "done: all tests pass"
    # Preview and tail come from the same items read; the assistant reply
    # is also the newest visible message here.
    assert body["last_message_preview"] == "done: all tests pass"


async def test_tail_skips_newer_user_message(
    client: httpx.AsyncClient, seeded: dict[str, str], db_uri: str
) -> None:
    """A newer USER turn does not replace the last assistant reply."""
    conv_store = SqlAlchemyConversationStore(db_uri)
    conv_store.append(
        seeded["with_text"],
        [
            NewConversationItem(
                type="message",
                response_id="resp_2",
                data=MessageData(
                    role="assistant",
                    agent="test-agent",
                    content=[{"type": "output_text", "text": "the assistant concluded"}],
                ),
            ),
            NewConversationItem(
                type="message",
                response_id="resp_3",
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": "a newer question"}],
                ),
            ),
        ],
    )
    resp = await client.get(
        f"/v1/sessions/{seeded['with_text']}", params={"include_preview": "true"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["last_message_tail"] == "the assistant concluded"
    assert body["last_message_preview"] == "a newer question"


async def test_tail_keeps_line_breaks(
    client: httpx.AsyncClient, seeded: dict[str, str], db_uri: str
) -> None:
    """Unlike the one-line preview, the tail preserves newlines."""
    text = "first line\nsecond line\nthird line"
    conv_store = SqlAlchemyConversationStore(db_uri)
    conv_store.append(
        seeded["with_text"],
        [
            NewConversationItem(
                type="message",
                response_id="resp_2",
                data=MessageData(
                    role="assistant",
                    agent="test-agent",
                    content=[{"type": "output_text", "text": text}],
                ),
            )
        ],
    )
    resp = await client.get(
        f"/v1/sessions/{seeded['with_text']}", params={"include_preview": "true"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["last_message_tail"] == text
    assert body["last_message_preview"] == "first line second line third line"


async def test_tail_truncates_to_limit_with_leading_ellipsis(
    client: httpx.AsyncClient, seeded: dict[str, str], db_uri: str
) -> None:
    """Over-limit tails keep the END and mark the cut with ``"…"``.

    The tail is the concluding words of the reply, so truncation must
    drop the head — the opposite end from ``last_message_preview``.
    """
    from omnigent.server.routes._sessions.common import _ASSISTANT_TAIL_LIMIT

    text = "start " + "a" * (_ASSISTANT_TAIL_LIMIT + 100)
    conv_store = SqlAlchemyConversationStore(db_uri)
    conv_store.append(
        seeded["with_text"],
        [
            NewConversationItem(
                type="message",
                response_id="resp_2",
                data=MessageData(
                    role="assistant",
                    agent="test-agent",
                    content=[{"type": "output_text", "text": text}],
                ),
            )
        ],
    )
    resp = await client.get(
        f"/v1/sessions/{seeded['with_text']}", params={"include_preview": "true"}
    )
    assert resp.status_code == 200
    tail = resp.json()["last_message_tail"]
    assert tail == "…" + text[-(_ASSISTANT_TAIL_LIMIT - 1) :].lstrip()
    assert len(tail) == _ASSISTANT_TAIL_LIMIT
    assert tail.startswith("…")


async def test_tail_null_without_preview(
    client: httpx.AsyncClient, seeded: dict[str, str], db_uri: str
) -> None:
    """The tail key stays null unless ``include_preview=true``."""
    conv_store = SqlAlchemyConversationStore(db_uri)
    conv_store.append(
        seeded["with_text"],
        [
            NewConversationItem(
                type="message",
                response_id="resp_2",
                data=MessageData(
                    role="assistant",
                    agent="test-agent",
                    content=[{"type": "output_text", "text": "not fetched by default"}],
                ),
            )
        ],
    )
    resp = await client.get(f"/v1/sessions/{seeded['with_text']}")
    assert resp.status_code == 200
    assert resp.json().get("last_message_tail") is None


async def test_tail_null_without_assistant_message(
    client: httpx.AsyncClient, seeded: dict[str, str]
) -> None:
    """A session with only USER text has no tail (but still a preview)."""
    resp = await client.get(
        f"/v1/sessions/{seeded['with_text']}", params={"include_preview": "true"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["last_message_tail"] is None
    assert body["last_message_preview"] == "hello from the peer session"
