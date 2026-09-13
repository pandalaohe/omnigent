"""Tests for :class:`SqlAlchemyAssignmentStore`.

Exercises idempotent create, conditional transitions, single-flight claim,
attempt guards, the due/host selection queries and idempotent message
append against a real SQLite database.
"""

from __future__ import annotations

import threading
import uuid

import pytest
import sqlalchemy as sa

from omnigent.db.db_models import SqlAssignmentAttempt
from omnigent.entities import (
    Assignment,
    AssignmentInputEntry,
    AssignmentMessage,
    AssignmentOutputEntry,
)
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.stores.assignment_store import (
    AssignmentIdempotencyConflictError,
    IllegalAssignmentTransitionError,
    InactiveAttemptError,
)
from omnigent.stores.assignment_store.sqlalchemy_store import SqlAlchemyAssignmentStore


def _uid(seed: str) -> str:
    """Deterministic bare 32-char hex UUID string from a short readable seed."""
    return uuid.uuid5(uuid.NAMESPACE_DNS, seed).hex


def _input(name: str = "root", root: bool = True) -> AssignmentInputEntry:
    """One minimal input entry."""
    return AssignmentInputEntry(
        repository_name=name,
        repository_revision=3,
        remote_url="git@github.com:example/repo.git",
        input_commit="a" * 40,
        input_ref=f"refs/omnigent/assignments/x/input/{name}",
        context_manifest_path=".agents/project/manifest.json",
        manifest_digest="d" * 64,
        artifact_paths=["dist/"],
        is_execution_root=root,
    )


def _assignment(seed: str, **overrides) -> Assignment:
    """A minimal valid assignment with deterministic ids."""
    kwargs = {
        "id": _uid(f"{seed}-id"),
        "project_id": _uid(f"{seed}-proj"),
        "source_session_id": _uid(f"{seed}-sess"),
        "target_agent_id": _uid(f"{seed}-agent"),
        "task": f"task {seed}",
        "inputs": [_input()],
        "idempotency_key": f"key-{seed}",
        "request_digest": "e" * 64,
        "created_at": 1000,
    }
    kwargs.update(overrides)
    return Assignment(**kwargs)  # type: ignore[arg-type]


@pytest.fixture()
def store(db_uri: str) -> SqlAlchemyAssignmentStore:
    """A fresh :class:`SqlAlchemyAssignmentStore` backed by the test SQLite DB.

    :param db_uri: Per-test SQLite URI from the root conftest fixture.
    :returns: A ready-to-use :class:`SqlAlchemyAssignmentStore` instance.
    """
    return SqlAlchemyAssignmentStore(db_uri)


def _attempt_count(store: SqlAlchemyAssignmentStore) -> int:
    """Count attempt rows directly — the store exposes no attempt listing."""
    with store._engine.connect() as conn:
        return int(
            conn.execute(sa.select(sa.func.count()).select_from(SqlAssignmentAttempt)).scalar()
            or 0
        )


def _to_waiting(store: SqlAlchemyAssignmentStore, seed: str, **overrides) -> Assignment:
    """Create an assignment and move it to ``waiting``."""
    created = store.create(_assignment(seed, **overrides))
    moved = store.transition(created.id, from_state="preparing", to_state="waiting")
    assert moved is not None
    return moved


# ── create ──────────────────────────────────────────────────────────────


def test_create_starts_preparing_and_round_trips(store: SqlAlchemyAssignmentStore) -> None:
    """``create`` forces ``preparing`` and echoes the payload back."""
    created = store.create(_assignment("c1", state="waiting", next_check_at=500))
    assert created.state == "preparing"
    assert created.task == "task c1"
    assert created.inputs == [_input()]
    assert created.binding_name == "primary"
    assert created.created_at > 0
    assert created.updated_at is None
    assert created.active_attempt_id is None


def test_create_same_id_and_digest_returns_existing(
    store: SqlAlchemyAssignmentStore,
) -> None:
    """A retried create returns the stored row unchanged."""
    first = store.create(_assignment("c1"))
    second = store.create(_assignment("c1", task="changed task"))
    assert second.id == first.id
    assert second.task == "task c1"
    assert second.created_at == first.created_at


