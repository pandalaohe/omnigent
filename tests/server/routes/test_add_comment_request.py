"""Tests for :class:`omnigent.server.routes.comments.AddCommentRequest` validation.

``AddCommentRequest`` has a ``model_validator`` that rejects semantically
invalid range field combinations at the HTTP boundary before they reach the
store.  These tests cover each rejection branch and the valid happy-path so
that any relaxation or tightening of the validator surfaces immediately.
"""

from __future__ import annotations

import json

import httpx
import pytest
import pytest_asyncio
from pydantic import ValidationError

from omnigent.db.utils import generate_agent_id
from omnigent.entities.element_annotation import ELEMENT_ANCHOR_PREFIX
from omnigent.server.routes.comments import AddCommentRequest
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
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


def _valid_kwargs(**overrides: object) -> dict:
    """Return a dict of valid ``AddCommentRequest`` kwargs, with optional overrides.

    :param overrides: Field values to substitute in the base valid payload.
    :returns: Keyword-argument dict suitable for ``AddCommentRequest(**...)``.
    """
    base: dict = {
        "path": "src/app.py",
        "body": "Fix this",
        "start_index": 0,
        "end_index": 10,
    }
    base.update(overrides)
    return base


# ── happy path ────────────────────────────────────────────────────────────────


def test_add_comment_request_valid() -> None:
    """A comment with valid range fields constructs without error."""
    req = AddCommentRequest(**_valid_kwargs())

    assert req.start_index == 0
    assert req.end_index == 10


def test_add_comment_request_valid_zero_length_selection() -> None:
    """A zero-length selection (start_index == end_index) is valid (cursor position)."""
    req = AddCommentRequest(**_valid_kwargs(start_index=5, end_index=5))

    assert req.start_index == 5
    assert req.end_index == 5


def test_add_comment_request_valid_anchor_content_optional() -> None:
    """anchor_content defaults to None and can be supplied."""
    req_no_anchor = AddCommentRequest(**_valid_kwargs())
    assert req_no_anchor.anchor_content is None

    req_with_anchor = AddCommentRequest(**_valid_kwargs(anchor_content="selected text"))
    assert req_with_anchor.anchor_content == "selected text"


# ── start_index validation ────────────────────────────────────────────────────


@pytest.mark.parametrize("start_index", [-1, -100])
def test_add_comment_request_rejects_negative_start_index(start_index: int) -> None:
    """start_index must be >= 0; negative values are rejected.

    :param start_index: An invalid (negative) start_index value.
    """
    with pytest.raises(ValidationError, match="start_index must be >= 0"):
        AddCommentRequest(**_valid_kwargs(start_index=start_index, end_index=0))


# ── end_index validation ──────────────────────────────────────────────────────


def test_add_comment_request_rejects_end_index_before_start_index() -> None:
    """end_index must be >= start_index; a smaller end_index is rejected."""
    with pytest.raises(ValidationError, match="end_index must be >= start_index"):
        AddCommentRequest(**_valid_kwargs(start_index=10, end_index=5))


# ── element anchor validation ─────────────────────────────────────────────────


def test_add_comment_request_rejects_invalid_element_anchor() -> None:
    """A prefixed anchor that does not parse is rejected with 422."""
    with pytest.raises(ValidationError, match="invalid element anchor"):
        AddCommentRequest(
            **_valid_kwargs(anchor_content=ELEMENT_ANCHOR_PREFIX + '{"v":2,"kind":"element"}')
        )


def test_add_comment_request_rejects_unparsable_prefixed_anchor() -> None:
    """The prefix without JSON is also rejected."""
    with pytest.raises(ValidationError, match="invalid element anchor"):
        AddCommentRequest(**_valid_kwargs(anchor_content=ELEMENT_ANCHOR_PREFIX + "{oops"))


def test_add_comment_request_accepts_valid_element_anchor() -> None:
    """A well-formed element anchor is stored as its canonical form."""
    anchor_content = (
        ELEMENT_ANCHOR_PREFIX + '{"v":1,"kind":"element","rect":{"x":0,"y":0,"w":1,"h":1}}'
    )

    req = AddCommentRequest(**_valid_kwargs(anchor_content=anchor_content))

    assert req.anchor_content == (
        ELEMENT_ANCHOR_PREFIX
        + '{"v":1,"kind":"element","rect":{"x":0,"y":0,"w":1,"h":1},"screenshot":null}'
    )


def test_add_comment_request_rejects_missing_rect() -> None:
    """``rect`` is required, matching the parent codec."""
    with pytest.raises(ValidationError, match="invalid element anchor"):
        AddCommentRequest(
            **_valid_kwargs(anchor_content=ELEMENT_ANCHOR_PREFIX + '{"v":1,"kind":"element"}')
        )


async def test_add_comment_route_oversized_int_rect_is_422_not_500(
    client: httpx.AsyncClient, session_id: str
) -> None:
    """A JSON integer too large for a float is rejected at the boundary."""
    anchor = {
        "v": 1,
        "kind": "element",
        "rect": {"x": 10**400, "y": 0, "w": 1, "h": 1},
    }

    resp = await client.post(
        f"/v1/sessions/{session_id}/comments",
        json=_valid_kwargs(
            anchor_content=ELEMENT_ANCHOR_PREFIX + json.dumps(anchor, separators=(",", ":"))
        ),
    )

    assert resp.status_code == 422
