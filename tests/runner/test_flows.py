"""Tests for runner flows: time composed with tool calls, one wake at the end."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from omnigent.runner import flows
from omnigent.runner.tool_dispatch import execute_tool
from omnigent.tools.builtins.flow import StopWhen, validate_flow_start_args

_SESSION = "conv_flow"


class _FakeServer:
    """``httpx.MockTransport`` handler standing in for the Omnigent server."""

    def __init__(self) -> None:
        self.flow_timer_enabled: bool | None = True
        self.tool_call_verdict = "POLICY_ACTION_ALLOW"
        self.tool_result_verdict: dict[str, Any] = {"result": "POLICY_ACTION_ALLOW"}
        self.rewrite_args: dict[str, Any] | None = None
        self.wakes: list[dict[str, Any]] = []
        self.requests: list[tuple[str, str]] = []
        self.woken = asyncio.Event()

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append((request.method, path))
        if path.endswith("/collab-settings"):
            if self.flow_timer_enabled is None:
                return httpx.Response(404, json={"detail": "not found"})
            return httpx.Response(200, json={"flow_timer_enabled": self.flow_timer_enabled})
        if path.endswith("/policies/evaluate"):
            event = json.loads(request.content)["event"]
            if event["type"] == "PHASE_TOOL_RESULT":
                return httpx.Response(200, json=self.tool_result_verdict)
            verdict: dict[str, Any] = {"result": self.tool_call_verdict}
            if self.rewrite_args is not None:
                verdict["data"] = self.rewrite_args
            return httpx.Response(200, json=verdict)
        if path.endswith("/events"):
            self.wakes.append(json.loads(request.content))
            self.woken.set()
            return httpx.Response(202, json={"queued": True})
        if path.endswith("/comments"):
            return httpx.Response(200, json={"comments": [], "state": "idle"})
        return httpx.Response(404)

    def wake_summary(self) -> dict[str, Any]:
        """Parse the single wake's summary JSON (last line of the text)."""
        assert len(self.wakes) == 1
        text = self.wakes[0]["data"]["content"][0]["text"]
        return json.loads(text.splitlines()[-1])


@pytest.fixture
def server() -> _FakeServer:
    return _FakeServer()


