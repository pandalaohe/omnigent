"""
Runner-side flow runtime: time composed with tool calls, one wake at the end.

A flow (``sys_flow_start``) is one ``asyncio.Task`` per flow. Each tick runs
the flow's steps in order through the runner's own dispatch — ``execute_tool``
for runner tools, the runner MCP manager for namespaced ``x__y`` tools — after
the same out-of-turn ``PHASE_TOOL_CALL`` check ``sys_call_async`` uses. No
model turn runs between ticks. When the flow ends it posts one summary into
the calling session (``POST /v1/sessions/{id}/events``, hidden meta message,
the timer wake), which wakes the agent once.

The task is registered in the runner's timer registry
(:func:`omnigent.runner.app.register_timer`), so the idle watchdog, CLI
retention and session teardown treat it like a timer. State lives in runner
memory only: a runner restart loses running flows, as it loses timers.

Settings row ``flow_timer_enabled`` (per-user "Session collaboration") is read
live through ``GET /v1/sessions/{id}/collab-settings`` at start, before every
tick, and before every timer firing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from omnigent.tools.builtins.flow import FlowPlan, StopWhen

if TYPE_CHECKING:
    from omnigent.runner.mcp_manager import RunnerMcpManager
    from omnigent.runner.resource_registry import SessionResourceRegistry
    from omnigent.runtime.filesystem_registry import FilesystemRegistry
    from omnigent.spec.types import AgentSpec
    from omnigent.terminals.registry import TerminalRegistry

_logger = logging.getLogger(__name__)

FLOW_TOOL_NAMES = frozenset({"sys_flow_start", "sys_flow_list", "sys_flow_cancel"})

# Tools a flow step may not call: flows and timers (no nesting), and the
# async-inbox family (a flow must not start detached work or drain the
# agent's inbox behind its back).
_STEP_DENYLIST = FLOW_TOOL_NAMES | frozenset(
    {"sys_timer_set", "sys_timer_cancel", "sys_call_async", "sys_read_inbox", "sys_cancel_async"}
)

FLOW_TIMER_OFF_ERROR = (
    "flow timer is turned off in Settings > Session collaboration (flow_timer_enabled)"
)
_SUPPRESSED = "[Result suppressed by policy]"

# The runner process's MCP manager, set by ``create_runner_app``. Runner tools
# reach ``execute_tool`` without it (the /mcp/execute route passes
# ``mcp_manager=None`` for them), so flows keep their own reference for
# namespaced MCP steps.
_runner_mcp_manager: RunnerMcpManager | None = None


def set_runner_mcp_manager(manager: RunnerMcpManager | None) -> None:
    """
    Record the runner's MCP manager for flow MCP steps.

    :param manager: The runner app's manager, or ``None`` when MCP is off.
    """
    global _runner_mcp_manager
    _runner_mcp_manager = manager


@dataclass(frozen=True)
class FlowContext:
    """
    The dispatch context a flow's steps run with, captured at start.

    Mirrors the ``execute_tool`` keyword arguments of the call that started
    the flow, so every step runs with the caller's spec, registries and
    workspace.
    """

    server_client: httpx.AsyncClient
    conversation_id: str
    agent_spec: AgentSpec | None = None
    terminal_registry: TerminalRegistry | None = None
    resource_registry: SessionResourceRegistry | None = None
    task_id: str | None = None
    agent_id: str | None = None
    agent_name: str | None = None
    runner_workspace: Path | None = None
    local_tool_workdir: Any = None
    filesystem_registry: FilesystemRegistry | None = None
    effective_harness: str | None = None


@dataclass
class _StepResult:
    tick: int
    step: int
    tool: str
    args: dict[str, Any]
    output: str


@dataclass
class _FlowRun:
    flow_id: str
    plan: FlowPlan
    ctx: FlowContext
    created_mono: float
    started_at: float
    ticks: int = 0
    next_tick_mono: float | None = None
    last_step_status: str | None = None
    reason: str | None = None
    detail: str | None = None
    # ``bring_back: "all"`` only: retained results, oldest dropped once their
    # total length passes ``max_chars`` (the summary cannot carry more).
    results: list[_StepResult] = field(default_factory=list)
    dropped: int = 0
    last_tick: list[_StepResult] = field(default_factory=list)
    task: asyncio.Task[None] | None = None


# session_id → flow_id → run. Entries leave when the flow task finishes.
_session_flows: dict[str, dict[str, _FlowRun]] = {}


# ── Settings row 8 ────────────────────────────────────────────


async def read_flow_timer_enabled(server_client: httpx.AsyncClient, session_id: str) -> bool:
    """
    Read the session owner's ``flow_timer_enabled`` setting.

    An unreadable setting reads as its default (on): an older server without
    the route (404), any other status, a transport error or a malformed body.

    :param server_client: HTTP client pointed at the Omnigent server.
    :param session_id: Session whose owner's setting applies, e.g.
        ``"conv_abc123"``.
    :returns: ``False`` only when the server says the row is off.
    """
    try:
        resp = await server_client.get(
            f"/v1/sessions/{session_id}/collab-settings", timeout=10.0
        )
        if resp.status_code != 200:
            if resp.status_code != 404:
                _logger.warning(
                    "collab-settings read returned %d; flow timer stays on",
                    resp.status_code,
                    extra={"session_id": session_id},
                )
            return True
        body = resp.json()
    except Exception:  # noqa: BLE001 — any failure reads as the default
        _logger.warning(
            "collab-settings read failed; flow timer stays on",
            exc_info=True,
            extra={"session_id": session_id},
        )
        return True
    value = body.get("flow_timer_enabled") if isinstance(body, dict) else None
    return value if isinstance(value, bool) else True


# ── Stop condition ────────────────────────────────────────────


def _resolve_path(data: object, path: str) -> tuple[bool, object]:
    """Walk a dot path (integer segments index lists); ``(found, value)``."""
    current = data
    for segment in path.split("."):
        if isinstance(current, dict) and segment in current:
            current = current[segment]
        elif isinstance(current, list) and segment.lstrip("-").isdigit():
            index = int(segment)
            if not -len(current) <= index < len(current):
                return False, None
            current = current[index]
        else:
            return False, None
    return True, current


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def stop_condition_met(output: str, stop: StopWhen) -> bool:
    """
    Evaluate a stop condition against one step's raw output.

    :param output: The step's output text, e.g. ``'{"status": "idle"}'``.
    :param stop: The flow's condition.
    :returns: ``True`` when met; a missing path, non-JSON output with a
        path, or mismatched types read as not met.
    """
    if stop.path is None:
        actual: object = output
        if stop.op in ("gt", "lt"):
            try:
                actual = float(output.strip())
            except ValueError:
                return False
    else:
        try:
            parsed = json.loads(output)
        except (json.JSONDecodeError, ValueError):
            return False
        found, actual = _resolve_path(parsed, stop.path)
        if not found:
            return False
    if stop.op == "eq":
        if _is_number(actual) and _is_number(stop.value):
            return float(actual) == float(stop.value)  # type: ignore[arg-type]
        return type(actual) is type(stop.value) and actual == stop.value
    if stop.op == "contains":
        if isinstance(actual, str):
            return isinstance(stop.value, str) and stop.value in actual
        if isinstance(actual, list):
            return stop.value in actual
        return False
    if not (_is_number(actual) and _is_number(stop.value)):
        return False
    if stop.op == "gt":
        return float(actual) > float(stop.value)  # type: ignore[arg-type]
    return float(actual) < float(stop.value)  # type: ignore[arg-type]


def _step_error(output: str) -> str | None:
    """Return the error text when a step output reports a failure."""
    stripped = output.lstrip()
    if stripped.startswith("Error:"):
        return stripped[:500]
    if stripped.startswith("{"):
        try:
            parsed = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            return None
        if isinstance(parsed, dict):
            error = parsed.get("error")
            if error:
                return str(error)[:500]
    return None


# ── Start / list / cancel ─────────────────────────────────────


def _step_refusal(tool: str, ctx: FlowContext) -> str | None:
    """Return why a step tool cannot run in a flow, or ``None``."""
    from omnigent.runner import tool_dispatch as _td

    if tool in _STEP_DENYLIST:
        return f"tool {tool!r} cannot run inside a flow"
    if "__" in tool:
        if _runner_mcp_manager is None:
            return f"MCP tool {tool!r} is unavailable: this runner has no MCP manager"
        if ctx.agent_spec is None:
            return f"MCP tool {tool!r} is unavailable: no agent spec for this session"
        return None
    return _td._ungranted_tool_reason(
        tool,
        ctx.agent_spec,
        ctx.effective_harness,
        project_assignments_enabled=_td._project_assignments_enabled_for(ctx.conversation_id),
        peer_messaging_enabled=_td._peer_messaging_enabled_for(ctx.conversation_id),
    )


async def start_flow(plan: FlowPlan, ctx: FlowContext) -> str:
    """
    Start a validated flow for ``ctx.conversation_id``.

    :param plan: Validated ``sys_flow_start`` request.
    :param ctx: Dispatch context captured from the calling tool call.
    :returns: JSON start result with ``flow_id``, or ``{"error": ...}``.
    """
    from omnigent.runner import app as _app

    for index, step in enumerate(plan.steps):
        refusal = _step_refusal(step.tool, ctx)
        if refusal is not None:
            return json.dumps({"error": f"steps[{index}]: {refusal}"})
    if not await read_flow_timer_enabled(ctx.server_client, ctx.conversation_id):
        return json.dumps({"error": FLOW_TIMER_OFF_ERROR})

    loop = asyncio.get_running_loop()
    flow_id = f"flow_{uuid.uuid4().hex}"
    run = _FlowRun(
        flow_id=flow_id,
        plan=plan,
        ctx=ctx,
        created_mono=loop.time(),
        started_at=time.time(),
    )
    run.next_tick_mono = run.created_mono + plan.start_after_s
    _session_flows.setdefault(ctx.conversation_id, {})[flow_id] = run
    run.task = asyncio.create_task(_run_flow(run), name=f"flow-{flow_id}")
    _app.register_timer(ctx.conversation_id, flow_id, run.task)
    return json.dumps(
        {
            "flow_id": flow_id,
            "status": "running",
            "first_tick_in_s": plan.start_after_s,
            "every_s": plan.every_s,
            "for_s": plan.for_s,
            "times": plan.times,
            "steps": [step.tool for step in plan.steps],
            "note": plan.note,
        }
    )


def list_flows(session_id: str) -> str:
    """
    List a session's running flows (metadata only, never step output).

    :param session_id: Calling session, e.g. ``"conv_abc123"``.
    :returns: JSON ``{"flows": [...]}``.
    """
    now = asyncio.get_running_loop().time()
    flows = []
    for run in _session_flows.get(session_id, {}).values():
        if run.reason is not None:
            continue
        next_in = None if run.next_tick_mono is None else max(0.0, run.next_tick_mono - now)
        flows.append(
            {
                "flow_id": run.flow_id,
                "note": run.plan.note,
                "steps": [step.tool for step in run.plan.steps],
                "started_at": run.started_at,
                "ticks": run.ticks,
                "next_tick_in_s": None if next_in is None else round(next_in, 1),
                "every_s": run.plan.every_s,
                "for_s": run.plan.for_s,
                "times": run.plan.times,
                "last_step_status": run.last_step_status,
            }
        )
    return json.dumps({"flows": flows})


async def cancel_flow(session_id: str, flow_id: str) -> str:
    """
    Cancel a running flow and return its summary (no wake is posted).

    :param session_id: Calling session, e.g. ``"conv_abc123"``.
    :param flow_id: Id returned by ``sys_flow_start``.
    :returns: JSON summary with ``status: "cancelled"``, or ``not_found``.
    """
    run = _session_flows.get(session_id, {}).get(flow_id)
    if run is None or run.reason is not None or run.task is None or run.task.done():
        return json.dumps({"flow_id": flow_id, "status": "not_found"})
    run.reason = "cancelled"
    run.detail = "cancelled by sys_flow_cancel"
    run.task.cancel()
    summary = await _summary(run)
    summary["status"] = "cancelled"
    return json.dumps(summary)


# ── Run loop ──────────────────────────────────────────────────


async def _step_policy(ctx: FlowContext, tool: str, args: dict[str, Any]) -> dict[str, Any] | None:
    """
    Evaluate ``PHASE_TOOL_CALL`` for one step out of turn.

    ASK parks server-side until the user answers or the policy times out;
    the endpoint then answers ALLOW or DENY.

    :returns: The arguments to run with — a policy's rewrite (``data``) when
        present, else ``args`` — or ``None`` when the step may not run.
    """
    from omnigent.native.native_policy_hook import post_evaluate_with_retry_async
    from omnigent.runner.tool_dispatch import _ASK_GATE_DELIVERY_TIMEOUT

    try:
        resp, _error = await post_evaluate_with_retry_async(
            ctx.server_client,
            f"/v1/sessions/{ctx.conversation_id}/policies/evaluate",
            {"event": {"type": "PHASE_TOOL_CALL", "data": {"name": tool, "arguments": args}}},
            _ASK_GATE_DELIVERY_TIMEOUT,
            "runner flow PHASE_TOOL_CALL evaluate",
        )
        verdict = resp.json() if resp is not None and resp.status_code == 200 else None
    except Exception:  # noqa: BLE001 — fail closed
        verdict = None
    if not isinstance(verdict, dict):
        return None
    if verdict.get("result") not in ("POLICY_ACTION_ALLOW", "POLICY_ACTION_UNSPECIFIED"):
        return None
    data = verdict.get("data")
    return data if isinstance(data, dict) else args


async def _run_step(run: _FlowRun, index: int, args: dict[str, Any]) -> str:
    """Run one step with ``args`` through the runner's dispatch; return its output."""
    from omnigent.runner.tool_dispatch import execute_tool

    step = run.plan.steps[index]
    ctx = run.ctx
    if "__" in step.tool:
        manager = _runner_mcp_manager
        if manager is None or ctx.agent_spec is None:
            return f"Error: MCP tool {step.tool!r} is unavailable"
        return await manager.call_tool(ctx.agent_spec, step.tool, args, session_id=ctx.conversation_id)
    return await execute_tool(
        tool_name=step.tool,
        arguments=json.dumps(args),
        server_client=ctx.server_client,
        terminal_registry=ctx.terminal_registry,
        resource_registry=ctx.resource_registry,
        agent_spec=ctx.agent_spec,
        conversation_id=ctx.conversation_id,
        task_id=ctx.task_id,
        agent_id=ctx.agent_id,
        agent_name=ctx.agent_name,
        runner_workspace=ctx.runner_workspace,
        local_tool_workdir=ctx.local_tool_workdir,
        mcp_manager=None,
        filesystem_registry=ctx.filesystem_registry,
        effective_harness=ctx.effective_harness,
    )


