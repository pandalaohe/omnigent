"""Linked terminal tools cross the authenticated server client boundary."""

import json

import httpx
import pytest

from omnigent.runner.tool_dispatch import _execute_terminal_tool
from omnigent.spec import AgentSpec
from omnigent.terminals import TerminalRegistry


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["read", "send", "close", "request_access"])
@pytest.mark.parametrize("status", [200, 403, 404, 409])
async def test_linked_terminal_tool_relay(operation, status):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(
            status,
            json={"status": "granted"}
            if operation == "request_access"
            else {"screen": "remote output"},
        )

    args = {
        "host_id": "remote-host",
        "terminal": "ignored",
        "session": "a" * 32,
        "text": "echo probe",
        "tier": "read",
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://server"
    ) as client:
        output = json.loads(
            await _execute_terminal_tool(
                "sys_terminal_" + operation,
                args,
                terminal_registry=TerminalRegistry(),
                agent_spec=AgentSpec(spec_version=1),
                conversation_id="session",
                task_id="test",
                agent_id="test",
                server_client=client,
            )
        )
    assert len(requests) == 1
    assert (
        requests[0].url.path
        == f"/v1/sessions/session/host-terminals/remote-host/{'a' * 32}/agent/{operation}"
    )
    assert json.loads(requests[0].content) == args
    if status == 200:
        assert "error" not in output
    else:
        assert output["status"] == status and "error" in output


@pytest.mark.asyncio
async def test_terminal_list_includes_linked_inventory_without_local_terminals():
    def handle(request):
        assert request.url.path == "/v1/sessions/session/host-terminals"
        return httpx.Response(
            200,
            json={
                "complete": False,
                "terminals": [
                    {
                        "terminal_id": "a" * 32,
                        "host_id": "remote-host",
                        "host_name": "Remote machine",
                        "instance_id": "one",
                        "agent_tier": "read",
                        "elevated": True,
                    }
                ],
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://server"
    ) as client:
        output = json.loads(
            await _execute_terminal_tool(
                "sys_terminal_list",
                {},
                terminal_registry=TerminalRegistry(),
                agent_spec=AgentSpec(spec_version=1),
                conversation_id="session",
                task_id="test",
                agent_id="test",
                server_client=client,
            )
        )
    remote = next(t for t in output if t.get("host_id") == "remote-host")
    assert remote["agent_tier"] == "read" and remote["elevated"] is True
    assert remote["session"] == "a" * 32
    assert {"linked_unavailable": True} in output
