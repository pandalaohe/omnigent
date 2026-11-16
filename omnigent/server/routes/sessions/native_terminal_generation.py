"""Persisted ordering for native terminal status, independent of publisher lifetime."""

from collections.abc import Mapping

from pydantic import BaseModel, Field, ValidationError

from omnigent.entities import Conversation

NATIVE_TERMINAL_GENERATION_LABEL = "omnigent.native_terminal_generation"


class NativeTerminalGeneration(BaseModel):
    generation: int = Field(strict=True, ge=0)
    turn_id: str = Field(min_length=1)
    runner_id: str | None
    status: str
    turn_completed: bool
    turn_outcome: str | None


def _unversioned_thread_failure(data: Mapping[str, object]) -> bool:
    return (
        data.get("status") == "failed"
        and "native_turn_generation" not in data
        and not data.get("turn_id")
        and not data.get("response_id")
    )


def native_terminal_generation(
    conv: Conversation, data: Mapping[str, object]
) -> NativeTerminalGeneration | None:
    """Parse the bridge's session-monotonic revision and terminal identity."""
    try:
        if _unversioned_thread_failure(data):
            raw = conv.labels.get(NATIVE_TERMINAL_GENERATION_LABEL)
            if raw is None:
                return None
            previous = NativeTerminalGeneration.model_validate_json(raw)
            if previous.runner_id != conv.runner_id:
                return None
            return previous.model_copy(
                update={"status": "failed", "turn_completed": False, "turn_outcome": None}
            )
        return NativeTerminalGeneration.model_validate(
            {
                "generation": data.get("native_turn_generation"),
                "turn_id": data.get("turn_id") or data.get("response_id"),
                "runner_id": conv.runner_id,
                "status": data.get("status"),
                "turn_completed": data.get("turn_completed") is True,
                "turn_outcome": data.get("turn_outcome"),
            }
        )
    except ValidationError:
        return None


def native_terminal_generation_is_current(conv: Conversation, data: Mapping[str, object]) -> bool:
    """Allow a newer terminal or an identical retry; legacy events work until fenced.

    Call under the session's status lock before any state or failure-label write.
    The persisted marker also permits read-only freshness checks by other consumers.
    """
    if (
        data.get("event_kind") == "dispatch_failed" and "native_turn_generation" not in data
    ) or _unversioned_thread_failure(data):
        return True
    incoming = native_terminal_generation(conv, data)
    raw = conv.labels.get(NATIVE_TERMINAL_GENERATION_LABEL)
    if raw is None:
        return incoming is not None or "native_turn_generation" not in data
    try:
        previous = NativeTerminalGeneration.model_validate_json(raw)
    except ValidationError:
        return False
    if previous.runner_id != conv.runner_id:
        return incoming is not None or "native_turn_generation" not in data
    return incoming is not None and (
        incoming.generation > previous.generation or incoming == previous
    )
