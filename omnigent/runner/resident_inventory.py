"""Runner owner evidence; backend and terminal registrations are not context counts."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, replace

from omnigent.runner.native.orchestration import (
    NativeBackendInventorySnapshot,
    native_backend_resident_inventory,
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
