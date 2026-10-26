"""Runner owner evidence; backend and terminal registrations are not context counts."""

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, replace

import httpx

from omnigent.runner.native.orchestration import (
    NativeBackendInventorySnapshot,
    native_backend_resident_inventory,
    native_context_source_v2,
    prepare_native_backend_thread_inventory,
)
from omnigent.runner.resource_registry import (
    SessionResourceRegistry,
    TerminalResourceRegistrationSnapshot,
)
from omnigent.runtime.harnesses.process_manager import (
    HarnessInventorySnapshot,
    HarnessProcessManager,
    HarnessThreadInventorySnapshot,
)
from omnigent.startup_protocol import (
    ContextEvidenceV2,
    SourceEvidenceV2,
    UnresolvedContextV2,
    harness_context_authority_v2,
    session_context_scopes_v2,
)

_OWNER_INVENTORY_TIMEOUT_S = 5.0
_MAX_CONCURRENT_OWNER_READS = 4


@dataclass(frozen=True)
class OwnerRegistrationSnapshot:
    native_backends: NativeBackendInventorySnapshot
    terminals: tuple[TerminalResourceRegistrationSnapshot, ...] | None
    harnesses: HarnessInventorySnapshot | None = None
    executor_contexts_complete: bool = False
    external_owners_complete: bool = False
    # Aligned with native_backends.owners; None is unknown, () is observed empty.
    native_thread_ids: tuple[tuple[str, ...] | None, ...] | None = None
    # Aligned first with harnesses.owners, then each context inventory's owners.
    harness_thread_ids: tuple[tuple[tuple[str, ...] | None, ...] | None, ...] | None = None


async def owner_registration_snapshot(
    resources: SessionResourceRegistry, process_manager: HarnessProcessManager | None
) -> OwnerRegistrationSnapshot:
    """Read registered evidence without probes, cleanup, or executor construction.

    The maps have separate owners; this read cannot establish an atomic host union
    or fresh vendor revisions and independent native side threads.
    """
    harnesses = None
    harness_threads = None
    native = None
    try:
        async with asyncio.timeout(_OWNER_INVENTORY_TIMEOUT_S):
            reconcilers: list[Callable[[], HarnessThreadInventorySnapshot | None]] = []
            if process_manager is not None:
                selected = await process_manager.resident_inventory()
                owners = iter(selected.owners)

                async def read_owners() -> None:
                    for owner in owners:
                        reconcile = await process_manager.prepare_resident_thread_inventory(
                            owner, timeout_s=_OWNER_INVENTORY_TIMEOUT_S
                        )
                        reconcilers.append(reconcile)

                async with asyncio.TaskGroup() as workers:
                    for _ in range(min(_MAX_CONCURRENT_OWNER_READS, len(selected.owners))):
                        workers.create_task(read_owners())
            reconcile_native = await prepare_native_backend_thread_inventory(
                timeout_s=_OWNER_INVENTORY_TIMEOUT_S
            )
            if process_manager is not None:
                reads = tuple(
                    read.context for reconcile in reconcilers if (read := reconcile()) is not None
                )
                harnesses = await process_manager.resident_inventory(context_reads=reads)
                fresh = {
                    read.context.owner: read
                    for reconcile in reconcilers
                    if (read := reconcile()) is not None
                }
                contexts = []
                threads = []
                for owner, context in zip(harnesses.owners, harnesses.contexts or (), strict=True):
                    read = fresh.get(owner)
                    current = read is not None and read.context == context
                    contexts.append(context if current else None)
                    threads.append(read.thread_ids if current and read is not None else None)
                harnesses = replace(harnesses, contexts=tuple(contexts))
                harness_threads = tuple(threads)
            native = reconcile_native()
    except TimeoutError:
        pass
    return OwnerRegistrationSnapshot(
        native_backends=native.inventory
        if native is not None
        else native_backend_resident_inventory(),
        terminals=resources.terminal_registration_snapshot(),
        harnesses=harnesses,
        native_thread_ids=native.thread_ids if native is not None else None,
        harness_thread_ids=harness_threads,
    )


def _unknown_context_source(scope, reason: str) -> SourceEvidenceV2:
    """An unsupported source still names its scope and why it cannot answer."""
    return SourceEvidenceV2(
        scope=scope,
        producer_incarnation=None,
        coverage="unsupported",
        contexts=(),
        unresolved=(UnresolvedContextV2(candidate=None, reason=reason),),
        membership_revision=None,
        context_revision=None,
        work_revision=None,
        revision_authority="observation_only",
        age_upper_bound_s=None,
        work_bindings=None,
    )


