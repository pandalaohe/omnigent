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
        self.collab_enabled = True
        self.tool_call_verdict = "POLICY_ACTION_ALLOW"
        self.tool_result_verdict: dict[str, Any] = {"result": "POLICY_ACTION_ALLOW"}
        self.rewrite_args: dict[str, Any] | None = None
        self.wakes: list[dict[str, Any]] = []
        # sys_session_send steps: target snapshots, the peer route's answer, child posts.
        self.sessions: dict[str, dict[str, Any]] = {_SESSION: {"id": _SESSION, "labels": {}}}
        self.peer_answer: dict[str, Any] = {"disposition": "delivered", "reason": None}
        self.child_messages: list[str] = []
        # Per child POST: was the child already a running flow's dispatch?
        self.owned_at_post: list[bool] = []
        self.requests: list[tuple[str, str]] = []
        self.woken = asyncio.Event()

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append((request.method, path))
        if path.endswith("/collab-settings"):
            if self.flow_timer_enabled is None:
                return httpx.Response(404, json={"detail": "not found"})
            return httpx.Response(
                200,
                json={
                    "enabled": self.collab_enabled,
                    "flow_timer_enabled": self.flow_timer_enabled,
                },
            )
        if path.endswith("/policies/evaluate"):
            event = json.loads(request.content)["event"]
            if event["type"] == "PHASE_TOOL_RESULT":
                return httpx.Response(200, json=self.tool_result_verdict)
            verdict: dict[str, Any] = {"result": self.tool_call_verdict}
            if self.rewrite_args is not None:
                verdict["data"] = self.rewrite_args
            return httpx.Response(200, json=verdict)
        if path == f"/v1/sessions/{_SESSION}/events":
            self.wakes.append(json.loads(request.content))
            self.woken.set()
            return httpx.Response(202, json={"queued": True})
        if path.endswith("/peer-messages"):
            return httpx.Response(
                200, json={"peer_id": "peer_1", "ref": "", "receiver": {}, **self.peer_answer}
            )
        target = path.removeprefix("/v1/sessions/").split("/")[0]
        if request.method == "POST" and path.endswith("/events"):
            self.child_messages.append(target)
            runs = flows._session_flows.get(_SESSION, {}).values()
            self.owned_at_post.append(any(target in run.children for run in runs))
            return httpx.Response(200, json={"status": "accepted"})
        if request.method == "PATCH":
            return httpx.Response(200, json={"id": target})
        if request.method == "GET" and target in self.sessions and path.count("/") == 3:
            return httpx.Response(200, json=self.sessions[target])
        if path.endswith("/comments"):
            return httpx.Response(200, json={"comments": [], "state": "idle"})
        return httpx.Response(404)

    def wake_summary(self) -> dict[str, Any]:
        """Parse the single wake's summary JSON (last line of the text)."""
        assert len(self.wakes) == 1
        text = self.wakes[0]["data"]["content"][0]["text"]
        return json.loads(text.splitlines()[-1])


