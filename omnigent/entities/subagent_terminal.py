"""Identity carried from an admitted child turn to terminal delivery."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

TERMINAL_IDENTITY_LABEL_PREFIX = "omnigent.subagent.identity."
RESULT_IDENTITY_LABEL_PREFIX = "omnigent.subagent.result_identity."


def _identifier(value: object) -> str | None:
    return (
        value
        if (
            isinstance(value, str)
            and 0 < len(value) <= 256
            and value not in {"codex_native", "codex_"}
        )
        else None
    )


class TurnIdentity(BaseModel):
    """An admitted turn; generation is supplied only by its server admission."""

    model_config = ConfigDict(frozen=True)

    turn_id: str | None = None
    input_id: str | None = None
    input_generation: int | None = Field(default=None, strict=True, ge=0)

    @property
    def identity_diagnostic(self) -> str | None:
        missing = [field for field in ("turn_id", "input_id") if getattr(self, field) is None]
        return "terminal_identity_unavailable: " + ", ".join(missing) if missing else None

    def identity_labels(self, prefix: str = TERMINAL_IDENTITY_LABEL_PREFIX) -> dict[str, str]:
        return {
            prefix + field: str(value) if value is not None else ""
            for field in ("turn_id", "input_id", "input_generation")
            for value in (getattr(self, field),)
        } | {prefix + "event_kind": "turn_terminal"}

    @classmethod
    def from_data(cls, data: Mapping[str, object] | None) -> TurnIdentity:
        values = data or {}
        generation = values.get("input_generation")
        return cls(
            turn_id=_identifier(values.get("turn_id")) or _identifier(values.get("response_id")),
            input_id=_identifier(values.get("input_id"))
            or _identifier(values.get("input_stable_id"))
            or _identifier(values.get("persisted_item_id"))
            or _identifier(values.get("pending_id")),
            input_generation=generation
            if isinstance(generation, int) and not isinstance(generation, bool) and generation >= 0
            else None,
        )


class TerminalEnvelope(BaseModel):
    """A missing identity is explicit; terminal delivery never allocates one."""

    model_config = ConfigDict(frozen=True)

    child_id: str
    parent_id: str | None = None
    event_kind: Literal["turn_terminal", "dispatch_failed"] = "turn_terminal"
    turn_id: str | None = None
    input_id: str | None = None
    input_generation: int | None = Field(default=None, strict=True, ge=0)
    status: Literal["completed", "failed", "cancelled", "stopped", "killed"]
    error: dict[str, Any] | None = None
    recovered: bool = False

    @property
    def identity_key(self) -> tuple[str, str, str] | None:
        return (self.event_kind, self.turn_id, self.status) if self.turn_id else None

    @property
    def identity_diagnostic(self) -> str | None:
        if self.event_kind == "dispatch_failed":
            return None
        return TurnIdentity.from_data(self.model_dump()).identity_diagnostic

    def identity_labels(self, prefix: str = TERMINAL_IDENTITY_LABEL_PREFIX) -> dict[str, str]:
        return TurnIdentity.from_data(self.model_dump()).identity_labels(prefix) | {
            prefix + "event_kind": self.event_kind
        }

    @classmethod
    def from_data(
        cls,
        child_id: str,
        *,
        parent_id: str | None,
        status: str,
        data: Mapping[str, object] | None = None,
        error: Mapping[str, object] | None = None,
        recovered: bool = False,
    ) -> TerminalEnvelope:
        values = data or {}
        return cls.model_validate(
            {
                **TurnIdentity.from_data(values).model_dump(),
                "child_id": child_id,
                "parent_id": parent_id,
                "status": status,
                "event_kind": "dispatch_failed"
                if values.get("event_kind") == "dispatch_failed"
                else "turn_terminal",
                "error": dict(error) if error is not None else None,
                "recovered": recovered,
            }
        )

    @classmethod
    def from_labels(
        cls,
        child_id: str,
        *,
        parent_id: str,
        status: str,
        labels: Mapping[str, object],
        error: Mapping[str, object] | None = None,
        prefix: str = TERMINAL_IDENTITY_LABEL_PREFIX,
    ) -> TerminalEnvelope:
        data = {
            field: labels.get(prefix + field)
            for field in ("turn_id", "input_id", "input_generation", "event_kind")
        }
        generation = data["input_generation"]
        if isinstance(generation, str) and generation.isdecimal():
            data["input_generation"] = int(generation)
        return cls.from_data(
            child_id, parent_id=parent_id, status=status, data=data, error=error, recovered=True
        )
