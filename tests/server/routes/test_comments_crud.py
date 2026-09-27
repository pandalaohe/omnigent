"""Tests for the comments CRUD routes (``/v1/sessions/{id}/comments``)."""

from __future__ import annotations

import httpx
import pytest_asyncio

from omnigent.db.utils import generate_agent_id
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
from omnigent.stores.comment_store.visitor_comments import (
    VISITOR_COMMENT_NOTE,
    visitor_author,
)
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)


@pytest_asyncio.fixture()
async def session_id(db_uri: str) -> str:
    """Seed a test agent and conversation, return the session ID."""
    agent_store = SqlAlchemyAgentStore(db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    agent_id = generate_agent_id()
    agent_store.create(agent_id, name="comment-test-agent", bundle_location="test:///bundle")
    conv = conv_store.create_conversation(agent_id=agent_id)
    return conv.id


def _comment_payload(**overrides: object) -> dict:
    """Build a valid AddCommentRequest payload."""
    base: dict = {
        "path": "src/App.tsx",
        "body": "Fix the import",
        "start_index": 0,
        "end_index": 10,
        "anchor_content": "import React",
    }
    base.update(overrides)  # type: ignore[arg-type]
    return base


# ── POST /sessions/{id}/comments ─────────────────────────────────────


async def test_add_comment(client: httpx.AsyncClient, session_id: str) -> None:
    """Adding a comment returns the serialized comment."""
    resp = await client.post(f"/v1/sessions/{session_id}/comments", json=_comment_payload())
    assert resp.status_code == 200
    body = resp.json()
    assert body["path"] == "src/App.tsx"
    assert body["body"] == "Fix the import"
    assert "id" in body


async def test_add_comment_negative_start_index(
    client: httpx.AsyncClient, session_id: str
) -> None:
    """Negative start_index is rejected with 422."""
    resp = await client.post(
        f"/v1/sessions/{session_id}/comments",
        json=_comment_payload(start_index=-1),
    )
    assert resp.status_code == 422


async def test_add_comment_end_before_start(client: httpx.AsyncClient, session_id: str) -> None:
    """end_index < start_index is rejected with 422."""
    resp = await client.post(
        f"/v1/sessions/{session_id}/comments",
        json=_comment_payload(start_index=10, end_index=5),
    )
    assert resp.status_code == 422


async def test_add_comment_nonexistent_session_returns_404_without_persisting(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    """Adding a comment to a nonexistent single-user session returns 404."""
    missing_session_id = "1d0b12236c77f69f5073a53583de1a3f"

    resp = await client.post(
        f"/v1/sessions/{missing_session_id}/comments",
        json=_comment_payload(),
    )

    assert resp.status_code == 404
    assert SqlAlchemyCommentStore(db_uri).list_for_conversation(missing_session_id) == []


# ── GET /sessions/{id}/comments ──────────────────────────────────────


async def test_list_comments_empty(client: httpx.AsyncClient, session_id: str) -> None:
    """Empty comments list returns []."""
    resp = await client.get(f"/v1/sessions/{session_id}/comments")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_list_comments_after_add(client: httpx.AsyncClient, session_id: str) -> None:
    """Comments appear in the list after adding."""
    add_resp = await client.post(f"/v1/sessions/{session_id}/comments", json=_comment_payload())
    comment_id = add_resp.json()["id"]

    resp = await client.get(f"/v1/sessions/{session_id}/comments")
    assert resp.status_code == 200
    ids = [c["id"] for c in resp.json()]
    assert comment_id in ids


async def test_list_comments_filter_by_path(client: httpx.AsyncClient, session_id: str) -> None:
    """Path filter returns only matching comments."""
    await client.post(f"/v1/sessions/{session_id}/comments", json=_comment_payload(path="a.py"))
    await client.post(f"/v1/sessions/{session_id}/comments", json=_comment_payload(path="b.py"))

    resp = await client.get(f"/v1/sessions/{session_id}/comments?path=a.py")
    assert resp.status_code == 200
    paths = {c["path"] for c in resp.json()}
    assert paths == {"a.py"}


async def test_list_comments_hides_visitor_drafts_and_marks_visitor_rows(
    client: httpx.AsyncClient, session_id: str, db_uri: str
) -> None:
    """Visitor drafts stay hidden; listed visitor rows carry the untrusted fields.

    A visitor comment that has not been sent to the agent is feedback the
    owner never vouched for, so the agent-facing listing must omit it. Once
    addressed (sent), it must still be visibly marked as untrusted, while
    owner rows stay byte-identical to today.
    """
    store = SqlAlchemyCommentStore(db_uri)
    owner = store.add(session_id, "src/App.tsx", "owner note", 0, 5)
    visitor_draft = store.add(
        session_id,
        "src/App.tsx",
        "unsent visitor note",
        10,
        15,
        created_by=visitor_author("Alice"),
    )
    visitor_sent = store.add(
        session_id,
        "src/App.tsx",
        "sent visitor note",
        20,
        25,
        created_by=visitor_author(""),
    )
    store.update_comment(visitor_sent.id, session_id, status="addressed")

    resp = await client.get(f"/v1/sessions/{session_id}/comments")
    assert resp.status_code == 200
    rows = {row["id"]: row for row in resp.json()}

    assert visitor_draft.id not in rows, "Unsent visitor feedback must be hidden from the listing"
    assert set(rows) == {owner.id, visitor_sent.id}

    # Non-visitor rows must not gain the new fields.
    assert "source" not in rows[owner.id]
    assert "note" not in rows[owner.id]

    marked = rows[visitor_sent.id]
    assert marked["source"] == "visitor"
    assert marked["note"] == VISITOR_COMMENT_NOTE


async def test_list_comments_include_visitor_drafts(
    client: httpx.AsyncClient, session_id: str, db_uri: str
) -> None:
    """``include_visitor_drafts`` returns drafts, still marked untrusted."""
    store = SqlAlchemyCommentStore(db_uri)
    visitor_draft = store.add(
        session_id,
        "src/App.tsx",
        "unsent visitor note",
        10,
        15,
        created_by=visitor_author("Alice"),
    )

    resp = await client.get(
        f"/v1/sessions/{session_id}/comments",
        params={"include_visitor_drafts": "1"},
    )
    assert resp.status_code == 200
    rows = {row["id"]: row for row in resp.json()}
    assert visitor_draft.id in rows
    assert rows[visitor_draft.id]["source"] == "visitor"
    assert rows[visitor_draft.id]["note"] == VISITOR_COMMENT_NOTE


# ── PATCH /sessions/{id}/comments/{comment_id} ───────────────────────


async def test_update_comment_status(client: httpx.AsyncClient, session_id: str) -> None:
    """Updating a comment's status returns the updated comment."""
    add_resp = await client.post(f"/v1/sessions/{session_id}/comments", json=_comment_payload())
    cid = add_resp.json()["id"]

    resp = await client.patch(
        f"/v1/sessions/{session_id}/comments/{cid}",
        json={"status": "addressed"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "addressed"


async def test_update_comment_not_found(client: httpx.AsyncClient, session_id: str) -> None:
    """Updating a nonexistent comment returns 404."""
    resp = await client.patch(
        f"/v1/sessions/{session_id}/comments/nonexistent_id",
        json={"status": "addressed"},
    )
    assert resp.status_code == 404


# ── DELETE /sessions/{id}/comments/{comment_id} ──────────────────────


async def test_delete_comment(client: httpx.AsyncClient, session_id: str) -> None:
    """Deleting a comment returns deleted: true."""
    add_resp = await client.post(f"/v1/sessions/{session_id}/comments", json=_comment_payload())
    cid = add_resp.json()["id"]

    resp = await client.delete(f"/v1/sessions/{session_id}/comments/{cid}")
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True

    # Verify it's gone
    list_resp = await client.get(f"/v1/sessions/{session_id}/comments")
    ids = [c["id"] for c in list_resp.json()]
    assert cid not in ids


async def test_delete_comment_not_found(client: httpx.AsyncClient, session_id: str) -> None:
    """Deleting a nonexistent comment returns 404."""
    resp = await client.delete(f"/v1/sessions/{session_id}/comments/nonexistent_id")
    assert resp.status_code == 404


# ── POST /sessions/{id}/comments/send ────────────────────────────────


async def test_send_comments(client: httpx.AsyncClient, session_id: str) -> None:
    """Sending comments returns formatted message and sent IDs."""
    add_resp = await client.post(f"/v1/sessions/{session_id}/comments", json=_comment_payload())
    cid = add_resp.json()["id"]

    resp = await client.post(
        f"/v1/sessions/{session_id}/comments/send",
        json={"comment_ids": [cid]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert cid in body["sent_comment_ids"]
    assert "formatted_message" in body
    assert "review comments" in body["formatted_message"].lower()


async def test_send_comments_mark_addressed_false_leaves_status(
    client: httpx.AsyncClient, session_id: str
) -> None:
    """``mark_addressed: false`` formats the message but marks nothing.

    A caller that delivers the formatted message itself (e.g. the web app
    pinning a send to a session) marks the comments afterwards, so a failed
    delivery must not silently consume them.
    """
    add_resp = await client.post(f"/v1/sessions/{session_id}/comments", json=_comment_payload())
    cid = add_resp.json()["id"]

    resp = await client.post(
        f"/v1/sessions/{session_id}/comments/send",
        json={"comment_ids": [cid], "mark_addressed": False},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["sent_comment_ids"] == [cid]
    assert "Fix the import" in body["formatted_message"]

    list_resp = await client.get(f"/v1/sessions/{session_id}/comments")
    statuses = {c["id"]: c["status"] for c in list_resp.json()}
    assert statuses[cid] == "draft", "mark_addressed=false must leave the comment unmarked"


async def test_send_comments_not_found(client: httpx.AsyncClient, session_id: str) -> None:
    """Sending with a nonexistent comment ID returns 404."""
    resp = await client.post(
        f"/v1/sessions/{session_id}/comments/send",
        json={"comment_ids": ["nonexistent_id"]},
    )
    assert resp.status_code == 404
