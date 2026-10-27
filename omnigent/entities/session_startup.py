"""Control-plane identities never enter model-visible input text."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

StartupState = Literal[
    "queued",
    "starting",
    "verifying",
    "uncertain",
    "blocked",
    "cancelling",
    "ready_empty",
    "waiting_input",
    "working",
    "opened",
    "cancelled",
    "stopped",
    "command_completed",
]


class StartupEvidence(BaseModel):
    runtime_ready: bool
    tools_ready: bool
    input_accepted: bool
    work_started: bool


class SessionStartupStatus(BaseModel):
    """Permission-filtered presentation carries no provider identity or input payload."""

    id: str
    attempt_id: str | None
    input_id: str | None
    state: StartupState
    reason: str | None
    elapsed_seconds: int = Field(ge=0)
    admitted_elapsed_seconds: int | None = Field(default=None, ge=0)
    input_elapsed_seconds: int | None = Field(default=None, ge=0)
    unconfirmed_startups: int = Field(default=0, ge=0)
    queue_position_estimate: int | None = Field(default=None, ge=1)
    can_cancel: bool = False
    evidence: StartupEvidence


class StartupReceipt(BaseModel):
    """The authenticated adapter reports a correlated observation, not a UI badge."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    startup_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    attempt_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    input_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    runtime_id: str = Field(min_length=1, max_length=128)
    phase: Literal[
        "opening_settled",
        "runtime_ready",
        "tools_ready",
        "input_accepted",
        "input_dispatched",
        "work_started",
        "blocked",
        "uncertain",
        "stopped",
    ]
    source: str = Field(min_length=1, max_length=64)
    vendor_session_id: str | None = Field(default=None, max_length=256)
    vendor_turn_id: str | None = Field(default=None, max_length=256)
    reason: str | None = Field(default=None, max_length=128)
