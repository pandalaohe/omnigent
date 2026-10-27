"""Observations from ACP process owners; independent SDK-key ownership is unsupported."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal
from uuid import uuid4

from omnigent.startup_protocol import (
    ContextEvidenceV2,
    ContextKeyV2,
    ContextOperationV2,
    ContextRecordV2,
    SourceEvidenceV2,
    UnresolvedContextV2,
    VendorContextIdV2,
    executor_context_scope_v2,
    harness_context_authority_v2,
    unknown_context_evidence_v2,
)


@dataclass(frozen=True)
class AcpContextAttempt:
    key: ContextKeyV2 | None
    generation: int
    process: asyncio.subprocess.Process | None


class AcpProcessContextSource:
    def __init__(
        self, namespace: str, process: Callable[[], asyncio.subprocess.Process | None]
    ) -> None:
        self.incarnation = uuid4().hex
        self.namespace = namespace
        self.process = process
        self.generation = 0
        self.spawning = False
        self.closing = False
        self._active: ContextKeyV2 | None = None
        self._records: dict[ContextKeyV2, ContextRecordV2] = {}
        self._overflow = False
        self._per_key_close = False

    def _mark(
        self, keys: tuple[ContextKeyV2, ...], lifecycle: Literal["closing", "unknown"]
    ) -> None:
        for key in keys:
            row = self._records[key]
            self._records[key] = row.model_copy(
                update={"lifecycle": lifecycle, "revision": row.revision + 1}
            )

    def begin_session(self) -> AcpContextAttempt:
        if self._active is None:
            if len(self._records) >= 1000:
                self._overflow = True
            else:
                key = ContextKeyV2(
                    source_owner_incarnation=self.incarnation, context_incarnation=uuid4().hex
                )
                self._records[key] = ContextRecordV2(
                    key=key,
                    vendor_id=None,
                    operation=ContextOperationV2(
                        operation_id=uuid4().hex, action="create", parent=None
                    ),
                    execution_keys=(),
                    provenance="primary",
                    lifecycle="starting",
                    aliases=(),
                    revision=0,
                    stop_proof=None,
                    continuity_proof=None,
                )
                self._active = key
        return AcpContextAttempt(self._active, self.generation, self.process())

    def begin_process(self) -> AcpContextAttempt:
        self._mark(tuple(self._records), "unknown")
        self._active = None
        self.generation += 1
        self.spawning, self.closing = True, False
        return self.begin_session()

    def process_started(self) -> None:
        self.spawning = False

    def failed(self, attempt: AcpContextAttempt) -> None:
        if attempt.key is not None and self._records[attempt.key].lifecycle != "closing":
            self._mark((attempt.key,), "unknown")
            if attempt.key == self._active:
                self._active = None

    def accepts_reply(self, attempt: AcpContextAttempt) -> bool:
        return (
            attempt.generation == self.generation
            and attempt.key == self._active
            and not self.closing
            and self.process() is attempt.process
            and (attempt.process is None or attempt.process.returncode is None)
        )

    def bind(self, attempt: AcpContextAttempt, vendor_id: object) -> None:
        if attempt.key is None:
            return
        current = (
            self.accepts_reply(attempt)
            and attempt.process is not None
            and self._records[attempt.key].lifecycle == "starting"
        )
        if not current or not isinstance(vendor_id, str) or not 0 < len(vendor_id) <= 256:
            self.failed(attempt)
            return
        row = self._records[attempt.key]
        self._records[attempt.key] = row.model_copy(
            update={
                "vendor_id": VendorContextIdV2(namespace=self.namespace, value=vendor_id),
                "lifecycle": "live",
                "revision": row.revision + 1,
            }
        )

    def reset_session(self) -> None:
        if self.spawning or self.closing:
            return
        if self._active is not None:
            self._mark((self._active,), "unknown")
        self._active = None

    def transport_ended(self, process: asyncio.subprocess.Process | None, generation: int) -> None:
        if process is self.process() and generation == self.generation and not self.closing:
            self._mark(tuple(self._records), "unknown")

    def per_key_close(self) -> None:
        self._per_key_close = True
        self._mark(tuple(self._records), "unknown")

    def begin_close(self) -> tuple[ContextKeyV2, ...]:
        self.closing = True
        self.generation += 1
        keys = tuple(self._records)
        self._mark(keys, "closing")
        self._active = None
        return keys

    def end_close(self, keys: tuple[ContextKeyV2, ...]) -> None:
        self._mark(keys, "unknown")

    async def prepare(
        self, session_id: str, read_id: str, *, timeout_s: float = 5.0
    ) -> Callable[[], ContextEvidenceV2]:
        if not 0 < timeout_s <= 5.0:
            return lambda: unknown_context_evidence_v2(session_id, read_id, "invalid_read_budget")

        def reconcile() -> ContextEvidenceV2:
            if self._overflow:
                return unknown_context_evidence_v2(session_id, read_id, "context_history_budget")
            records = []
            process = self.process()
            for row in self._records.values():
                if row.lifecycle == "live" and (process is None or process.returncode is not None):
                    row = row.model_copy(update={"lifecycle": "unknown"})
                records.append(row)
            reasons = {
                "independent_sdk_key_mapping_unsupported",
                "readonly_alias_provenance_unavailable",
                "acp_internal_contexts_unobserved",
            }
            if self._per_key_close:
                reasons.add("per_key_close_unsupported")
            if any(row.vendor_id is None for row in records):
                reasons.add("session_identity_unconfirmed")
            return ContextEvidenceV2(
                wire_version=2,
                read_id=read_id,
                authority=harness_context_authority_v2(session_id),
                expected_scopes=(executor_context_scope_v2(session_id),),
                sources=(
                    SourceEvidenceV2(
                        scope=executor_context_scope_v2(session_id),
                        producer_incarnation=self.incarnation,
                        coverage="partial",
                        contexts=tuple(records),
                        unresolved=tuple(
                            UnresolvedContextV2(candidate=None, reason=r) for r in sorted(reasons)
                        ),
                        membership_revision=None,
                        context_revision=None,
                        work_revision=None,
                        revision_authority="observation_only",
                        age_upper_bound_s=None,
                        work_bindings=None,
                    ),
                ),
            )

        return reconcile
