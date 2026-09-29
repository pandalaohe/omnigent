"""Runner REST paths for session archive / unarchive and the close fix."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from omnigent.util.session_lifecycle import CLOSED_LABEL_KEY, CLOSED_LABEL_VALUE


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    """Build an AsyncClient answered by ``handler``."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://server")


@pytest.mark.asyncio
async def test_session_close_closes_created_child_with_plain_title() -> None:
    """
    ``sys_session_close`` closes a ``sys_session_create`` child whose title
    is not ``"<agent>:<title>"``.

    ``sys_session_create`` stores the caller's title verbatim, so a plain
    title such as ``"build fix"`` has no colon. The tree gate (same root,
    has a parent) is what makes it a sub-agent; the title shape must not
    refuse it. The tombstone keeps the display title and frees the
    duplicate-title slot.
    """
    from omnigent.runner.tool_dispatch import _execute_session_query_tool

    patched: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/sessions/conv_child":
            return httpx.Response(
                200,
                json={
                    "id": "conv_child",
                    "title": "build fix",
                    "agent_name": "claude-native-ui",
                    "sub_agent_name": None,
                    "root_conversation_id": "conv_root",
                    "parent_session_id": "conv_root",
                },
            )
        if request.method == "GET" and request.url.path == "/v1/sessions/conv_root":
            return httpx.Response(200, json={"id": "conv_root", "root_conversation_id": "conv_root"})
        if request.method == "PATCH" and request.url.path == "/v1/sessions/conv_child":
            patched.update(json.loads(request.content))
            return httpx.Response(200, json={"id": "conv_child"})
        raise AssertionError(f"unexpected {request.method} {request.url.path}")

    async with _client(handler) as client:
        out = json.loads(
            await _execute_session_query_tool(
                "sys_session_close",
                json.dumps({"conversation_id": "conv_child"}),
                conversation_id="conv_root",
                server_client=client,
            )
        )
    assert out["closed"] is True
    assert out["conversation_id"] == "conv_child"
    assert out["title"] == "build fix"
    assert patched["title"] == "build fix:closed:conv_child"
    assert patched["labels"] == {CLOSED_LABEL_KEY: CLOSED_LABEL_VALUE}
    assert patched["archived"] is True
