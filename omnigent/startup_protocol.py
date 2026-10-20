"""Startup permission travels on control channels, never inside model input."""

from pydantic import BaseModel, ConfigDict, Field


class StartupAttemptRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    startup_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    attempt_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    input_id: str | None = Field(pattern=r"^[0-9a-f]{32}$")
    runtime_id: str = Field(min_length=1, max_length=128)
    host_generation: str = Field(min_length=1, max_length=128)
