"""Runner owner evidence; backend and terminal registrations are not context counts."""

import asyncio
from dataclasses import dataclass

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
    HarnessContextInventorySnapshot,
    HarnessInventorySnapshot,
    HarnessProcessManager,
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


async def owner_registration_snapshot(
    resources: SessionResourceRegistry, process_manager: HarnessProcessManager | None
) -> OwnerRegistrationSnapshot:
    """Read registered evidence without probes, cleanup, or executor construction.

    The maps have separate owners; this read cannot establish an atomic host union
    or fresh vendor revisions and independent native side threads.
    """
    harnesses = None
    native = None
    try:
        async with asyncio.timeout(_OWNER_INVENTORY_TIMEOUT_S):
            reads: list[HarnessContextInventorySnapshot] = []
            if process_manager is not None:
                selected = await process_manager.resident_inventory()
                owners = iter(selected.owners)

                async def read_owners() -> None:
                    for owner in owners:
                        read = await process_manager.read_resident_inventory(
                            owner, timeout_s=_OWNER_INVENTORY_TIMEOUT_S
                        )
                        if read is not None:
                            reads.append(read)

                async with asyncio.TaskGroup() as workers:
                    for _ in range(min(_MAX_CONCURRENT_OWNER_READS, len(selected.owners))):
                        workers.create_task(read_owners())
            reconcile_native = await prepare_native_backend_thread_inventory(
                timeout_s=_OWNER_INVENTORY_TIMEOUT_S
            )
            if process_manager is not None:
                harnesses = await process_manager.resident_inventory(context_reads=tuple(reads))
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
    )
