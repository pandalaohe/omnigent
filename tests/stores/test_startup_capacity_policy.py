"""Lower startup policy must be enforced by the shared reservation transaction."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from sqlalchemy import select

from omnigent.db.session_startup import SqlStartupBudget
from omnigent.stores.session_startup_store import SessionStartupStore


def test_one_start_policy_survives_store_replacement(db_uri):
    first, replacement = SessionStartupStore(db_uri), SessionStartupStore(db_uri)
    host = uuid4().hex
    first.configure(host, host_resident_limit=7, deployment_resident_limit=20, startup_limit=1)
    for _ in range(2):
        first.enqueue(
            owner_user_id="owner",
            host_id=host,
            session_id=uuid4().hex,
            operation_key=uuid4().hex,
            input_id=None,
        )
    assert first.reserve(host) is not None
    assert replacement.reserve(host) is None


def test_startup_only_ignores_resident_budget_but_reserves_exactly_two(db_uri):
    stores = [SessionStartupStore(db_uri), SessionStartupStore(db_uri)]
    host = uuid4().hex
    stores[0].configure(host, mode="startup_only")
    with stores[0]._session("retain_unknown_resident_budget") as db:
        for budget in db.scalars(select(SqlStartupBudget)):
            budget.resident_inventory_unknown = True
            budget.resident_limit = 0
    for _ in range(5):
        stores[0].enqueue(
            owner_user_id="owner",
            host_id=host,
            session_id=uuid4().hex,
            operation_key=uuid4().hex,
            input_id=None,
        )
    with ThreadPoolExecutor(max_workers=2) as pool:
        reserved = list(pool.map(lambda i: stores[i % 2].reserve(host), range(5)))
    active = [row for row in reserved if row is not None]
    assert len(active) == 2
    assert all(row.admission_mode == "startup_only" and row.policy_revision == 1 for row in active)
    first = active[0]
    assert stores[1].claim(first.id, first.attempt_id, runtime_id="owned-runtime")
    assert stores[1].claim_boundary(
        first.id,
        first.attempt_id,
        runtime_id="owned-runtime",
        host_generation="host-one",
        boundary="runner_prepare",
        claim_id="prepare-once",
    )
    assert not stores[0].claim_boundary(
        first.id,
        first.attempt_id,
        runtime_id="owned-runtime",
        host_generation="host-one",
        boundary="runner_prepare",
        claim_id="prepare-again",
    )
    assert stores[1].reserve(host) is None
    with stores[0]._session("unknown_inventory_is_not_cleared") as db:
        assert all(b.resident_inventory_unknown for b in db.scalars(select(SqlStartupBudget)))


def test_policy_change_cannot_reinterpret_an_issued_startup(db_uri):
    store, host = SessionStartupStore(db_uri), uuid4().hex
    store.configure(host, mode="startup_only")
    store.enqueue(
        owner_user_id="owner",
        host_id=host,
        session_id=uuid4().hex,
        operation_key=uuid4().hex,
        input_id=None,
    )
    issued = store.reserve(host)
    store.configure(host, host_resident_limit=7, deployment_resident_limit=20)
    retained = store.get(issued.id)
    assert retained.admission_mode == "startup_only" and retained.occupied
    assert not store.claim(issued.id, issued.attempt_id, runtime_id="late-runtime")
    assert not store.claim_boundary(
        issued.id,
        issued.attempt_id,
        runtime_id="late-runtime",
        host_generation="host-one",
        boundary="runner_prepare",
        claim_id="late-prepare",
    )
