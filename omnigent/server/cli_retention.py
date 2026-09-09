"""Server-side coordination for Host-scoped idle CLI pools."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import secrets
import time
from collections import defaultdict
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, cast

from omnigent.db.db_models import current_workspace_id

_logger = logging.getLogger(__name__)


class CliRetentionHostLeaseBusy(RuntimeError):
    """Another Server replica currently owns this Host's retention boundary."""


class CliRetentionHostLeaseLost(RuntimeError):
    """The current Server replica lost its Host retention lease."""


@dataclass(frozen=True)
class CliRetentionHostLease:
    """Live cross-replica lease token used to fence external side effects."""

    coordinator: CliRetentionCoordinator
    host_id: str
    token: str
    lost: asyncio.Event

    async def ensure_owned(self) -> None:
        """Renew immediately and fail before issuing another external command."""
        if self.lost.is_set():
            raise CliRetentionHostLeaseLost(self.host_id)
        renewed = await asyncio.to_thread(
            self.coordinator._host_store.renew_cli_retention,
            self.host_id,
            self.token,
            claimed_at=int(time.time()),
        )
        if not renewed:
            self.lost.set()
            raise CliRetentionHostLeaseLost(self.host_id)


@dataclass(frozen=True)
class IdleCliSnapshot:
    """One Runner-confirmed main-session CLI that reached the idle threshold."""

    session_id: str
    runner_id: str
    host_id: str
    family: str
    idle_seconds: float
    idle_since_monotonic: float
    activity_token: str
    runtime_generation: str
    policy_revision: int


def select_idle_cli_overflow(
    snapshots: list[IdleCliSnapshot],
    *,
    max_idle_clis: int,
) -> list[IdleCliSnapshot]:
    """Select the oldest excess members independently within each CLI family."""
    by_family: dict[str, list[IdleCliSnapshot]] = defaultdict(list)
    for snapshot in snapshots:
        by_family[snapshot.family].append(snapshot)
    selected: list[IdleCliSnapshot] = []
    for family in sorted(by_family):
        ordered = sorted(
            by_family[family],
            key=lambda item: (item.idle_since_monotonic, item.session_id),
        )
        selected.extend(ordered[: max(0, len(ordered) - max_idle_clis)])
    return selected


