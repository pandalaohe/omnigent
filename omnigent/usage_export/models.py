"""Validated, content-free measurements accepted by the usage feed."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

Harness = Literal["codex-native", "codex-sdk", "claude-native", "claude-sdk", "pi-native", "pi"]
ResponseNamespace = Literal[
    "claude-code/message-id/v1",
    "claude-code/request-id/v1",
    "codex-responses/response-id/v1",
    "pi/response-id/v1",
]
TerminalEvidence = Literal["terminal_record", "stop_hook", "executor_end", "writer_exit"]


class AccountHint(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    provider: Literal["openai", "anthropic", "other"]
    billing_kind: Literal["subscription", "api", "gateway", "free"]
    billing_route: Literal["direct_provider", "gateway", "other"]
    route_ref: str = Field(min_length=1)
    opaque_account_ref: str = Field(min_length=1)
    ref_generation: int = Field(gt=0)
    basis: Literal["runtime_identity", "owner_binding"]
    binding_version: int = Field(gt=0)

    @field_validator("opaque_account_ref", "route_ref")
    @classmethod
    def bounded_ref(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 256:
            raise ValueError("account reference exceeds 256 UTF-8 bytes")
        return value


class SourceRef(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["request", "response"]
    namespace: ResponseNamespace
    id: str = Field(min_length=1)

    @field_validator("id")
    @classmethod
    def bounded_id(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 256:
            raise ValueError("source ref id exceeds 256 UTF-8 bytes")
        return value


class ThreadTerminal(BaseModel):
    """One physical thread's termination evidence, declared by the producer."""

    model_config = ConfigDict(extra="forbid", strict=True)

    physical_session_id: str = Field(min_length=1)
    physical_subagent_id: str | None = None
    evidence: TerminalEvidence
    source_ref: str | None = Field(default=None, max_length=256)

    @field_validator("physical_session_id", "physical_subagent_id", "source_ref")
    @classmethod
    def bounded_identifier(cls, value: str | None) -> str | None:
        if value is not None and (not value or len(value.encode("utf-8")) > 256):
            raise ValueError("identifier must be 1..256 UTF-8 bytes")
        return value

    @property
    def key(self) -> str:
        return thread_key(self.physical_session_id, self.physical_subagent_id)


def thread_key(physical_session_id: str, physical_subagent_id: str | None) -> str:
    """Stable key for one physical thread inside an owner's feed."""
    return json.dumps(
        [physical_session_id, physical_subagent_id], separators=(",", ":"), ensure_ascii=False
    )


