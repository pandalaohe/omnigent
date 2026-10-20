"""An exact, durable host-process recovery target; no executable commands."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class HostRecoveryPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    recovery_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    host_id: str
    account_generation: str
    epoch: int = Field(ge=1)
    work_revision: int = Field(ge=0)
    host_generation: str
    process_identity: str
    supervisor: str
    members: tuple[tuple[str, str], ...]
    startup_attempts: tuple[tuple[str, str], ...]
    started_at: float = Field(allow_inf_nan=False)
    committed_at: float | None = Field(default=None, allow_inf_nan=False)
    phase: Literal[
        "quiescing", "restart_committed", "recovery_unconfirmed", "complete", "cancelled"
    ] = "quiescing"

    def same_operation(self, other: HostRecoveryPlan) -> bool:
        mutable = {"phase", "committed_at"}
        return self.model_dump(exclude=mutable) == other.model_dump(exclude=mutable)
