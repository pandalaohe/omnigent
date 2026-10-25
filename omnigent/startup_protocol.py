"""Startup permission travels on control channels, never inside model input."""

import json
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from omnigent.entities.session_startup import StartupReceipt

SDK_STARTUP_SOURCES = frozenset({"codex", "claude-sdk"})
CONTEXT_INVENTORY_V2 = "resident_context_inventory_v2"
CONTEXT_INVENTORY_CONTRACT_HEADER = "Omnigent-Context-Inventory-Contract"
CONTEXT_INVENTORY_MAX_BYTES = 1024 * 1024

OpaqueIdV2 = Annotated[str, Field(min_length=1, max_length=256)]
RevisionV2 = Annotated[int, Field(strict=True, ge=0)]


class _ContextValueV2(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ScopeKeyV2(_ContextValueV2):
    namespace: OpaqueIdV2
    owner_id: OpaqueIdV2
    population: OpaqueIdV2


class ContextAuthorityV2(_ContextValueV2):
    session_id: OpaqueIdV2
    account_id: OpaqueIdV2 | None
    host_id: OpaqueIdV2 | None
    host_generation: OpaqueIdV2 | None
    runner_id: OpaqueIdV2 | None
    runner_generation: OpaqueIdV2 | None
    connection_generation: OpaqueIdV2 | None


class ContextKeyV2(_ContextValueV2):
    source_owner_incarnation: OpaqueIdV2
    context_incarnation: OpaqueIdV2


class VendorContextIdV2(_ContextValueV2):
    namespace: OpaqueIdV2
    value: OpaqueIdV2


class ContextOperationV2(_ContextValueV2):
    operation_id: OpaqueIdV2
    action: Literal["create", "resume", "fork", "maintenance", "unknown"]
    parent: ContextKeyV2 | None = None


class ContextLifecycleProofV2(_ContextValueV2):
    context: ContextKeyV2
    receipt_id: OpaqueIdV2
    authority: ContextAuthorityV2
    execution_keys: tuple[OpaqueIdV2, ...] = Field(max_length=1000)


class AliasBindingV2(_ContextValueV2):
    view_key: OpaqueIdV2
    generation: OpaqueIdV2
    target: ContextKeyV2
    read_only: Literal[True]
    operation_id: OpaqueIdV2
    receipt_id: OpaqueIdV2

    @field_validator("read_only", mode="before")
    @classmethod
    def _exact_read_only(cls, value: object) -> object:
        if value is not True:
            raise ValueError("alias must be a proven read-only view")
        return value


class ContextRecordV2(_ContextValueV2):
    key: ContextKeyV2
    vendor_id: VendorContextIdV2 | None
    operation: ContextOperationV2
    execution_keys: tuple[OpaqueIdV2, ...] = Field(max_length=1000)
    provenance: Literal[
        "primary", "managed_side_chat", "maintenance", "harness_internal", "unknown"
    ]
    lifecycle: Literal["reserved", "starting", "live", "closing", "stopped", "unknown"]
    aliases: tuple[AliasBindingV2, ...] = Field(max_length=1000)
    revision: RevisionV2
    stop_proof: ContextLifecycleProofV2 | None
    continuity_proof: ContextLifecycleProofV2 | None

    @model_validator(mode="after")
    def _consistent_lifetime(self) -> Self:
        if len(set(self.execution_keys)) != len(self.execution_keys):
            raise ValueError("duplicate execution key")
        if len({alias.view_key for alias in self.aliases}) != len(self.aliases) or any(
            alias.target != self.key or alias.view_key in self.execution_keys
            for alias in self.aliases
        ):
            raise ValueError("conflicting alias")
        for proof in (self.stop_proof, self.continuity_proof):
            if proof is not None and (
                proof.context != self.key
                or set(proof.execution_keys) != set(self.execution_keys)
                or len(set(proof.execution_keys)) != len(proof.execution_keys)
            ):
                raise ValueError("proof belongs to another execution lifetime")
        if (self.lifecycle == "stopped") != (self.stop_proof is not None):
            raise ValueError("stopped requires its owned lifecycle proof")
        if self.provenance == "managed_side_chat" and (
            self.operation.action != "fork" or self.operation.parent is None
        ):
            raise ValueError("managed side chat requires its fork operation")
        return self


class UnresolvedContextV2(_ContextValueV2):
    candidate: OpaqueIdV2 | None
    reason: OpaqueIdV2


class ContextWorkBindingV2(_ContextValueV2):
    context: ContextKeyV2
    runtime_id: OpaqueIdV2
    generation: OpaqueIdV2


class SourceEvidenceV2(_ContextValueV2):
    scope: ScopeKeyV2
    producer_incarnation: OpaqueIdV2 | None
    coverage: Literal["partial", "complete", "unsupported"]
    contexts: tuple[ContextRecordV2, ...] = Field(max_length=1000)
    unresolved: tuple[UnresolvedContextV2, ...] = Field(max_length=1000)
    membership_revision: RevisionV2 | None
    context_revision: RevisionV2 | None
    work_revision: RevisionV2 | None
    revision_authority: Literal[
        "observation_only", "authoritative_membership", "authoritative_work"
    ]
    age_upper_bound_s: float | None = Field(ge=0, allow_inf_nan=False)
    work_bindings: tuple[ContextWorkBindingV2, ...] | None = Field(max_length=1000)

    @model_validator(mode="after")
    def _consistent_source(self) -> Self:
        keys = {record.key for record in self.contexts}
        if len(keys) != len(self.contexts):
            raise ValueError("duplicate context key")
        if any(
            record.key.source_owner_incarnation != self.producer_incarnation
            for record in self.contexts
        ):
            raise ValueError("context has no owning producer association")
        execution_keys = {key for record in self.contexts for key in record.execution_keys}
        view_keys: set[str] = set()
        for record in self.contexts:
            for alias in record.aliases:
                if alias.view_key in view_keys or alias.view_key in execution_keys:
                    raise ValueError("conflicting source alias")
                view_keys.add(alias.view_key)
        if self.coverage != "complete" and not self.unresolved:
            raise ValueError("incomplete scope requires an explicit reason")
        if self.coverage == "complete" and (
            self.producer_incarnation is None
            or self.membership_revision is None
            or self.context_revision is None
            or self.revision_authority == "observation_only"
            or self.unresolved
            or self.age_upper_bound_s is None
        ):
            raise ValueError("complete scope requires authoritative exhaustive membership")
        if self.work_bindings is not None and (
            any(binding.context not in keys for binding in self.work_bindings)
            or len(set(self.work_bindings)) != len(self.work_bindings)
        ):
            raise ValueError("work association is outside the observed contexts")
        return self


class ContextEvidenceV2(_ContextValueV2):
    """Immutable observation; decoding grants no admission or recovery authority."""

    wire_version: Literal[2]
    read_id: str = Field(min_length=1, max_length=128)
    authority: ContextAuthorityV2
    expected_scopes: tuple[ScopeKeyV2, ...] = Field(min_length=1, max_length=64)
    sources: tuple[SourceEvidenceV2, ...] = Field(max_length=64)

    @field_validator("wire_version", mode="before")
    @classmethod
    def _exact_version(cls, value: object) -> object:
        if type(value) is not int or value != 2:
            raise ValueError("unsupported context evidence version")
        return value

    @model_validator(mode="after")
    def _unique_scopes(self) -> Self:
        if len(set(self.expected_scopes)) != len(self.expected_scopes) or len(
            {source.scope for source in self.sources}
        ) != len(self.sources):
            raise ValueError("duplicate source scope")
        for source in self.sources:
            if source.coverage == "complete" and any(
                value is None for value in self.authority.model_dump().values()
            ):
                raise ValueError("complete scope has unbound authority")
            for record in source.contexts:
                if any(
                    proof is not None and proof.authority != self.authority
                    for proof in (record.stop_proof, record.continuity_proof)
                ) or any(
                    alias.generation != self.authority.connection_generation
                    for alias in record.aliases
                ):
                    raise ValueError("lifecycle or alias proof belongs to another authority")
        return self


def executor_context_scope_v2(session_id: str) -> ScopeKeyV2:
    return ScopeKeyV2(namespace="harness", owner_id=session_id, population="executor_contexts")


def native_context_scope_v2(session_id: str) -> ScopeKeyV2:
    return ScopeKeyV2(namespace="native", owner_id=session_id, population="backend_contexts")


def terminal_context_scope_v2(session_id: str) -> ScopeKeyV2:
    return ScopeKeyV2(namespace="terminal", owner_id=session_id, population="terminal_roles")


def host_context_scope_v2(session_id: str) -> ScopeKeyV2:
    return ScopeKeyV2(namespace="host", owner_id=session_id, population="runner_membership")


def session_context_scopes_v2(session_id: str) -> tuple[ScopeKeyV2, ScopeKeyV2, ScopeKeyV2]:
    """The three owner scopes one runner aggregates for a conversation."""
    return (
        executor_context_scope_v2(session_id),
        native_context_scope_v2(session_id),
        terminal_context_scope_v2(session_id),
    )


def harness_context_authority_v2(session_id: str) -> ContextAuthorityV2:
    """This channel knows its conversation, not the outer host/account authority."""
    return ContextAuthorityV2(
        session_id=session_id,
        account_id=None,
        host_id=None,
        host_generation=None,
        runner_id=None,
        runner_generation=None,
        connection_generation=None,
    )


def unknown_context_evidence_v2(
    session_id: str, read_id: str, reason: str, *, unsupported: bool = False
) -> ContextEvidenceV2:
    scope = executor_context_scope_v2(session_id)
    return ContextEvidenceV2(
        wire_version=2,
        read_id=read_id,
        authority=harness_context_authority_v2(session_id),
        expected_scopes=(scope,),
        sources=(
            SourceEvidenceV2(
                scope=scope,
                producer_incarnation=None,
                coverage="unsupported" if unsupported else "partial",
                contexts=(),
                unresolved=(UnresolvedContextV2(candidate=None, reason=reason),),
                membership_revision=None,
                context_revision=None,
                work_revision=None,
                revision_authority="observation_only",
                age_upper_bound_s=None,
                work_bindings=None,
            ),
        ),
    )


def decode_context_evidence_v2(
    payload: bytes,
    *,
    read_id: str,
    authority: ContextAuthorityV2,
    expected_scopes: tuple[ScopeKeyV2, ...],
) -> ContextEvidenceV2:
    """Strict bounded wire decode, correlated to this read and consumer-owned manifest."""
    if len(payload) > CONTEXT_INVENTORY_MAX_BYTES:
        raise ValueError("context evidence exceeds byte budget")

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError("duplicate wire field")
        return result

    try:
        json.loads(payload, object_pairs_hook=unique_object)
    except RecursionError as error:
        raise ValueError("context evidence exceeds nesting budget") from error
    evidence = ContextEvidenceV2.model_validate_json(payload, strict=True)
    if (
        evidence.read_id != read_id
        or evidence.authority != authority
        or set(evidence.expected_scopes) != set(expected_scopes)
    ):
        raise ValueError("context evidence read, authority or manifest mismatch")
    return evidence


class StartupAttemptRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    startup_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    attempt_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    input_id: str | None = Field(pattern=r"^[0-9a-f]{32}$")
    runtime_id: str = Field(min_length=1, max_length=128)
    host_generation: str = Field(min_length=1, max_length=128)


class StartupBoundaryClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    attempt: StartupAttemptRef
    boundary: Literal[
        "harness_prepare", "native_prepare", "executor_construct", "executor_prepare"
    ]
    claim_id: str = Field(min_length=1, max_length=128)


class StartupObservation(BaseModel):
    """Only an authenticated adapter can turn its vendor observation into startup proof."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attempt: StartupAttemptRef
    phase: Literal[
        "runtime_ready",
        "tools_ready",
        "input_dispatched",
        "input_accepted",
        "work_started",
        "blocked",
        "uncertain",
    ]
    source: str = Field(min_length=1, max_length=64)
    vendor_session_id: str | None = Field(default=None, max_length=256)
    vendor_turn_id: str | None = Field(default=None, max_length=256)
    reason: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def _require_vendor_identity(self) -> Self:
        if self.phase in ("input_dispatched", "input_accepted", "work_started") and (
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
