"""
LLM-callable flow builtins: time composed with tool calls.

Three tools:

- :class:`SysFlowStartTool` (``sys_flow_start``) — starts a flow: when to
  begin, how often, for how long or how many times, which tool calls to make
  each time, when to stop, and how much result to bring back.
- :class:`SysFlowListTool` (``sys_flow_list``) — lists the calling session's
  running flows.
- :class:`SysFlowCancelTool` (``sys_flow_cancel``) — cancels one.

The runner executes a flow with no model turn between ticks and wakes the
calling session once when it ends (:mod:`omnigent.runner.flows`). These
classes own the LLM-facing schema and argument validation; like the timer
builtins they are gated on the agent spec's ``timers:`` flag and are
intercepted by the runner before ``invoke`` is reached.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Literal

from omnigent.tools.base import Tool, ToolContext

FLOW_MAX_STEPS = 20
FLOW_DEFAULT_MAX_CHARS = 2000
FLOW_MAX_CHARS_CEILING = 20_000

StopOp = Literal["eq", "contains", "gt", "lt"]
BringBack = Literal["last", "all", "none"]
_STOP_OPS: tuple[str, ...] = ("eq", "contains", "gt", "lt")
_BRING_BACK: tuple[str, ...] = ("last", "all", "none")


@dataclass(frozen=True)
class FlowStep:
    """
    One tool call made on every tick.

    :param tool: Tool name, e.g. ``"sys_session_get_info"``.
    :param args: The tool's argument object, e.g. ``{"session_id": "conv_x"}``.
    """

    tool: str
    args: dict[str, Any]


@dataclass(frozen=True)
class StopWhen:
    """
    Stop condition evaluated on one step's result after each tick.

    :param op: ``"eq"``, ``"contains"``, ``"gt"`` or ``"lt"``.
    :param value: Value compared against, e.g. ``"idle"``.
    :param path: Optional dot path into the result parsed as JSON, e.g.
        ``"sessions.0.status"``; ``None`` compares the raw output.
    :param step: Index into the flow's steps whose result is checked.
    """

    op: StopOp
    value: Any
    path: str | None
    step: int


@dataclass(frozen=True)
class FlowPlan:
    """
    A validated ``sys_flow_start`` request.

    :param steps: Tool calls made on every tick, in order.
    :param start_after_s: Delay before the first tick.
    :param every_s: Interval between tick starts; ``None`` → one tick.
    :param for_s: Total lifetime from the start call; ``None`` → unbounded.
    :param times: Maximum number of ticks; ``None`` → unbounded.
    :param stop_when: Optional stop condition.
    :param bring_back: Which results the end summary carries.
    :param max_chars: Character budget for returned results.
    :param note: Optional note echoed in list and summary.
    """

    steps: tuple[FlowStep, ...]
    start_after_s: float
    every_s: float | None
    for_s: float | None
    times: int | None
    stop_when: StopWhen | None
    bring_back: BringBack
    max_chars: int
    note: str | None


def _seconds(args: dict[str, Any], key: str, *, allow_zero: bool) -> float | None | str:
    """Validate an optional seconds field; return the value, ``None`` or an error."""
    raw = args.get(key)
    if raw is None:
        return None
    if not isinstance(raw, (int, float)) or isinstance(raw, bool):
        return f"{key} must be a number"
    value = float(raw)
    if not math.isfinite(value):
        return f"{key} must be a finite number"
    if value < 0 or (value == 0 and not allow_zero):
        return f"{key} must be {'non-negative' if allow_zero else '> 0'}"
    return value


def _positive_int(args: dict[str, Any], key: str, *, ceiling: int | None = None) -> int | None | str:
    """Validate an optional positive integer field."""
    raw = args.get(key)
    if raw is None:
        return None
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < 1:
        return f"{key} must be an integer >= 1"
    if ceiling is not None and raw > ceiling:
        return f"{key} must be <= {ceiling}"
    return raw


def _validate_steps(raw: object) -> tuple[FlowStep, ...] | str:
    """Validate the ``steps`` array."""
    if not isinstance(raw, list) or not raw:
        return "steps must be a non-empty array"
    if len(raw) > FLOW_MAX_STEPS:
        return f"steps must have at most {FLOW_MAX_STEPS} entries"
    steps: list[FlowStep] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            return f"steps[{index}] must be an object"
        tool = entry.get("tool")
        if not isinstance(tool, str) or not tool:
            return f"steps[{index}].tool must be a non-empty string"
        step_args = entry.get("args", {})
        if step_args is None:
            step_args = {}
        if not isinstance(step_args, dict):
            return f"steps[{index}].args must be an object"
        steps.append(FlowStep(tool=tool, args=step_args))
    return tuple(steps)


def _validate_stop_when(raw: object, step_count: int) -> StopWhen | None | str:
    """Validate the optional ``stop_when`` object."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        return "stop_when must be an object"
    op = raw.get("op")
    if op not in _STOP_OPS:
        return f"stop_when.op must be one of {', '.join(_STOP_OPS)}"
    if "value" not in raw:
        return "stop_when.value is required"
    value = raw["value"]
    if isinstance(value, (dict, list)):
        return "stop_when.value must be a string, number, boolean or null"
    if op in ("gt", "lt") and (not isinstance(value, (int, float)) or isinstance(value, bool)):
        return f"stop_when.value must be a number for op {op!r}"
    path = raw.get("path")
    if path is not None and (not isinstance(path, str) or not path):
        return "stop_when.path must be a non-empty string"
    step = raw.get("step", step_count - 1)
    if not isinstance(step, int) or isinstance(step, bool) or not 0 <= step < step_count:
        return f"stop_when.step must be an integer from 0 to {step_count - 1}"
    return StopWhen(op=op, value=value, path=path, step=step)


