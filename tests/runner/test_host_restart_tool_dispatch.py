"""Restart tool discovery, explicit previews and execution input."""

import json

import httpx
import pytest
from jsonschema import validate

from omnigent.harnesses.antigravity_native.bridge import _AGY_ENABLED_TOOLS
from omnigent.harnesses.cursor_native.bridge import build_mcp_config
from omnigent.runner.host_restart import HOST_RESTART_TOOL_HEADER
from omnigent.runner.tool_dispatch import (
    _ALL_LOCAL_TOOLS,
    _NATIVE_RELAY_BUILTIN_TOOLS,
    execute_tool,
)
from omnigent.spec import AgentSpec
from omnigent.tools.manager import ToolManager


def test_restart_tool_registered_and_relayed(tmp_path):
    name = "sys_host_restart"
    schemas = {
        schema["function"]["name"]: schema["function"]
        for schema in ToolManager(AgentSpec(spec_version=1)).get_tool_schemas()
    }
    assert name in _ALL_LOCAL_TOOLS & _NATIVE_RELAY_BUILTIN_TOOLS
    assert name in build_mcp_config(tmp_path)["mcpServers"]["omnigent"]["autoApprove"]
    assert name in _AGY_ENABLED_TOOLS
    validate({"host": "test"}, schemas[name]["parameters"])


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run,preview_id", [(True, None), (False, None), (False, "approved")])
async def test_restart_dispatch_uses_approved_preview(dry_run, preview_id):
    calls = []

    def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json={"hosts": [{"host_id": "host-a", "name": "test"}]})
        body = json.loads(request.content)
        calls.append((body, request.headers.get(HOST_RESTART_TOOL_HEADER)))
        if body["dry_run"]:
            return httpx.Response(200, json={"preview_id": "approved"})
        return httpx.Response(200, json={"request_id": "restart", "phase": "draining"})

    async with httpx.AsyncClient(
        base_url="http://server", transport=httpx.MockTransport(handle)
    ) as client:
        args = {"host": "test", "dry_run": dry_run}
        if preview_id is not None:
            args["preview_id"] = preview_id
        result = json.loads(
            await execute_tool(
                tool_name="sys_host_restart",
                arguments=json.dumps(args),
                server_client=client,
                conversation_id="requester",
            )
        )
    assert result == (
        {"preview_id": "approved"} if dry_run else {"request_id": "restart", "phase": "draining"}
    )
    assert all(body["requested_by_session_id"] == "requester" for body, _ in calls)
    if not dry_run:
        assert calls[-1][0]["preview_id"] == "approved"
        assert all(header == "1" for _, header in calls)
    else:
        assert calls[0][1] is None
