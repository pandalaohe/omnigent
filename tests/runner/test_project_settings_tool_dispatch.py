"""Project settings tools are registered and use authenticated server transport."""

import json

import httpx
import pytest
from jsonschema import validate

from omnigent.harnesses.antigravity_native.bridge import _AGY_ENABLED_TOOLS
from omnigent.harnesses.cursor_native.bridge import build_mcp_config
from omnigent.runner.tool_dispatch import (
    _ALL_LOCAL_TOOLS,
    _NATIVE_RELAY_BUILTIN_TOOLS,
    execute_tool,
)
from omnigent.spec import AgentSpec
from omnigent.tools.manager import ToolManager


def test_registered_and_native_relay(tmp_path):
    names = {"sys_project_settings_read", "sys_project_settings_update"}
    schemas = {
        schema["function"]["name"]: schema["function"]
        for schema in ToolManager(AgentSpec(spec_version=1)).get_tool_schemas()
    }
    assert names <= _ALL_LOCAL_TOOLS & _NATIVE_RELAY_BUILTIN_TOOLS
    assert names <= set(build_mcp_config(tmp_path)["mcpServers"]["omnigent"]["autoApprove"])
    assert names <= set(_AGY_ENABLED_TOOLS)
    validate({"project_id": "project-a"}, schemas["sys_project_settings_read"]["parameters"])
    validate(
        {
            "project_id": "project-a",
            "expected_revision": "revision-a",
            "host": {"host_id": "host-a", "harnesses": {"codex": {"model": None}}},
        },
        schemas["sys_project_settings_update"]["parameters"],
    )


@pytest.mark.asyncio
async def test_update_rejects_identity_and_missing_guard_before_server():
    requests = []

    def receive(request):
        requests.append(request)
        return httpx.Response(200, json={"revision": "next"})

    async with httpx.AsyncClient(
        base_url="http://server", transport=httpx.MockTransport(receive)
    ) as client:
        for args in (
            {"project_id": "project-a", "project": {"host_id": "host-a"}},
            {
                "project_id": "project-a",
                "expected_revision": "current",
                "user_id": "admin",
                "project": {"host_id": "host-a"},
            },
        ):
            result = await execute_tool(
                tool_name="sys_project_settings_update",
                arguments=json.dumps(args),
                server_client=client,
            )
            assert "error" in json.loads(result)
        assert requests == []
        result = await execute_tool(
            tool_name="sys_project_settings_update",
            arguments=json.dumps(
                {
                    "project_id": "project-a",
                    "expected_revision": "current",
                    "project": {"host_id": "host-a"},
                }
            ),
            server_client=client,
        )
    assert json.loads(result) == {"revision": "next"}
    assert requests[0].url.path == "/v1/projects/project-a/settings"
    assert "user_id" not in json.loads(requests[0].content)
