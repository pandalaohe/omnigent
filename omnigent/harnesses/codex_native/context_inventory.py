"""Bounded operation evidence shared by Codex's SDK and native owners."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Mapping
from typing import Literal

from omnigent.harnesses.codex_native.side_chat import KEEP_WARM_THREAD_SOURCE
from omnigent.host.recovery import RuntimeObservation
from omnigent.startup_protocol import (
    ContextKeyV2,
    ContextOperationV2,
    ContextRecordV2,
    ContextWorkBindingV2,
    ScopeKeyV2,
    SourceEvidenceV2,
    UnresolvedContextV2,
    VendorContextIdV2,
)

CodexContextProvenance = Literal[
    "primary", "managed_side_chat", "maintenance", "harness_internal", "unknown"
]


class CodexContextInventory:
    """An owning registry, not a vendor reader or a transport lifetime."""

    def __init__(self, incarnation: str | None = None) -> None:
        self.incarnation = incarnation or uuid.uuid4().hex
        self.records: dict[ContextKeyV2, ContextRecordV2] = {}
        self._domains: dict[ContextKeyV2, str] = {}
        self._overflow = False
        self._gaps: set[str] = set()
        self._work_member: tuple[str, str] | None = None
        self._work_current: Callable[[], bool] = lambda: False
        self._active_work: dict[ContextKeyV2, set[object]] = {}
        self._terminal_work: dict[ContextKeyV2, set[str]] = {}

    def bind_work_member(
        self, runtime_id: str, generation: str, current: Callable[[], bool]
    ) -> None:
        """Bind a backend observation lifetime, never a reservation or capacity unit."""
        self._work_member = (runtime_id, generation)
        self._work_current = current

    def _work_context(self, thread_id: object) -> ContextKeyV2 | None:
        matches = [
            record.key
            for record in self.records.values()
            if record.vendor_id is not None and record.vendor_id.value == thread_id
        ]
        if len(matches) != 1:
            self._gaps.add("context_work_thread_ambiguous_or_unobserved")
            return None
        return matches[0]

    def begin_work(
        self, method: str, params: Mapping[str, object], *, current: Callable[[], bool]
    ) -> Callable[[Mapping[str, object] | None], None]:
        if method != "turn/start" or not self._work_current() or not current():
            return lambda _response: None
        key = self._work_context(params.get("threadId"))
        if key is None or self._overflow:
            return lambda _response: None
        active = self._active_work.setdefault(key, set())
        if (
            sum(map(len, self._active_work.values())) + sum(map(len, self._terminal_work.values()))
            >= 1000
        ):
            self._gaps.add("context_work_history_budget")
            return lambda _response: None
        pending = object()
        active.add(pending)

        def finish(response: Mapping[str, object] | None) -> None:
            result = response.get("result") if response else None
            turn = result.get("turn") if isinstance(result, Mapping) else None
            turn_id = turn.get("id") if isinstance(turn, Mapping) else None
            if (
                not current()
                or not self._work_current()
                or not isinstance(turn_id, str)
                or not 0 < len(turn_id) <= 256
            ):
                self._gaps.add("context_work_outcome_unconfirmed")
                return
            active.discard(pending)
            if turn_id not in self._terminal_work.get(key, ()):
                active.add(turn_id)

        return finish

    def observe_work_event(self, method: object, params: object) -> None:
        if (
            not isinstance(method, str)
            or method not in {"turn/started", "turn/completed", "turn/failed"}
            or not isinstance(params, Mapping)
            or not self._work_current()
        ):
            return
        key = self._work_context(params.get("threadId"))
        turn = params.get("turn")
        turn_id = turn.get("id") if isinstance(turn, Mapping) else params.get("turnId")
        if key is None or not isinstance(turn_id, str) or not 0 < len(turn_id) <= 256:
            self._gaps.add("context_work_event_unbound")
            return
        if (
            sum(map(len, self._active_work.values())) + sum(map(len, self._terminal_work.values()))
            >= 1000
        ):
            self._gaps.add("context_work_history_budget")
            return
        active = self._active_work.setdefault(key, set())
        terminal = self._terminal_work.setdefault(key, set())
        if method == "turn/started":
            if turn_id not in terminal:
                active.add(turn_id)
        else:
            terminal.add(turn_id)
            active.discard(turn_id)

    def work_observation(self) -> RuntimeObservation | None:
        """Positive observed work only; unobserved work and descendants stay unknown."""
        if self._work_member is None or not self._work_current() or self._overflow:
            return None
        return RuntimeObservation(
            runtime_id=self._work_member[0],
            generation=self._work_member[1],
            observed_at=time.time(),
            live_turn=any(self._active_work.values()),
        )

    def begin(
        self,
        method: str,
        params: Mapping[str, object],
        *,
        domain: str,
        current: Callable[[], bool],
        execution_keys: tuple[str, ...] = (),
        provenance: CodexContextProvenance | None = None,
        reserved: bool = False,
    ) -> Callable[[Mapping[str, object] | None], None]:
        if method not in {"thread/start", "thread/resume", "thread/fork"}:
            return lambda _response: None
        action: Literal["create", "resume", "fork"] = (
            "create"
            if method == "thread/start"
            else "resume"
            if method == "thread/resume"
            else "fork"
        )
        requested = params.get("threadId")
        requested = requested if isinstance(requested, str) and 0 < len(requested) <= 256 else None
        parents = [
            row
            for key, row in self.records.items()
            if self._domains[key] == domain
            and row.vendor_id is not None
            and row.vendor_id.value == requested
        ]
        parent = parents[0] if len(parents) == 1 else None
        if action == "fork" and parent is None:
            self._gaps.add("fork_parent_unobserved")
            if provenance == "managed_side_chat":
                provenance = "unknown"
        if provenance is None:
            provenance = (
                "maintenance"
                if action == "fork" and params.get("threadSource") == KEEP_WARM_THREAD_SOURCE
                else "primary"
                if action == "create"
                else "unknown"
            )
        pending = [
            row
            for key, row in self.records.items()
            if self._domains[key] == domain
            and row.lifecycle == "reserved"
            and row.operation.action == "create"
        ]
        reuse = (
            parent
            if action == "resume"
            else (pending[0] if action == "create" and len(pending) == 1 else None)
        )
        key = (
            reuse.key
            if reuse
            else ContextKeyV2(
                source_owner_incarnation=self.incarnation, context_incarnation=uuid.uuid4().hex
            )
        )
        if key not in self.records and len(self.records) >= 1000:
            self._overflow = True
            return lambda _response: None
        record = ContextRecordV2(
            key=key,
            vendor_id=reuse.vendor_id
            if reuse
            else (
                VendorContextIdV2(namespace="codex", value=requested)
                if action == "resume" and requested
                else None
            ),
            operation=reuse.operation
            if reuse and (reuse.operation.action == "fork" or action == "create")
            else ContextOperationV2(
                operation_id=uuid.uuid4().hex,
                action=action,
                parent=parent.key if action == "fork" and parent else None,
            ),
            execution_keys=reuse.execution_keys if reuse else execution_keys,
            provenance=reuse.provenance if reuse else provenance,
            lifecycle="reserved" if reserved else "starting",
            aliases=(),
            revision=reuse.revision + 1 if reuse else 0,
            stop_proof=None,
            continuity_proof=None,
        )
        self.records[key] = record
        self._domains[key] = domain

        def finish(response: Mapping[str, object] | None) -> None:
            latest = self.records[key]
            if latest.revision != record.revision:
                return
            result = response.get("result") if response else None
            thread = result.get("thread") if isinstance(result, Mapping) else None
            thread_id = thread.get("id") if isinstance(thread, Mapping) else None
            valid = (
                current()
                and isinstance(thread_id, str)
                and 0 < len(thread_id) <= 256
                and (action != "resume" or thread_id == requested)
                and (action != "fork" or thread_id != requested)
            )
            self.records[key] = latest.model_copy(
                update={
                    "vendor_id": VendorContextIdV2(namespace="codex", value=thread_id)
                    if valid and isinstance(thread_id, str)
                    else latest.vendor_id,
                    "execution_keys": latest.execution_keys
                    or ((thread_id,) if valid and isinstance(thread_id, str) else ()),
                    "lifecycle": "live" if valid else "unknown",
                    "revision": latest.revision + 1,
                }
            )

        return finish

    def mark(
        self,
        domain: str,
        lifecycle: Literal["closing", "unknown"],
        *,
        provenance: CodexContextProvenance | None = None,
    ) -> None:
        for key, record in self.records.items():
            if self._domains[key] == domain and (
                provenance is None or record.provenance == provenance
            ):
                self.records[key] = record.model_copy(
                    update={
                        "lifecycle": lifecycle,
                        "revision": record.revision + 1,
                    }
                )

    def source(self, scope: ScopeKeyV2, *reasons: str) -> SourceEvidenceV2:
        gaps = {
            "codex_internal_contexts_unobserved",
            "readonly_alias_provenance_unavailable",
            *reasons,
            *self._gaps,
        }
        if self._overflow:
            gaps.add("context_history_budget")
        bindings = None
        if not self._overflow and self._work_member is not None and self._work_current():
            bindings = tuple(
                ContextWorkBindingV2(
                    context=record.key,
                    runtime_id=self._work_member[0],
                    generation=self._work_member[1],
                )
                for record in self.records.values()
                if record.lifecycle == "live"
                and record.vendor_id is not None
                and sum(other.vendor_id == record.vendor_id for other in self.records.values())
                == 1
            )
            if len(bindings) != len(self.records):
                gaps.add("context_work_association_incomplete")
        return SourceEvidenceV2(
            scope=scope,
            producer_incarnation=self.incarnation,
            coverage="partial",
            contexts=() if self._overflow else tuple(self.records.values()),
            unresolved=tuple(UnresolvedContextV2(candidate=None, reason=r) for r in sorted(gaps)),
            membership_revision=None,
            context_revision=None,
            work_revision=None,
            revision_authority="observation_only",
            age_upper_bound_s=0.0 if bindings is not None else None,
            work_bindings=bindings,
        )
