"""Conservative work evidence required before planning a host-process recovery."""

from dataclasses import dataclass
from itertools import pairwise
from math import isfinite


@dataclass(frozen=True)
class RuntimeObservation:
    runtime_id: str
    generation: str
    observed_at: float
    previous_observed_at: float | None = None
    previous_no_live_work_confirmed: bool = False
    previous_no_owned_descendants: bool = False
    nonworking_since: float | None = None
    no_live_work_confirmed: bool = False
    no_owned_descendants: bool = False
    live_turn: bool = False
    live_tool: bool = False
    background_job: bool = False
    protected_interaction: bool = False
    exited: bool = False
    stalled_diagnostic: bool = False
    unreachable: bool = False
    failed_probe_times: tuple[float, ...] = ()


@dataclass(frozen=True)
class RecoveryInventory:
    host_generation: str
    process_identity: str
    supervisor: str | None
    server_members: frozenset[tuple[str, str]]
    local_members: frozenset[tuple[str, str]]
    unresolved_startups: frozenset[tuple[str, str]]
    server_complete: bool
    local_complete: bool
    execution_fence: bool
    host_fault_confirmed: bool
    observations: tuple[RuntimeObservation, ...]


@dataclass(frozen=True)
class RecoveryAssessment:
    allowed: bool
    reason: str


def assess_recovery(inventory: RecoveryInventory, *, now: float) -> RecoveryAssessment:
    """An allowed plan still needs an owned fence and final local commit check."""
    times = [now]
    for row in inventory.observations:
        times.extend((row.observed_at, *row.failed_probe_times))
        times.extend(
            value
            for value in (row.previous_observed_at, row.nonworking_since)
            if value is not None
        )
    if not all(isfinite(value) for value in times):
        return RecoveryAssessment(False, "invalid_observation_time")
    for observation in inventory.observations:
        if (
            observation.live_turn
            or observation.live_tool
            or observation.background_job
            or observation.protected_interaction
        ):
            return RecoveryAssessment(False, "other_session_working")
    if not (inventory.server_complete and inventory.local_complete):
        return RecoveryAssessment(False, "inventory_incomplete")
    if not (
        inventory.host_generation
        and inventory.process_identity
        and inventory.supervisor
        and inventory.execution_fence
        and inventory.host_fault_confirmed
    ):
        return RecoveryAssessment(False, "safe_recovery_unavailable")
    members = inventory.server_members | inventory.local_members
    if len(inventory.unresolved_startups) != 2 or not inventory.unresolved_startups.issubset(
        members
    ):
        return RecoveryAssessment(False, "startup_incident_changed")
    observed = {(row.runtime_id, row.generation) for row in inventory.observations}
    if observed != members:
        return RecoveryAssessment(False, "runtime_membership_unknown")
    for row in inventory.observations:
        if not 0 <= now - row.observed_at <= 10:
            return RecoveryAssessment(False, "runtime_observation_stale")
        if not (row.no_live_work_confirmed and row.no_owned_descendants):
            return RecoveryAssessment(False, "runtime_work_unknown")
        if row.exited:
            continue
        quiet = row.nonworking_since
        if row.stalled_diagnostic:
            if quiet is not None and row.observed_at - quiet >= 300:
                continue
        elif row.unreachable:
            times = row.failed_probe_times
            if (
                len(times) >= 3
                and all(first < second for first, second in pairwise(times))
                and times[-1] - times[0] >= 180
                and 0 <= now - times[-1] <= 10
                and times[-1] <= row.observed_at
            ):
                continue
        elif (
            quiet is not None
            and row.observed_at - quiet >= 120
            and row.previous_observed_at is not None
            and quiet <= row.previous_observed_at <= row.observed_at - 10
            and row.previous_no_live_work_confirmed
            and row.previous_no_owned_descendants
        ):
            continue
        return RecoveryAssessment(False, "nonworking_window_unconfirmed")
    return RecoveryAssessment(True, "all_affected_contexts_nonworking")
