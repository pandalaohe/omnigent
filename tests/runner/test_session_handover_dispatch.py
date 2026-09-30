"""Runner dispatch and native-relay coverage for session handover."""

from __future__ import annotations

import json
from collections.abc import Iterator

import httpx
import pytest

from omnigent.runner import app as runner_app
from omnigent.runner.tool_dispatch import (
    build_native_relay_tool_schemas,
    execute_tool,
)
from omnigent.spec.types import AgentSpec

_ROTATION_NOTE = "The session will continue in a new session when this turn ends."


@pytest.fixture(autouse=True)
def _clean_pending_rotations() -> Iterator[None]:
    """Keep the module-level pending-rotation set per-test."""
    saved = set(runner_app._pending_rotations)
    runner_app._pending_rotations.clear()
    try:
        yield
    finally:
        runner_app._pending_rotations.clear()
        runner_app._pending_rotations.update(saved)


@pytest.mark.parametrize("spec", [AgentSpec(spec_version=1), None])
def test_native_relay_exposes_session_handover(spec: AgentSpec | None) -> None:
    schemas = build_native_relay_tool_schemas(spec)

    handover = next(schema for schema in schemas if schema["name"] == "sys_session_handover")

    assert handover["parameters"]["required"] == ["handover"]
    assert handover["parameters"]["additionalProperties"] is False
    assert handover["parameters"]["properties"]["rotate"]["default"] is True


@pytest.mark.asyncio
async def test_session_handover_dispatch_records_pending_rotation() -> None:
    """A successful rotating handover arms the runner's turn-end /clear."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"item_id": "msg_handover", "rotate": True})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://server",
    ) as server_client:
        output = await execute_tool(
            tool_name="sys_session_handover",
            arguments=json.dumps({"handover": "Continue the migration."}),
            server_client=server_client,
            conversation_id="conv_current",
        )

    assert json.loads(output) == {
        "recorded": True,
        "item_id": "msg_handover",
        "rotate": True,
        "note": _ROTATION_NOTE,
    }
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/v1/sessions/conv_current/handover"
    assert json.loads(requests[0].content) == {
        "handover": "Continue the migration.",
        "rotate": True,
    }
    assert runner_app.pop_pending_rotation("conv_current") is True


@pytest.mark.asyncio
async def test_session_handover_without_rotation_arms_nothing() -> None:
    """``rotate=false`` records the note and leaves the idle edge alone."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"item_id": "msg_handover", "rotate": False})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://server",
    ) as server_client:
        output = await execute_tool(
            tool_name="sys_session_handover",
            arguments=json.dumps({"handover": "Context only.", "rotate": False}),
            server_client=server_client,
            conversation_id="conv_current",
        )

    assert json.loads(output) == {
        "recorded": True,
        "item_id": "msg_handover",
        "rotate": False,
    }
    assert runner_app.pop_pending_rotation("conv_current") is False


@pytest.mark.asyncio
async def test_session_handover_preserves_server_refusal() -> None:
    """A refused handover surfaces the server status without a tool crash."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"code": "invalid_input"}})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://server",
    ) as server_client:
        output = await execute_tool(
            tool_name="sys_session_handover",
            arguments=json.dumps({"handover": "Child note."}),
            server_client=server_client,
            conversation_id="conv_child",
        )

    payload = json.loads(output)
    assert "returned 400" in payload["error"]
    assert runner_app.pop_pending_rotation("conv_child") is False


@pytest.mark.asyncio
async def test_session_handover_validates_arguments() -> None:
    """Empty text and a non-boolean rotate never reach the server."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"item_id": "msg", "rotate": True})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://server",
    ) as server_client:
        empty = await execute_tool(
            tool_name="sys_session_handover",
            arguments=json.dumps({"handover": "   "}),
            server_client=server_client,
            conversation_id="conv_current",
        )
        bad_rotate = await execute_tool(
            tool_name="sys_session_handover",
            arguments=json.dumps({"handover": "Note.", "rotate": "yes"}),
            server_client=server_client,
            conversation_id="conv_current",
        )

    assert "non-empty string 'handover'" in json.loads(empty)["error"]
    assert "boolean 'rotate'" in json.loads(bad_rotate)["error"]
    assert requests == []
