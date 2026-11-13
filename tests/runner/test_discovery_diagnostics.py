"""Advisory discovery failures remain distinct from absent configuration."""

import json

import httpx
import pytest

from omnigent.runner.tool_dispatch import execute_tool


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "http", "malformed"])
async def test_quota_read_failure_is_visible(failure):
    def handle(request):
        if request.url.path.endswith("/agent-quota"):
            assert request.extensions["timeout"]["read"] == 60
            if failure == "timeout":
                raise httpx.ReadTimeout("slow")
            return httpx.Response(503) if failure == "http" else httpx.Response(200, json=[])
        if request.url.path == "/v1/agents":
            return httpx.Response(200, json={"data": [{"id": "a", "harness": "codex-native"}]})
        return httpx.Response(200, json={"data": []})

    async with httpx.AsyncClient(
        base_url="http://server", transport=httpx.MockTransport(handle)
    ) as client:
        result = json.loads(
            await execute_tool(
                tool_name="sys_agent_list",
                arguments="{}",
                conversation_id="s",
                server_client=client,
            )
        )
    assert result["builtins"][0]["quota"] is None
    assert result["quota_read_reason"] == (
        "quota_read_timeout" if failure == "timeout" else "quota_unavailable"
    )


@pytest.mark.asyncio
async def test_quota_window_summary_declares_omissions():
    def handle(request):
        if request.url.path.endswith("/agent-quota"):
            return httpx.Response(
                200,
                json={
                    "agents": {
                        "codex": {
                            "state": "low",
                            "windows": [{"id": str(i), "state": "low"} for i in range(6)],
                        }
                    }
                },
            )
        if request.url.path == "/v1/agents":
            return httpx.Response(200, json={"data": [{"id": "a", "harness": "codex-native"}]})
        return httpx.Response(200, json={"data": []})

    async with httpx.AsyncClient(
        base_url="http://server", transport=httpx.MockTransport(handle)
    ) as client:
        result = json.loads(
            await execute_tool(
                tool_name="sys_agent_list",
                arguments="{}",
                conversation_id="s",
                server_client=client,
            )
        )
    quota = result["builtins"][0]["quota"]
    assert len(quota["windows"]) == 4
    assert quota["total_windows"] == 6
    assert quota["omitted_windows"] == 2
    assert quota["full_evidence"]["path"] == "/v1/sessions/s/agent-quota"


@pytest.mark.asyncio
async def test_failed_discovery_source_has_retry_reason():
    def handle(request):
        return (
            httpx.Response(503)
            if request.url.path == "/v1/agents"
            else httpx.Response(200, json={"data": []})
        )

    async with httpx.AsyncClient(
        base_url="http://server", transport=httpx.MockTransport(handle)
    ) as client:
        result = json.loads(
            await execute_tool(tool_name="sys_agent_list", arguments="{}", server_client=client)
        )
    assert result["page"]["source_errors"]["builtins"] == {
        "reason": "source_unavailable",
        "retryable": True,
    }
    assert result["page"]["next_cursor"]


@pytest.mark.asyncio
async def test_host_lookup_failure_is_visible():
    def handle(request):
        if request.url.path == "/v1/sessions/s":
            return httpx.Response(200, json={"id": "s", "host_id": "h"})
        return httpx.Response(503)

    async with httpx.AsyncClient(
        base_url="http://server", transport=httpx.MockTransport(handle)
    ) as client:
        result = json.loads(
            await execute_tool(
                tool_name="sys_session_get_info",
                arguments='{"session_id":"s"}',
                conversation_id="s",
                server_client=client,
            )
        )
    assert result["configured_harnesses"] is None
    assert result["host_lookup_reason"] == "host_lookup_unavailable"


@pytest.mark.asyncio
async def test_readiness_failure_is_visible(monkeypatch):
    from omnigent.runner.identity import RUNNER_SLICE_KEY_ENV_VAR

    monkeypatch.setenv(RUNNER_SLICE_KEY_ENV_VAR, "h")

    def handle(request):
        return (
            httpx.Response(503)
            if request.url.path == "/v1/hosts/h"
            else httpx.Response(200, json={"data": []})
        )

    async with httpx.AsyncClient(
        base_url="http://server", transport=httpx.MockTransport(handle)
    ) as client:
        result = json.loads(
            await execute_tool(tool_name="sys_agent_list", arguments="{}", server_client=client)
        )
    assert result["readiness_lookup_reason"] == "host_lookup_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failed_path,source",
    [
        ("/v1/sessions/s", "parent"),
        ("/v1/sessions/s/child_sessions", "children"),
        ("/v1/sessions/p/child_sessions", "siblings"),
    ],
)
async def test_subagent_enrichment_failure_is_visible(failed_path, source):
    def handle(request):
        if request.url.path == failed_path:
            return httpx.Response(503)
        if request.url.path == "/v1/sessions/s":
            return httpx.Response(200, json={"parent_session_id": "p"})
        return httpx.Response(200, json={"data": []})

    async with httpx.AsyncClient(
        base_url="http://server", transport=httpx.MockTransport(handle)
    ) as client:
        result = json.loads(
            await execute_tool(
                tool_name="sys_session_list",
                arguments="{}",
                conversation_id="s",
                server_client=client,
            )
        )
    assert result["partial_sources"][source] == source + "_lookup_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool,args,path",
    [
        ("sys_instruction_layers_read", {"mode": "layers"}, "/v1/instruction-layers"),
        ("sys_standing_approval", {"op": "read"}, "/v1/sessions/s/standing-approval"),
        ("sys_session_choice_read", {}, "/v1/me/session-choice"),
    ],
)
async def test_local_control_uses_one_minute(tool, args, path):
    def handle(request):
        assert request.url.path == path
        assert request.extensions["timeout"]["read"] == 60
        return httpx.Response(200, json={"revision": "r"})

    async with httpx.AsyncClient(
        base_url="http://server", transport=httpx.MockTransport(handle)
    ) as client:
        result = json.loads(
            await execute_tool(
                tool_name=tool,
                arguments=json.dumps(args),
                conversation_id="s",
                server_client=client,
            )
        )
    assert result == {"revision": "r"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool,args,path",
    [
        (
            "sys_instruction_layer_update",
            {"scope": "shared", "text": "new", "expected_revision_id": "r"},
            "/v1/instruction-layers",
        ),
        (
            "sys_session_choice_update",
            {"settings": {}, "expected_revision": "r"},
            "/v1/me/session-choice",
        ),
        ("sys_quota_floor", {"tile": "a", "off": True}, "/v1/me/session-choice/floor"),
    ],
)
async def test_control_write_timeout_rereads_without_replay(tool, args, path):
    requests = []

    def handle(request):
        requests.append((request.method, request.url.path))
        if request.method != "GET":
            raise httpx.ReadTimeout("response lost")
        return httpx.Response(200, json={"revision": "next", "data": []})

    async with httpx.AsyncClient(
        base_url="http://server", transport=httpx.MockTransport(handle)
    ) as client:
        result = json.loads(
            await execute_tool(tool_name=tool, arguments=json.dumps(args), server_client=client)
        )
    assert len(requests) == 2
    assert requests[-1] == ("GET", path)
    assert result["error"] == "write_outcome_unknown"
    assert result["observed"]["revision"] == "next"
    assert result["expected_revision"] == args.get(
        "expected_revision_id", args.get("expected_revision")
    )