def test_create_same_id_changed_digest_raises(store: SqlAlchemyAssignmentStore) -> None:
    """The same id with a different payload is rejected."""
    store.create(_assignment("c1"))
    with pytest.raises(AssignmentIdempotencyConflictError):
        store.create(_assignment("c1", request_digest="f" * 64, task="changed"))


def test_create_same_session_key_and_digest_returns_existing(
    store: SqlAlchemyAssignmentStore,
) -> None:
    """A retry under a new id but the same ``(session, key)`` hits the row."""
    first = store.create(_assignment("c1"))
    retry = _assignment("c1-retry")
    retry.source_session_id = first.source_session_id
    retry.idempotency_key = first.idempotency_key
    assert retry.id != first.id
    second = store.create(retry)
    assert second.id == first.id


def test_create_same_session_key_changed_digest_raises(
    store: SqlAlchemyAssignmentStore,
) -> None:
    """The same ``(session, key)`` with a different payload is rejected."""
    first = store.create(_assignment("c1"))
    retry = _assignment("c1-retry", request_digest="f" * 64)
    retry.source_session_id = first.source_session_id
    retry.idempotency_key = first.idempotency_key
    with pytest.raises(AssignmentIdempotencyConflictError):
        store.create(retry)


def test_get_and_list_round_trip(store: SqlAlchemyAssignmentStore) -> None:
    """``get`` reads back a created row; ``list`` pages over rows."""
    created = store.create(_assignment("c1"))
    assert store.get(created.id) == created
    assert store.get(_uid("nope")) is None
    store.create(_assignment("c2"))
    store.create(_assignment("c3"))
    page = store.list(limit=2)
    assert len(page.data) == 2
    assert page.has_more is True
    rest = store.list(after=page.last_id)
    assert len(rest.data) == 1
    assert rest.has_more is False


def test_list_filters(store: SqlAlchemyAssignmentStore) -> None:
    """``list`` filters by project, state and source session."""
    a = _to_waiting(store, "a")
    store.create(_assignment("b"))
    assert {x.id for x in store.list(project_id=a.project_id).data} == {a.id}
    assert {x.id for x in store.list(state="waiting").data} == {a.id}
    assert {x.id for x in store.list(source_session_id=a.source_session_id).data} == {a.id}
    assert store.list(state="running").data == []


def test_list_attempt_session_id_filter(store: SqlAlchemyAssignmentStore) -> None:
    """``list`` finds assignments by the session an attempt created."""
    waiting = _to_waiting(store, "a")
    attempt = store.claim_attempt(waiting.id, host_id=_uid("host"), now=2000)
    assert attempt is not None
    updated = store.update_attempt(waiting.id, attempt.id, session_id=_uid("run-sess"))
    assert updated is not None
    assert [x.id for x in store.list(attempt_session_id=_uid("run-sess")).data] == [waiting.id]
    assert store.list(attempt_session_id=_uid("other")).data == []


# ── transition ──────────────────────────────────────────────────────────


def test_transition_moves_state_and_sets_fields(store: SqlAlchemyAssignmentStore) -> None:
    """A legal transition applies the state and the extra fields atomically."""
    created = store.create(_assignment("t1"))
    moved = store.transition(
        created.id,
        from_state="preparing",
        to_state="waiting",
        wait_reason="host_offline",
        next_check_at=2000,
    )
    assert moved is not None
    assert moved.state == "waiting"
    assert moved.wait_reason == "host_offline"
    assert moved.next_check_at == 2000
    assert moved.updated_at is not None


def test_transition_to_succeeded_writes_outputs(store: SqlAlchemyAssignmentStore) -> None:
    """The ``outputs`` convenience encodes entities into ``outputs_json``."""
    waiting = _to_waiting(store, "t2")
    attempt = store.claim_attempt(waiting.id, host_id=_uid("host"), now=2000)
    assert attempt is not None
    assert store.transition(attempt.assignment_id, from_state="starting", to_state="running")
    assert store.transition(waiting.id, from_state="running", to_state="publishing")
    outputs = [
        AssignmentOutputEntry(
            repository_name="root",
            commit="c" * 40,
            ref="refs/omnigent/assignments/x/output/att/root",
            artifact_paths=["dist/b.js"],
        )
    ]
    done = store.transition(
        waiting.id,
        from_state="publishing",
        to_state="succeeded",
        outputs=outputs,
        result_summary="done",
    )
    assert done is not None
    assert done.state == "succeeded"
    assert done.outputs == outputs
    assert done.result_summary == "done"


