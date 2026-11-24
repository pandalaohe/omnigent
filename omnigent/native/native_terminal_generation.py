"""Shared representation of the server's last accepted native terminal edge."""

from collections.abc import Mapping

from pydantic import BaseModel, Field, ValidationError

NATIVE_TERMINAL_GENERATION_LABEL = "omnigent.native_terminal_generation"


class NativeTerminalGeneration(BaseModel):
    generation: int = Field(strict=True, ge=0)
    turn_id: str = Field(min_length=1)
    runner_id: str | None
    status: str
    turn_completed: bool
    turn_outcome: str | None


def persisted_terminal_revision(labels: Mapping[str, object], runner_id: str | None) -> int:
    """Recover the last accepted revision from this session's runner binding."""
    raw = labels.get(NATIVE_TERMINAL_GENERATION_LABEL)
    if not isinstance(raw, str):
        return 0
    try:
        previous = NativeTerminalGeneration.model_validate_json(raw)
    except ValidationError:
        return 0
    return previous.generation if previous.runner_id == runner_id else 0
