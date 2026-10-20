"""Host recovery never converts quiet output or failed inventory into safety."""

from dataclasses import replace

import pytest

from omnigent.host.recovery import RecoveryInventory, RuntimeObservation, assess_recovery


def inventory(*observations):
    return RecoveryInventory(
        host_generation="host-generation",
        process_identity="pid:birth",
        supervisor="service",
        server_members=frozenset((name, "generation") for name in ("first", "second", "other")),
        local_members=frozenset((name, "generation") for name in ("first", "second", "other")),
        unresolved_startups=frozenset((name, "generation") for name in ("first", "second")),
        server_complete=True,
        local_complete=True,
        execution_fence=True,
        host_fault_confirmed=True,
        observations=observations,
    )


def idle(runtime):
    return RuntimeObservation(
        runtime_id=runtime,
        generation="generation",
        observed_at=1000,
        previous_observed_at=990,
        previous_no_live_work_confirmed=True,
        previous_no_owned_descendants=True,
        nonworking_since=800,
        no_live_work_confirmed=True,
        no_owned_descendants=True,
    )


def test_working_and_protected_waits_veto_even_after_long_quiet():
    for field in ("live_turn", "live_tool", "background_job", "protected_interaction"):
        observations = [idle("first"), idle("second"), replace(idle("other"), **{field: True})]
        result = assess_recovery(inventory(*observations), now=1001)
        assert not result.allowed and result.reason == "other_session_working"


def test_unknown_stale_incomplete_and_unfenceable_inventory_fail_closed():
    base = inventory(idle("first"), idle("second"), idle("other"))
    for changes in (
        {"server_complete": False},
        {"local_complete": False},
        {"execution_fence": False},
        {"supervisor": None},
        {"host_fault_confirmed": False},
        {"unresolved_startups": frozenset((name, "old") for name in ("first", "second"))},
        {
            "local_members": frozenset(
                (name, "generation") for name in ("first", "second", "other", "hidden")
            )
        },
        {"observations": (idle("first"), idle("second"))},
        {"observations": (idle("first"), idle("second"), replace(idle("other"), observed_at=980))},
    ):
        assert not assess_recovery(replace(base, **changes), now=1001).allowed


def test_complete_fresh_negative_evidence_and_two_samples_allow_plan():
    base = inventory(idle("first"), idle("second"), idle("other"))
    assert assess_recovery(base, now=1001).allowed
    for observation in (
        replace(idle("other"), no_live_work_confirmed=False),
        replace(idle("other"), no_owned_descendants=False),
        replace(idle("other"), nonworking_since=950),
        replace(idle("other"), previous_observed_at=999),
        replace(idle("other"), previous_no_live_work_confirmed=False),
        replace(idle("other"), previous_no_owned_descendants=False),
    ):
        assert not assess_recovery(
            replace(base, observations=(idle("first"), idle("second"), observation)), now=1001
        ).allowed


def test_unreachable_requires_fresh_spanning_failed_probes_and_independent_no_work():
    others = (idle("first"), idle("second"))
    current = replace(idle("other"), unreachable=True, failed_probe_times=(800, 900, 1000))
    assert assess_recovery(inventory(*others, current), now=1001).allowed
    for times in (
        (),
        (800, 1000),
        (950, 975, 1000),
        (600, 700, 800),
        (800, 800, 1000),
        (800, 999, 1002),
    ):
        assert not assess_recovery(
            inventory(*others, replace(current, failed_probe_times=times)), now=1001
        ).allowed
    assert not assess_recovery(
        inventory(*others, replace(current, no_live_work_confirmed=False)), now=1001
    ).allowed


def test_stalled_requires_independent_diagnostic_and_full_quiet_window():
    others = (idle("first"), idle("second"))
    stalled = replace(idle("other"), stalled_diagnostic=True, nonworking_since=600)
    assert assess_recovery(inventory(*others, stalled), now=1001).allowed
    assert not assess_recovery(
        inventory(*others, replace(stalled, nonworking_since=750)), now=1001
    ).allowed
    assert not assess_recovery(
        inventory(*others, replace(stalled, no_owned_descendants=False)), now=1001
    ).allowed


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_safety_times_are_never_eligible(invalid):
    others = (idle("first"), idle("second"))
    for field in ("observed_at", "previous_observed_at", "nonworking_since"):
        assert not assess_recovery(
            inventory(*others, replace(idle("other"), **{field: invalid})), now=1001
        ).allowed
    assert not assess_recovery(inventory(*others, idle("other")), now=invalid).allowed
    assert not assess_recovery(
        inventory(
            *others,
            replace(idle("other"), unreachable=True, failed_probe_times=(invalid, 900, 1000)),
        ),
        now=1001,
    ).allowed
