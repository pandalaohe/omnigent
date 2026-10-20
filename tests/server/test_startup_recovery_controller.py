"""Recovery commits once under a final local work fence, never by timeout replay."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from uuid import uuid4

import pytest

from omnigent.entities.session_startup import StartupReceipt
from omnigent.host.recovery_controller import HostRecoveryController
from omnigent.stores.session_startup_store import SessionStartupStore
from tests.host.test_startup_recovery import idle, inventory


def incident(db_uri):
    store, host = SessionStartupStore(db_uri), uuid4().hex
    store.configure(host, host_resident_limit=8, deployment_resident_limit=8)
    attempts = []
    for _ in range(3):
        store.enqueue(
            owner_user_id="owner",
            host_id=host,
            session_id=uuid4().hex,
            operation_key=uuid4().hex,
            input_id=None,
        )
    for name in ("first", "second"):
        row = store.reserve(host)
        store.claim_boundary(
            row.id,
            row.attempt_id,
            runtime_id=name,
            host_generation="host-generation",
            boundary="runner_prepare",
            claim_id=uuid4().hex,
        )
        store.mark_uncertain(row.id, row.attempt_id, reason="unconfirmed")
        attempts.append((row.id, row.attempt_id))
    observations = inventory(idle("first"), idle("second"), idle("other"))
    observations = replace(
        observations,
        server_members=frozenset(
            (name, "host-generation") for name in ("first", "second", "other")
        ),
        local_members=frozenset(
            (name, "host-generation") for name in ("first", "second", "other")
        ),
        unresolved_startups=frozenset((name, "host-generation") for name in ("first", "second")),
        observations=tuple(
            replace(row, generation="host-generation") for row in observations.observations
        ),
    )
    return store, host, attempts, observations


class Lifecycle:
    def __init__(self, observations):
        self.observations = observations
        self.account_generation = "account"
        self.restarts = []

    @asynccontextmanager
    async def execution_guard(self, plan):
        yield self

    async def inventory(self):
        return self.observations

    async def restart_owned(self, plan):
        self.restarts.append(plan.recovery_id)


@pytest.mark.asyncio
async def test_idle_adoption_requires_the_original_attempt_receipts(db_uri):
    store, host, attempts, observations = incident(db_uri)
    controller = HostRecoveryController(store, now=lambda: 1001)
    plan = await controller.plan(
        host,
        expected_epoch=0,
        account_generation="account",
        startup_attempts=attempts,
        inventory=observations,
    )
    lifecycle = Lifecycle(observations)
    assert await controller.commit(plan, lifecycle=lifecycle) == "restart_committed"
    lifecycle.observations = replace(observations, host_generation="replacement")
    args = {
        "new_generation": "replacement",
        "stopped": set(),
        "adopted": set(plan.members),
        "inputs_reconciled": True,
        "account_generation": "account",
        "lifecycle": lifecycle,
    }
    assert not await controller.reconcile(plan, **args)
    for startup_id, attempt_id in attempts:
        row = store.get(startup_id)
        for phase in ("runtime_ready", "tools_ready"):
            assert store.observe(
                StartupReceipt(
                    startup_id=startup_id,
                    attempt_id=attempt_id,
                    runtime_id=row.runtime_id,
                    input_id=None,
                    phase=phase,
                    source="runner",
                )
            )
    assert await controller.reconcile(plan, **args)
    assert store.reserve(host) is not None


@pytest.mark.asyncio
async def test_restart_commit_is_durable_once_only_and_unknown_reply_stays_fenced(db_uri):
    store, host, attempts, observations = incident(db_uri)
    controller = HostRecoveryController(store, now=lambda: 1001)
    plan = await controller.plan(
        host,
        expected_epoch=0,
        account_generation="account",
        startup_attempts=attempts,
        inventory=observations,
    )
    lifecycle = Lifecycle(observations)
    assert store.reserve(host) is None
    results = await asyncio.gather(
        controller.commit(plan, lifecycle=lifecycle),
        controller.commit(plan, lifecycle=lifecycle),
    )
    assert "restart_committed" in results
    replacement = HostRecoveryController(SessionStartupStore(db_uri), now=lambda: 1062)
    assert await replacement.commit(plan, lifecycle=lifecycle) == "recovery_unconfirmed"
    assert lifecycle.restarts == [plan.recovery_id]
    assert store.reserve(host) is None
    assert not await replacement.cancel(plan)


@pytest.mark.asyncio
async def test_complete_stop_reconciliation_reopens_only_the_two_slot_queue(db_uri):
    store, host, attempts, observations = incident(db_uri)
    controller = HostRecoveryController(store, now=lambda: 1001)
    plan = await controller.plan(
        host,
        expected_epoch=0,
        account_generation="account",
        startup_attempts=attempts,
        inventory=observations,
    )
    lifecycle = Lifecycle(observations)
    assert await controller.commit(plan, lifecycle=lifecycle) == "restart_committed"
    lifecycle.observations = replace(
        observations,
        host_generation="replacement",
        observations=tuple(replace(row, exited=True) for row in observations.observations),
    )
    forged = plan.model_copy(update={"account_generation": "replacement-account"})
    lifecycle.account_generation = "replacement-account"
    assert not await controller.reconcile(
        forged,
        new_generation="replacement",
        stopped=set(plan.members),
        adopted=set(),
        inputs_reconciled=True,
        account_generation="replacement-account",
        lifecycle=lifecycle,
    )
    lifecycle.account_generation = "account"
    assert await controller.reconcile(
        plan,
        new_generation="replacement",
        stopped=set(plan.members),
        adopted=set(),
        inputs_reconciled=True,
        account_generation="account",
        lifecycle=lifecycle,
    )
    assert store.reserve(host) is not None
    assert store.recovery(host).phase == "complete"
    with pytest.raises(ValueError, match="cooldown"):
        await controller.plan(
            host,
            expected_epoch=1,
            account_generation="account",
            startup_attempts=attempts,
            inventory=observations,
        )


@pytest.mark.asyncio
async def test_final_work_revision_and_local_positive_work_abort_before_restart(db_uri):
    store, host, attempts, observations = incident(db_uri)
    controller = HostRecoveryController(store, now=lambda: 1001)
    plan = await controller.plan(
        host,
        expected_epoch=0,
        account_generation="account",
        startup_attempts=attempts,
        inventory=observations,
    )
    store.note_host_work(host)
    lifecycle = Lifecycle(observations)
    assert await controller.commit(plan, lifecycle=lifecycle) == "recovery_changed"
    assert lifecycle.restarts == []
    assert await controller.cancel(plan)


@pytest.mark.asyncio
async def test_local_work_or_changed_process_veto_and_partial_survivor_reconciliation(db_uri):
    store, host, attempts, observations = incident(db_uri)
    controller = HostRecoveryController(store, now=lambda: 1001)
    plan = await controller.plan(
        host,
        expected_epoch=0,
        account_generation="account",
        startup_attempts=attempts,
        inventory=observations,
    )
    lifecycle = Lifecycle(replace(observations, process_identity="reused-pid:new-birth"))
    assert await controller.commit(plan, lifecycle=lifecycle) == "recovery_changed"
    lifecycle.observations = replace(
        observations,
        observations=tuple(
            replace(row, live_tool=row.runtime_id == "other") for row in observations.observations
        ),
    )
    assert await controller.commit(plan, lifecycle=lifecycle) == "other_session_working"
    assert lifecycle.restarts == []
    lifecycle.observations = observations
    assert await controller.commit(plan, lifecycle=lifecycle) == "restart_committed"
    assert not await controller.reconcile(
        plan,
        new_generation="replacement",
        stopped={("first", "host-generation")},
        adopted=set(),
        inputs_reconciled=True,
        account_generation="account",
        lifecycle=Lifecycle(replace(observations, host_generation="replacement")),
    )
    assert store.reserve(host) is None
