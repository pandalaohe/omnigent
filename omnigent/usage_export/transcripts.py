"""Extract metering identities and counters without retaining transcript content."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any


def _counter(usage: Mapping[str, object], name: str) -> int | None:
    value = usage.get(name)
    return value if type(value) is int and value >= 0 else None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def claude_physical_identity(path: Path) -> dict[str, str | None]:
    """Keep the parent and child physical IDs separate from row sessionId."""
    if path.suffix != ".jsonl":
        raise ValueError("unsupported_transcript_path")
    child = path.parent.name == "subagents"
    if child and not path.stem.startswith("agent-"):
        raise ValueError("unsupported_subagent_path")
    session = path.parent.parent.name if child else path.stem
    subagent = path.stem.removeprefix("agent-") if child else None
    if not session or subagent == "":
        raise ValueError("missing_physical_identity")
    return {
        "physical_transcript_path": str(path.resolve()),
        "physical_session_id": session,
        "physical_subagent_id": subagent,
        "upstream_thread_id": subagent or session,
        "thread_kind": "subagent" if child else "main",
        "harness_family": "claude",
        "identity_namespace": "claude-code/transcript/v1",
    }


def claude_measurement(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """Read one assistant response, preserving null for unobserved counters."""
    if record.get("type") != "assistant":
        return None
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    usage = message.get("usage")
    response_id = _text(message.get("id"))
    if not isinstance(usage, dict) or response_id is None:
        return None
    refs = [{"kind": "response", "namespace": "claude-code/message-id/v1", "id": response_id}]
    if request_id := _text(record.get("requestId")):
        refs.append(
            {"kind": "request", "namespace": "claude-code/request-id/v1", "id": request_id}
        )
    counters = {
        "input_tokens": _counter(usage, "input_tokens"),
        "cached_input_tokens": _counter(usage, "cache_read_input_tokens"),
        "cache_write_input_tokens": _counter(usage, "cache_creation_input_tokens"),
        "output_tokens": _counter(usage, "output_tokens"),
        "reasoning_tokens": None,
    }
    return {
        **counters,
        "measurement_kind": "request",
        "producer_unit_id": response_id,
        "logical_call_id": response_id,
        "source_refs": refs,
        "model_id": _text(message.get("model")),
        "transcript_session_id": _text(record.get("sessionId")),
        "occurred_at": _text(record.get("timestamp")),
        "call_identity_coverage": "complete",
        "usage_status": "complete"
        if all(value is not None for key, value in counters.items() if key != "reasoning_tokens")
        else "partial",
    }


def codex_measurement(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """Use per-response usage; cumulative token_count is not a call identity."""
    if record.get("type") != "token_usage_record":
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    response_id = _text(payload.get("response_id"))
    usage = payload.get("usage")
    if response_id is None or not isinstance(usage, dict):
        return None
    total_input = _counter(usage, "input_tokens")
    cached = _counter(usage, "cached_input_tokens")
    cache_write = _counter(usage, "cache_write_input_tokens")
    # Codex input includes cache reads, whereas the feed's input excludes them.
    input_tokens = (
        total_input - cached - cache_write
        if total_input is not None
        and cached is not None
        and cache_write is not None
        and cached + cache_write <= total_input
        else None
    )
    output = _counter(usage, "output_tokens")
    reasoning = _counter(usage, "reasoning_output_tokens")
    return {
        "measurement_kind": "request",
        "producer_unit_id": response_id,
        "logical_call_id": response_id,
        "source_refs": [
            {"kind": "response", "namespace": "codex-responses/response-id/v1", "id": response_id}
        ],
        "model_id": _text(payload.get("model")),
        "turn_id": _text(payload.get("turn_id")),
        "occurred_at": _text(record.get("timestamp")),
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "cache_write_input_tokens": cache_write,
        "output_tokens": output,
        "reasoning_tokens": reasoning,
        "call_identity_coverage": "complete",
        "usage_status": "complete"
        if input_tokens is not None and output is not None and reasoning is not None
        else "partial",
    }
