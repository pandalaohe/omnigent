"""Durable identities and receipts for failed child turns."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

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
    state: Literal["prepared", "dispatching", "accepted", "closed", "rejected"]
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