def _retain(run: _FlowRun, result: _StepResult) -> None:
    """Keep a tick result for ``bring_back: "all"`` within the char budget."""
    run.results.append(result)
    total = sum(len(kept.output) for kept in run.results)
    while len(run.results) > 1 and total - len(run.results[0].output) >= run.plan.max_chars:
        total -= len(run.results.pop(0).output)
        run.dropped += 1


_DENIED = object()


async def _gated_step(run: _FlowRun, index: int) -> tuple[dict[str, Any], str] | object:
    """Policy-check then run one step; ``_DENIED`` when policy refuses it."""
    step = run.plan.steps[index]
    args = await _step_policy(run.ctx, step.tool, step.args)
    if args is None:
        return _DENIED
    try:
        output = await _run_step(run, index, args)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — a step failure ends the flow
        output = f"Error: {type(exc).__name__}: {exc}"
    return args, str(output)


async def _tick(run: _FlowRun, deadline: float | None) -> bool:
    """
    Run one tick. Sets ``run.reason`` and returns ``True`` when the flow ends.

    :param deadline: Loop time at which ``for_s`` expires; a policy wait or step
        still running then is cancelled and no later step starts.
    """
    loop = asyncio.get_running_loop()
    run.ticks += 1
    tick_results: list[_StepResult] = []
    for index, step in enumerate(run.plan.steps):
        remaining = None if deadline is None else deadline - loop.time()
        try:
            if remaining is not None and remaining <= 0:
                raise TimeoutError
            outcome = await asyncio.wait_for(_gated_step(run, index), timeout=remaining)
        except TimeoutError:
            run.reason = "time_up"
            run.detail = f"for_s {run.plan.for_s} reached during steps[{index}] ({step.tool})"
            run.last_tick = tick_results
            return True
        if outcome is _DENIED:
            run.last_step_status = "policy_denied"
            run.reason = "policy_denied"
            run.detail = f"PHASE_TOOL_CALL denied steps[{index}] ({step.tool})"
            run.last_tick = tick_results
            return True
        args, output = outcome  # type: ignore[misc]
        result = _StepResult(run.ticks, index, step.tool, args, output)
        tick_results.append(result)
        error = _step_error(result.output)
        run.last_step_status = "error" if error else "ok"
        if error is not None:
            run.reason = "step_error"
            run.detail = f"steps[{index}] ({step.tool}): {error}"
            run.last_tick = tick_results
            return True
    run.last_tick = tick_results
    stop = run.plan.stop_when
    if run.plan.bring_back == "all":
        _retain(run, tick_results[stop.step if stop is not None else -1])
    if stop is not None and stop_condition_met(tick_results[stop.step].output, stop):
        run.reason = "condition_met"
        run.detail = f"steps[{stop.step}] result {stop.op} {stop.value!r}"
        return True
    if run.plan.times is not None and run.ticks >= run.plan.times:
        run.reason = "count_done"
        run.detail = f"{run.ticks} tick(s) run"
        return True
    if run.plan.every_s is None:
        run.reason = "count_done"
        run.detail = "single tick run"
        return True
    return False