def validate_flow_start_args(args: dict[str, Any]) -> FlowPlan | str:
    """
    Validate parsed ``sys_flow_start`` arguments.

    Shared by :meth:`SysFlowStartTool.invoke` and the runner's flow start so
    both surfaces reject the same inputs with identical messages.

    :param args: JSON-decoded argument mapping, e.g.
        ``{"steps": [{"tool": "sys_session_get_info", "args": {}}],
        "every_s": 60, "for_s": 600}``.
    :returns: The validated plan, or an error message naming the first
        invalid field, e.g. ``"steps must be a non-empty array"``.
    """
    steps = _validate_steps(args.get("steps"))
    if isinstance(steps, str):
        return steps
    start_after = _seconds(args, "start_after_s", allow_zero=True)
    if isinstance(start_after, str):
        return start_after
    every = _seconds(args, "every_s", allow_zero=False)
    if isinstance(every, str):
        return every
    for_s = _seconds(args, "for_s", allow_zero=False)
    if isinstance(for_s, str):
        return for_s
    times = _positive_int(args, "times")
    if isinstance(times, str):
        return times
    if every is not None and for_s is None and times is None:
        return "a repeating flow (every_s) needs for_s or times so it always ends"
    stop_when = _validate_stop_when(args.get("stop_when"), len(steps))
    if isinstance(stop_when, str):
        return stop_when
    bring_back = args.get("bring_back", "last")
    if bring_back not in _BRING_BACK:
        return f"bring_back must be one of {', '.join(_BRING_BACK)}"
    max_chars = _positive_int(args, "max_chars", ceiling=FLOW_MAX_CHARS_CEILING)
    if isinstance(max_chars, str):
        return max_chars
    note = args.get("note")
    if note is not None and not isinstance(note, str):
        return "note must be a string"
    return FlowPlan(
        steps=steps,
        start_after_s=start_after or 0.0,
        every_s=every,
        for_s=for_s,
        times=times,
        stop_when=stop_when,
        bring_back=bring_back,
        max_chars=max_chars if max_chars is not None else FLOW_DEFAULT_MAX_CHARS,
        note=note,
    )


_RUNNER_ONLY_ERROR = (
    "{name} is executed by the runner dispatch path; this in-process call cannot run flows."
)


