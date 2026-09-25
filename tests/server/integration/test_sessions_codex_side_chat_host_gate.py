"""Integration test for the codex ``/side`` old-host gate.

A web ``/side`` to a codex-native pane forks inside the host's Codex
app-server, so the server must refuse it up front when the connected host
predates the capability. The gate hangs off ``opens_side_chat`` in the native
dispatch, which needs the pane's resolved harness; an un-awaited harness probe
silently read as "not codex" and disabled both the refusal and the bubble
cleanup that replaces it.

The gate must classify exactly what the executor forks. Text-only content
normalizes to one Codex input item, so several text blocks still open a side
chat when the joined text starts with ``/side``; any multimodal block
(``input_image`` / ``input_file`` / ``input_audio``) keeps the content a block
list, which forks across several input items and reaches the main thread
instead.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from omnigent.host.frames import HostHelloFrame
from omnigent.runtime import pending_inputs
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.host_store import HostStore
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio

# Uuid-shaped so the host registry's canonical keying accepts it.
_HOST_ID = "6b9c07bfb42f687d53af44f018adeb19"


async def _create_old_host_codex_session(
    client: httpx.AsyncClient,
    app: Any,
    db_uri: str,
    *,
    name: str,
) -> str:
    """Connect a host without the side-chat capability and return its session.

    :param client: Test HTTP client.
    :param app: Test app carrying ``host_registry``.
    :param db_uri: Test database URI.
    :param name: Agent name for the created session.
    :returns: The codex-native session id bound to the old host.
    """
    HostStore(db_uri).upsert_on_connect(_HOST_ID, "old-host", "owner@example.com")
    # No CAP_CODEX_SIDE_CHAT: the host build cannot fork a side chat.
    app.state.host_registry.register(
        _HOST_ID,
        AsyncMock(),
        HostHelloFrame(version="0.1.0-test", frame_protocol_version=1, name="old-host"),
        owner="owner@example.com",
    )
    agent = await create_test_agent(client, name=name)
    created = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "harness_override": "codex-native"},
    )
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    SqlAlchemyConversationStore(db_uri).set_host_id(
        session_id, host_id=_HOST_ID, workspace="/tmp/codex-side"
    )
    return session_id


async def _post_user_content(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    session_id: str,
    content: list[dict[str, str]],
) -> httpx.Response:
    """Post a user message with the runner mocked to accept it.

    :param client: Test HTTP client.
    :param monkeypatch: Patch handle for the route's runner lookup.
    :param session_id: Session to post the event to.
    :param content: User message content blocks.
    :returns: The ``POST /events`` response.
    """
    from omnigent.server.routes import sessions as sessions_module
    from omnigent.server.routes._sessions import orchestration as orchestration_module

    async with httpx.AsyncClient(
        base_url="http://runner.test",
        transport=httpx.MockTransport(lambda _request: httpx.Response(202, json={})),
    ) as runner:
        monkeypatch.setattr(sessions_module, "_get_runner_client", AsyncMock(return_value=runner))
        monkeypatch.setattr(
            orchestration_module, "_get_runner_client", AsyncMock(return_value=runner)
        )
        return await client.post(
            f"/v1/sessions/{session_id}/events",
            json={"type": "message", "data": {"role": "user", "content": content}},
        )


async def test_codex_side_command_is_refused_on_a_host_without_the_capability(
    client: httpx.AsyncClient,
    app: Any,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An old host's codex-native ``/side`` 400s before anything is forwarded."""
    session_id = await _create_old_host_codex_session(
        client, app, db_uri, name="codex-side-old-host"
    )
    resp = await _post_user_content(
        client,
        monkeypatch,
        session_id,
        [{"type": "input_text", "text": "/side why does this fail?"}],
    )

    assert resp.status_code == 400, resp.text
    error = resp.json()["error"]
    assert error["code"] == "invalid_input"
    assert "too old to open a Codex side chat" in error["message"]
    # The refused command must not leave an optimistic bubble behind: a side
    # chat never mirrors the message back, so the dispatch skips recording it.
    assert not pending_inputs.has_pending(session_id)


async def test_side_command_with_a_second_text_block_is_gated_as_a_side_chat(
    client: httpx.AsyncClient,
    app: Any,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Text-only content joins into one item, so a second text block still forks.

    The executor joins every text block with a newline into a single Codex
    input item, so :func:`side_chat_question` sees the ``/side`` command and
    the message never reaches the parent thread. On an old host the gate must
    refuse it rather than let the web side chat hang.
    """
    session_id = await _create_old_host_codex_session(
        client, app, db_uri, name="codex-side-multi-text"
    )
    resp = await _post_user_content(
        client,
        monkeypatch,
        session_id,
        [
            {"type": "input_text", "text": "/side why does this fail?"},
            {"type": "input_text", "text": "more"},
        ],
    )

    assert resp.status_code == 400, resp.text
    error = resp.json()["error"]
    assert error["code"] == "invalid_input"
    assert "too old to open a Codex side chat" in error["message"]
    assert not pending_inputs.has_pending(session_id)


async def test_side_command_with_a_multimodal_block_is_not_gated_as_a_side_chat(
    client: httpx.AsyncClient,
    app: Any,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A multimodal block splits the message into several items: not a side chat.

    Content carrying an ``input_image`` stays a block list, which normalizes to
    several Codex input items, so :func:`side_chat_question` returns ``None``
    and the message reaches the parent thread. The gate must accept it like any
    other message and keep the optimistic bubble record.
    """
    session_id = await _create_old_host_codex_session(
        client, app, db_uri, name="codex-side-text-image"
    )
    resp = await _post_user_content(
        client,
        monkeypatch,
        session_id,
        [
            {"type": "input_text", "text": "/side why does this fail?"},
            {
                "type": "input_image",
                "image_url": (
                    "data:image/png;base64,"
                    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
                    "AAAADUlEQVR42mP8/5+hHgAHggJ/PchI7wAAAABJRU5ErkJggg=="
                ),
            },
        ],
    )

    assert resp.status_code == 202, resp.text
    assert resp.json().get("pending_id")
    assert pending_inputs.has_pending(session_id)