class UsageObservation(BaseModel):
    """One producer unit's full current measurement; no server fields accepted."""

    model_config = ConfigDict(extra="forbid", strict=True)

    producer_unit_id: str = Field(min_length=1)
    meter_epoch: str = Field(min_length=1)
    host_id: str = Field(min_length=1)
    harness: Harness
    agent_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    measurement_kind: Literal["request", "increment", "legacy_snapshot"]
    logical_call_id: str | None = None
    occurred_at: str | None = None
    time_quality: Literal["reported", "unknown"] = "reported"
    model_id: str | None = None
    parent_session_id: str | None = None
    upstream_thread_id: str | None = None
    turn_id: str | None = None
    thread_kind: Literal["main", "subagent", "internal", "guardian", "unknown"]
    harness_family: Literal["claude", "codex", "pi"]
    identity_namespace: str = Field(min_length=1)
    physical_transcript_path: str | None = None
    physical_session_id: str | None = None
    transcript_session_id: str | None = None
    physical_subagent_id: str | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    cache_write_input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)
    cost_usd: str | None = None
    currency: Literal["USD"] = "USD"
    cost_estimated: bool | None = None
    cost_basis: Literal["metered", "estimated", "unknown"] = "unknown"
    usage_status: Literal["complete", "partial", "missing"] = "missing"
    source_refs: list[SourceRef] = Field(default_factory=list, max_length=8)
    call_identity_coverage: Literal["complete", "partial", "none"] = "none"
    # Association evidence: the admitted turn this unit belongs to, the
    # runner's route comparison, and per-thread termination evidence.
    admission_id: str | None = None
    turn_key: str | None = None
    route_check: Literal["match", "mismatch"] | None = None
    terminal: list[ThreadTerminal] = Field(default_factory=list, max_length=32)

    @field_validator(
        "producer_unit_id",
        "meter_epoch",
        "host_id",
        "agent_id",
        "session_id",
        "logical_call_id",
        "model_id",
        "parent_session_id",
        "upstream_thread_id",
        "turn_id",
        "identity_namespace",
        "physical_session_id",
        "transcript_session_id",
        "physical_subagent_id",
        "admission_id",
        "turn_key",
    )
    @classmethod
    def bounded_identifier(cls, value: str | None) -> str | None:
        if value is not None and (not value or len(value.encode("utf-8")) > 256):
            raise ValueError("identifier must be 1..256 UTF-8 bytes")
        return value

    @field_validator("physical_transcript_path")
    @classmethod
    def bounded_path(cls, value: str | None) -> str | None:
        if value is not None and len(value.encode("utf-8")) > 1024:
            raise ValueError("physical_transcript_path exceeds 1024 UTF-8 bytes")
        return value

    @field_validator("occurred_at")
    @classmethod
    def utc_time(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|\+00:00)", value):
            raise ValueError("timestamp must be UTC RFC3339")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("invalid UTC timestamp") from exc
        return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")

    @field_validator("cost_usd")
    @classmethod
    def canonical_cost(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if len(value.encode("utf-8")) > 64 or not re.fullmatch(r"(?:0|[1-9]\d*)(?:\.\d+)?", value):
            raise ValueError("cost_usd must be a nonnegative decimal string")
        try:
            amount = Decimal(value)
            if not amount.is_finite():
                raise ValueError("cost_usd must be finite")
        except InvalidOperation as exc:
            raise ValueError("invalid cost_usd") from exc
        whole, separator, fraction = value.partition(".")
        fraction = fraction.rstrip("0")
        result = whole + (separator + fraction if fraction else "")
        if len(result.encode("utf-8")) > 64:
            raise ValueError("cost_usd exceeds 64 UTF-8 bytes")
        return result

    @model_validator(mode="after")
    def consistent_identity(self) -> UsageObservation:
        if self.harness_family != self.harness.split("-", 1)[0]:
            raise ValueError("harness_family conflicts with harness")
        identity_namespaces = {
            "codex": "codex/rollout/v1",
            "claude": "claude-code/transcript/v1",
            "pi": "pi/session/v1" if self.harness == "pi-native" else "pi/rpc/v1",
        }
        if self.identity_namespace != identity_namespaces[self.harness_family]:
            raise ValueError("identity_namespace conflicts with harness family")
        expected = {
            "claude": ("claude-code/message-id/v1", "claude-code/request-id/v1"),
            "codex": ("codex-responses/response-id/v1",),
            "pi": ("pi/response-id/v1",),
        }[self.harness_family]
        if any(ref.namespace not in expected for ref in self.source_refs):
            raise ValueError("source ref namespace conflicts with harness family")
        responses = [ref for ref in self.source_refs if ref.kind == "response"]
        preferred = next(
            (ref for namespace in expected for ref in responses if ref.namespace == namespace),
            None,
        )
        if self.call_identity_coverage == "complete" and (
            self.logical_call_id is None or preferred is None
        ):
            raise ValueError("complete call identity requires a response ref and logical call id")
        if self.logical_call_id is not None and (
            preferred is None or preferred.id != self.logical_call_id
        ):
            raise ValueError("logical_call_id must match preferred response ref")
        if len({entry.key for entry in self.terminal}) != len(self.terminal):
            raise ValueError("duplicate terminal thread evidence")
        return self


class UsageEvent(UsageObservation):
    """Complete committed event, validated again at the export boundary.

    The admission-derived fields (``call_started_not_before``,
    ``call_start_basis``, ``account_hint``) and the server fields
    (``ended_at``, ``sequence``) are filled by the server only.
    """

    event_id: str = Field(min_length=1)
    original_event_id: str = Field(min_length=1)
    kind: Literal["usage", "correction"]
    correction_of: str | None
    revision: int = Field(gt=0)
    sequence: int = Field(ge=1)
    recorded_at: str
    request_count: int | None = Field(ge=0, le=1)
    call_started_not_before: str | None = None
    call_start_basis: Literal["turn_admission"] | None = None
    ended_at: str | None = None
    account_hint: AccountHint | None = None
    # Association evidence stays on the server's unit row; it is never part of
    # the committed event the consumer reads.
    admission_id: str | None = Field(default=None, exclude=True)
    turn_key: str | None = Field(default=None, exclude=True)
    route_check: Literal["match", "mismatch"] | None = Field(default=None, exclude=True)
    terminal: list[ThreadTerminal] = Field(default_factory=list, exclude=True)

    @field_validator("event_id", "original_event_id", "correction_of")
    @classmethod
    def event_identifier(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[0-9a-f]{32}", value):
            raise ValueError("invalid event identifier")
        return value

    @field_validator("recorded_at", "call_started_not_before", "ended_at")
    @classmethod
    def recorded_time(cls, value: str | None, info: ValidationInfo) -> str | None:
        normalized = UsageObservation.utc_time(value)
        if normalized is None and info.field_name == "recorded_at":
            raise ValueError("recorded_at is required")
        return normalized

    @model_validator(mode="after")
    def valid_revision(self) -> UsageEvent:
        if self.revision == 1:
            if (
                self.kind != "usage"
                or self.correction_of is not None
                or self.original_event_id != self.event_id
            ):
                raise ValueError("invalid original usage event")
        elif self.kind != "correction" or self.correction_of != self.original_event_id:
            raise ValueError("invalid correction lineage")
        # I1: the start basis is null exactly when the start bound is null.
        if (self.call_start_basis is None) != (self.call_started_not_before is None):
            raise ValueError("call start basis and bound must both be set or both null")
        # I2: a hint requires a start basis.
        if self.account_hint is not None and self.call_start_basis is None:
            raise ValueError("account hint requires a call start basis")
        if (
            self.call_started_not_before is not None
            and self.ended_at is not None
            and datetime.fromisoformat(self.call_started_not_before.replace("Z", "+00:00"))
            > datetime.fromisoformat(self.ended_at.replace("Z", "+00:00"))
        ):
            raise ValueError("call start bound after the end bound")
        return self


class HostProgress(BaseModel):
    """Progress derived from the delivered event prefix for one host."""

    model_config = ConfigDict(extra="forbid", strict=True)

    host_id: str = Field(min_length=1)
    last_sealed_turn_at: str | None
    state: Literal["unknown"]
    observed_at: str

    @field_validator("host_id")
    @classmethod
    def bounded_host_id(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 256:
            raise ValueError("host_id exceeds 256 UTF-8 bytes")
        return value

    @field_validator("last_sealed_turn_at", "observed_at")
    @classmethod
    def progress_time(cls, value: str | None) -> str | None:
        return UsageObservation.utc_time(value)
