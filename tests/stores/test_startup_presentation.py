"""Startup projections and cancellation use the current durable attempt."""

from uuid import uuid4

from omnigent.db.db_models import workspace_scope
from omnigent.stores.session_startup_store import SessionStartupStore


def test_startup_projection_keeps_evidence_separate_and_estimates_queue_order(db_uri):
    store = SessionStartupStore(db_uri)
    host, sessions = uuid4().hex, [uuid4().hex, uuid4().hex]
    store.configure(host, host_resident_limit=7, deployment_resident_limit=20)
    handles = [
        store.enqueue(
            owner_user_id="owner",
            host_id=host,
            session_id=session,
            operation_key=session,
            input_id=None,
        )
        for session in sessions
    ]
    status = store.presentation(sessions[1])
    assert status.state == "queued"
    assert status.queue_position_estimate == 2
    assert status.elapsed_seconds >= 0
    assert not status.evidence.input_accepted and not status.evidence.work_started
    assert store.presentation(uuid4().hex) is None
    with workspace_scope(42):
        assert store.presentation(sessions[1]) is None
    attempt = store.reserve(host)
    assert attempt.id == handles[0].id
    assert store.cancel_current(attempt.id, expected_attempt_id=None) is None
    assert store.get(attempt.id).state == "starting"
    cancelled = store.cancel_current(attempt.id, expected_attempt_id=attempt.attempt_id)
    assert cancelled.state == "cancelling"
    assert not store.claim(attempt.id, attempt.attempt_id, runtime_id="late")
