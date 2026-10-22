"""Instruction tools are always available and cannot omit the write guard."""

import json

import httpx
import pytest
from jsonschema import validate

from omnigent.harnesses.cursor_native.bridge import build_mcp_config
from omnigent.runner.tool_dispatch import (
    _ALL_LOCAL_TOOLS,
    _NATIVE_RELAY_BUILTIN_TOOLS,
    execute_tool,
)
from omnigent.spec import AgentSpec
from omnigent.tools.manager import ToolManager


def test_instruction_tools_registered_and_relayed():
    manager = ToolManager(AgentSpec(spec_version=1))
    schemas = {s["function"]["name"]: s["function"] for s in manager.get_tool_schemas()}
    for name in ("sys_instruction_layers_read", "sys_instruction_layer_update"):
        assert name in _ALL_LOCAL_TOOLS & _NATIVE_RELAY_BUILTIN_TOOLS
        validate(
            {"scope": "shared", "text": "", "expected_revision_id": None}
            if name.endswith("update")
            else {"mode": "layers"},
            schemas[name]["parameters"],
        )


def test_cursor_exposes_instruction_tools_without_a_second_native_prompt(tmp_path):
    config = build_mcp_config(tmp_path)
    tools = config["mcpServers"]["omnigent"]["autoApprove"]
    assert {"sys_instruction_layers_read", "sys_instruction_layer_update"} <= set(tools)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args",
    [
        [],
        {"scope": "shared", "text": "unguarded"},
        {"scope": "shared", "text": "x", "expected_revision_id": None, "user_id": "admin"},
    ],
)
async def test_invalid_updates_never_reach_server(args):
    requests = []

    def receive(request):
        requests.append(request)
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(
        base_url="http://server", transport=httpx.MockTransport(receive)
    ) as client:
        output = await execute_tool(
            tool_name="sys_instruction_layer_update",
            arguments=json.dumps(args),
            server_client=client,
        )
    assert "error" in json.loads(output)
    assert requests == []
