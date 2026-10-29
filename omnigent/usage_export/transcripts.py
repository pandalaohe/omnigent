"""Extract metering identities and counters without retaining transcript content."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def _counter(usage: Mapping[str, object], name: str) -> int | None:
    value = usage.get(name)
    return value if type(value) is int and value >= 0 else None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def antigravity_measurement(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """Read one native model call; billing totals never represent context fill."""
    if record.get("status") not in {
        "CORTEX_STEP_STATUS_DONE",
        "CORTEX_STEP_STATUS_ERROR",
    }:
        return None
    metadata = record.get("metadata")
    if not isinstance(metadata, dict):
        return None
    usage = metadata.get("modelUsage")
    if not isinstance(usage, dict):
        return None

    def count(name: str) -> int | None:
        value = usage.get(name)
        if isinstance(value, str) and len(value) <= 20 and value.isascii() and value.isdigit():
            value = int(value)
        return value if type(value) is int and 0 <= value < 2**64 else None

    total, cached, written = (
        count("inputTokens"),
        count("cacheReadTokens"),
        count("cacheWriteTokens"),
    )
    counters = {
        "input_tokens": total - cached - written
        if total is not None
        and cached is not None
        and written is not None
        and cached + written <= total
        else None,
        "cached_input_tokens": cached,
        "cache_write_input_tokens": written,
        "output_tokens": count("outputTokens"),
        "reasoning_tokens": count("thinkingOutputTokens"),
    }
    if total is None and all(value is None for value in counters.values()):
        return None
    refs = [
        {"kind": "response", "namespace": namespace, "id": value}
        for field, namespace in (
            ("responseId", "antigravity/response-id/v1"),
            ("providerAssignedMessageId", "antigravity/provider-message-id/v1"),
            ("messageId", "antigravity/message-id/v1"),
        )
        if (value := _text(usage.get(field))) is not None
    ]
    source = metadata.get("sourceTrajectoryStepInfo")
    trajectory = _text(source.get("trajectoryId")) if isinstance(source, dict) else None
    index = source.get("stepIndex", 0) if isinstance(source, dict) else None
    if refs:
        unit = json.dumps([refs[0]["namespace"], refs[0]["id"]], separators=(",", ":"))
    elif trajectory and type(index) is int and index >= 0:
        unit = json.dumps(
            [trajectory, index, record.get("usage_source", "model")], separators=(",", ":")
        )
    else:
        return None
    return {
        **counters,
        "measurement_kind": "request",
        "producer_unit_id": unit,
        "logical_call_id": refs[0]["id"] if refs else None,
        "source_refs": refs,
        "model_id": _text(usage.get("model")),
        "turn_id": _text(metadata.get("executionId")),
        "occurred_at": _text(metadata.get("completedAt")) or _text(metadata.get("createdAt")),
        "call_identity_coverage": "complete" if refs else "none",
        "usage_status": "complete"
        if all(value is not None for value in counters.values())
        else "partial",
    }


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


def pi_measurement(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """Extract one Pi request or auxiliary usage entry without retaining content."""
    entry_id = _text(record.get("id"))
    if entry_id is None:
        return None
    kind = record.get("type")
    if kind == "message":
        message = record.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            return None
        usage = message.get("usage")
        model = _text(message.get("model"))
        response_id = _text(message.get("responseId"))
        measurement_kind = "request"
    elif kind in {"usage", "compaction", "branch_summary"}:
        usage = record.get("usage")
        model = _text(record.get("model"))
        response_id = None
        measurement_kind = "increment"
    else:
        return None
    if not isinstance(usage, dict):
        return None
    counters = {
        "input_tokens": _counter(usage, "input"),
        "cached_input_tokens": _counter(usage, "cacheRead"),
        "cache_write_input_tokens": _counter(usage, "cacheWrite"),
        "output_tokens": _counter(usage, "output"),
        "reasoning_tokens": _counter(usage, "reasoning"),
    }
    return {
        "measurement_kind": measurement_kind,
        "producer_unit_id": entry_id,
        "logical_call_id": response_id,
        "source_refs": (
            [{"kind": "response", "namespace": "pi/response-id/v1", "id": response_id}]
            if response_id
            else []
        ),
        "model_id": model,
        "occurred_at": _text(record.get("timestamp")),
        **counters,
        "call_identity_coverage": "complete" if response_id else "none",
        "usage_status": "complete"
        if all(
            counters[name] is not None
            for name in (
                "input_tokens",
                "cached_input_tokens",
                "cache_write_input_tokens",
                "output_tokens",
            )
        )
        else "partial",
    }