class CliRetentionCoordinator:
    """Poll Runner-authoritative idle state and reconcile each Host family pool."""

    def __init__(
        self,
        *,
        host_store: Any,
        conversation_store: Any,
        runner_router: Any,
        intent_store: Any = None,
        release_coordinator: Any = None,
        host_registry: Any = None,
        scan_interval_seconds: float = 60.0,
    ) -> None:
        self._host_store = host_store
        self._conversation_store = conversation_store
        self._runner_router = runner_router
        self._intent_store = intent_store
        self._release_coordinator = release_coordinator
        self._host_registry = host_registry
        self._scan_interval_seconds = scan_interval_seconds
        self._tasks: dict[tuple[int, str], asyncio.Task[None]] = {}
        self._last_results: dict[tuple[int, str], dict[str, Any]] = {}
        self._host_locks: dict[tuple[int, str], asyncio.Lock] = {}

    def lock_for_host(self, host_id: str) -> asyncio.Lock:
        """Serialize policy replacement with the final release decision on this replica."""
        key = (current_workspace_id(), host_id)
        lock = self._host_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._host_locks[key] = lock
        return lock

    @contextlib.asynccontextmanager
    async def lease_for_host(
        self,
        host_id: str,
        *,
        wait_timeout_s: float = 15.0,
    ) -> AsyncIterator[CliRetentionHostLease]:
        """Own the local lock and cross-replica Host lease for one operation."""
        async with self.lock_for_host(host_id):
            token = secrets.token_hex(16)
            deadline = time.monotonic() + max(0.0, wait_timeout_s)
            while True:
                now = int(time.time())
                claimed = await asyncio.to_thread(
                    self._host_store.claim_cli_retention,
                    host_id,
                    token,
                    claimed_at=now,
                    stale_before=now - 15 * 60,
                )
                if claimed:
                    break
                if time.monotonic() >= deadline:
                    raise CliRetentionHostLeaseBusy(host_id)
                await asyncio.sleep(0.05)
            owner_task = asyncio.current_task()
            if owner_task is None:
                raise RuntimeError("Host lease requires an asyncio task owner")
            lease = CliRetentionHostLease(self, host_id, token, asyncio.Event())
            heartbeat = asyncio.create_task(
                self._renew_host_claim(lease, owner_task),
                name=f"cli-retention-lease:{host_id}",
            )
            try:
                yield lease
            finally:
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat
                await asyncio.shield(
                    asyncio.to_thread(
                        self._host_store.release_cli_retention,
                        host_id,
                        token,
                    )
                )

    def trigger(self, host_id: str) -> None:
        """Start one retained reconciliation loop in the caller's workspace context."""
        key = (current_workspace_id(), host_id)
        existing = self._tasks.get(key)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(self._run_host(key), name=f"cli-retention:{host_id}")
        self._tasks[key] = task
        task.add_done_callback(lambda done, task_key=key: self._forget_task(task_key, done))

    def _forget_task(self, key: tuple[int, str], task: asyncio.Task[None]) -> None:
        if self._tasks.get(key) is task:
            self._tasks.pop(key, None)

    async def _run_host(self, key: tuple[int, str]) -> None:
        host_id = key[1]
        while True:
            try:
                result = await self.reconcile_host_once(host_id)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — a transient DB fault must not retire the policy
                _logger.warning(
                    "CLI retention reconciliation failed for Host %s",
                    host_id,
                    exc_info=True,
                )
                await asyncio.sleep(self._scan_interval_seconds)
                continue
            if not result.get("configured"):
                return
            await asyncio.sleep(self._scan_interval_seconds)

    async def shutdown(self) -> None:
        """Cancel every Host loop and await teardown."""
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _renew_host_claim(
        self,
        lease: CliRetentionHostLease,
        owner_task: asyncio.Task[Any],
    ) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                renewed = await asyncio.to_thread(
                    self._host_store.renew_cli_retention,
                    lease.host_id,
                    lease.token,
                    claimed_at=int(time.time()),
                )
            except Exception:  # noqa: BLE001 - retry before the stale window expires.
                _logger.warning(
                    "Could not renew CLI retention claim for Host %s",
                    lease.host_id,
                    exc_info=True,
                )
                continue
            if not renewed:
                lease.lost.set()
                owner_task.cancel()
                return

    def last_result(self, host_id: str) -> dict[str, Any] | None:
        """Return the latest in-process application snapshot for the current workspace."""
        result = self._last_results.get((current_workspace_id(), host_id))
        return dict(result) if result is not None else None

    async def cancel_pending_idle(self, host_id: str) -> int:
        """Cancel old policy revisions before a replacement leaves the Host lock."""
        if self._intent_store is None:
            return 0
        return await asyncio.to_thread(
            self._intent_store.cancel_pending_idle_for_host,
            host_id,
        )

    async def reset_host(
        self,
        host_id: str,
        *,
        policy_revision: int,
    ) -> dict[str, Any] | None:
        """Acquire the Host lease and return every reachable CLI to legacy TTL."""
        async with self.lease_for_host(host_id) as lease:
            current = await asyncio.to_thread(self._host_store.get_host, host_id)
            if (
                current is None
                or current.cli_retention_policy is not None
                or current.cli_retention_revision != policy_revision
            ):
                return None
            return await self.reset_host_under_lease(
                host_id,
                policy_revision=policy_revision,
                lease=lease,
            )

    async def reset_host_under_lease(
        self,
        host_id: str,
        *,
        policy_revision: int,
        lease: CliRetentionHostLease | None = None,
    ) -> dict[str, Any]:
        """Release pool ownership on every reachable session for safe downgrade."""
        key = (current_workspace_id(), host_id)
        if lease is not None:
            await lease.ensure_owned()
        if self._intent_store is not None:
            await asyncio.to_thread(self._intent_store.cancel_pending_idle_for_host, host_id)
        conversations = await self._host_conversations(host_id, include_archived=True)
        reset: list[str] = []
        unavailable: list[str] = []
        for conversation in conversations:
            if not conversation.runner_id:
                continue
            try:
                if lease is not None:
                    await lease.ensure_owned()
                routed = self._runner_router.client_for_session_resources(
                    conversation.id, conversation=conversation
                )
                response = await routed.client.post(
                    f"/v1/sessions/{conversation.id}/cli-retention/reset",
                    json={
                        "host_id": host_id,
                        "policy_revision": policy_revision,
                    },
                    timeout=10.0,
                )
                if response.status_code < 400:
                    reset.append(conversation.id)
                else:
                    unavailable.append(conversation.id)
            except Exception:  # noqa: BLE001 - offline sessions retry legacy on reconnect.
                unavailable.append(conversation.id)
        result = {
            "configured": False,
            "policy_revision": policy_revision,
            "reset": reset,
            "unavailable": unavailable,
            "observed_at": int(time.time()),
        }
        self._last_results[key] = result
        return dict(result)

    async def _host_conversations(
        self,
        host_id: str,
        *,
        include_archived: bool,
    ) -> list[Any]:
        rows: list[Any] = []
        after: str | None = None
        while True:
            page = await asyncio.to_thread(
                self._conversation_store.list_conversations,
                limit=500,
                after=after,
                kind="default",
                host_id=host_id,
                include_archived=include_archived,
            )
            rows.extend(page.data)
            if not page.has_more or not page.last_id:
                return rows
            after = page.last_id

    async def _roots_with_persisted_descendant_protection(self, root_ids: set[str]) -> set[str]:
        """Return roots with active or undelivered descendants in durable state."""
        if not root_ids:
            return set()
        list_children = getattr(
            self._conversation_store, "list_child_conversation_ids_by_parent", None
        )
        get_conversations = getattr(self._conversation_store, "get_conversations", None)
        if not callable(list_children) or not callable(get_conversations):
            # A nonstandard store cannot prove the safety dependency. Protect
            # every root instead of letting missing evidence become eligibility.
            return set(root_ids)

        root_for: dict[str, str] = {root_id: root_id for root_id in root_ids}
        frontier = list(root_ids)
        seen = set(root_ids)
        protected: set[str] = set()
        while frontier:
            grouped = cast(
                dict[str, list[str]],
                await asyncio.to_thread(list_children, frontier),
            )
            next_frontier: list[str] = []
            for parent_id in frontier:
                root_id = root_for[parent_id]
                for child_id in grouped.get(parent_id, ()):
                    if child_id in seen:
                        continue
                    seen.add(child_id)
                    root_for[child_id] = root_id
                    next_frontier.append(child_id)
            if not next_frontier:
                break
            rows = cast(
                dict[str, Any],
                await asyncio.to_thread(get_conversations, next_frontier),
            )
            for child_id in next_frontier:
                child = rows.get(child_id)
                if child is None:
                    continue
                labels = child.labels if isinstance(child.labels, dict) else {}
                dispatch_id = labels.get("omnigent.subagent.dispatch_id")
                undelivered = (
                    isinstance(dispatch_id, str)
                    and bool(dispatch_id)
                    and labels.get("omnigent.subagent.delivered_id") != dispatch_id
                )
                if child.live_status in {"running", "waiting"} or undelivered:
                    protected.add(root_for[child_id])
            frontier = next_frontier
        return protected

    async def reconcile_host_once(self, host_id: str) -> dict[str, Any]:
        """Lease one Host pool and reserve only its oldest excess idle CLIs."""
        if self._intent_store is None:
            return await self._reconcile_host_claimed(host_id, expected_connection=None)
        host = await asyncio.to_thread(self._host_store.get_host, host_id)
        if host is None or host.cli_retention_policy is None:
            return await self._reconcile_host_claimed(host_id, expected_connection=None)
        expected_connection = (
            self._host_registry.get(host_id) if self._host_registry is not None else None
        )
        if self._host_registry is not None and expected_connection is None:
            result = {
                "configured": True,
                "owner": False,
                "families": {},
                "scheduled": [],
            }
            self._last_results[(current_workspace_id(), host_id)] = result
            return dict(result)
        try:
            async with self.lease_for_host(host_id, wait_timeout_s=0) as lease:
                return await self._reconcile_host_claimed(
                    host_id,
                    expected_connection=expected_connection,
                    lease=lease,
                )
        except CliRetentionHostLeaseBusy:
            return {
                "configured": True,
                "owner": True,
                "families": {},
                "scheduled": [],
                "lease": "busy",
            }

    async def _reconcile_host_claimed(
        self,
        host_id: str,
        *,
        expected_connection: Any,
        lease: CliRetentionHostLease | None = None,
    ) -> dict[str, Any]:
        """Inspect one Host under its cross-replica selection lease."""
        key = (current_workspace_id(), host_id)
        host = await asyncio.to_thread(self._host_store.get_host, host_id)
        policy = host.cli_retention_policy if host is not None else None
        if policy is None:
            result = {"configured": False, "families": {}, "released": []}
            self._last_results[key] = result
            return dict(result)

        threshold_s = float(policy.idle_threshold_minutes * 60)
        conversations = await self._host_conversations(
            host_id,
            include_archived=not policy.close_on_archive,
        )
        conversations_by_id = {conversation.id: conversation for conversation in conversations}
        protected_by_descendant = await self._roots_with_persisted_descendant_protection(
            set(conversations_by_id)
        )
        snapshots: list[IdleCliSnapshot] = []
        family_stats: dict[str, dict[str, int]] = defaultdict(
            lambda: {"idle": 0, "active": 0, "below_threshold": 0, "total": 0}
        )
        bound_count = 0
        supported_count = 0
        absent_count = 0
        unsupported_count = 0
        unknown_count = 0
        for conversation in conversations:
            if not conversation.runner_id:
                continue
            bound_count += 1
            try:
                routed = self._runner_router.client_for_session_resources(
                    conversation.id,
                    conversation=conversation,
                )
                response = await routed.client.get(
                    f"/v1/sessions/{conversation.id}/cli-retention",
                    params={
                        "idle_threshold_seconds": threshold_s,
                        "host_id": host_id,
                        "policy_revision": host.cli_retention_revision,
                    },
                    timeout=5.0,
                )
                if response.status_code >= 400:
                    unknown_count += 1
                    continue
                payload = response.json()
                observed_monotonic = time.monotonic()
            except Exception:  # noqa: BLE001 — unknown is never eligible
                unknown_count += 1
                _logger.debug(
                    "Could not inspect CLI retention state for session %s",
                    conversation.id,
                    exc_info=True,
                    extra={"session_id": conversation.id},
                )
                continue
            if not isinstance(payload, dict):
                unknown_count += 1
                continue
            runtime_generation = payload.get("runtime_generation")
            observed_revision = payload.get("policy_revision")
            if (
                payload.get("session_id") != conversation.id
                or payload.get("host_id") != host_id
                or isinstance(observed_revision, bool)
                or not isinstance(observed_revision, int)
                or observed_revision != host.cli_retention_revision
                or not isinstance(runtime_generation, str)
                or not runtime_generation
            ):
                unknown_count += 1
                continue
            present = payload.get("present")
            supported = payload.get("supported")
            if not isinstance(present, bool) or not isinstance(supported, bool):
                unknown_count += 1
                continue
            if not present:
                absent_count += 1
                continue
            if not supported:
                unsupported_count += 1
                continue
            family = payload.get("family") if isinstance(payload, dict) else None
            busy = payload.get("busy") if isinstance(payload, dict) else None
            eligible = payload.get("eligible") if isinstance(payload, dict) else None
            idle_seconds = payload.get("idle_seconds") if isinstance(payload, dict) else None
            token = payload.get("activity_token") if isinstance(payload, dict) else None
            if (
                not isinstance(family, str)
                or not family
                or not isinstance(busy, bool)
                or not isinstance(eligible, bool)
                or (busy and eligible)
                or isinstance(idle_seconds, bool)
                or not isinstance(idle_seconds, (int, float))
                or not math.isfinite(idle_seconds)
                or idle_seconds < 0
                or not isinstance(token, str)
                or not token
            ):
                unknown_count += 1
                continue
            supported_count += 1
            if conversation.id in protected_by_descendant:
                busy = True
                eligible = False
                idle_seconds = 0.0
            stats = family_stats[family]
            stats["total"] += 1
            if busy:
                stats["active"] += 1
            elif eligible:
                stats["idle"] += 1
            else:
                stats["below_threshold"] += 1
            if not eligible:
                continue
            snapshots.append(
                IdleCliSnapshot(
                    session_id=conversation.id,
                    runner_id=routed.runner_id,
                    host_id=host_id,
                    family=family,
                    idle_seconds=float(idle_seconds),
                    idle_since_monotonic=observed_monotonic - float(idle_seconds),
                    activity_token=token,
                    runtime_generation=runtime_generation,
                    policy_revision=host.cli_retention_revision,
                )
            )

        families = {family: dict(stats) for family, stats in sorted(family_stats.items())}
        released: list[str] = []
        selected = (
            []
            if policy.max_idle_clis is None
            else select_idle_cli_overflow(snapshots, max_idle_clis=policy.max_idle_clis)
        )
        if unknown_count and (supported_count or absent_count or unsupported_count):
            application_status = "partial"
        elif unknown_count:
            application_status = "unknown"
        elif unsupported_count and not supported_count:
            application_status = "unsupported"
        elif unsupported_count:
            application_status = "partial"
        else:
            application_status = "applied"
        application = {
            "status": application_status,
            "bound": bound_count,
            "supported": supported_count,
            "absent": absent_count,
            "unsupported": unsupported_count,
            "unknown": unknown_count,
        }
        if self._intent_store is not None:
            if (
                self._host_registry is not None
                and self._host_registry.get(host_id) is not expected_connection
            ):
                return {
                    "configured": True,
                    "owner": False,
                    "families": families,
                    "scheduled": [],
                }
            # Existing pending/claimed intents already account for part of the
            # current overflow. Exclude their exact runtime generations and
            # reserve only the remaining oldest candidates.
            scheduled: list[str] = []
            by_family: dict[str, list[IdleCliSnapshot]] = defaultdict(list)
            for snapshot in snapshots:
                by_family[snapshot.family].append(snapshot)
            for family, family_snapshots in sorted(by_family.items()):
                active = await asyncio.to_thread(
                    self._intent_store.active_idle_runtime_keys,
                    host_id=host_id,
                    family=family,
                    policy_revision=host.cli_retention_revision,
                )
                current_runtime_keys = {
                    (snapshot.session_id, snapshot.runtime_generation)
                    for snapshot in family_snapshots
                }
                active_current = active & current_runtime_keys
                needed = (
                    0
                    if policy.max_idle_clis is None
                    else max(
                        0,
                        len(family_snapshots) - policy.max_idle_clis - len(active_current),
                    )
                )
                candidates = sorted(
                    (
                        snapshot
                        for snapshot in family_snapshots
                        if (snapshot.session_id, snapshot.runtime_generation) not in active_current
                    ),
                    key=lambda item: (item.idle_since_monotonic, item.session_id),
                )[:needed]
                for snapshot in candidates:
                    if lease is not None:
                        await lease.ensure_owned()
                    await asyncio.to_thread(
                        self._intent_store.ensure_idle_intent,
                        host_id=host_id,
                        target_session_id=snapshot.session_id,
                        runner_id=snapshot.runner_id,
                        family=snapshot.family,
                        policy_revision=snapshot.policy_revision,
                        runtime_generation=snapshot.runtime_generation,
                        activity_token=snapshot.activity_token,
                        idle_threshold_seconds=int(threshold_s),
                    )
                    scheduled.append(snapshot.session_id)
            if self._release_coordinator is not None and scheduled:
                self._release_coordinator.trigger_pending(host_id=host_id)
            result = {
                "configured": True,
                "owner": True,
                "policy_revision": host.cli_retention_revision,
                "observed_at": int(time.time()),
                "application": application,
                "families": families,
                "scheduled": scheduled,
            }
            self._last_results[key] = result
            return dict(result)
        for snapshot in selected:
            try:
                async with self.lock_for_host(host_id):
                    if lease is not None:
                        await lease.ensure_owned()
                    current_host = await asyncio.to_thread(self._host_store.get_host, host_id)
                    if (
                        current_host is None
                        or current_host.cli_retention_policy is None
                        or current_host.cli_retention_revision != host.cli_retention_revision
                        or current_host.cli_retention_policy != policy
                    ):
                        break
                    conversation = conversations_by_id[snapshot.session_id]
                    routed = self._runner_router.client_for_session_resources(
                        snapshot.session_id,
                        conversation=conversation,
                    )
                    if routed.runner_id != snapshot.runner_id:
                        continue
                    response = await routed.client.post(
                        f"/v1/sessions/{snapshot.session_id}/cli-retention/release",
                        json={
                            "reason": "idle_pool_overflow",
                            "idle_threshold_seconds": threshold_s,
                            "expected_activity_token": snapshot.activity_token,
                            "runtime_generation": snapshot.runtime_generation,
                            "host_id": host_id,
                            "policy_revision": snapshot.policy_revision,
                        },
                        timeout=10.0,
                    )
                if response.status_code < 400 and response.json().get("status") == "released":
                    released.append(snapshot.session_id)
            except Exception:  # noqa: BLE001 — next scan retries from fresh truth
                _logger.debug(
                    "Could not release idle CLI for session %s",
                    snapshot.session_id,
                    exc_info=True,
                    extra={"session_id": snapshot.session_id},
                )

        result = {
            "configured": True,
            "owner": True,
            "policy_revision": host.cli_retention_revision,
            "observed_at": int(time.time()),
            "application": application,
            "families": families,
            "released": released,
        }
        self._last_results[key] = result
        return dict(result)
