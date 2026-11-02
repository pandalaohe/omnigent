"""Host-terminal wire values shared by the server and terminal-only runner."""

from pydantic import BaseModel, ConfigDict, Field


class HostTerminalCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    elevated: bool = False


class RunnerHostTerminalCreate(HostTerminalCreate):
    session_key: str = Field(pattern=r"^[a-f0-9]{32}$")


class HostTerminalResource(BaseModel):
    terminal_id: str
    elevated: bool
    created_at: int
    attach_path: str


class HostTerminalList(BaseModel):
    terminals: list[HostTerminalResource]
