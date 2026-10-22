"""The spawned-session cap is available and runner dispatched with spawn capability."""

import json

import httpx
import pytest
from jsonschema import ValidationError, validate

from omnigent.runner.tool_dispatch import execute_tool
from omnigent.spec import AgentSpec
from omnigent.tools.builtins.child_cap import SysChildCapTool


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments,method,expected_cap",
    [
        ({}, "GET", None),
        ({"cap": 4}, "PATCH", 4),
        ({"cap": "6"}, "PATCH", 6),
        ({"cap": "none"}, "PATCH", "none"),
    ],
)
async def test_child_cap_runner_reads_and_sets_own_session(arguments, method, expected_cap):
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
        assert json.loads(seen[0].content) == {"cap": expected_cap}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cap",
    [
        True,
        0,
        -1,
        1.5,
        None,
        "0",
        "04",
        "+4",
        "-4",
        " 4",
        "4 ",
        "4\n",
        "4.0",
        "text",
        "٤",
        "６",
        pytest.param("9" * 5000, id="too-many-digits"),
    ],
)
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


@pytest.mark.parametrize("cap", [1, "6", "none"])
def test_child_cap_schema_accepts_documented_values(cap):
    parameters = SysChildCapTool().get_schema()["function"]["parameters"]
    validate({"cap": cap}, parameters)


@pytest.mark.parametrize("cap", ["0", "04", "+4", " 4", "4\n", "٤", None])
def test_child_cap_schema_rejects_noncanonical_values(cap):
    parameters = SysChildCapTool().get_schema()["function"]["parameters"]
    with pytest.raises(ValidationError):
        validate({"cap": cap}, parameters)