@pytest.fixture
def scripted(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Replace step dispatch with a scripted list of outputs (last one repeats)."""
    outputs: list[str] = []
    calls: list[str] = []

    async def _fake_run_step(run: Any, index: int, args: dict[str, Any]) -> str:
        calls.append(run.plan.steps[index].tool)
        return outputs.pop(0) if len(outputs) > 1 else outputs[0]

    monkeypatch.setattr(flows, "_run_step", _fake_run_step)
    yield outputs
    flows._session_flows.clear()


async def _start(client: httpx.AsyncClient, args: dict[str, Any]) -> dict[str, Any]:
    output = await execute_tool(
        tool_name="sys_flow_start",
        arguments=json.dumps(args),
        server_client=client,
        conversation_id=_SESSION,
    )
    return json.loads(output)


def _client(server: _FakeServer) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(server), base_url="http://server")


_STEP = [{"tool": "list_comments", "args": {}}]


@pytest.mark.asyncio
async def test_condition_met_wakes_once_with_the_matching_result(
    server: _FakeServer, scripted: list[str]
) -> None:
    scripted.extend(['{"status": "running"}', '{"status": "running"}', '{"status": "idle"}'])
    async with _client(server) as client:
        started = await _start(
            client,
            {
                "steps": _STEP,
                "every_s": 0.02,
                "times": 10,
                "stop_when": {"op": "eq", "value": "idle", "path": "status"},
            },
        )
        assert started["status"] == "running"
        await asyncio.wait_for(server.woken.wait(), timeout=2)
        await asyncio.sleep(0.05)
    summary = server.wake_summary()
    assert summary["reason"] == "condition_met"
    assert summary["ticks"] == 3
    assert summary["results"][0]["output"] == '{"status": "idle"}'
    assert server.wakes[0]["data"]["is_meta"] is True
    # No model is involved: the only server traffic is settings, policy and one wake.
    assert {path.rsplit("/", 1)[-1] for _m, path in server.requests} == {
        "collab-settings",
        "evaluate",
        "events",
    }


@pytest.mark.asyncio
async def test_time_up_ends_the_flow(server: _FakeServer, scripted: list[str]) -> None:
    scripted.append('{"status": "running"}')
    async with _client(server) as client:
        await _start(client, {"steps": _STEP, "every_s": 0.03, "for_s": 0.1})
        await asyncio.wait_for(server.woken.wait(), timeout=2)
    summary = server.wake_summary()
    assert summary["reason"] == "time_up"
    assert 1 <= summary["ticks"] <= 5


@pytest.mark.asyncio
async def test_count_done_after_times_ticks(server: _FakeServer, scripted: list[str]) -> None:
    scripted.append("ok")
    async with _client(server) as client:
        await _start(client, {"steps": _STEP, "every_s": 0.01, "times": 3})
        await asyncio.wait_for(server.woken.wait(), timeout=2)
    summary = server.wake_summary()
    assert (summary["reason"], summary["ticks"]) == ("count_done", 3)


@pytest.mark.asyncio
async def test_single_tick_flow_without_every(server: _FakeServer, scripted: list[str]) -> None:
    scripted.append("done")
    async with _client(server) as client:
        started = await _start(client, {"steps": _STEP, "start_after_s": 0.01})
        assert started["first_tick_in_s"] == 0.01
        await asyncio.wait_for(server.woken.wait(), timeout=2)
    summary = server.wake_summary()
    assert (summary["reason"], summary["ticks"]) == ("count_done", 1)
    assert summary["results"][0]["output"] == "done"


@pytest.mark.asyncio
@pytest.mark.parametrize("output", ['{"error": "boom"}', "Error: boom"])
async def test_step_error_ends_and_wakes(
    server: _FakeServer, scripted: list[str], output: str
) -> None:
    scripted.append(output)
    async with _client(server) as client:
        await _start(client, {"steps": _STEP, "every_s": 0.01, "times": 5})
        await asyncio.wait_for(server.woken.wait(), timeout=2)
    summary = server.wake_summary()
    assert (summary["reason"], summary["ticks"]) == ("step_error", 1)
    assert "boom" in summary["detail"]


@pytest.mark.asyncio
async def test_policy_denial_stops_before_the_step_and_wakes(
    server: _FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[int] = []

    async def _never(run: Any, index: int, args: dict[str, Any]) -> str:
        ran.append(index)
        return "x"

    monkeypatch.setattr(flows, "_run_step", _never)
    server.tool_call_verdict = "POLICY_ACTION_DENY"
    async with _client(server) as client:
        await _start(client, {"steps": _STEP, "every_s": 0.01, "times": 5})
        await asyncio.wait_for(server.woken.wait(), timeout=2)
    assert ran == []
    summary = server.wake_summary()
    assert summary["reason"] == "policy_denied"
    assert summary["results"] == []


@pytest.mark.asyncio
async def test_tool_result_policy_deny_suppresses_returned_output(
    server: _FakeServer, scripted: list[str]
) -> None:
    scripted.append("secret")
    server.tool_result_verdict = {"result": "POLICY_ACTION_DENY"}
    async with _client(server) as client:
        await _start(client, {"steps": _STEP})
        await asyncio.wait_for(server.woken.wait(), timeout=2)
    assert server.wake_summary()["results"][0]["output"] == "[Result suppressed by policy]"


@pytest.mark.asyncio
async def test_row8_off_refuses_start(server: _FakeServer, scripted: list[str]) -> None:
    scripted.append("x")
    server.flow_timer_enabled = False
    async with _client(server) as client:
        started = await _start(client, {"steps": _STEP})
    assert "flow_timer_enabled" in started["error"]
    assert flows._session_flows == {}


@pytest.mark.asyncio
async def test_row8_turned_off_mid_run_stops_without_wake(
    server: _FakeServer, scripted: list[str]
) -> None:
    scripted.append("x")
    async with _client(server) as client:
        started = await _start(client, {"steps": _STEP, "every_s": 0.03, "times": 50})
        await asyncio.sleep(0.05)
        server.flow_timer_enabled = False
        for _ in range(50):
            if not flows._session_flows:
                break
            await asyncio.sleep(0.02)
    assert flows._session_flows == {}
    assert server.wakes == []
    assert started["flow_id"].startswith("flow_")


@pytest.mark.asyncio
async def test_settings_route_missing_reads_as_on(server: _FakeServer, scripted: list[str]) -> None:
    scripted.append("x")
    server.flow_timer_enabled = None  # older server: 404
    async with _client(server) as client:
        started = await _start(client, {"steps": _STEP})
        await asyncio.wait_for(server.woken.wait(), timeout=2)
    assert started["status"] == "running"


@pytest.mark.asyncio
async def test_cancel_returns_summary_and_list_shows_running(
    server: _FakeServer, scripted: list[str]
) -> None:
    scripted.append('{"status": "running"}')
    async with _client(server) as client:
        first = await _start(
            client, {"steps": _STEP, "every_s": 0.02, "times": 100, "note": "watch build"}
        )
        second = await _start(client, {"steps": _STEP, "start_after_s": 30})
        await asyncio.sleep(0.05)
        listed = json.loads(
            await execute_tool(
                tool_name="sys_flow_list", arguments="{}", conversation_id=_SESSION
            )
        )["flows"]
        by_id = {flow["flow_id"]: flow for flow in listed}
        assert set(by_id) == {first["flow_id"], second["flow_id"]}
        assert by_id[first["flow_id"]]["note"] == "watch build"
        assert by_id[first["flow_id"]]["ticks"] >= 1
        assert by_id[second["flow_id"]]["ticks"] == 0
        assert "output" not in json.dumps(listed)

        cancelled = json.loads(
            await execute_tool(
                tool_name="sys_flow_cancel",
                arguments=json.dumps({"flow_id": first["flow_id"]}),
                server_client=client,
                conversation_id=_SESSION,
            )
        )
        assert (cancelled["status"], cancelled["reason"]) == ("cancelled", "cancelled")
        assert cancelled["results"][0]["output"] == '{"status": "running"}'
        await flows.cancel_flow(_SESSION, second["flow_id"])
        await asyncio.sleep(0.02)
        again = json.loads(
            await execute_tool(
                tool_name="sys_flow_cancel",
                arguments=json.dumps({"flow_id": first["flow_id"]}),
                conversation_id=_SESSION,
            )
        )
    assert again["status"] == "not_found"
    assert server.wakes == []
    assert flows._session_flows == {}


@pytest.mark.asyncio
async def test_bring_back_all_keeps_newest_within_budget(
    server: _FakeServer, scripted: list[str]
) -> None:
    scripted.extend(["a" * 10, "b" * 10, "c" * 10, "d" * 10])
    async with _client(server) as client:
        await _start(
            client,
            {"steps": _STEP, "every_s": 0.01, "times": 4, "bring_back": "all", "max_chars": 15},
        )
        await asyncio.wait_for(server.woken.wait(), timeout=2)
    summary = server.wake_summary()
    assert [r["output"] for r in summary["results"]] == ["ccccc", "d" * 10]
    assert summary["results"][0]["truncated"] is True
    assert summary["dropped_results"] == 2


@pytest.mark.asyncio
async def test_real_step_runs_through_execute_tool(server: _FakeServer) -> None:
    async with _client(server) as client:
        await _start(
            client,
            {
                "steps": _STEP,
                "every_s": 0.01,
                "times": 3,
                "stop_when": {"op": "eq", "value": "idle", "path": "comments.state"},
            },
        )
        await asyncio.wait_for(server.woken.wait(), timeout=2)
    summary = server.wake_summary()
    assert (summary["reason"], summary["ticks"]) == ("condition_met", 1)
    assert ("GET", f"/v1/sessions/{_SESSION}/comments") in server.requests


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("args", "fragment"),
    [
        ({"steps": []}, "non-empty"),
        ({"steps": _STEP, "every_s": 5}, "for_s or times"),
        ({"steps": [{"tool": "sys_flow_start"}]}, "cannot run inside a flow"),
        ({"steps": [{"tool": "sys_call_async"}]}, "cannot run inside a flow"),
        ({"steps": _STEP, "stop_when": {"op": "ne", "value": 1}}, "stop_when.op"),
        ({"steps": _STEP, "stop_when": {"op": "gt", "value": "x"}}, "number"),
        ({"steps": _STEP, "times": True}, "times"),
        ({"steps": _STEP, "every_s": float("inf"), "times": 1}, "finite"),
        ({"steps": [{"tool": "github__issues"}]}, "MCP"),
    ],
)
async def test_start_refusals(server: _FakeServer, args: dict[str, Any], fragment: str) -> None:
    async with _client(server) as client:
        started = await _start(client, args)
    assert fragment in started["error"]
    assert flows._session_flows == {}


@pytest.mark.parametrize(
    ("output", "stop", "met"),
    [
        ('{"status": "idle"}', StopWhen("eq", "idle", "status", 0), True),
        ('{"status": "busy"}', StopWhen("eq", "idle", "status", 0), False),
        ('{"a": [{"n": 3}]}', StopWhen("gt", 2, "a.0.n", 0), True),
        ('{"a": [{"n": 3}]}', StopWhen("lt", 2, "a.0.n", 0), False),
        ('{"a": [1]}', StopWhen("eq", 1, "a.5", 0), False),
        ('{"n": 1}', StopWhen("eq", 1.0, "n", 0), True),
        ('{"n": true}', StopWhen("eq", 1, "n", 0), False),
        ('{"n": true}', StopWhen("gt", 0, "n", 0), False),
        ('{"tags": ["x", "y"]}', StopWhen("contains", "y", "tags", 0), True),
        ("build passed", StopWhen("contains", "passed", None, 0), True),
        ("42", StopWhen("gt", 41, None, 0), True),
        ("not json", StopWhen("eq", "x", "status", 0), False),
        ("idle", StopWhen("eq", "idle", None, 0), True),
    ],
)
def test_stop_condition_met(output: str, stop: StopWhen, met: bool) -> None:
    assert flows.stop_condition_met(output, stop) is met


def test_validation_defaults() -> None:
    plan = validate_flow_start_args({"steps": [{"tool": "t"}], "every_s": 1, "times": 2})
    assert not isinstance(plan, str)
    assert (plan.start_after_s, plan.bring_back, plan.max_chars) == (0.0, "last", 2000)
    assert plan.steps[0].args == {}
    assert plan.stop_when is None


@pytest.mark.asyncio
async def test_timer_set_refused_when_row8_off(server: _FakeServer) -> None:
    server.flow_timer_enabled = False
    async with _client(server) as client:
        output = json.loads(
            await execute_tool(
                tool_name="sys_timer_set",
                arguments=json.dumps({"seconds": 0}),
                server_client=client,
                conversation_id=_SESSION,
            )
        )
    assert "flow_timer_enabled" in output["error"]


@pytest.mark.asyncio
async def test_timer_firing_skipped_after_row8_turns_off(server: _FakeServer) -> None:
    from omnigent.runner import app as runner_app

    async with _client(server) as client:
        output = json.loads(
            await execute_tool(
                tool_name="sys_timer_set",
                arguments=json.dumps({"seconds": 0.05, "repeat": True}),
                server_client=client,
                conversation_id=_SESSION,
            )
        )
        server.flow_timer_enabled = False
        await asyncio.sleep(0.15)
    assert server.wakes == []
    assert output["timer_id"] not in runner_app._session_timers.get(_SESSION, {})


@pytest.mark.asyncio
async def test_policy_rewritten_arguments_are_what_the_step_runs_with(
    server: _FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict[str, Any]] = []

    async def _record(run: Any, index: int, args: dict[str, Any]) -> str:
        seen.append(args)
        return "ok"

    monkeypatch.setattr(flows, "_run_step", _record)
    server.rewrite_args = {"text": "[redacted]"}
    async with _client(server) as client:
        await _start(client, {"steps": [{"tool": "list_comments", "args": {"text": "secret"}}]})
        await asyncio.wait_for(server.woken.wait(), timeout=2)
    assert seen == [{"text": "[redacted]"}]


@pytest.mark.asyncio
async def test_deadline_inside_a_step_ends_time_up_and_skips_later_steps(
    server: _FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[int] = []

    async def _slow(run: Any, index: int, args: dict[str, Any]) -> str:
        ran.append(index)
        await asyncio.sleep(1)
        return "late"

    monkeypatch.setattr(flows, "_run_step", _slow)
    async with _client(server) as client:
        await _start(client, {"steps": _STEP + _STEP, "every_s": 0.01, "for_s": 0.1})
        await asyncio.wait_for(server.woken.wait(), timeout=2)
    summary = server.wake_summary()
    assert summary["reason"] == "time_up"
    assert ran == [0]


@pytest.mark.asyncio
async def test_wake_is_posted_once_even_when_it_fails(
    server: _FakeServer, scripted: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    scripted.append("x")
    original = server.__call__

    async def _failing(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/events"):
            server.wakes.append({})
            server.woken.set()
            return httpx.Response(503)
        return await original(request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_failing), base_url="http://server"
    ) as client:
        await _start(client, {"steps": _STEP})
        await asyncio.wait_for(server.woken.wait(), timeout=2)
        await asyncio.sleep(0.1)
    assert len(server.wakes) == 1
    assert flows._session_flows == {}


def _relay_names(spec: Any, *, peer: bool) -> set[str]:
    from omnigent.runner import tool_dispatch

    return {s["name"] for s in tool_dispatch.build_native_relay_tool_schemas(spec, peer_messaging_enabled=peer)}


_TIMED = {"sys_timer_set", "sys_timer_cancel", "sys_flow_start", "sys_flow_list", "sys_flow_cancel"}


@pytest.mark.parametrize("spec_timers", [False, True])
def test_native_relay_advertises_timers_and_flows_under_the_collab_flag(spec_timers: bool) -> None:
    from omnigent.spec import AgentSpec

    spec = AgentSpec(spec_version=1, timers=spec_timers)
    assert _relay_names(spec, peer=True) >= _TIMED
    off = _relay_names(spec, peer=False) & _TIMED
    assert off == ({"sys_timer_set", "sys_timer_cancel"} if spec_timers else set())
    assert _relay_names(None, peer=True) >= _TIMED
    assert _relay_names(None, peer=False).isdisjoint(_TIMED)


def test_flow_tools_are_refused_when_the_collab_flag_is_off() -> None:
    from omnigent.runner import tool_dispatch
    from omnigent.spec import AgentSpec

    spec = AgentSpec(spec_version=1, timers=True)
    assert tool_dispatch._ungranted_tool_reason("sys_flow_start", spec) is not None
    assert tool_dispatch._ungranted_tool_reason("sys_timer_set", spec) is None
    assert (
        tool_dispatch._ungranted_tool_reason("sys_flow_start", spec, peer_messaging_enabled=True)
        is None
    )