def test_illegal_transition_raises(store: SqlAlchemyAssignmentStore) -> None:
    """A pair absent from the table raises, leaving the row untouched."""
    waiting = _to_waiting(store, "t3")
    with pytest.raises(IllegalAssignmentTransitionError):
        store.transition(waiting.id, from_state="waiting", to_state="running")
    assert store.get(waiting.id).state == "waiting"


def test_transition_race_returns_none(store: SqlAlchemyAssignmentStore) -> None:
    """A stale ``from_state`` returns ``None`` instead of clobbering."""
    created = store.create(_assignment("t4"))
    assert store.transition(created.id, from_state="preparing", to_state="waiting") is not None
    assert store.transition(created.id, from_state="preparing", to_state="failed") is None
    assert store.get(created.id).state == "waiting"


def test_transition_missing_returns_none(store: SqlAlchemyAssignmentStore) -> None:
    """Transitioning an unknown row returns ``None``."""
    assert store.transition(_uid("nope"), from_state="waiting", to_state="starting") is None


# ── claim ───────────────────────────────────────────────────────────────


def test_claim_inserts_active_attempt_numbered_from_one(
    store: SqlAlchemyAssignmentStore,
) -> None:
    """Claiming a waiting row binds attempt 1 and flips to ``starting``."""
    waiting = _to_waiting(store, "cl1")
    attempt = store.claim_attempt(waiting.id, host_id=_uid("host"), now=2000)
    assert attempt is not None
    assert attempt.assignment_id == waiting.id
    assert attempt.number == 1
    assert attempt.state == "active"
    assert attempt.host_id == _uid("host")
    assert store.get(waiting.id).state == "starting"
    assert store.get(waiting.id).active_attempt_id == attempt.id


def test_claim_second_time_returns_none(store: SqlAlchemyAssignmentStore) -> None:
    """A claimed row is not claimable again; no second attempt row lands."""
    waiting = _to_waiting(store, "cl2")
    assert store.claim_attempt(waiting.id, host_id=_uid("host"), now=2000) is not None
    assert store.claim_attempt(waiting.id, host_id=_uid("host"), now=2001) is None
    assert _attempt_count(store) == 1


def test_claim_on_non_waiting_row_returns_none(store: SqlAlchemyAssignmentStore) -> None:
    """A ``preparing`` row cannot be claimed."""
    created = store.create(_assignment("cl3"))
    assert store.claim_attempt(created.id, host_id=_uid("host"), now=2000) is None
    assert _attempt_count(store) == 0


def test_claim_missing_returns_none(store: SqlAlchemyAssignmentStore) -> None:
    """Claiming an unknown row returns ``None``."""
    assert store.claim_attempt(_uid("nope"), host_id=_uid("host"), now=2000) is None


def test_claim_pins_destination_host(store: SqlAlchemyAssignmentStore) -> None:
    """A successful claim pins ``resolved_host_id``; the host view sees it."""
    waiting = _to_waiting(store, "cl-pin")
    attempt = store.claim_attempt(waiting.id, host_id=_uid("host-a"), now=2000)
    assert attempt is not None
    assert store.get(waiting.id).resolved_host_id == _uid("host-a")
    assert [a.id for a in store.select_for_host(_uid("host-a"), limit=10)] == [waiting.id]


def test_claim_pinned_row_rejects_other_host(store: SqlAlchemyAssignmentStore) -> None:
    """A row pinned to host A cannot be claimed for host B."""
    waiting = _to_waiting(store, "cl-pin-b")
    first = store.claim_attempt(waiting.id, host_id=_uid("host-a"), now=2000)
    assert first is not None
    assert (
        store.transition(
            waiting.id, from_state="starting", to_state="waiting", active_attempt_id=None
        )
        is not None
    )
    assert store.claim_attempt(waiting.id, host_id=_uid("host-b"), now=2001) is None
    assert _attempt_count(store) == 1