@pytest.fixture(autouse=True)
def _collab_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Flow tools dispatch only for sessions initialized with the collab flag."""
    from omnigent.runner import app as runner_app

    monkeypatch.setattr(
        runner_app, "get_session_peer_messaging_enabled", lambda sid: sid == _SESSION
    )


@pytest.fixture(autouse=True)
def _isolated_runner_state(_clean_subagent_registry: None) -> Iterator[None]:
    """Send steps register child work and child sessions; restore both after each test."""
    from omnigent.runner import app as runner_app

    saved = dict(runner_app._child_session_parents)
    runner_app._child_session_parents.clear()
    try:
        yield
    finally:
        runner_app._child_session_parents.clear()
        runner_app._child_session_parents.update(saved)


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
    # No model is involved: the only server traffic is settings, the archive
    # admission check, policy and one wake.
    assert {path.rsplit("/", 1)[-1] for _m, path in server.requests} == {
        "collab-settings",
        _SESSION,
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
    assert summary["results"][0]["output"] == output


@pytest.mark.asyncio
async def test_step_error_text_stays_behind_the_result_policy(
    server: _FakeServer, scripted: list[str]
) -> None:
    scripted.append('{"error": "BLOCKED_MARKER"}')
    server.tool_result_verdict = {"result": "POLICY_ACTION_DENY"}
    async with _client(server) as client:
        await _start(client, {"steps": _STEP, "bring_back": "none"})
        await asyncio.wait_for(server.woken.wait(), timeout=2)
    assert server.wake_summary()["reason"] == "step_error"
    assert "BLOCKED_MARKER" not in json.dumps(server.wakes)


def _send_step(target: str) -> list[dict[str, Any]]:
    return [{"tool": "sys_session_send", "args": {"session_id": target, "args": "status?"}}]


@pytest.fixture
def inbox() -> None:
    from omnigent.runner import app as runner_app

    runner_app._session_inboxes_ref[_SESSION] = asyncio.Queue()


@pytest.mark.asyncio
async def test_send_step_to_own_child_holds_its_wake_until_the_flow_ends(
    server: _FakeServer, inbox: None
) -> None:
    from omnigent.runner import app as runner_app

    child = "conv_child"
    server.sessions[child] = {"id": child, "parent_session_id": _SESSION, "labels": {}}
    async with _client(server) as client:
        await _start(client, {"steps": _send_step(child), "every_s": 0.05, "times": 3})
        for _ in range(100):
            if server.child_messages:
                break
            await asyncio.sleep(0.01)
        # The child's turn starts; later ticks steer it through the same work entry.
        assert runner_app.mark_subagent_work_started(child) is not None
        # Stands in for the runner's delivered-result hook (runner-app test covers it).
        assert flows.hold_child_wake(_SESSION, child) is True
        await asyncio.wait_for(server.woken.wait(), timeout=2)
    summary = server.wake_summary()
    assert (summary["reason"], summary["ticks"]) == ("count_done", 3)
    assert summary["child_results_in_inbox"] == 1
    assert "sys_read_inbox" in server.wakes[0]["data"]["content"][0]["text"]
    assert server.child_messages == [child] * 3
    # Tick 1 registers new work, ticks 2-3 steer it: owned before every post.
    assert server.owned_at_post == [True] * 3
    assert flows.hold_child_wake(_SESSION, child) is False


@pytest.mark.asyncio
async def test_steering_the_agents_running_child_joins_the_flow_before_the_post(
    server: _FakeServer, inbox: None
) -> None:
    from omnigent.runner import app as runner_app
    from omnigent.runner import subagent_work

    child = "conv_child"
    server.sessions[child] = {"id": child, "parent_session_id": _SESSION, "labels": {}}
    # The agent's own send started the child's turn; the flow then steers that turn.
    subagent_work.register_subagent_work(
        parent_session_id=_SESSION, child_session_id=child, agent="a", title="t"
    )
    assert runner_app.mark_subagent_work_started(child) is not None
    async with _client(server) as client:
        await _start(client, {"steps": _send_step(child)})
        await asyncio.wait_for(server.woken.wait(), timeout=2)
    assert server.wake_summary()["reason"] == "count_done"
    assert server.owned_at_post == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize("post_status", [200, 503])
async def test_the_agents_own_steer_releases_a_flow_child_only_when_posted(
    server: _FakeServer, scripted: list[str], post_status: int
) -> None:
    from omnigent.runner.tool_dispatch import _send_to_in_flight_child

    child = "conv_child"
    scripted.append("x")
    async with _client(server) as client:
        started = await _start(client, {"steps": _STEP, "start_after_s": 30})
        run = flows._session_flows[_SESSION][started["flow_id"]]
        run.children.add(child)

        def _child_post(request: httpx.Request) -> httpx.Response:
            return httpx.Response(post_status, json={})

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(_child_post), base_url="http://server"
        ) as child_client:
            out = await _send_to_in_flight_child(
                child,
                "more",
                server_client=child_client,
                conversation_id=_SESSION,
                agent="a",
                title="t",
                child_display_title="t",
                wrapper_label=None,
            )
        owned = child in run.children
        await flows.cancel_flow(_SESSION, started["flow_id"])
    assert out.startswith("Error:") is (post_status == 503)
    assert owned is (post_status == 503)


@pytest.mark.asyncio
@pytest.mark.parametrize("disposition", ["delivered", "queued", "held", "pending"])
async def test_send_step_to_a_peer_is_a_normal_step(
    server: _FakeServer, inbox: None, disposition: str
) -> None:
    server.sessions["conv_peer"] = {"id": "conv_peer", "parent_session_id": None, "labels": {}}
    server.peer_answer = {"disposition": disposition, "reason": None}
    async with _client(server) as client:
        await _start(client, {"steps": _send_step("conv_peer")})
        await asyncio.wait_for(server.woken.wait(), timeout=2)
    summary = server.wake_summary()
    assert summary["reason"] == "count_done"
    assert json.loads(summary["results"][0]["output"])["disposition"] == disposition
    assert "child_results_in_inbox" not in summary
    assert server.child_messages == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("parent", "disposition", "reason"),
    [
        pytest.param(None, "refused", "not_same_owner", id="refused-peer"),
        pytest.param("conv_child", "refused", "is_subagent", id="grandchild"),
        pytest.param(None, "dropped", "duplicate", id="dropped"),
        pytest.param(None, "failed", "closed", id="failed"),
    ],
)
async def test_send_step_the_server_refuses_ends_step_error(
    server: _FakeServer, inbox: None, parent: str | None, disposition: str, reason: str
) -> None:
    server.sessions["conv_target"] = {
        "id": "conv_target",
        "parent_session_id": parent,
        "labels": {},
    }
    server.peer_answer = {"disposition": disposition, "reason": reason}
    async with _client(server) as client:
        await _start(client, {"steps": _send_step("conv_target"), "every_s": 0.01, "times": 5})
        await asyncio.wait_for(server.woken.wait(), timeout=2)
    summary = server.wake_summary()
    assert (summary["reason"], summary["ticks"]) == ("step_error", 1)
    assert summary["detail"] == "steps[0] (sys_session_send) returned an error"
    output = json.loads(summary["results"][0]["output"])
    assert (output["disposition"], output["reason"]) == (disposition, reason)


@pytest.mark.asyncio
async def test_cancel_before_the_first_step_leaves_no_registry_entry(
    server: _FakeServer, scripted: list[str]
) -> None:
    from omnigent.runner import app as runner_app

    scripted.append("x")
    async with _client(server) as client:
        started = await _start(client, {"steps": _STEP, "start_after_s": 30})
        # No yield between start and cancel: the task body has not run yet.
        await flows.cancel_flow(_SESSION, started["flow_id"])
        teardown = await _start(client, {"steps": _STEP, "start_after_s": 30})
        runner_app.cancel_timer(_SESSION, teardown["flow_id"])
        await asyncio.sleep(0.01)
    assert flows._session_flows == {}
    assert json.loads(flows.list_flows(_SESSION))["flows"] == []
    assert runner_app._session_timers.get(_SESSION, {}) == {}


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
async def test_self_archived_session_ends_the_run_before_its_next_tick(
    server: _FakeServer, scripted: list[str]
) -> None:
    """A snapshot that shows the session archived ends the run with no wake."""
    from omnigent.runner import app as runner_app

    scripted.append("x")
    async with _client(server) as client:
        started = await _start(client, {"steps": _STEP, "every_s": 0.1, "times": 50})
        run = flows._session_flows[_SESSION][started["flow_id"]]
        for _ in range(100):
            if run.ticks >= 1:
                break
            await asyncio.sleep(0.01)
        # The next admission check sees the session archived: end, no wake.
        server.sessions[_SESSION]["archived"] = True
        for _ in range(100):
            if not flows._session_flows.get(_SESSION):
                break
            await asyncio.sleep(0.02)
    assert run.reason == "archived"
    assert run.detail == "the session or an ancestor was archived"
    assert run.ticks == 1
    assert server.wakes == []
    assert flows._session_flows == {}
    assert started["flow_id"] not in runner_app._session_timers.get(_SESSION, {})


@pytest.mark.asyncio
async def test_archived_ancestor_ends_the_run_before_its_next_tick(
    server: _FakeServer, scripted: list[str]
) -> None:
    """An ancestor-only archive ends the run with no tick and no wake."""
    from omnigent.runner import app as runner_app

    scripted.append("x")
    server.sessions[_SESSION]["parent_session_id"] = "conv_archived_parent"
    server.sessions["conv_archived_parent"] = {"id": "conv_archived_parent", "archived": True}
    async with _client(server) as client:
        started = await _start(client, {"steps": _STEP, "every_s": 0.1, "times": 50})
        run = flows._session_flows[_SESSION][started["flow_id"]]
        for _ in range(100):
            if not flows._session_flows.get(_SESSION):
                break
            await asyncio.sleep(0.02)
    assert run.reason == "archived"
    assert run.detail == "the session or an ancestor was archived"
    assert run.ticks == 0
    assert server.wakes == []
    assert flows._session_flows == {}
    assert started["flow_id"] not in runner_app._session_timers.get(_SESSION, {})


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
async def test_master_switch_off_refuses_start(server: _FakeServer, scripted: list[str]) -> None:
    scripted.append("x")
    server.collab_enabled = False
    async with _client(server) as client:
        started = await _start(client, {"steps": _STEP})
    assert "error" in started


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
async def test_settings_route_missing_reads_as_on(
    server: _FakeServer, scripted: list[str]
) -> None:
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
            await execute_tool(tool_name="sys_flow_list", arguments="{}", conversation_id=_SESSION)
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


def _timer_spec(spec_timers: bool) -> Any:
    """``timers: true`` spec (upstream's switch) or none (timers via the collab flag)."""
    from omnigent.spec import AgentSpec

    return AgentSpec(spec_version=1, timers=True) if spec_timers else None


@pytest.mark.asyncio
@pytest.mark.parametrize("spec_timers", [False, True])
async def test_timer_set_follows_collab_settings_only_without_spec_timers(
    server: _FakeServer, spec_timers: bool
) -> None:
    server.flow_timer_enabled = False
    server.collab_enabled = False
    async with _client(server) as client:
        output = json.loads(
            await execute_tool(
                tool_name="sys_timer_set",
                arguments=json.dumps({"seconds": 30}),
                server_client=client,
                agent_spec=_timer_spec(spec_timers),
                conversation_id=_SESSION,
            )
        )
    if spec_timers:
        assert output["status"] == "scheduled"
        assert not any(path.endswith("/collab-settings") for _m, path in server.requests)
        from omnigent.runner import app as runner_app

        runner_app.cancel_timer(_SESSION, output["timer_id"])
    else:
        assert "flow_timer_enabled" in output["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("session", [_SESSION, "conv_no_flag"])
async def test_timer_without_a_resolved_spec_is_governed_only_under_the_collab_flag(
    server: _FakeServer, session: str
) -> None:
    from omnigent.runner import app as runner_app

    server.flow_timer_enabled = False
    async with _client(server) as client:
        output = json.loads(
            await execute_tool(
                tool_name="sys_timer_set",
                arguments=json.dumps({"seconds": 30}),
                server_client=client,
                conversation_id=session,
            )
        )
    if session == _SESSION:
        assert "flow_timer_enabled" in output["error"]
    else:
        # Flag off: only a ``timers: true`` spec can have granted the tool.
        assert output["status"] == "scheduled"
        runner_app.cancel_timer(session, output["timer_id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("spec_timers", [False, True])
async def test_timer_firing_stops_on_row8_off_only_without_spec_timers(
    server: _FakeServer, spec_timers: bool
) -> None:
    from omnigent.runner import app as runner_app

    async with _client(server) as client:
        output = json.loads(
            await execute_tool(
                tool_name="sys_timer_set",
                arguments=json.dumps({"seconds": 0.05, "repeat": True}),
                server_client=client,
                agent_spec=_timer_spec(spec_timers),
                conversation_id=_SESSION,
            )
        )
        server.flow_timer_enabled = False
        await asyncio.sleep(0.15)
        running = output["timer_id"] in runner_app._session_timers.get(_SESSION, {})
        runner_app.cancel_timer(_SESSION, output["timer_id"])
    assert running is spec_timers
    assert (len(server.wakes) >= 2) is spec_timers


@pytest.mark.asyncio
async def test_timer_stops_when_an_ancestor_is_archived(server: _FakeServer) -> None:
    """An ancestor-only archive stops the timer before its next wake."""
    from omnigent.runner import app as runner_app

    server.sessions[_SESSION]["parent_session_id"] = "conv_archived_parent"
    server.sessions["conv_archived_parent"] = {"id": "conv_archived_parent", "archived": True}
    async with _client(server) as client:
        output = json.loads(
            await execute_tool(
                tool_name="sys_timer_set",
                arguments=json.dumps({"seconds": 0.05, "repeat": True}),
                server_client=client,
                conversation_id=_SESSION,
            )
        )
        assert output["status"] == "scheduled"
        await asyncio.sleep(0.15)
        running = output["timer_id"] in runner_app._session_timers.get(_SESSION, {})
        runner_app.cancel_timer(_SESSION, output["timer_id"])
    assert not running
    assert server.wakes == []


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

    return {
        s["name"]
        for s in tool_dispatch.build_native_relay_tool_schemas(spec, peer_messaging_enabled=peer)
    }


_TIMED = {
    "sys_timer_set",
    "sys_timer_cancel",
    "sys_flow_start",
    "sys_flow_list",
    "sys_flow_cancel",
}


@pytest.mark.parametrize("spec_timers", [False, True])
def test_native_relay_advertises_timers_and_flows_under_the_collab_flag(spec_timers: bool) -> None:
    from omnigent.spec import AgentSpec

    spec = AgentSpec(spec_version=1, timers=spec_timers)
    assert _relay_names(spec, peer=True) >= _TIMED
    off = _relay_names(spec, peer=False) & _TIMED
    assert off == ({"sys_timer_set", "sys_timer_cancel"} if spec_timers else set())
    assert _relay_names(None, peer=True) >= _TIMED
    assert _relay_names(None, peer=False).isdisjoint(_TIMED)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", sorted(flows.FLOW_TOOL_NAMES))
async def test_specless_dispatch_refuses_flow_tools_when_the_collab_flag_is_off(
    tool: str,
) -> None:
    output = await execute_tool(
        tool_name=tool, arguments='{"flow_id": "flow_x"}', conversation_id="conv_other"
    )
    assert json.loads(output) == {"error": f"tool {tool!r} is not enabled"}


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
