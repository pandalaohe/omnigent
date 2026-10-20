"""One-shot recovery coordinated with an exact owned lifecycle controller."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from math import isfinite
from typing import Protocol

from omnigent.entities.host_recovery import HostRecoveryPlan
from omnigent.host.recovery import RecoveryInventory, assess_recovery
from omnigent.stores.session_startup_store import SessionStartupStore


class OwnedRecoveryLifecycle(Protocol):
    """Advertise support only when every affected input boundary obeys this guard."""

    account_generation: str

    def execution_guard(
        self, plan: HostRecoveryPlan
    ) -> AbstractAsyncContextManager[OwnedRecoveryLifecycle]: ...

    async def inventory(self) -> RecoveryInventory: ...

    async def restart_owned(self, plan: HostRecoveryPlan) -> None:
        """Restart only the plan's exact PID/birth and supervised service instance."""
        ...


class HostRecoveryController:
    def __init__(
        self, store: SessionStartupStore, *, now: Callable[[], float] = time.time
    ) -> None:
        self.store, self._now = store, now

    async def plan(
        self,
        host_id: str,
        *,
        expected_epoch: int,
        account_generation: str,
        startup_attempts: list[tuple[str, str]],
        inventory: RecoveryInventory,
    ) -> HostRecoveryPlan:
        """The caller authenticates host-owner authority before requesting a plan."""
        now = self._now()
        assessment = assess_recovery(inventory, now=now)
        if not assessment.allowed or not account_generation:
            raise ValueError(assessment.reason if not assessment.allowed else "owner_unknown")
        assert inventory.supervisor is not None
        return await asyncio.to_thread(
            self.store.begin_recovery,
            host_id,
            expected_epoch=expected_epoch,
            account_generation=account_generation,
            host_generation=inventory.host_generation,
            process_identity=inventory.process_identity,
            supervisor=inventory.supervisor,
            members=inventory.server_members | inventory.local_members,
            unresolved_runtimes=inventory.unresolved_startups,
            startup_attempts=tuple(startup_attempts),
            now=now,
        )

    def _final_check(
        self, plan: HostRecoveryPlan, inventory: RecoveryInventory, *, account_generation: str
    ) -> str | None:
        assessment = assess_recovery(inventory, now=self._now())
        if not assessment.allowed:
            return assessment.reason
        if (
            account_generation != plan.account_generation
            or inventory.host_generation != plan.host_generation
            or inventory.process_identity != plan.process_identity
            or inventory.supervisor != plan.supervisor
            or inventory.server_members | inventory.local_members != frozenset(plan.members)
            or any(not 0 <= self._now() - row.observed_at <= 5 for row in inventory.observations)
        ):
            return "recovery_changed"
        return None

    async def commit(self, plan: HostRecoveryPlan, *, lifecycle: OwnedRecoveryLifecycle) -> str:
        current = await asyncio.to_thread(self.store.recovery, plan.host_id)
        if current is None or current.recovery_id != plan.recovery_id:
            return "recovery_changed"
        if current.phase != "quiescing":
            if (
                current.phase == "restart_committed"
                and current.committed_at is not None
                and isfinite(self._now())
                and self._now() - current.committed_at >= 60
            ):
                await asyncio.to_thread(self.store.mark_recovery_unconfirmed, current)
                return "recovery_unconfirmed"
            return current.phase
        async with lifecycle.execution_guard(plan) as owned:
            reason = self._final_check(
                plan, await owned.inventory(), account_generation=owned.account_generation
            )
            if reason:
                return reason
            committed = await asyncio.to_thread(self.store.commit_recovery, plan, now=self._now())
            if not committed:
                return "recovery_changed"
            # Database latency must not age the final proof beyond the execution budget.
            reason = self._final_check(
                plan, await owned.inventory(), account_generation=owned.account_generation
            )
            if reason:
                await asyncio.to_thread(self.store.mark_recovery_unconfirmed, plan)
                return "recovery_unconfirmed"
            try:
                await owned.restart_owned(plan)
            except Exception:  # noqa: BLE001 -- committed effects may have happened
                return "restart_committed"
        return "restart_committed"

    async def cancel(self, plan: HostRecoveryPlan) -> bool:
        return await asyncio.to_thread(self.store.cancel_recovery, plan)

    async def reconcile(
        self,
        plan: HostRecoveryPlan,
        *,
        new_generation: str,
        account_generation: str,
        stopped: set[tuple[str, str]],
        adopted: set[tuple[str, str]],
        inputs_reconciled: bool,
        lifecycle: OwnedRecoveryLifecycle,
    ) -> bool:
        """A new handshake or an empty manager map cannot establish that old work stopped."""
        async with lifecycle.execution_guard(plan) as owned:
            if owned.account_generation != account_generation:
                return False
            return await self._reconcile_guarded(
                plan,
                new_generation=new_generation,
                account_generation=account_generation,
                stopped=stopped,
                adopted=adopted,
                inputs_reconciled=inputs_reconciled,
                inventory=await owned.inventory(),
            )

    async def _reconcile_guarded(
        self,
        plan: HostRecoveryPlan,
        *,
        new_generation: str,
        account_generation: str,
        stopped: set[tuple[str, str]],
        adopted: set[tuple[str, str]],
        inputs_reconciled: bool,
        inventory: RecoveryInventory,
    ) -> bool:
        now = self._now()
        if (
            not isfinite(now)
            or not inventory.server_complete
            or not inventory.local_complete
            or not inventory.execution_fence
            or inventory.host_generation != new_generation
            or inventory.server_members | inventory.local_members != frozenset(plan.members)
            or {(row.runtime_id, row.generation) for row in inventory.observations}
            != set(plan.members)
        ):
            return False
        for row in inventory.observations:
            if (
                not isfinite(row.observed_at)
                or not 0 <= now - row.observed_at <= 10
                or not row.no_live_work_confirmed
                or not row.no_owned_descendants
                or row.live_turn
                or row.live_tool
                or row.background_job
                or row.protected_interaction
                or ((row.runtime_id, row.generation) in stopped and not row.exited)
            ):
                return False
        return await asyncio.to_thread(
            self.store.reconcile_recovery,
            plan,
            new_generation=new_generation,
            account_generation=account_generation,
            stopped=stopped,
            adopted=adopted,
            inputs_reconciled=inputs_reconciled,
        )
