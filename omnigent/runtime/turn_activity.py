"""Observed turn/tool lifecycle, including events omitted from durable history."""

from __future__ import annotations

import copy
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from omnigent.db.workspace_cache import WorkspaceScopedCache


@dataclass
class TurnActivity:
    response_id: str
    active: bool = True
    updated: float = field(default_factory=time.monotonic)
    tools: dict[str, tuple[str, float]] = field(default_factory=dict)
    finished_calls: set[str] = field(default_factory=set)


_turns: WorkspaceScopedCache[str, TurnActivity] = WorkspaceScopedCache()
_lock = threading.Lock()


def record_publish(session_id: str, event: dict[str, Any]) -> None:
    """An absent lifecycle start means tool absence cannot be established."""
    kind = event.get("type")
    response = event.get("response")
    response_id = response.get("id") if isinstance(response, dict) else None
    with _lock:
        state = _turns.get(session_id)
        if kind in ("response.created", "response.in_progress"):
            if not isinstance(response_id, str) or not response_id:
                return
            if state is None or state.response_id != response_id:
                _turns[session_id] = TurnActivity(response_id)
            return
        if state is None or not state.active:
            return
        if kind in (
            "response.completed",
            "response.failed",
            "response.cancelled",
            "response.incomplete",
        ):
            if response_id != state.response_id:
                return
            state.active = False
            state.tools.clear()
            state.finished_calls.clear()
            return
        item = event.get("item")
        if kind in ("response.output_item.added", "response.output_item.done") and isinstance(
            item, dict
        ):
            item_type = item.get("type")
            if item_type == "native_tool" or (
                isinstance(item_type, str)
                and item_type.endswith("_call")
                and item_type != "function_call"
            ):
                native = item.get("item", item)
                if not isinstance(native, dict) or not isinstance(native.get("id"), str):
                    state.tools.setdefault("unknown-native-tool", ("tool", time.time()))
                    return
                key = "native:" + native["id"]
                if native.get("status") in ("completed", "failed", "cancelled", "incomplete"):
                    state.tools.pop(key, None)
                else:
                    state.tools.setdefault(key, (str(native.get("type", "tool")), time.time()))
                state.updated = time.monotonic()
                return
            call_id = item.get("call_id")
            if not isinstance(call_id, str) or not call_id:
                return
            if item.get("type") == "function_call_output":
                status = item.get("tool_status")
                terminal = status in ("completed", "failed", "cancelled", "killed")
                if terminal or (
                    not item.get("is_async")
                    and status not in ("running", "in_progress", "pending")
                ):
                    state.tools.pop(call_id, None)
                    state.finished_calls.add(call_id)
                else:
                    state.tools.setdefault(call_id, ("tool", time.time()))
            elif item.get("type") == "function_call" and call_id not in state.finished_calls:
                state.tools.setdefault(call_id, (str(item.get("name", "tool")), time.time()))
        if kind in (
            "response.output_item.added",
            "response.output_item.done",
            "response.output_text.delta",
            "response.reasoning_text.delta",
            "response.function_call_output.delta",
        ):
            state.updated = time.monotonic()


def snapshot_for(session_id: str) -> TurnActivity | None:
    with _lock:
        state = _turns.get(session_id)
        return copy.deepcopy(state) if state else None


def discard(session_id: str) -> None:
    with _lock:
        _turns.pop(session_id, None)


def reset_for_tests() -> None:
    with _lock:
        _turns.clear()
