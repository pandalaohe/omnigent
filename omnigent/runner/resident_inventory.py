"""Pure owner evidence; backend and terminal registrations are not context counts."""

from dataclasses import dataclass

from omnigent.runner.native.orchestration import (
    NativeBackendInventorySnapshot,
    native_backend_resident_inventory,
)
from omnigent.runner.resource_registry import (
    SessionResourceRegistry,
    TerminalResourceRegistrationSnapshot,
)


@dataclass(frozen=True)
class OwnerRegistrationSnapshot:
    native_backends: NativeBackendInventorySnapshot
    terminals: tuple[TerminalResourceRegistrationSnapshot, ...] | None
    executor_contexts_complete: bool = False
    external_owners_complete: bool = False


def owner_registration_snapshot(resources: SessionResourceRegistry) -> OwnerRegistrationSnapshot:
    """Copy owner maps without liveness probes, cleanup, or executor construction.

    The maps have separate owners; this read cannot establish an atomic host union
    or enumerate harness-owned SDK contexts and independent native side threads.
    """
    return OwnerRegistrationSnapshot(
        native_backends=native_backend_resident_inventory(),
        terminals=resources.terminal_registration_snapshot(),
    )