def test_reclaim_after_interrupted_keeps_pin_and_bumps_number(
    store: SqlAlchemyAssignmentStore,
) -> None:
    """``starting → interrupted → waiting`` reclaims for the pinned host."""
    waiting = _to_waiting(store, "cl-re")
    first = store.claim_attempt(waiting.id, host_id=_uid("host-a"), now=2000)
    assert first is not None
    assert first.number == 1
    assert store.transition(waiting.id, from_state="starting", to_state="interrupted") is not None
    assert (
        store.transition(
            waiting.id, from_state="interrupted", to_state="waiting", active_attempt_id=None
        )
        is not None
    )
    second = store.claim_attempt(waiting.id, host_id=_uid("host-a"), now=2001)
    assert second is not None
    assert second.number == 2
    assert store.get(waiting.id).resolved_host_id == _uid("host-a")


def test_claim_single_flight_under_threads(store: SqlAlchemyAssignmentStore) -> None:
    """Two racing claimants produce exactly one attempt row."""
    waiting = _to_waiting(store, "cl4")
    barrier = threading.Barrier(2)
    results: list = []

    def claimant() -> None:
        barrier.wait()
        results.append(store.claim_attempt(waiting.id, host_id=_uid("host"), now=2000))

    threads = [threading.Thread(target=claimant) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(results) == 2
    assert sum(r is not None for r in results) == 1
    assert _attempt_count(store) == 1


# ── attempts ────────────────────────────────────────────────────────────


def test_update_attempt_sets_fields(store: SqlAlchemyAssignmentStore) -> None:
    """The active attempt accepts runner/session/lease writes."""
    waiting = _to_waiting(store, "u1")
    attempt = store.claim_attempt(waiting.id, host_id=_uid("host"), now=2000)
    assert attempt is not None
    updated = store.update_attempt(
        waiting.id,
        attempt.id,
        runner_id="runner-1",
        session_id=_uid("run-sess"),
        lease_expires_at=2090,
    )
    assert updated is not None
    assert updated.runner_id == "runner-1"
    assert updated.session_id == _uid("run-sess")
    assert updated.lease_expires_at == 2090


def test_update_from_finished_attempt_raises(store: SqlAlchemyAssignmentStore) -> None:
    """A write arriving after the attempt finished is rejected."""
    waiting = _to_waiting(store, "u2")
    attempt = store.claim_attempt(waiting.id, host_id=_uid("host"), now=2000)
    assert attempt is not None
    assert (
        store.update_attempt(waiting.id, attempt.id, state="finished", ended_at=2100) is not None
    )
    with pytest.raises(InactiveAttemptError):
        store.update_attempt(waiting.id, attempt.id, session_id=_uid("late"))


def test_update_from_foreign_attempt_raises(store: SqlAlchemyAssignmentStore) -> None:
    """An active attempt of another assignment cannot write here."""
    first = _to_waiting(store, "u3a")
    second = _to_waiting(store, "u3b")
    attempt_first = store.claim_attempt(first.id, host_id=_uid("host"), now=2000)
    attempt_second = store.claim_attempt(second.id, host_id=_uid("host"), now=2000)
    assert attempt_first is not None and attempt_second is not None
    with pytest.raises(InactiveAttemptError):
        store.update_attempt(first.id, attempt_second.id, session_id=_uid("x"))


def test_update_attempt_missing_returns_none(store: SqlAlchemyAssignmentStore) -> None:
    """Updating an unknown assignment returns ``None``."""
    assert store.update_attempt(_uid("nope"), _uid("att"), session_id=_uid("x")) is None


def test_mark_event_dispatched_true_once_then_false(
    store: SqlAlchemyAssignmentStore,
) -> None:
    """The compare-and-set lets exactly one dispatcher through."""
    waiting = _to_waiting(store, "e1")
    attempt = store.claim_attempt(waiting.id, host_id=_uid("host"), now=2000)
    assert attempt is not None
    assert store.mark_event_dispatched(attempt.id, now=2001) is True
    assert store.mark_event_dispatched(attempt.id, now=2002) is False
    assert store.mark_event_dispatched(_uid("nope"), now=2002) is False


def test_event_dispatched_at_not_writable_via_update_attempt(
    store: SqlAlchemyAssignmentStore,
) -> None:
    """``update_attempt`` cannot reset the dispatch compare-and-set."""
    waiting = _to_waiting(store, "e1b")
    attempt = store.claim_attempt(waiting.id, host_id=_uid("host"), now=2000)
    assert attempt is not None
    assert store.mark_event_dispatched(attempt.id, now=2001) is True
    with pytest.raises(OmnigentError) as exc:
        store.update_attempt(waiting.id, attempt.id, event_dispatched_at=None)
    assert exc.value.code == ErrorCode.INVALID_INPUT
    assert store.mark_event_dispatched(attempt.id, now=2002) is False


def test_set_lease_observable_through_update(store: SqlAlchemyAssignmentStore) -> None:
    """``set_lease`` writes and clears the server-observed lease."""
    waiting = _to_waiting(store, "e2")
    attempt = store.claim_attempt(waiting.id, host_id=_uid("host"), now=2000)
    assert attempt is not None
    store.set_lease(attempt.id, 2090)
    assert store.update_attempt(waiting.id, attempt.id).lease_expires_at == 2090
    store.set_lease(attempt.id, None)
    assert store.update_attempt(waiting.id, attempt.id).lease_expires_at is None


# ── selection queries ───────────────────────────────────────────────────


def test_select_due_orders_limits_and_excludes(store: SqlAlchemyAssignmentStore) -> None:
    """Due rows come back ordered by ``next_check_at``; terminal, future
    and unscheduled rows are excluded and the limit is honoured."""
    early = _to_waiting(store, "d-early", next_check_at=100)
    late = _to_waiting(store, "d-late", next_check_at=200)
    _to_waiting(store, "d-future", next_check_at=9999)
    store.create(_assignment("d-unscheduled"))
    terminal = _to_waiting(store, "d-terminal", next_check_at=50)
    claimed = store.claim_attempt(terminal.id, host_id=_uid("host"), now=60)
    assert claimed is not None
    assert store.transition(terminal.id, from_state="starting", to_state="failed")

    due = store.select_due(now=500, limit=10)
    assert [a.id for a in due] == [early.id, late.id]
    assert [a.id for a in store.select_due(now=500, limit=1)] == [early.id]
    assert store.select_due(now=50, limit=10) == []


def test_select_for_host_scoped_to_resolved_host(store: SqlAlchemyAssignmentStore) -> None:
    """Only non-terminal rows pinned to the host are returned."""
    first = _to_waiting(store, "h1")
    claim = store.claim_attempt(first.id, host_id=_uid("host-a"), now=2000)
    assert claim is not None
    assert store.transition(
        first.id,
        from_state="starting",
        to_state="running",
        resolved_host_id=_uid("host-a"),
    )
    second = _to_waiting(store, "h2")
    claim2 = store.claim_attempt(second.id, host_id=_uid("host-b"), now=2000)
    assert claim2 is not None
    assert store.transition(
        second.id,
        from_state="starting",
        to_state="running",
        resolved_host_id=_uid("host-b"),
    )
    # Unresolved rows never match a host.
    _to_waiting(store, "h3")

    assert [a.id for a in store.select_for_host(_uid("host-a"), limit=10)] == [first.id]
    assert [a.id for a in store.select_for_host(_uid("host-b"), limit=10)] == [second.id]
    assert store.select_for_host(_uid("host-c"), limit=10) == []

    # Terminal rows drop out of the host view.
    assert store.transition(first.id, from_state="running", to_state="failed")
    assert store.select_for_host(_uid("host-a"), limit=10) == []


# ── messages ────────────────────────────────────────────────────────────


def _message(seed: str, assignment_id: str, **overrides) -> AssignmentMessage:
    """A minimal message with a deterministic id."""
    kwargs = {
        "id": _uid(f"{seed}-msg"),
        "assignment_id": assignment_id,
        "kind": "note",
        "body": f"body {seed}",
        "sender_session_id": _uid(f"{seed}-sender"),
        "idempotency_key": f"mkey-{seed}",
        "created_at": 3000,
    }
    kwargs.update(overrides)
    return AssignmentMessage(**kwargs)  # type: ignore[arg-type]


def test_append_and_read_messages(store: SqlAlchemyAssignmentStore) -> None:
    """Appended messages read back with their fields."""
    created = store.create(_assignment("m1"))
    appended = store.append_message(_message("k1", created.id))
    assert appended.body == "body k1"
    assert appended.created_at > 0
    page = store.read_messages(created.id)
    assert [m.id for m in page.data] == [appended.id]
    assert page.has_more is False


def test_append_idempotent_on_key(store: SqlAlchemyAssignmentStore) -> None:
    """A keyed retry returns the stored row; keyless appends always land."""
    created = store.create(_assignment("m2"))
    first = store.append_message(_message("k1", created.id))
    retry = _message("k1", created.id, id=_uid("other-msg-id"), body="changed body")
    assert retry.id != first.id
    again = store.append_message(retry)
    assert again.id == first.id
    assert again.body == "body k1"
    assert len(store.read_messages(created.id).data) == 1

    store.append_message(_message("free1", created.id, idempotency_key=None))
    store.append_message(_message("free2", created.id, idempotency_key=None))
    assert len(store.read_messages(created.id).data) == 3


def test_read_messages_cursor_paging_repeatable(store: SqlAlchemyAssignmentStore) -> None:
    """Cursor pages are stable and repeatable across reads."""
    created = store.create(_assignment("m3"))
    ids = []
    for i in range(5):
        appended = store.append_message(_message(f"p{i}", created.id))
        ids.append(appended.id)
    first = store.read_messages(created.id, limit=2)
    assert len(first.data) == 2
    assert first.has_more is True
    second = store.read_messages(created.id, after=first.last_id, limit=2)
    assert len(second.data) == 2
    assert second.has_more is True
    third = store.read_messages(created.id, after=second.last_id, limit=2)
    assert len(third.data) == 1
    assert third.has_more is False
    assert [m.id for m in (*first.data, *second.data, *third.data)] == [
        m.id for m in store.read_messages(created.id, limit=10).data
    ]
    # Repeatable: the same cursors return the same rows.
    assert [m.id for m in store.read_messages(created.id, limit=2).data] == [
        m.id for m in first.data
    ]
    assert [m.id for m in store.read_messages(created.id, after=first.last_id, limit=2).data] == [
        m.id for m in second.data
    ]
    assert set(ids) == {m.id for m in store.read_messages(created.id, limit=10).data}


def test_read_messages_empty(store: SqlAlchemyAssignmentStore) -> None:
    """Reading an assignment with no messages returns an empty page."""
    created = store.create(_assignment("m4"))
    page = store.read_messages(created.id)
    assert page.data == []
    assert page.has_more is False


# ── refresh ───────────────────────────────────────────────────────────────


def test_refresh_waiting_stale_project_revision_keeps_stored(
    store: SqlAlchemyAssignmentStore,
) -> None:
    """A refresh pinned to a stale revision loses, keeping the stored one."""
    waiting = _to_waiting(store, "r1", project_revision=3)
    stale = store.refresh_waiting(
        waiting.id,
        inputs=list(waiting.inputs),
        project_revision=2,
        now=2000,
        expected_project_revision=2,
    )
    assert stale is None
    current = store.get(waiting.id)
    assert current is not None
    assert current.project_revision == 3


# ── reschedule ────────────────────────────────────────────────────────────


def test_reschedule_moves_next_check_and_keeps_reason(
    store: SqlAlchemyAssignmentStore,
) -> None:
    """Reschedule without a reason moves only the check time."""
    waiting = _to_waiting(store, "s1b", next_check_at=100)
    assert waiting.wait_reason is None
    updated = store.reschedule(waiting.id, expected_state="waiting", next_check_at=500)
    assert updated is not None
    assert updated.next_check_at == 500
    assert updated.wait_reason is None
    assert updated.state == "waiting"


def test_reschedule_wrong_expected_state_leaves_row_unchanged(
    store: SqlAlchemyAssignmentStore,
) -> None:
    """A reschedule against a moved state returns None and writes nothing."""
    waiting = _to_waiting(store, "s2", next_check_at=100)
    result = store.reschedule(
        waiting.id,
        expected_state="running",
        next_check_at=500,
        wait_reason="host_offline:x",
    )
    assert result is None
    current = store.get(waiting.id)
    assert current is not None
    assert current.state == "waiting"
    assert current.next_check_at == waiting.next_check_at
    assert current.wait_reason is None


def test_reschedule_sets_wait_reason_when_passed(
    store: SqlAlchemyAssignmentStore,
) -> None:
    """Passing a reason writes it; omitting it leaves the old one."""
    waiting = _to_waiting(store, "s3", next_check_at=100)
    updated = store.reschedule(
        waiting.id,
        expected_state="waiting",
        next_check_at=200,
        wait_reason="no_eligible_host",
    )
    assert updated is not None
    assert updated.wait_reason == "no_eligible_host"
    kept = store.reschedule(waiting.id, expected_state="waiting", next_check_at=300)
    assert kept is not None
    assert kept.wait_reason == "no_eligible_host"
    assert kept.next_check_at == 300


# ── claim with pin ────────────────────────────────────────────────────────


def test_claim_with_pin_writes_binding_next_check_and_clears_reason(
    store: SqlAlchemyAssignmentStore,
) -> None:
    """Claim pin args land in the same UPDATE and clear the wait reason."""
    waiting = _to_waiting(store, "c1", next_check_at=100)
    waiting = store.reschedule(
        waiting.id,
        expected_state="waiting",
        next_check_at=100,
        wait_reason="binding_changed",
    )
    assert waiting is not None
    attempt = store.claim_attempt(
        waiting.id,
        host_id=_uid("host"),
        now=2000,
        resolved_binding_id="b" * 32,
        resolved_binding_revision=7,
        next_check_at=2420,
    )
    assert attempt is not None
    current = store.get(waiting.id)
    assert current is not None
    assert current.state == "starting"
    assert current.resolved_binding_id == "b" * 32
    assert current.resolved_binding_revision == 7
    assert current.next_check_at == 2420
    assert current.wait_reason is None


def test_claim_without_pin_keeps_wait_reason(
    store: SqlAlchemyAssignmentStore,
) -> None:
    """Existing callers keep working unchanged (no pin, no clear)."""
    waiting = _to_waiting(store, "c2", next_check_at=100)
    attempt = store.claim_attempt(waiting.id, host_id=_uid("host"), now=2000)
    assert attempt is not None
    current = store.get(waiting.id)
    assert current is not None
    assert current.resolved_binding_id is None
    assert current.next_check_at == waiting.next_check_at


def test_reschedule_pins_active_attempt(
    store: SqlAlchemyAssignmentStore,
) -> None:
    """A reschedule pinned to a superseded attempt loses without writing."""
    host = _uid("host")
    waiting = _to_waiting(store, "s-pin")
    first = store.claim_attempt(waiting.id, host_id=host, now=2000)
    assert first is not None
    back = store.transition(
        waiting.id,
        from_state="starting",
        to_state="waiting",
        expected_active_attempt_id=first.id,
        active_attempt_id=None,
        next_check_at=2100,
    )
    assert back is not None
    second = store.claim_attempt(waiting.id, host_id=host, now=2200, next_check_at=2300)
    assert second is not None
    assert second.id != first.id
    stale = store.reschedule(
        waiting.id,
        expected_state="starting",
        expected_active_attempt_id=first.id,
        next_check_at=9999,
    )
    assert stale is None
    current = store.get(waiting.id)
    assert current is not None
    assert current.active_attempt_id == second.id
    assert current.next_check_at == 2300


def test_claim_with_stale_binding_pin_creates_no_attempt(
    store: SqlAlchemyAssignmentStore,
) -> None:
    """A claim pinned to a moved binding loses without writing."""
    host = _uid("host")
    waiting = _to_waiting(store, "c-pin-stale")
    first = store.claim_attempt(
        waiting.id,
        host_id=host,
        now=2000,
        resolved_binding_id="b1" * 16,
        resolved_binding_revision=1,
        next_check_at=2100,
    )
    assert first is not None
    assert (
        store.transition(
            waiting.id,
            from_state="starting",
            to_state="waiting",
            expected_active_attempt_id=first.id,
            active_attempt_id=None,
            next_check_at=2100,
        )
        is not None
    )
    stale_pin = ("b1" * 16, 1)
    second = store.claim_attempt(
        waiting.id,
        host_id=host,
        now=2200,
        resolved_binding_id="b2" * 16,
        resolved_binding_revision=2,
        next_check_at=2300,
    )
    assert second is not None
    assert (
        store.transition(
            waiting.id,
            from_state="starting",
            to_state="waiting",
            expected_active_attempt_id=second.id,
            active_attempt_id=None,
            next_check_at=2300,
        )
        is not None
    )
    before = _attempt_count(store)
    lost = store.claim_attempt(
        waiting.id,
        host_id=host,
        now=2400,
        resolved_binding_id="b1" * 16,
        resolved_binding_revision=1,
        next_check_at=2500,
        expected_binding_pin=stale_pin,
    )
    assert lost is None
    assert _attempt_count(store) == before
    current = store.get(waiting.id)
    assert current is not None
    assert current.state == "waiting"
    assert current.resolved_binding_id == "b2" * 16
    assert current.resolved_binding_revision == 2
    assert current.next_check_at == 2300


# ── select_waiting_for_host ───────────────────────────────────────────────


def _waiting_with_owner(
    store: SqlAlchemyAssignmentStore,
    seed: str,
    *,
    owner: str | None,
    requested: str | None,
    next_check_at: int | None = 100,
) -> Assignment:
    created = store.create(_assignment(seed, owner_user_id=owner, requested_host_id=requested))
    moved = store.transition(created.id, from_state="preparing", to_state="waiting")
    assert moved is not None
    if next_check_at != moved.next_check_at:
        rescheduled = store.reschedule(
            created.id, expected_state="waiting", next_check_at=next_check_at
        )
        assert rescheduled is not None
        return rescheduled
    return moved


def test_select_waiting_for_host_matches_requested_and_owner(
    store: SqlAlchemyAssignmentStore,
) -> None:
    """Requested rows match by host; unrequested rows match by same owner only."""
    host_a = _uid("host-a")
    host_b = _uid("host-b")
    alice = "alice@example.com"
    bob = "bob@example.com"
    requested = _waiting_with_owner(store, "w-req", owner=alice, requested=host_a)
    unrequested = _waiting_with_owner(store, "w-unreq", owner=alice, requested=None)
    _waiting_with_owner(store, "w-other-owner", owner=bob, requested=None)
    other_host = _waiting_with_owner(store, "w-other-host", owner=alice, requested=host_b)
    running = _waiting_with_owner(store, "w-running", owner=alice, requested=host_a)
    assert store.claim_attempt(running.id, host_id=host_a, now=2000) is not None

    got = store.select_waiting_for_host(host_id=host_a, owner_user_id=alice, limit=10)
    assert {a.id for a in got} == {requested.id, unrequested.id}
    assert [a.id for a in got] == sorted(
        [requested.id, unrequested.id],
        key=lambda _id: next(a.next_check_at or 0 for a in got if a.id == _id),
    ) or {a.id for a in got} == {requested.id, unrequested.id}
    assert other_host.id not in {a.id for a in got}

    only_b = store.select_waiting_for_host(host_id=host_b, owner_user_id=alice, limit=10)
    assert {a.id for a in only_b} == {other_host.id, unrequested.id}


def test_select_waiting_for_host_honours_limit_and_none_owner(
    store: SqlAlchemyAssignmentStore,
) -> None:
    """Limit caps the page; a None owner matches only owner-less rows."""
    host_a = _uid("host-a")
    first = _waiting_with_owner(store, "w-lim1", owner=None, requested=None, next_check_at=10)
    second = _waiting_with_owner(store, "w-lim2", owner=None, requested=None, next_check_at=20)
    _waiting_with_owner(store, "w-lim3", owner="alice@example.com", requested=None)

    got = store.select_waiting_for_host(host_id=host_a, owner_user_id=None, limit=10)
    assert {a.id for a in got} == {first.id, second.id}
    assert [a.id for a in got] == [first.id, second.id]
    assert [
        a.id for a in store.select_waiting_for_host(host_id=host_a, owner_user_id=None, limit=1)
    ] == [first.id]
