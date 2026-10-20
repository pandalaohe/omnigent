"""Only exact post-gate input and execution receipts release startup capacity."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from omnigent.db.db_models import current_workspace_id
from omnigent.db.session_startup import SqlSessionStartup
from omnigent.entities.session_startup import StartupReceipt
from omnigent.startup_protocol import StartupAttemptRef, StartupObservation
from omnigent.stores.session_startup_store import SessionStartupStore


@pytest.mark.parametrize("work_first", [False, True])
def test_receipts_join_by_turn_in_either_order_and_keep_resident_budget(db_uri, work_first):
    store = SessionStartupStore(db_uri)
    host, input_id = uuid4().hex, uuid4().hex
    store.configure(host, host_resident_limit=2, deployment_resident_limit=2)
    start = store.enqueue(
        owner_user_id="owner",
        host_id=host,
        session_id=uuid4().hex,
        operation_key="first",
        input_id=input_id,
    )
    store.reserve(host)
    start = store.get(start.id)
    assert store.claim(start.id, start.attempt_id, runtime_id="runner-generation")
    assert store.bind_dispatch(
        start.id,
        start.attempt_id,
        runtime_id="runner-generation",
        source="codex-native",
        vendor_session_id="thread",
        vendor_turn_id="turn",
    )
    receipt = {
        "startup_id": start.id,
        "attempt_id": start.attempt_id,
        "input_id": input_id,
        "runtime_id": "runner-generation",
        "source": "codex-native",
        "vendor_session_id": "thread",
        "vendor_turn_id": "turn",
    }
    # Bare turn/started is not one of the accepted proof phases.
    assert not store.observe(
        StartupReceipt(**{**receipt, "vendor_turn_id": "old"}, phase="work_started")
    )
    phases = (
        ["work_started", "input_accepted"] if work_first else ["input_accepted", "work_started"]
    )
    for phase in ["runtime_ready", "tools_ready", *phases[:-1]]:
        assert store.observe(StartupReceipt(**receipt, phase=phase))
        assert store.get(start.id).state == "verifying"
    assert store.observe(StartupReceipt(**receipt, phase=phases[-1]))
    assert store.get(start.id).state == "working"
    assert store.observe(StartupReceipt(**receipt, phase=phases[-1]))
    # Working frees startup occupancy, but neither host nor deployment residency.
    for operation in ("second", "third"):
        store.enqueue(
            owner_user_id="owner",
            host_id=host,
            session_id=uuid4().hex,
            operation_key=operation,
            input_id=uuid4().hex,
        )
    assert store.reserve(host) is not None
    assert store.reserve(host) is None
    store.cancel(start.id)
    assert not store.observe(StartupReceipt(**receipt, phase="work_started"))
    assert store.get(start.id).state == "cancelled"


def test_unknown_and_blocked_inputs_keep_permits_and_empty_runtime_needs_tools(db_uri):
    store = SessionStartupStore(db_uri)
    host = uuid4().hex
    store.configure(host, host_resident_limit=3, deployment_resident_limit=3)
    for i in range(3):
        store.enqueue(
            owner_user_id="owner",
            host_id=host,
            session_id=uuid4().hex,
            operation_key=str(i),
            input_id=None,
        )
    first = store.reserve(host)
    second = store.reserve(host)
    for start in (first, second):
        assert store.claim(start.id, start.attempt_id, runtime_id="runtime")
    receipt = {
        "startup_id": first.id,
        "attempt_id": first.attempt_id,
        "runtime_id": "runtime",
        "source": "runner",
    }
    assert store.observe(StartupReceipt(**receipt, phase="uncertain"))
    assert store.reserve(host) is None
    assert store.observe(StartupReceipt(**receipt, phase="runtime_ready"))
    assert store.reserve(host) is None
    assert store.observe(StartupReceipt(**receipt, phase="tools_ready"))
    assert store.get(first.id).state == "ready_empty"
    assert store.reserve(host) is not None


def test_blocked_attempt_counts_toward_limit_and_late_proof_cannot_restart_it(db_uri):
    store = SessionStartupStore(db_uri)
    host, input_id = uuid4().hex, uuid4().hex
    store.configure(host, host_resident_limit=2, deployment_resident_limit=2)
    first = store.enqueue(
        owner_user_id="owner",
        host_id=host,
        session_id=uuid4().hex,
        operation_key="first",
        input_id=input_id,
    )
    attempt = store.reserve(host)
    assert store.claim(first.id, attempt.attempt_id, runtime_id="runtime")
    assert store.bind_dispatch(
        first.id,
        attempt.attempt_id,
        runtime_id="runtime",
        source="codex-native",
        vendor_session_id="thread",
        vendor_turn_id="turn",
    )
    receipt = {
        "startup_id": first.id,
        "attempt_id": attempt.attempt_id,
        "input_id": input_id,
        "runtime_id": "runtime",
        "source": "codex-native",
        "vendor_session_id": "thread",
        "vendor_turn_id": "turn",
    }
    assert store.observe(StartupReceipt(**receipt, phase="blocked", reason="sign_in_required"))
    for phase in ("runtime_ready", "tools_ready", "input_accepted", "work_started"):
        store.observe(StartupReceipt(**receipt, phase=phase))
    assert store.get(first.id).state == "blocked"
    assert not store.claim(first.id, attempt.attempt_id, runtime_id="runtime")
    assert not store.bind_dispatch(
        first.id,
        attempt.attempt_id,
        runtime_id="runtime",
        source="codex-native",
        vendor_session_id="thread",
        vendor_turn_id="turn",
    )
    for index in range(99):
        store.enqueue(
            owner_user_id="owner",
            host_id=host,
            session_id=uuid4().hex,
            operation_key=str(index),
            input_id=uuid4().hex,
        )
    with pytest.raises(ValueError, match="queue is full"):
        store.enqueue(
            owner_user_id="owner",
            host_id=host,
            session_id=uuid4().hex,
            operation_key="excess",
            input_id=uuid4().hex,
        )


def test_observation_deadlines_use_admission_and_exact_first_vendor_dispatch(db_uri):
    store, host, input_id = SessionStartupStore(db_uri), uuid4().hex, uuid4().hex
    store.configure(host, host_resident_limit=2, deployment_resident_limit=2)
    queued = store.enqueue(
        owner_user_id="owner",
        host_id=host,
        session_id=uuid4().hex,
        operation_key="deadline",
        input_id=input_id,
    )
    attempt = store.reserve(host)
    assert attempt.admitted_at is not None
    assert attempt.first_dispatch_at is None
    before_dispatch = store.presentation(queued.session_id)
    assert before_dispatch.admitted_elapsed_seconds is not None
    assert before_dispatch.input_elapsed_seconds is None
    assert store.claim(queued.id, attempt.attempt_id, runtime_id="runtime")
    assert store.bind_dispatch(
        queued.id,
        attempt.attempt_id,
        runtime_id="runtime",
        source="codex-native",
        vendor_session_id="thread",
        vendor_turn_id="turn",
    )
    receipt = {
        "startup_id": queued.id,
        "attempt_id": attempt.attempt_id,
        "input_id": input_id,
        "runtime_id": "runtime",
        "source": "codex-native",
        "vendor_session_id": "thread",
        "vendor_turn_id": "turn",
    }
    assert not store.observe(
        StartupReceipt(**{**receipt, "vendor_turn_id": "wrong"}, phase="input_dispatched")
    )
    assert store.get(queued.id).first_dispatch_at is None
    assert store.observe(StartupReceipt(**receipt, phase="input_dispatched"))
    dispatched_at = store.get(queued.id).first_dispatch_at
    assert dispatched_at is not None
    assert store.observe(StartupReceipt(**receipt, phase="input_dispatched"))
    assert store.get(queued.id).first_dispatch_at == dispatched_at
    assert store.expire_observations(host, now=dispatched_at + 120)[0].reason == (
        "input_observation_pending"
    )
    expired = store.expire_observations(host, now=dispatched_at + 150)[0]
    assert (expired.state, expired.reason, expired.occupied, expired.resident) == (
        "uncertain",
        "input_start_unconfirmed",
        True,
        True,
    )
    assert store.unconfirmed_startups(host) == 1
    assert store.presentation(queued.session_id).unconfirmed_startups == 1
    assert store.presentation(queued.session_id).input_elapsed_seconds is not None
    for phase in ("runtime_ready", "tools_ready", "input_accepted", "work_started"):
        assert store.observe(StartupReceipt(**receipt, phase=phase))
    assert store.get(queued.id).state == "working"
    assert store.unconfirmed_startups(host) == 0


def test_readiness_deadline_does_not_restart_on_progress_and_legacy_retains_permit(db_uri):
    store, host = SessionStartupStore(db_uri), uuid4().hex
    store.configure(host, host_resident_limit=2, deployment_resident_limit=2)
    first = store.enqueue(
        owner_user_id="owner",
        host_id=host,
        session_id=uuid4().hex,
        operation_key="readiness",
        input_id=None,
    )
    second = store.enqueue(
        owner_user_id="owner",
        host_id=host,
        session_id=uuid4().hex,
        operation_key="legacy",
        input_id=None,
    )
    admitted = store.reserve(host)
    legacy = store.reserve(host)
    assert admitted.id == first.id and legacy.id == second.id
    assert store.claim(first.id, admitted.attempt_id, runtime_id="runtime")
    assert store.observe(
        StartupReceipt(
            startup_id=first.id,
            attempt_id=admitted.attempt_id,
            runtime_id="runtime",
            source="runner",
            phase="runtime_ready",
        )
    )
    with store._session("seed_legacy_startup_without_admission_origin") as db:
        db.get(SqlSessionStartup, (current_workspace_id(), legacy.id)).admitted_at = None
    changed = store.expire_observations(host, now=admitted.admitted_at + 180)
    assert (store.get(first.id).state, store.get(first.id).reason) == (
        "verifying",
        "runtime_observation_pending",
    )
    assert (store.get(legacy.id).state, store.get(legacy.id).reason) == (
        "uncertain",
        "admission_origin_unknown",
    )
    assert {item.id for item in changed} == {first.id, legacy.id}
    final = store.expire_observations(host, now=admitted.admitted_at + 210)
    assert [(item.id, item.state, item.reason, item.occupied) for item in final] == [
        (first.id, "uncertain", "runtime_readiness_unconfirmed", True)
    ]
    assert store.reserve(host) is None
    assert store.unconfirmed_startups(host) == 2


def test_input_dispatched_observation_requires_vendor_identity():
    attempt = StartupAttemptRef(
        startup_id=uuid4().hex,
        attempt_id=uuid4().hex,
        input_id=uuid4().hex,
        runtime_id="runtime",
        host_generation="generation",
    )
    with pytest.raises(ValidationError, match="vendor turn identity"):
        StartupObservation(attempt=attempt, phase="input_dispatched", source="runner")
    observation = StartupObservation(
        attempt=attempt,
        phase="input_dispatched",
        source="codex-native",
        vendor_session_id="thread",
        vendor_turn_id="turn",
    )
    assert observation.receipt().phase == "input_dispatched"