class SysFlowStartTool(Tool):
    """Start a flow the runner executes to completion, waking the session once."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_flow_start"``."""
        return "sys_flow_start"

    @classmethod
    def description(cls) -> str:
        """:returns: Description visible to the LLM in tool listings."""
        return (
            "Start a flow: tool calls repeated on a schedule and run by the host with no "
            "model turn in between. Give the steps (tool + args, run in order on every "
            "tick), when to start (start_after_s), how often (every_s), and when to end "
            "(for_s from now and/or times ticks; a repeating flow needs at least one), plus "
            "an optional stop condition on a step's result. When the flow ends (stop "
            "condition met, time up, count used, step error or policy denial) one "
            "[System: flow X ended: reason] message wakes you with the summary. Steps may "
            "call shell, Omnigent and MCP tools you have, not your own harness tools. "
            "Returns the flow_id immediately; see sys_flow_list / sys_flow_cancel."
        )

    def get_schema(self) -> dict[str, Any]:
        """:returns: OpenAI tool schema for ``sys_flow_start``."""
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "steps": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": FLOW_MAX_STEPS,
                            "description": "Tool calls made on every tick, in order.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "tool": {
                                        "type": "string",
                                        "description": "Tool name, e.g. 'sys_session_get_info'.",
                                    },
                                    "args": {
                                        "type": "object",
                                        "description": "That tool's arguments.",
                                    },
                                },
                                "required": ["tool"],
                                "additionalProperties": False,
                            },
                        },
                        "start_after_s": {
                            "type": "number",
                            "description": "Seconds before the first tick (default 0).",
                        },
                        "every_s": {
                            "type": "number",
                            "description": "Seconds between ticks. Omit to run one tick.",
                        },
                        "for_s": {
                            "type": "number",
                            "description": "End the flow this many seconds after the start call.",
                        },
                        "times": {
                            "type": "integer",
                            "description": "End the flow after this many ticks.",
                        },
                        "stop_when": {
                            "type": "object",
                            "description": (
                                "End when a step's result matches. path is a dot path into "
                                "the result parsed as JSON (e.g. 'status', 'items.0.state'); "
                                "omit it to compare the raw text. step is the step index "
                                "(default: last step)."
                            ),
                            "properties": {
                                "op": {"type": "string", "enum": list(_STOP_OPS)},
                                "value": {
                                    "description": "Value compared against (number for gt/lt)."
                                },
                                "path": {"type": "string"},
                                "step": {"type": "integer"},
                            },
                            "required": ["op", "value"],
                            "additionalProperties": False,
                        },
                        "bring_back": {
                            "type": "string",
                            "enum": list(_BRING_BACK),
                            "description": (
                                "Results in the end summary: 'last' (default) the last "
                                "tick's last step, 'all' every tick's stop-step result, "
                                "'none'."
                            ),
                        },
                        "max_chars": {
                            "type": "integer",
                            "description": (
                                f"Character budget for returned results (default "
                                f"{FLOW_DEFAULT_MAX_CHARS}, max {FLOW_MAX_CHARS_CEILING})."
                            ),
                        },
                        "note": {
                            "type": "string",
                            "description": "Optional note echoed in list and summary.",
                        },
                    },
                    "required": ["steps"],
                    "additionalProperties": False,
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        """
        Validate arguments; report that the in-process path runs no flow.

        :param arguments: JSON-encoded ``sys_flow_start`` arguments.
        :param ctx: Tool context (unused off the runner path).
        :returns: JSON ``{"error": ...}``.
        """
        del ctx
        try:
            args = json.loads(arguments) if arguments else {}
        except json.JSONDecodeError as exc:
            return json.dumps({"error": f"invalid arguments: {exc}"})
        if not isinstance(args, dict):
            return json.dumps({"error": "arguments must be an object"})
        validated = validate_flow_start_args(args)
        if isinstance(validated, str):
            return json.dumps({"error": validated})
        return json.dumps({"error": _RUNNER_ONLY_ERROR.format(name=self.name())})


class SysFlowListTool(Tool):
    """List the calling session's running flows."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_flow_list"``."""
        return "sys_flow_list"

    @classmethod
    def description(cls) -> str:
        """:returns: Description visible to the LLM in tool listings."""
        return (
            "List your running flows (started with sys_flow_start): id, note, steps, "
            "ticks run, time to next tick and bounds. Does not return step results."
        )

    def get_schema(self) -> dict[str, Any]:
        """:returns: OpenAI tool schema with no parameters."""
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        """:returns: An empty list — no flow registry exists in-process."""
        del arguments, ctx
        return json.dumps({"flows": []})


class SysFlowCancelTool(Tool):
    """Cancel a running flow by ``flow_id``."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_flow_cancel"``."""
        return "sys_flow_cancel"

    @classmethod
    def description(cls) -> str:
        """:returns: Description visible to the LLM in tool listings."""
        return (
            "Cancel a running flow by flow_id. Returns its summary with "
            "status='cancelled' (no wake message follows), or status='not_found'."
        )

    def get_schema(self) -> dict[str, Any]:
        """:returns: OpenAI tool schema with ``flow_id`` (string, required)."""
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "flow_id": {
                            "type": "string",
                            "description": "The flow_id returned by sys_flow_start.",
                        },
                    },
                    "required": ["flow_id"],
                    "additionalProperties": False,
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        """:returns: ``not_found`` — no flow registry exists in-process."""
        del ctx
        try:
            args = json.loads(arguments) if arguments else {}
        except json.JSONDecodeError as exc:
            return json.dumps({"error": f"invalid arguments: {exc}"})
        flow_id = args.get("flow_id") if isinstance(args, dict) else None
        if not isinstance(flow_id, str) or not flow_id:
            return json.dumps({"error": "flow_id is required"})
        return json.dumps({"flow_id": flow_id, "status": "not_found"})
