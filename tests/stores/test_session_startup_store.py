"""Durable admission counts unresolved starts across server replicas."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from omnigent.db.account_authority import AccountAuthority
from omnigent.db.db_models import (
    SqlConversationMetadata,
    SqlUser,
    current_workspace_id,
    workspace_scope,
)
from omnigent.db.session_startup import SqlSessionStartup, SqlStartupBudget
from omnigent.entities.session_startup import StartupReceipt
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.stores.session_startup_store import SessionStartupStore


def enqueue(store, host, *, owner="owner", session=None, operation=None, parent=None):
    return store.enqueue(
        owner_user_id=owner,
        host_id=host,
        session_id=session or uuid4().hex,
        operation_key=operation or uuid4().hex,
        input_id=uuid4().hex,
        parent_startup_id=parent,
    )


def test_two_replicas_reserve_only_two_and_restart_retains_permits(db_uri):
    host = uuid4().hex
    stores = [SessionStartupStore(db_uri), SessionStartupStore(db_uri)]
    stores[0].configure(host, host_resident_limit=24, deployment_resident_limit=64)
    for _ in range(6):
        enqueue(stores[0], host)
    with ThreadPoolExecutor(max_workers=2) as pool:
        starts = list(pool.map(lambda index: stores[index % 2].reserve(host), range(6)))
    assert len([start for start in starts if start is not None]) == 2
    assert SessionStartupStore(db_uri).reserve(host) is None
    assert stores[0].reserve(uuid4().hex) is None


@pytest.mark.parametrize("with_input", [False, True])
def test_queue_settlement_releases_without_claiming_vendor_acceptance_or_work(db_uri, with_input):
    store, host = SessionStartupStore(db_uri), uuid4().hex
    store.configure(host, mode="startup_only")
    queued = store.enqueue(
        owner_user_id="owner",
        host_id=host,
        session_id=uuid4().hex,
        operation_key="opening",
        input_id=uuid4().hex if with_input else None,
    )
    attempt = store.reserve(host)
    assert store.claim(queued.id, attempt.attempt_id, runtime_id="runtime")
    receipt = StartupReceipt(
        startup_id=queued.id,
        attempt_id=attempt.attempt_id,
        input_id=queued.input_id,
        runtime_id="runtime",
        phase="opening_settled",
        source="runner",
    )
    assert store.observe(receipt)
    current = SessionStartupStore(db_uri).get(queued.id)
    assert not current.occupied and not current.input_accepted
    assert current.state == ("opened" if with_input else "ready_empty")
    assert current.proof_source is None
    assert store.observe(receipt)


@pytest.mark.parametrize("unbound", [False, True])
def test_reconnect_keeps_unreconciled_runtime_generation_unknown(db_uri, unbound):
    store, host = SessionStartupStore(db_uri), uuid4().hex
    store.configure(host, host_resident_limit=4, deployment_resident_limit=8)
    store.register_host_generation(host, "old-generation")
    queued = enqueue(store, host)
    attempt = store.reserve(host)
    assert store.claim_boundary(
        queued.id,
        attempt.attempt_id,
        runtime_id="old-runtime",
        host_generation="old-generation",
        boundary="runner_prepare",
        claim_id="old-claim",
    )
    if unbound:
        with store._session("seed_unbound_predecessor_generation") as db:
            db.get(SqlSessionStartup, (current_workspace_id(), queued.id)).host_generation = None
    old = store.get(queued.id)
    enqueue(store, host)
    store.register_host_generation(host, "new-generation")
    assert store.get(queued.id) == old
    assert store.reserve(host) is None
    reloaded = SessionStartupStore(db_uri)
    assert reloaded.get(queued.id) == old
    reloaded.register_host_generation(host, "new-generation")
    reloaded.configure(host, host_resident_limit=20, deployment_resident_limit=20)
    assert reloaded.reserve(host) is None
    assert reloaded.confirm_stopped(queued.id, attempt.attempt_id)
    assert reloaded.reserve(host) is None


@pytest.mark.parametrize(
    "population", ["fresh", "unassigned", "current", "released", "other_host", "other_workspace"]
)
def test_host_generation_does_not_veto_without_an_unreconciled_runtime(db_uri, population):
    store, host = SessionStartupStore(db_uri), uuid4().hex
    store.configure(host, host_resident_limit=4, deployment_resident_limit=8)
    store.register_host_generation(host, "old-generation")
    if population != "fresh":
        owned_host = uuid4().hex if population == "other_host" else host
        workspace = 17 if population == "other_workspace" else current_workspace_id()
        with workspace_scope(workspace):
            store.configure(owned_host, host_resident_limit=4, deployment_resident_limit=8)
            store.register_host_generation(owned_host, "old-generation")
            queued = enqueue(store, owned_host)
            attempt = store.reserve(owned_host)
            if population != "unassigned":
                assert store.claim_boundary(
                    queued.id,
                    attempt.attempt_id,
                    runtime_id="runtime",
                    host_generation="old-generation",
                    boundary="runner_prepare",
                    claim_id="claim",
                )
            if population == "released":
                assert store.confirm_stopped(queued.id, attempt.attempt_id)
    generation = "old-generation" if population == "current" else "new-generation"
    store.register_host_generation(host, generation)
    with store._session("inspect_generation_unknown_scope") as db:
        budget = db.get(SqlStartupBudget, f"{current_workspace_id()}:{host}")
        assert not budget.resident_inventory_unknown
    followup = enqueue(store, host)
    assert store.reserve(host).id == followup.id


def test_reconnect_keeps_idle_resident_runtime_unknown(db_uri):
    store, host = SessionStartupStore(db_uri), uuid4().hex
    store.configure(host, host_resident_limit=4, deployment_resident_limit=8)
    store.register_host_generation(host, "old-generation")
    queued = store.enqueue(
        owner_user_id="owner",
        host_id=host,
        session_id=uuid4().hex,
        operation_key="idle",
        input_id=None,
    )
    attempt = store.reserve(host)
    assert store.claim_boundary(
        queued.id,
        attempt.attempt_id,
        runtime_id="idle-runtime",
        host_generation="old-generation",
        boundary="runner_prepare",
        claim_id="claim",
    )
    for phase in ("runtime_ready", "tools_ready"):
        assert store.observe(
            StartupReceipt(
                startup_id=queued.id,
                attempt_id=attempt.attempt_id,
                runtime_id="idle-runtime",
                source="runner",
                phase=phase,
            )
        )
    idle = store.get(queued.id)
    assert idle.resident and not idle.occupied
    store.register_host_generation(host, "new-generation")
    assert store.get(queued.id) == idle
    enqueue(store, host)
    assert store.reserve(host) is None


def test_receipt_writes_refuse_rotated_account_in_same_transaction(db_uri):
    store = SessionStartupStore(db_uri)
    host = uuid4().hex
    store.configure(host, host_resident_limit=2, deployment_resident_limit=2)
    queued = enqueue(store, host)
    attempt = store.reserve(host)
    assert attempt is not None
    assert store.claim_boundary(
        queued.id,
        attempt.attempt_id,
        runtime_id="runtime",
        host_generation="generation",
        boundary="runner_prepare",
        claim_id="claim",
    )
    with store._session("seed_receipt_owner") as db:
        db.add(SqlUser(id="alice", account_generation="old-generation"))
    stale = AccountAuthority("alice", "old-generation", current_workspace_id())
    with store._session("rotate_receipt_owner") as db:
        db.execute(
            update(SqlUser)
            .where(SqlUser.workspace_id == current_workspace_id(), SqlUser.id == "alice")
            .values(account_generation="new-generation")
        )
    with pytest.raises(OmnigentError) as claim_error:
        store.claim_boundary(
            queued.id,
            attempt.attempt_id,
            runtime_id="runtime",
            host_generation="generation",
            boundary="runner_spawn",
            claim_id="spawn",
            account=stale,
        )
    assert claim_error.value.code == ErrorCode.UNAUTHORIZED
    with pytest.raises(OmnigentError) as bind_error:
        store.bind_dispatch(
            queued.id,
            attempt.attempt_id,
            runtime_id="runtime",
            source="codex",
            vendor_session_id="thread",
            vendor_turn_id="turn",
            account_authority=stale,
        )
    assert bind_error.value.code == ErrorCode.UNAUTHORIZED
    with pytest.raises(OmnigentError) as observe_error:
        store.observe(
            StartupReceipt(
                startup_id=queued.id,
                attempt_id=attempt.attempt_id,
                input_id=queued.input_id,
                runtime_id="runtime",
                source="runner",
                phase="runtime_ready",
            ),
            account_authority=stale,
        )
    assert observe_error.value.code == ErrorCode.UNAUTHORIZED
    snapshot = store.get(queued.id)
    assert snapshot.proof_source is None
    assert snapshot.first_dispatch_at is None
    assert snapshot.occupied
    with store._session("check_rejected_claim") as db:
        row = db.get(SqlSessionStartup, (current_workspace_id(), queued.id))
        assert "runner_spawn" not in row.cold_claims


def test_operation_replay_conflict_and_tenant_partition(db_uri):
    store = SessionStartupStore(db_uri)
    args = {
        "owner_user_id": "owner",
        "host_id": uuid4().hex,
        "session_id": uuid4().hex,
        "operation_key": "create",
        "input_id": uuid4().hex,
    }
    first = store.enqueue(**args)
    assert store.enqueue(**args) == first
    with pytest.raises(ValueError, match="operation key"):
        store.enqueue(**{**args, "input_id": uuid4().hex})
    with workspace_scope(17):
        assert store.get(first.id) is None
        assert store.enqueue(**args).id != first.id


def test_resident_limit_retains_work_and_parent_prerequisite(db_uri):
    store = SessionStartupStore(db_uri)
    host = uuid4().hex
    store.configure(host, host_resident_limit=1, deployment_resident_limit=2)
    parent = enqueue(store, host)
    child = enqueue(store, host, parent=parent.id)
    attempt = store.reserve(host)
    assert attempt is not None and attempt.id == parent.id
    assert store.reserve(host) is None
    assert store.confirm_stopped(parent.id, attempt.attempt_id)
    # Stopping the parent does not turn its dependent child into an orphan launch.
    assert store.reserve(host) is None
    assert store.get(child.id).state == "queued"


def test_cancel_fences_claim_but_does_not_free_uncertain_runtime(db_uri):
    store = SessionStartupStore(db_uri)
    host = uuid4().hex
    store.configure(host, host_resident_limit=24, deployment_resident_limit=64)
    first = enqueue(store, host)
    second = enqueue(store, host)
    enqueue(store, host)
    attempt = store.reserve(host)
    other = store.reserve(host)
    assert attempt.id == first.id and other.id == second.id
    assert store.cancel(first.id).state == "cancelling"
    assert not store.claim(first.id, attempt.attempt_id, runtime_id="runner-generation")
    assert store.reserve(host) is None
    assert not store.confirm_stopped(first.id, uuid4().hex)
    assert store.confirm_stopped(first.id, attempt.attempt_id)
    assert store.reserve(host) is not None


def test_family_round_robin_preserves_fifo_when_another_family_drains(db_uri):
    store = SessionStartupStore(db_uri)
    host = uuid4().hex
    store.configure(host, host_resident_limit=24, deployment_resident_limit=64)
    parents = [enqueue(store, host) for _ in range(3)]
    # Seed the persisted state after already-working parents were adopted.
    with store._session("seed_adopted_parents") as db:
        for parent in parents:
            db.get(SqlSessionStartup, (current_workspace_id(), parent.id)).state = "working"
    children = [
        [enqueue(store, host, parent=parent.id) for _ in range(count)]
        for parent, count in zip(parents, [3, 1, 2], strict=True)
    ]
    admitted = []
    for _ in range(6):
        start = store.reserve(host)
        admitted.append(start.id)
        store.confirm_stopped(start.id, start.attempt_id)
    assert admitted == [
        children[0][0].id,
        children[1][0].id,
        children[2][0].id,
        children[0][1].id,
        children[2][1].id,
        children[0][2].id,
    ]


def test_empty_database_budget_bootstrap_is_disabled_and_replica_safe(db_uri):
    store = SessionStartupStore(db_uri)
    with store._session("remove_test_seed") as db:
        seed = db.get(SqlStartupBudget, "deployment")
        if seed is not None:
            db.delete(seed)
    host = uuid4().hex
    stores = [SessionStartupStore(db_uri), SessionStartupStore(db_uri)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        handles = list(pool.map(lambda index: enqueue(stores[index], host), range(2)))
    assert len({handle.id for handle in handles}) == 2
    assert store.reserve(host) is None
    with store._session("read_bootstrap_sequence") as db:
        assert sorted(db.scalars(select(SqlSessionStartup.sequence))) == [1, 2]


@pytest.mark.parametrize("stop_proof", ["direct", "receipt"])
def test_first_input_reuses_empty_runtime_at_full_residency_and_stops_all_generations(
    db_uri, stop_proof
):
    store, host, session = SessionStartupStore(db_uri), uuid4().hex, uuid4().hex
    store.configure(host, host_resident_limit=1, deployment_resident_limit=1)
    empty = store.enqueue(
        owner_user_id="owner",
        host_id=host,
        session_id=session,
        operation_key="empty",
        input_id=None,
    )
    attempt = store.reserve(host)
    assert store.claim_boundary(
        empty.id,
        attempt.attempt_id,
        runtime_id="context",
        host_generation="generation",
        boundary="runner_prepare",
        claim_id="claim",
    )
    for phase in ("runtime_ready", "tools_ready"):
        assert store.observe(
            StartupReceipt(
                startup_id=empty.id,
                attempt_id=attempt.attempt_id,
                runtime_id="context",
                source="runner",
                phase=phase,
            )
        )
    other = enqueue(store, host)
    args = {
        "owner_user_id": "owner",
        "host_id": host,
        "session_id": session,
        "operation_key": "first-input",
        "input_id": uuid4().hex,
        "runtime_generation": 2,
        "reuse_startup_id": empty.id,
    }
    first = store.enqueue(**args)
    assert store.enqueue(**args).id == first.id
    assert first.runtime_id == "context"
    reserved = store.reserve(host)
    assert reserved.id == first.id
    assert reserved.runtime_generation == 2
    assert not store.claim_boundary(
        first.id,
        reserved.attempt_id,
        runtime_id="context",
        host_generation="generation",
        boundary="native_prepare",
        claim_id="cold-repair",
    )
    assert store.reserve(host) is None
    store.cancel(first.id)
    assert store.reserve(host) is None
    if stop_proof == "direct":
        assert store.confirm_stopped(first.id, reserved.attempt_id)
    else:
        assert store.observe(
            StartupReceipt(
                startup_id=first.id,
                attempt_id=reserved.attempt_id,
                input_id=first.input_id,
                runtime_id="context",
                source="runner",
                phase="stopped",
            )
        )
    assert store.reserve(host).id == other.id


@pytest.mark.parametrize("changed", ["owner", "host", "session", "generation"])
def test_runtime_reuse_rejects_foreign_or_unready_context(db_uri, changed):
    store, host, session = SessionStartupStore(db_uri), uuid4().hex, uuid4().hex
    store.configure(host, host_resident_limit=2, deployment_resident_limit=2)
    empty = store.enqueue(
        owner_user_id="owner",
        host_id=host,
        session_id=session,
        operation_key="empty",
        input_id=None,
    )
    args = {
        "owner_user_id": "other" if changed == "owner" else "owner",
        "host_id": uuid4().hex if changed == "host" else host,
        "session_id": uuid4().hex if changed == "session" else session,
        "operation_key": "first-input",
        "input_id": uuid4().hex,
        "runtime_generation": 3 if changed == "generation" else 2,
        "reuse_startup_id": empty.id,
    }
    with pytest.raises(ValueError, match="reuse"):
        store.enqueue(**args)


@pytest.mark.parametrize("claimed", [False, True])
def test_session_cancel_releases_reserved_permission_before_any_command(db_uri, claimed):
    store, host = SessionStartupStore(db_uri), uuid4().hex
    store.configure(host, host_resident_limit=2, deployment_resident_limit=2)
    first = enqueue(store, host)
    second = enqueue(store, host)
    attempt = store.reserve(host)
    if claimed:
        assert store.claim(first.id, attempt.attempt_id, runtime_id="context")
    cancelled = store.cancel_session_current(first.session_id)
    assert cancelled.state == "cancelled" and not cancelled.resident
    assert not store.claim(first.id, attempt.attempt_id, runtime_id="late")
    assert store.reserve(host).id == second.id


def test_old_queue_requires_attention_and_keeps_canonical_work(db_uri):
    store, host = SessionStartupStore(db_uri), uuid4().hex
    store.configure(host, host_resident_limit=2, deployment_resident_limit=2)
    queued = enqueue(store, host)
    with store._session("read_startup_created_at") as db:
        created_at = db.get(SqlSessionStartup, (current_workspace_id(), queued.id)).created_at
    expired = store.expire_observations(host, now=created_at + 86400)
    assert [(row.id, row.state, row.reason) for row in expired] == [
        (queued.id, "blocked", "needs_attention")
    ]
    assert store.get(queued.id).input_id == queued.input_id
    assert store.reserve(host) is None


def test_attach_parent_dependency_rechecks_current_parent_and_replay(db_uri):
    store, host = SessionStartupStore(db_uri), uuid4().hex
    store.configure(host, host_resident_limit=3, deployment_resident_limit=3)
    parent_session, child_session, child_input = uuid4().hex, uuid4().hex, uuid4().hex
    parent = store.enqueue(
        owner_user_id="owner",
        host_id=host,
        session_id=parent_session,
        operation_key="parent-1",
        input_id=uuid4().hex,
    )
    child = store.enqueue(
        owner_user_id="owner",
        host_id=host,
        session_id=child_session,
        operation_key="child",
        input_id=child_input,
    )
    assert store.attach_parent_dependency(child.id, parent.id)
    assert store.attach_parent_dependency(child.id, parent.id)
    assert store.get(child.id).parent_startup_id == parent.id
    assert (
        store.enqueue(
            owner_user_id="owner",
            host_id=host,
            session_id=child_session,
            operation_key="child",
            input_id=child_input,
        ).id
        == child.id
    )
    newer = store.enqueue(
        owner_user_id="owner",
        host_id=host,
        session_id=parent_session,
        operation_key="parent-2",
        input_id=uuid4().hex,
        runtime_generation=2,
    )
    assert not store.attach_parent_dependency(child.id, parent.id)
    assert store.attach_parent_dependency(child.id, newer.id)
    assert store.get(child.id).parent_startup_id == newer.id
    assert store.reserve(host).id == parent.id
    assert store.reserve(host).id == newer.id
    assert store.reserve(host) is None


def test_reserve_fences_child_when_ready_parent_has_newer_generation(db_uri):
    store, host = SessionStartupStore(db_uri), uuid4().hex
    store.configure(host, host_resident_limit=3, deployment_resident_limit=3)
    parent_session = uuid4().hex
    ready_parent = store.enqueue(
        owner_user_id="owner",
        host_id=host,
        session_id=parent_session,
        operation_key="parent-old",
        input_id=uuid4().hex,
    )
    with store._session("seed_ready_old_parent_generation") as db:
        db.get(SqlSessionStartup, (current_workspace_id(), ready_parent.id)).state = "working"
    child = enqueue(store, host)
    assert store.attach_parent_dependency(child.id, ready_parent.id)
    current_parent = store.enqueue(
        owner_user_id="owner",
        host_id=host,
        session_id=parent_session,
        operation_key="parent-new",
        input_id=uuid4().hex,
        runtime_generation=2,
    )
    assert store.reserve(host, eligible_ids={child.id}) is None
    assert store.attach_parent_dependency(child.id, current_parent.id)
    assert store.reserve(host, eligible_ids={child.id}) is None
    with store._session("seed_ready_current_parent_generation") as db:
        db.get(SqlSessionStartup, (current_workspace_id(), current_parent.id)).state = "working"
    assert store.reserve(host, eligible_ids={child.id}).id == child.id


@pytest.mark.parametrize("cancel_path", ["current", "session"])
def test_lazy_sdk_empty_releases_only_unclaimed_bound_context(db_uri, cancel_path):
    store, host, session = SessionStartupStore(db_uri), uuid4().hex, uuid4().hex
    store.configure(host, host_resident_limit=1, deployment_resident_limit=1)
    store.register_host_generation(host, "generation")
    with store._session("seed_bound_runner_for_lazy_empty") as db:
        db.add(
            SqlConversationMetadata(
                workspace_id=current_workspace_id(),
                id=session,
                kind=1,
                host_id=host,
                workspace="/opt/work",
                runner_id="bound-runner",
            )
        )
    empty = store.enqueue(
        owner_user_id="owner",
        host_id=host,
        session_id=session,
        operation_key="empty-sdk",
        input_id=None,
    )
    attempt = store.reserve(host)
    assert store.claim_boundary(
        empty.id,
        attempt.attempt_id,
        runtime_id="runtime",
        host_generation="generation",
        boundary="runner_prepare",
        claim_id="runner-claim",
        expected_runner_id="bound-runner",
    )
    assert not store.defer_empty_sdk(
        empty.id,
        attempt.attempt_id,
        runtime_id="runtime",
        host_generation="generation",
        expected_runner_id="wrong-runner",
    )
    assert store.defer_empty_sdk(
        empty.id,
        attempt.attempt_id,
        runtime_id="runtime",
        host_generation="generation",
        expected_runner_id="bound-runner",
    )
    deferred = store.get(empty.id)
    assert (deferred.state, deferred.occupied, deferred.resident) == (
        "waiting_input",
        False,
        False,
    )
    assert not deferred.input_accepted
    assert store.defer_empty_sdk(
        empty.id,
        attempt.attempt_id,
        runtime_id="runtime",
        host_generation="generation",
        expected_runner_id="bound-runner",
    )
    cancelled = (
        store.cancel_current(empty.id, expected_attempt_id=attempt.attempt_id)
        if cancel_path == "current"
        else store.cancel_session_current(session)
    )
    assert cancelled.state == "cancelling"
    assert (
        store.cancel_current(empty.id, expected_attempt_id=attempt.attempt_id).state
        == "cancelling"
    )
    assert store.confirm_stopped(empty.id, attempt.attempt_id)
    assert store.get(empty.id).state == "cancelled"