async def _run_flow(run: _FlowRun) -> None:
    """Background task: tick on schedule, then post one wake summary."""
    from omnigent.runner import app as _app

    loop = asyncio.get_running_loop()
    plan = run.plan
    deadline = None if plan.for_s is None else run.created_mono + plan.for_s
    scheduled = run.created_mono + plan.start_after_s
    wake = False
    try:
        while True:
            run.next_tick_mono = scheduled
            wait_until = scheduled if deadline is None else min(scheduled, deadline)
            delay = wait_until - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            if deadline is not None and loop.time() >= deadline:
                run.reason = "time_up"
                run.detail = f"for_s {plan.for_s} reached after {run.ticks} tick(s)"
                wake = True
                break
            if not await read_flow_timer_enabled(run.ctx.server_client, run.ctx.conversation_id):
                run.reason = "disabled"
                run.detail = FLOW_TIMER_OFF_ERROR
                _logger.info(
                    "flow %s stopped: flow_timer_enabled is off",
                    run.flow_id,
                    extra={"session_id": run.ctx.conversation_id},
                )
                break
            run.next_tick_mono = None
            if await _tick(run, deadline):
                wake = True
                break
            assert plan.every_s is not None
            scheduled = max(scheduled + plan.every_s, loop.time())
        if wake:
            await _post_wake(run)
    except asyncio.CancelledError:
        return
    except Exception:  # noqa: BLE001 — never leak a background failure
        _logger.exception(
            "flow %s failed", run.flow_id, extra={"session_id": run.ctx.conversation_id}
        )
    finally:
        flows = _session_flows.get(run.ctx.conversation_id)
        if flows is not None:
            flows.pop(run.flow_id, None)
            if not flows:
                _session_flows.pop(run.ctx.conversation_id, None)
        _app.unregister_timer(run.ctx.conversation_id, run.flow_id)


