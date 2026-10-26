"""Bounded observations shared by the persistent SDK session owners."""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal
from uuid import uuid4

from omnigent.startup_protocol import (
    ContextEvidenceV2,
    ContextKeyV2,
    ContextOperationV2,
    ContextRecordV2,
    SourceEvidenceV2,
    UnresolvedContextV2,
    executor_context_scope_v2,
    harness_context_authority_v2,
    unknown_context_evidence_v2,
)


class SdkContextSource:
    def __init__(self, active: Callable[[], set[ContextKeyV2]]) -> None:
        self.incarnation = uuid4().hex
        self._active = active
        self._records: dict[ContextKeyV2, ContextRecordV2] = {}
        self._overflow = False
        self._invalid_keys = False

    def begin(self, execution_keys: tuple[str, ...]) -> ContextKeyV2:
        if len(execution_keys) > 1000 or any(
            not isinstance(k, str) or not 0 < len(k) <= 256 for k in execution_keys
        ):
            self._invalid_keys = True
            execution_keys = ()
        key = ContextKeyV2(
            source_owner_incarnation=self.incarnation, context_incarnation=uuid4().hex
        )
        if len(self._records) >= 1000:
            self._overflow = True
            return key
        self._records[key] = ContextRecordV2(
            key=key,
            vendor_id=None,
            operation=ContextOperationV2(operation_id=uuid4().hex, action="create", parent=None),
            execution_keys=execution_keys,
            provenance="primary",
            lifecycle="starting",
            aliases=(),
            revision=0,
            stop_proof=None,
            continuity_proof=None,
        )
        return key

    def mark(
        self, key: ContextKeyV2 | None, lifecycle: Literal["live", "closing", "unknown"]
    ) -> None:
        row = self._records.get(key) if key is not None else None
        if row is not None:
            self._records[row.key] = row.model_copy(
                update={"lifecycle": lifecycle, "revision": row.revision + 1}
            )

    async def prepare(
        self, session_id: str, read_id: str, *, timeout_s: float = 5.0
    ) -> Callable[[], ContextEvidenceV2]:
        if not 0 < timeout_s <= 5.0:
            return lambda: unknown_context_evidence_v2(session_id, read_id, "invalid_read_budget")

        def reconcile() -> ContextEvidenceV2:
            if self._overflow:
                return unknown_context_evidence_v2(session_id, read_id, "context_history_budget")
            if self._invalid_keys:
                return unknown_context_evidence_v2(
                    session_id, read_id, "sdk_execution_key_invalid"
                )
            active = self._active()
            records = tuple(
                row.model_copy(update={"lifecycle": "unknown"})
                if row.lifecycle == "live" and row.key not in active
                else row
                for row in self._records.values()
            )
            reasons = {
                "sdk_vendor_identity_unconfirmed",
                "sdk_internal_contexts_unobserved",
                "readonly_alias_provenance_unavailable",
            }
            if any(not row.execution_keys for row in records):
                reasons.add("sdk_execution_key_unbound")
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
                        contexts=records,
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