async def _harness_context_source_v2(
    session_id: str,
    scope,
    process_manager: HarnessProcessManager | None,
    timeout_s: float,
) -> SourceEvidenceV2:
    """Read the resident harness's own v2 evidence; never construct or spawn one."""
    if process_manager is None:
        return _unknown_context_source(scope, "no_resident_harness")
    selected = await process_manager.resident_inventory()
    owner = next(
        (
            candidate
            for candidate in selected.owners
            if candidate.session_id == session_id
            and not candidate.closing
            and not candidate.wrapper_exited
            and not candidate.exit_confirmed
        ),
        None,
    )
    source = _unknown_context_source(scope, "no_resident_harness")
    reconcile = None
    if owner is not None:
        reconcile = await process_manager.prepare_context_inventory_v2(owner, timeout_s=timeout_s)
    current = await process_manager.resident_inventory()
    if reconcile is not None:
        snapshot = reconcile()
        sources = snapshot.evidence.sources
        if snapshot.evidence.authority != harness_context_authority_v2(session_id):
            source = _unknown_context_source(scope, "foreign_session")
        elif len(sources) != 1 or sources[0].scope != scope:
            source = _unknown_context_source(scope, "invalid_harness_evidence")
        else:
            source = sources[0]
    current_live = {
        candidate.incarnation_id
        for candidate in current.owners
        if not candidate.closing and not candidate.wrapper_exited and not candidate.exit_confirmed
    }
    predecessors = {
        candidate.incarnation_id
        for candidate in (*selected.owners, *current.owners)
        if candidate.session_id == session_id
        and not candidate.exit_confirmed
        and candidate.incarnation_id not in current_live
    }
    gaps = tuple(
        UnresolvedContextV2(candidate=incarnation, reason="harness_predecessor_exit_unconfirmed")
        for incarnation in sorted(predecessors)
    )
    if not selected.exit_coverage_complete or not current.exit_coverage_complete:
        gaps += (UnresolvedContextV2(candidate=None, reason="harness_exit_coverage_incomplete"),)
    if not gaps:
        return source
    unresolved = (*source.unresolved, *gaps)
    if len(unresolved) > 1000:
        unresolved = (
            *unresolved[:999],
            UnresolvedContextV2(candidate=None, reason="harness_predecessor_reason_budget"),
        )
    return source.model_copy(
        update={
            "coverage": "unsupported" if source.coverage == "unsupported" else "partial",
            "unresolved": unresolved,
            "membership_revision": None,
            "context_revision": None,
            "work_revision": None,
            "revision_authority": "observation_only",
            "age_upper_bound_s": None,
            "work_bindings": None,
        }
    )


def _terminal_context_source_v2(session_id: str, scope, resources: SessionResourceRegistry):
    source = _unknown_context_source(scope, "terminal_context_source_unimplemented")
    terminals = resources.terminal_registry
    if terminals is None:
        return source
    rows, overflow = terminals.owner_uncertainty_snapshot()
    unresolved = (
        *source.unresolved,
        *(
            UnresolvedContextV2(candidate=row.instance_id, reason="terminal_lifetime_unconfirmed")
            for row in rows
            if row.conversation_id == session_id
        ),
    )
    if overflow or len(unresolved) > 1000:
        unresolved = (
            *unresolved[:999],
            UnresolvedContextV2(candidate=None, reason="terminal_owner_history_budget"),
        )
    return source.model_copy(update={"unresolved": unresolved})


async def session_context_evidence_v2(
    session_id: str,
    read_id: str,
    *,
    process_manager: HarnessProcessManager | None,
    resources: SessionResourceRegistry,
    timeout_s: float = _OWNER_INVENTORY_TIMEOUT_S,
) -> ContextEvidenceV2:
    """Compose the runner's three session scopes into one v2 envelope.

    Native OpenCode owners retain partial operation evidence; other native sources
    and terminal roles remain unsupported. Reads neither construct nor probe owners.
    """
    scopes = session_context_scopes_v2(session_id)
    started = time.monotonic()
    harness = _unknown_context_source(scopes[0], "no_resident_harness")
    if 0 < timeout_s <= 5.0:
        try:
            async with asyncio.timeout_at(started + timeout_s):
                harness = await _harness_context_source_v2(
                    session_id, scopes[0], process_manager, timeout_s
                )
        except TimeoutError:
            harness = _unknown_context_source(scopes[0], "read_timeout")
        except (httpx.HTTPError, RuntimeError, ValueError):
            harness = _unknown_context_source(scopes[0], "invalid_or_failed_read")
    return ContextEvidenceV2(
        wire_version=2,
        read_id=read_id,
        authority=harness_context_authority_v2(session_id),
        expected_scopes=scopes,
        sources=(
            harness,
            native_context_source_v2(session_id)
            or _unknown_context_source(scopes[1], "native_context_source_unimplemented"),
            _terminal_context_source_v2(session_id, scopes[2], resources),
        ),
    )
