"""Startup permission travels on control channels, never inside model input."""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnigent.entities.session_startup import StartupReceipt


class StartupAttemptRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    startup_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    attempt_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    input_id: str | None = Field(pattern=r"^[0-9a-f]{32}$")
    runtime_id: str = Field(min_length=1, max_length=128)
    host_generation: str = Field(min_length=1, max_length=128)


class StartupObservation(BaseModel):
    """Only an authenticated adapter can turn its vendor observation into startup proof."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attempt: StartupAttemptRef
    phase: Literal[
        "runtime_ready", "tools_ready", "input_accepted", "work_started", "blocked", "uncertain"
    ]
    source: str = Field(min_length=1, max_length=64)
    vendor_session_id: str | None = Field(default=None, max_length=256)
    vendor_turn_id: str | None = Field(default=None, max_length=256)
    reason: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def _require_vendor_identity(self) -> Self:
        if self.phase in ("input_accepted", "work_started") and (
            self.source == "runner"
            or not self.vendor_session_id
            or not self.vendor_turn_id
            or self.attempt.input_id is None
        ):
            raise ValueError("Input proof requires its adapter, input and vendor turn identity")
        return self

    def receipt(self) -> StartupReceipt:
        return StartupReceipt(
            **self.attempt.model_dump(exclude={"host_generation"}),
            **self.model_dump(exclude={"attempt"}),
        )
