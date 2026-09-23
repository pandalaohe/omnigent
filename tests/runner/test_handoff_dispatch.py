"""The hand-off runner tools call the server routes with the caller's identity."""

from __future__ import annotations

import json

import httpx
import pytest

from omnigent.runner import tool_dispatch
from omnigent.runner.tool_dispatch import _execute_handoff_tool
from omnigent.spec.types import AgentSpec

_CALLER = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
_HID = "00112233445566778899aabbccddeeff"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "args", "method", "path", "body", "timeout"),
    [
        (
            "sys_session_handoff",
            {"project": "Omnigent", "task": "Review changes", "allow_onward": True},
            "POST",
            f"/v1/sessions/{_CALLER}/handoffs",
            {"project": "Omnigent", "task": "Review changes", "allow_onward": True},
            90.0,
        ),
        (
            "sys_session_handoff",
            {"action": "status", "handoff_id": _HID},
            "GET",
            f"/v1/handoffs/{_HID}",
            None,
            30.0,
        ),
        (
            "sys_session_handoff",
            {"action": "status"},
            "GET",
            f"/v1/sessions/{_CALLER}/handoffs",
            None,
            30.0,
        ),
        (
            "sys_session_handoff",
            {"action": "cancel", "handoff_id": _HID},
            "POST",
            f"/v1/handoffs/{_HID}/cancel",
            None,
            30.0,
        ),
        (
            "sys_handoff_report",
            {
                "handoff_id": _HID,
                "status": "incomplete",
                "summary": "Partial",
                "done": ["A"],
                "not_done": ["B"],
            },
            "POST",
            f"/v1/handoffs/{_HID}/report",
            {"status": "incomplete", "summary": "Partial", "done": ["A"], "not_done": ["B"]},
            30.0,
        ),
    ],
)
async def test_routes(
    tool: str, args: dict[str, object], method: str, path: str, body: object, timeout: float
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json=[]
            if path == f"/v1/sessions/{_CALLER}/handoffs" and method == "GET"
            else {"handoff_id": _HID, "state": "open"},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server"
    ) as client:
        output = await _execute_handoff_tool(
            tool, args, server_client=client, conversation_id=_CALLER
        )
    assert seen[0].method == method
    assert seen[0].url.path == path
    if body is not None:
        assert json.loads(seen[0].content) == body
    else:
        assert not seen[0].content
    assert seen[0].extensions["timeout"]["read"] == timeout
    assert json.loads(output) == (
        []
        if path == f"/v1/sessions/{_CALLER}/handoffs" and method == "GET"
        else {"handoff_id": _HID, "state": "open"}
    )


@pytest.mark.asyncio
async def test_start_transport_error_tells_agent_to_check_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server"
    ) as client:
        output = await _execute_handoff_tool(
            "sys_session_handoff",
            {"project": "P", "task": "T"},
            server_client=client,
            conversation_id=_CALLER,
        )
    assert "status" in json.loads(output)["message"]


@pytest.mark.asyncio
async def test_handoff_tool_rejects_report_action_without_calling_route() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected route: {request.url.path}")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server"
    ) as client:
        output = await _execute_handoff_tool(
            "sys_session_handoff",
            {"action": "report"},
            server_client=client,
            conversation_id=_CALLER,
        )
    assert json.loads(output)["error"] == "invalid_handoff_args"


@pytest.mark.asyncio
async def test_report_conflict_is_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"handoff_id": _HID, "state": "completed"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server"
    ) as client:
        output = await _execute_handoff_tool(
            "sys_handoff_report",
            {"handoff_id": _HID, "status": "completed", "summary": "Done"},
            server_client=client,
            conversation_id=_CALLER,
        )
    assert json.loads(output)["error"] == "handoff_conflict"


@pytest.mark.asyncio
async def test_execute_tool_grant_and_dispatch_follow_peer_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server"
    ) as client:
        for enabled in (False, True):
            monkeypatch.setattr(
                tool_dispatch, "_peer_messaging_enabled_for", lambda _cid, flag=enabled: flag
            )
            output = await tool_dispatch.execute_tool(
                tool_name="sys_session_handoff",
                arguments=json.dumps({"action": "status"}),
                agent_spec=AgentSpec(spec_version=1),
                conversation_id=_CALLER,
                server_client=client,
            )
            assert (json.loads(output) == []) is enabled
    assert len(calls) == 1
