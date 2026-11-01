"""Durable identities and receipts for failed child turns."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

from omnigent.entities.conversation import AutoRetryMetadata
from omnigent.entities.subagent_terminal import TerminalEnvelope


def retry_identity(namespace: str, *parts: str) -> str:
    """Use separate, unambiguous namespaces for chain, input and final identities."""
    encoded = json.dumps([namespace, *parts], separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode()).hexdigest()[:32]


class InboxOwnerFence(BaseModel):
    model_config = ConfigDict(frozen=True)

    parent_id: str
    runner_id: str
    epoch: int = Field(strict=True, ge=1)


class RetryControl(BaseModel):
    model_config = ConfigDict(frozen=True)

    child_id: str
    generation: int = Field(strict=True, ge=0)
    turn_id: str | None
    input_id: str | None


class RetryChain(BaseModel):
    model_config = ConfigDict(frozen=True)

    chain_id: str
    child_id: str
    parent_id: str
    owner: InboxOwnerFence
    root_turn_id: str
    expected_turn_id: str
    expected_input_generation: int
    version: int
    limit: int
    accepted_attempts: int
    phase: Literal[
        "waiting", "submitting", "running", "resolving", "terminal", "finalized", "superseded"
    ]
    latest_error: dict[str, Any] | None
    latest_terminal: TerminalEnvelope
    observed_failure_at: float
    due_at: float | None
    pending_input_id: str | None
    retry_closed: bool


class RetryMutation(BaseModel):
    model_config = ConfigDict(frozen=True)

    chain: RetryChain
    duplicate: bool


class RetryInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    input_id: str
    chain_id: str | None
    target_id: str
    kind: Literal["human", "control", "retry", "wake"]
    attempt: int
    state: Literal["prepared", "dispatching", "reserved", "accepted", "closed", "rejected"]
    payload: dict[str, Any]
    version: int
    expected_input_generation: int | None = Field(default=None, strict=True, ge=0)
    turn_id: str | None
    input_generation: int | None
    reason: dict[str, Any] | None


class RetryInputClaim(BaseModel):
    model_config = ConfigDict(frozen=True)

    receipt: RetryInput
    claimed: bool


class RetryExecutionGuard(BaseModel):
    """Server-issued expectations carried outside the visible message payload."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    chain_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    input_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    owner: InboxOwnerFence
    expected_turn_id: str = Field(min_length=1, max_length=256)
    expected_input_generation: int = Field(strict=True, ge=0)
    attempt: int = Field(strict=True, ge=1)
    of: int = Field(strict=True, ge=1)

    @property
    def metadata(self) -> AutoRetryMetadata:
        return AutoRetryMetadata(
            chain_id=self.chain_id, input_id=self.input_id, attempt=self.attempt, of=self.of
        )


class InputExecutionClaim(BaseModel):
    """The receiver's final consumption boundary, authenticated by its runner."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_id: str = Field(min_length=1, max_length=256)
    turn_id: str | None = Field(default=None, min_length=1, max_length=256)
    retry: RetryExecutionGuard | None = None
    native_generation: int | None = Field(default=None, strict=True, ge=1)
    admission_deadline: float | None = Field(default=None, allow_inf_nan=False)
    boundary: Literal["receiver", "native_reserve", "native_accept", "native_refuse"] = "receiver"
    native_turn_id: str | None = Field(default=None, min_length=1, max_length=256)
    native_reason: str | None = Field(default=None, min_length=1, max_length=64)
    native_detail: str | None = Field(default=None, max_length=4096)


class InputExecutionReceipt(BaseModel):
    model_config = ConfigDict(frozen=True)

    state: Literal["reserved", "accepted", "refused", "pending"] = "refused"
    reason: str
    receipt: RetryInput | None = None

    @computed_field
    @property
    def accepted(self) -> bool:
        return self.state == "accepted"


class ControlInputReference(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str = Field(min_length=1)
    input_id: str = Field(pattern=r"^[0-9a-f]{32}$")


class ControlInputResolution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: Literal["accepted", "refused", "retired"]
    expected_version: int = Field(strict=True, ge=0)


class RetryOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["completed", "failed", "cancelled"]
    reason: str = Field(min_length=1, max_length=64)
    output: str
    error: dict[str, Any] | None = None


class RetryFinal(BaseModel):
    model_config = ConfigDict(frozen=True)

    chain_id: str
    child_id: str
    parent_id: str
    result_id: str
    notice_id: str
    outcome: RetryOutcome
    attempts: int
    limit: int
    inbox_parent_id: str | None
    wake_receipt: str | None
    drain_receipt: str | None
