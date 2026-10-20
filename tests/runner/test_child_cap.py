"""The spawned-session cap is available and runner dispatched with spawn capability."""

import json

import httpx
import pytest

from omnigent.runner.tool_dispatch import execute_tool
from omnigent.spec import AgentSpec


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments,method", [({}, "GET"), ({"cap": 4}, "PATCH"), ({"cap": "none"}, "PATCH")]
)
async def test_child_cap_runner_reads_and_sets_own_session(arguments, method):
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(
            200, json={"cap": arguments.get("cap", 4), "source": "session", "open_children": 2}
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://server"
    ) as client:
        result = json.loads(
            await execute_tool(
                tool_name="sys_child_cap",
                arguments=json.dumps(arguments),
                server_client=client,
                conversation_id="parent",
                agent_spec=AgentSpec(spec_version=1, name="Parent", spawn=True),
            )
        )
    assert result["open_children"] == 2
    assert len(seen) == 1
    assert seen[0].url.path == "/v1/sessions/parent/child-cap"
    assert seen[0].method == method
    if method == "PATCH":
        assert json.loads(seen[0].content) == arguments


@pytest.mark.asyncio
@pytest.mark.parametrize("cap", [True, 0, 1.5, "4", None])
async def test_child_cap_runner_rejects_invalid_values_without_server_write(cap):
    def handle(request):
        pytest.fail("invalid cap must not reach server")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://server"
    ) as client:
        result = json.loads(
            await execute_tool(
                tool_name="sys_child_cap",
                arguments=json.dumps({"cap": cap}),
                server_client=client,
                conversation_id="parent",
                agent_spec=AgentSpec(spec_version=1, name="Parent", spawn=True),
            )
        )
    assert "error" in result
