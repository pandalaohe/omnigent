"""Check-in tools bind their REST target to the calling session."""

import json

import httpx
import pytest

from omnigent.runner.tool_dispatch import (
    build_native_relay_tool_schemas,
    execute_tool,
    should_dispatch_locally,
)
from omnigent.spec import AgentSpec
from omnigent.tools.builtins.child_checkin import validate_child_checkin_arguments


@pytest.mark.parametrize("spawn", [False, True])
def test_native_checkin_surface_requires_spawn_and_has_no_target(spawn):
    schemas = build_native_relay_tool_schemas(AgentSpec(spec_version=1, spawn=spawn))
    tools = {schema["name"]: schema for schema in schemas}
    assert ("sys_child_checkin" in tools) is spawn
    if spawn:
        fields = tools["sys_child_checkin"]["parameters"]["properties"]
        assert "session_id" not in fields
        assert "conversation_id" not in fields


@pytest.mark.parametrize(
    "arguments,method,suffix,body",
    [
        ({"action": "read"}, "GET", "", None),
        ({"action": "set", "min_gap_minutes": 30}, "PATCH", "", {"min_gap_seconds": 1800}),
        ({"action": "set", "instructions": ""}, "PATCH", "", {"instructions": ""}),
        (
            {"action": "follow_up_add", "text": "Recheck deployment", "after_minutes": 15},
            "POST",
            "/follow-ups",
            {"text": "Recheck deployment", "after_minutes": 15},
        ),
        ({"action": "follow_up_remove", "id": "12abcdef"}, "DELETE", "/follow-ups/12abcdef", None),
    ],
)
async def test_child_checkin_runner_target_and_payload(arguments, method, suffix, body):
    assert should_dispatch_locally("sys_child_checkin")
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(200, json={"delivery": "off"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://server"
    ) as client:
        result = json.loads(
            await execute_tool(
                tool_name="sys_child_checkin",
                arguments=json.dumps(arguments),
                server_client=client,
                conversation_id="caller",
                agent_spec=AgentSpec(spec_version=1, spawn=True),
            )
        )
    assert result == {"delivery": "off"}
    assert len(seen) == 1
    assert seen[0].method == method
    assert seen[0].url.path == f"/v1/sessions/caller/child-checkin{suffix}"
    if body is not None:
        assert json.loads(seen[0].content) == body


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"action": "bad"},
        {"action": "read", "session_id": "mother"},
        {"action": "read", "reset": True},
        {"action": "set", "min_gap_minutes": True},
        {"action": "set", "min_gap_minutes": 1441},
        {"action": "set", "instructions": " "},
        {"action": "set", "instructions": "x" * 1001},
        {"action": "set", "coverage": []},
        {"action": "set", "reset": True, "coverage": "changed"},
        {"action": "set", "reset_instructions": True, "instructions": ""},
        {"action": "follow_up_add", "text": "later", "after_minutes": 14},
        {"action": "follow_up_add", "text": "later"},
        {"action": "follow_up_add", "text": "\0", "after_minutes": 15},
        {"action": "follow_up_remove", "id": "../other"},
    ],
)
async def test_invalid_checkin_arguments_never_reach_server(arguments):
    def handle(request):
        pytest.fail("invalid check-in arguments reached server")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://server"
    ) as client:
        result = json.loads(
            await execute_tool(
                tool_name="sys_child_checkin",
                arguments=json.dumps(arguments),
                server_client=client,
                conversation_id="caller",
                agent_spec=AgentSpec(spec_version=1, spawn=True),
            )
        )
    assert "error" in result


def test_instructions_validate_stored_form_without_truncation():
    assert (
        validate_child_checkin_arguments({"action": "set", "instructions": "x" * 1000 + "\0"})
        == "set"
    )


async def test_checkin_tool_does_not_dispatch_without_spawn():
    def handle(request):
        pytest.fail("ungranted tool reached server")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://server"
    ) as client:
        result = await execute_tool(
            tool_name="sys_child_checkin",
            arguments='{"action":"read"}',
            server_client=client,
            conversation_id="caller",
            agent_spec=AgentSpec(spec_version=1),
        )
    assert "not enabled for this agent" in json.loads(result)["error"]