# ── Summary and wake ──────────────────────────────────────────


async def _result_policy_output(ctx: FlowContext, result: _StepResult) -> str:
    """Pass a returned result through ``PHASE_TOOL_RESULT`` (fail closed)."""
    try:
        resp = await ctx.server_client.post(
            f"/v1/sessions/{ctx.conversation_id}/policies/evaluate",
            json={
                "event": {
                    "type": "PHASE_TOOL_RESULT",
                    "data": {"result": result.output},
                    "request_data": {"name": result.tool, "tool": result.tool, "args": result.args},
                }
            },
            timeout=30.0,
        )
        verdict = resp.json() if resp.status_code == 200 else None
    except Exception:  # noqa: BLE001 — fail closed
        verdict = None
    if not isinstance(verdict, dict):
        return _SUPPRESSED
    action = verdict.get("result")
    if action in ("POLICY_ACTION_ALLOW", "POLICY_ACTION_UNSPECIFIED"):
        data = verdict.get("data")
        return result.output if data is None else str(data)
    return _SUPPRESSED


def _returned_results(run: _FlowRun) -> list[_StepResult]:
    """Pick the results the summary carries per ``bring_back``."""
    if run.plan.bring_back == "none":
        return []
    if run.plan.bring_back == "last":
        return run.last_tick[-1:]
    return list(run.results)


