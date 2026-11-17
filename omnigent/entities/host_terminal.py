"""Host-terminal wire values shared by the server and terminal-only runner."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class HostTerminalCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    elevated: bool = False
    agent_tier: Literal["off", "read", "read_write"] | None = None


class RunnerHostTerminalCreate(HostTerminalCreate):
    session_key: str = Field(pattern=r"^[a-f0-9]{32}$")
    linked_session_id: str | None = Field(default=None, min_length=1)


class HostTerminalAgentRead(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(min_length=1)
    scrollback: int = Field(default=0, ge=0, strict=True)


class HostTerminalAgentSend(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(min_length=1)
    text: str | None = None
    keys: str = "Enter"


class HostTerminalAgentClose(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(min_length=1)


class HostTerminalTierUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instance_id: str = Field(min_length=1)
    agent_tier: Literal["off", "read", "read_write"]


class HostTerminalResource(BaseModel):
    terminal_id: str
    elevated: bool
    created_at: int
    attach_path: str
    linked_session_id: str | None = None
    agent_tier: Literal["off", "read", "read_write"] = "off"
    instance_id: str = ""


class HostTerminalList(BaseModel):
    terminals: list[HostTerminalResource]
