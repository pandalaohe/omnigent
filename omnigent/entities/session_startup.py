"""Control-plane identities never enter model-visible input text."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StartupReceipt(BaseModel):
    """The authenticated adapter reports a correlated observation, not a UI badge."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    startup_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    attempt_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    input_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    runtime_id: str = Field(min_length=1, max_length=128)
    phase: Literal[
        "runtime_ready",
        "tools_ready",
        "input_accepted",
        "work_started",
        "blocked",
        "uncertain",
        "stopped",
    ]
    source: str = Field(min_length=1, max_length=64)
    vendor_session_id: str | None = Field(default=None, max_length=256)
    vendor_turn_id: str | None = Field(default=None, max_length=256)
    reason: str | None = Field(default=None, max_length=128)