async def _summary(run: _FlowRun) -> dict[str, Any]:
    """Build the end summary, returned results policy-checked and truncated."""
    loop = asyncio.get_running_loop()
    picked = _returned_results(run)
    budget = run.plan.max_chars
    rendered: list[dict[str, Any]] = []
    # Newest results win the shared budget; oldest are dropped first.
    for result in reversed(picked):
        if budget <= 0:
            break
        output = await _result_policy_output(run.ctx, result)
        truncated = len(output) > budget
        output = output[:budget]
        budget -= len(output)
        rendered.append(
            {
                "tick": result.tick,
                "step": result.step,
                "tool": result.tool,
                "output": output,
                "truncated": truncated,
            }
        )
    rendered.reverse()
    return {
        "flow_id": run.flow_id,
        "reason": run.reason,
        "detail": run.detail,
        "note": run.plan.note,
        "ticks": run.ticks,
        "elapsed_s": round(loop.time() - run.created_mono, 1),
        "results": rendered,
        "dropped_results": run.dropped + len(picked) - len(rendered),
    }


async def _post_wake(run: _FlowRun) -> None:
    """
    Post the one wake message carrying the summary.

    Posted once, never retried: a retry after a lost response could inject the
    summary twice, and the receiver has nothing to deduplicate on.
    """
    summary = await _summary(run)
    text = f"[System: flow {run.flow_id} ended: {run.reason}]"
    if run.plan.note:
        text += f"\nnote: {run.plan.note!r}"
    text += "\n" + json.dumps(summary, ensure_ascii=False)
    ctx = run.ctx
    try:
        resp = await ctx.server_client.post(
            f"/v1/sessions/{ctx.conversation_id}/events",
            json={
                "type": "message",
                "data": {
                    "role": "user",
                    "is_meta": True,
                    "content": [{"type": "input_text", "text": text}],
                },
            },
            timeout=30.0,
        )
        resp.raise_for_status()
    except (httpx.HTTPError, asyncio.TimeoutError):
        _logger.warning(
            "flow %s wake POST failed",
            run.flow_id,
            exc_info=True,
            extra={"session_id": ctx.conversation_id},
        )
