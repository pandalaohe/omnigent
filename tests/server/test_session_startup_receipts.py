"""Only exact post-gate input and execution receipts release startup capacity."""

from uuid import uuid4

import pytest

from omnigent.entities.session_startup import StartupReceipt
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
