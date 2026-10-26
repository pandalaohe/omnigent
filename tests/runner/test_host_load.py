"""Host-load reads use the granted surface and the caller's server transport."""

import json

import httpx
import pytest
from jsonschema import ValidationError, validate

from omnigent.runner.tool_dispatch import (
    _ALL_LOCAL_TOOLS,
    _NATIVE_RELAY_BUILTIN_TOOLS,
    build_native_relay_tool_schemas,
    execute_tool,
)
from omnigent.spec import AgentSpec
from omnigent.tools.builtins.host_load import SysHostLoadTool
from omnigent.tools.manager import ToolManager


@pytest.mark.parametrize("spawn", [False, True])
def test_host_load_registered_with_spawn_on_request_and_native_surfaces(spawn):
    spec = AgentSpec(spec_version=1, spawn=spawn)
    manager = ToolManager(spec)
    try:
        names = set(manager.get_tool_names())
    finally:
        manager.shutdown()
    assert ("sys_host_load" in names) is spawn
    relay = {schema["name"] for schema in build_native_relay_tool_schemas(spec)}
    assert ("sys_host_load" in relay) is spawn
    assert "sys_host_load" in _ALL_LOCAL_TOOLS & _NATIVE_RELAY_BUILTIN_TOOLS
    parameters = SysHostLoadTool().get_schema()["function"]["parameters"]
    validate({}, parameters)
    with pytest.raises(ValidationError):
        validate({"host_id": "host-a"}, parameters)


@pytest.mark.asyncio
async def test_host_load_returns_every_visible_host_with_current_load_and_advice():
    hosts = [
        {
            "name": f"Host {level}",
            "host_id": f"host-{level}",
            "state": "online",
            "cpu_count": 8,
            "load_per_core": load,
            "load_equiv": load * 8 if load is not None else None,
            "estimate": level == "heavy_hold",
            "level": level,
            "reason": reason,
            "lines": {"heavy": 4.0, "new_session": 5.0},
            "lines_source": "host",
            "mem_pct": 92.0 if reason == "memory" else 50.0,
            "queued_children": 2 if level == "new_session_hold" else 0,
            "sampled_at": "2026-10-25T10:00:00+00:00",
            "level_since": "2026-10-25T09:57:00+00:00",
            "last_snapshot": {"processes": [{"command": "unrelated process details"}]},
        }
        for level, load, reason in (
            ("spare", 0.5, None),
            ("normal", 2.0, None),
            ("heavy_hold", 2.0, "memory"),
            ("new_session_hold", 5.5, "load"),
            ("unknown", None, None),
        )
    ]
    hosts.append(
        {"host_id": "offline-host", "name": "Offline host", "state": "offline", "level": "unknown"}
    )
    seen = []

    def handle(request):
        seen.append(request)
        assert request.headers["X-Forwarded-Email"] == "member@example.test"
        return httpx.Response(200, json={"hosts": hosts, "server": None, "findings": []})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle),
        base_url="http://server",
        headers={"X-Forwarded-Email": "member@example.test"},
    ) as client:
        result = json.loads(
            await execute_tool(
                tool_name="sys_host_load",
                arguments="{}",
                server_client=client,
                agent_spec=AgentSpec(spec_version=1, spawn=True),
            )
        )
    assert len(seen) == 1
    assert seen[0].method == "GET"
    assert seen[0].url.path == "/v1/system/status"
    assert not seen[0].url.query
    assert set(result) == {"hosts"}
    assert len(result["hosts"]) == len(hosts)
    for original, returned in zip(hosts, result["hosts"], strict=True):
        for field, value in original.items():
            if field != "last_snapshot":
                assert returned[field] == value
        assert "last_snapshot" not in returned
    advice = [host["scheduling_advice"] for host in result["hosts"]]
    assert "Spare capacity" in advice[0]
    assert "Heavy work may proceed" in advice[1]
    assert "Wait before starting new heavy work" in advice[2]
    assert "new child sessions" in advice[3]
    assert "stale" in advice[4]
    assert "not online" in advice[5]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "spawn,arguments,with_client,error",
    [
        (False, "{}", True, "not enabled"),
        (True, '{"host_id": "host-a"}', True, "takes no arguments"),
        (True, "{}", False, "requires server access"),
    ],
)
async def test_host_load_rejects_invalid_calls_before_server(spawn, arguments, with_client, error):
    def handle(request):
        pytest.fail("invalid call must not reach server")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://server"
    ) as client:
        result = json.loads(
            await execute_tool(
                tool_name="sys_host_load",
                arguments=arguments,
                server_client=client if with_client else None,
                agent_spec=AgentSpec(spec_version=1, spawn=spawn),
            )
        )
    assert error in result["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,payload,expected",
    [
        (200, {"hosts": []}, {"hosts": []}),
        (200, {}, "invalid host list"),
        (200, {"hosts": [None]}, "invalid host list"),
        (403, {"detail": "Forbidden"}, "sys_host_load failed"),
    ],
)
async def test_host_load_empty_scope_and_server_failures_are_observable(status, payload, expected):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(status, json=payload)),
        base_url="http://server",
    ) as client:
        result = json.loads(
            await execute_tool(
                tool_name="sys_host_load",
                arguments="{}",
                server_client=client,
                agent_spec=AgentSpec(spec_version=1, spawn=True),
            )
        )
    if isinstance(expected, dict):
        assert result == expected
    else:
        assert expected in result["error"]
