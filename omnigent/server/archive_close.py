"""Durable retry coordinator for archive and idle CLI release intents."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import secrets
import time
from typing import Any

from omnigent.db.db_models import current_workspace_id, workspace_scope
from omnigent.server.cli_release_store import CliReleaseIntent, CliReleaseIntentStore
from omnigent.stores.conversation_store import ARCHIVE_CLOSE_CLAIM_STALE_AFTER_S
from omnigent.stores.host_store import host_is_live

_logger = logging.getLogger(__name__)
_INTENT_CLAIM_STALE_AFTER_S = 15 * 60
_INTENT_HEARTBEAT_S = 60
_RETRY_DELAY_S = 15
_RETRY_MAX_DELAY_S = 3600


def _retry_delay_seconds(intent: CliReleaseIntent, now: int) -> int:
    """Grow the retry gap with the intent's own age: clamp(age/4, 15 s, 1 h)."""
    age = max(0, now - intent.created_at)
    return max(_RETRY_DELAY_S, min(_RETRY_MAX_DELAY_S, age // 4))


class ArchiveCloseCoordinator:
    """Materialize, lease, and execute generation-fenced CLI release work."""

    def __init__(
        self,
        *,
        conversation_store: Any,
        host_store: Any,
        host_registry: Any,
        runner_router: Any,
        intent_store: CliReleaseIntentStore,
        scan_interval_seconds: float = 30.0,
    ) -> None:
        self._conversation_store = conversation_store
        self._host_store = host_store
        self._host_registry = host_registry
        self._runner_router = runner_router
        self._intent_store = intent_store
        self._scan_interval_seconds = scan_interval_seconds
        self._root_tasks: dict[tuple[int, str], asyncio.Task[None]] = {}
        self._intent_tasks: dict[tuple[int, str], asyncio.Task[None]] = {}
        self._scan_task: asyncio.Task[None] | None = None
        self._host_lock_provider: Any = None

    def set_host_lock_provider(self, provider: Any) -> None:
        """Share the online Host policy/release linearization lock."""
        self._host_lock_provider = provider

    async def start(self) -> None:
        """Start restart recovery for the current workspace."""
        if self._scan_task is None or self._scan_task.done():
            self._scan_task = asyncio.create_task(
                self._scan_loop(), name="cli-release-intent-reconcile"
            )
        try:
            self._trigger_all_workspaces()
        except Exception:  # noqa: BLE001 - periodic recovery remains live.
            _logger.warning("Could not start CLI release recovery scan", exc_info=True)

    def _trigger_all_workspaces(self) -> None:
        """Discover due work without assuming the lifespan's default tenant."""
        now = int(time.time())
        workspaces = self._conversation_store.pending_archive_close_workspaces()
        workspaces.update(self._intent_store.due_workspaces(now=now))
        if not workspaces:
            workspaces.add(current_workspace_id())
        for workspace_id in workspaces:
            with workspace_scope(workspace_id):
                self.trigger_pending()

    def trigger(self, session_id: str) -> None:
        """Expand one newly committed archive request without awaiting teardown."""
        key = (current_workspace_id(), session_id)
        existing = self._root_tasks.get(key)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(
            self._expand_archive_root(key), name=f"archive-cli-expand:{session_id}"
        )
        self._root_tasks[key] = task
        self._retain_task(task, key=key, roots=True)

    def trigger_pending(
        self,
        *,
        host_id: str | None = None,
        runner_id: str | None = None,
    ) -> None:
        """Schedule pending roots and due target intents visible in this workspace."""
        try:
            roots = self._conversation_store.list_pending_archive_closes(limit=200)
            intents = self._intent_store.list_due(now=int(time.time()), limit=200)
        except Exception:  # noqa: BLE001 - periodic retry is the recovery boundary.
            _logger.warning("Could not list pending CLI release work", exc_info=True)
            return
        for root in roots:
            if host_id is not None and root.host_id != host_id:
                continue
            if runner_id is not None and root.runner_id != runner_id:
                continue
            self.trigger(root.id)
        for intent in intents:
            if host_id is not None and intent.host_id != host_id:
                continue
            if runner_id is not None and intent.runner_id != runner_id:
                continue
            self._trigger_intent(intent)

    def _trigger_next_root_target(self, root_session_id: str) -> None:
        """Schedule at most one due archive target for one root (O(1) work)."""
        try:
            intents = self._intent_store.list_due_for_root(
                root_session_id, now=int(time.time()), limit=1
            )
        except Exception:  # noqa: BLE001 - periodic retry is the recovery boundary.
            _logger.warning("Could not list pending CLI release work", exc_info=True)
            return
        for intent in intents:
            self._trigger_intent(intent)

    def _retain_task(
        self,
        task: asyncio.Task[None],
        *,
        key: tuple[int, str],
        roots: bool,
    ) -> None:
        table = self._root_tasks if roots else self._intent_tasks

        def _done(done: asyncio.Task[None]) -> None:
            if table.get(key) is done:
                table.pop(key, None)
            with contextlib.suppress(asyncio.CancelledError, Exception):
                done.result()

        task.add_done_callback(_done)
        from omnigent.server.routes._sessions.orchestration import _detached_stop_tasks

        _detached_stop_tasks.add(task)
        task.add_done_callback(_detached_stop_tasks.discard)

    def _trigger_intent(self, intent: CliReleaseIntent) -> asyncio.Task[None] | None:
        key = (current_workspace_id(), intent.id)
        existing = self._intent_tasks.get(key)
        if existing is not None and not existing.done():
            return existing
        task = asyncio.create_task(
            self._process_intent(key, intent), name=f"cli-release:{intent.id}"
        )
        self._intent_tasks[key] = task
        self._retain_task(task, key=key, roots=False)
        return task

    async def _scan_loop(self) -> None:
        while True:
            await asyncio.sleep(self._scan_interval_seconds)
            try:
                self._trigger_all_workspaces()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one DB fault must not retire recovery.
                _logger.warning("CLI release recovery scan failed", exc_info=True)

    async def _expand_archive_root(self, key: tuple[int, str]) -> None:
        root_id = key[1]
        root = await asyncio.to_thread(self._conversation_store.get_conversation, root_id)
        if root is None:
            return
        revision = root.archive_close_requested_revision
        if (
            not root.archived
            or revision is None
            or revision != root.archive_revision
            or root.archive_close_completed_revision == revision
        ):
            return
        now = int(time.time())
        token = secrets.token_hex(16)
        claim = await asyncio.to_thread(
            self._conversation_store.claim_archive_close,
            root_id,
            revision,
            token,
            claimed_at=now,
            stale_before=now - ARCHIVE_CLOSE_CLAIM_STALE_AFTER_S,
        )
        if claim != "claimed":
            return
        owner_task = asyncio.current_task()
        if owner_task is None:
            raise RuntimeError("Archive expansion requires an asyncio task owner")
        root_heartbeat = asyncio.create_task(
            self._root_lease_heartbeat(root_id, token, owner_task),
            name=f"archive-expand-lease:{root_id}",
        )
        from omnigent.server.routes import sessions as _sessions_facade
        from omnigent.server.routes._sessions.orchestration import _archive_close_intents

        _archive_close_intents.add(root_id)
        try:
            from omnigent.server.routes._sessions.orchestration import (
                _collect_descendant_conversation_ids,
            )

            descendant_ids = await _collect_descendant_conversation_ids(
                self._conversation_store, root_id
            )
            targets: list[Any] = [root]
            for target_id in descendant_ids:
                target = await asyncio.to_thread(
                    self._conversation_store.get_conversation, target_id
                )
                if target is not None:
                    targets.append(target)
            intents = await asyncio.to_thread(
                self._intent_store.ensure_archive_targets,
                root_id,
                revision,
                targets,
            )
            await self._ensure_root_lease(root_id, token)
            await _sessions_facade._best_effort_stop(
                root_id, self._conversation_store, self._runner_router
            )
        except asyncio.CancelledError:
            await asyncio.shield(
                asyncio.to_thread(
                    self._conversation_store.release_archive_close_claim,
                    root_id,
                    revision,
                    token,
                    error="worker_cancelled",
                )
            )
            _archive_close_intents.discard(root_id)
            raise
        except Exception as exc:  # keep the root pending for retry
            await asyncio.to_thread(
                self._conversation_store.release_archive_close_claim,
                root_id,
                revision,
                token,
                error=type(exc).__name__,
            )
            _archive_close_intents.discard(root_id)
            raise
        finally:
            root_heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await root_heartbeat
        await asyncio.to_thread(
            self._conversation_store.release_archive_close_claim,
            root_id,
            revision,
            token,
        )
        _archive_close_intents.discard(root_id)
        initial_tasks: list[asyncio.Task[None]] = []
        now = int(time.time())
        for intent in intents:
            if intent.status in {"pending", "claimed"} and intent.next_attempt_at <= now:
                task = self._trigger_intent(intent)
                if task is not None:
                    initial_tasks.append(task)
        if initial_tasks:
            await asyncio.gather(*initial_tasks)
        if await asyncio.to_thread(self._intent_store.archive_targets_complete, root_id, revision):
            await asyncio.to_thread(
                self._conversation_store.finalize_archive_close, root_id, revision
            )

    async def _intent_routable_here(self, intent: CliReleaseIntent) -> bool:
        if intent.host_id is not None:
            if self._host_store is None:
                return intent.reason == "archive"
            host = await asyncio.to_thread(self._host_store.get_host, intent.host_id)
            local_connection = self._host_registry.get(intent.host_id)
            # A rotated/deleted Host makes an idle policy intent permanently
            # stale. Let its executor claim and cancel it instead of leaving it
            # pending forever because no replica can own the old host id.
            if host is None and intent.reason == "idle_pool_overflow":
                return True
            if host is not None and host_is_live(host) and local_connection is None:
                return False
            if intent.reason == "idle_pool_overflow" and local_connection is None:
                return False
        if intent.reason == "idle_pool_overflow" and intent.runner_id is not None:
            return self._runner_router.runner_is_online(intent.runner_id)
        return True

    async def _lease_heartbeat(
        self,
        intent_id: str,
        token: str,
        owner_task: asyncio.Task[Any],
    ) -> None:
        while True:
            await asyncio.sleep(_INTENT_HEARTBEAT_S)
            try:
                renewed = await asyncio.to_thread(
                    self._intent_store.renew,
                    intent_id,
                    token,
                    claimed_at=int(time.time()),
                )
            except Exception:  # noqa: BLE001 - retry before the stale window expires.
                _logger.warning("Could not renew CLI release intent %s", intent_id, exc_info=True)
                continue
            if not renewed:
                owner_task.cancel()
                return

    async def _root_lease_heartbeat(
        self,
        root_id: str,
        token: str,
        owner_task: asyncio.Task[Any],
    ) -> None:
        """Keep the root archive fence owned while expansion or release waits."""
        while True:
            await asyncio.sleep(_INTENT_HEARTBEAT_S)
            try:
                renewed = await asyncio.to_thread(
                    self._conversation_store.renew_archive_close_claim,
                    root_id,
                    token,
                    claimed_at=int(time.time()),
                )
            except Exception:  # noqa: BLE001 - retry before the stale window expires.
                _logger.warning(
                    "Could not renew archive close claim for %s", root_id, exc_info=True
                )
                continue
            if not renewed:
                owner_task.cancel()
                return

    async def _ensure_intent_lease(self, intent_id: str, token: str) -> None:
        """Fence the next side effect with an immediate intent-lease renewal."""
        renewed = await asyncio.to_thread(
            self._intent_store.renew,
            intent_id,
            token,
            claimed_at=int(time.time()),
        )
        if not renewed:
            raise asyncio.CancelledError

    async def _ensure_root_lease(self, root_id: str, token: str) -> None:
        """Fence archive work with an immediate root-lease renewal."""
        renewed = await asyncio.to_thread(
            self._conversation_store.renew_archive_close_claim,
            root_id,
            token,
            claimed_at=int(time.time()),
        )
        if not renewed:
            raise asyncio.CancelledError

    async def _claim_root_for_target(self, intent: CliReleaseIntent, token: str, now: int) -> bool:
        revision = intent.archive_revision
        if revision is None:
            return False
        result = await asyncio.to_thread(
            self._conversation_store.claim_archive_close,
            intent.root_session_id,
            revision,
            token,
            claimed_at=now,
            stale_before=now - ARCHIVE_CLOSE_CLAIM_STALE_AFTER_S,
        )
        return result == "claimed"

    async def _process_intent(self, key: tuple[int, str], intent: CliReleaseIntent) -> None:
        del key
        if not await self._intent_routable_here(intent):
            return
        now = int(time.time())
        root_token = secrets.token_hex(16)
        if intent.reason == "archive":
            root = await asyncio.to_thread(
                self._conversation_store.get_conversation, intent.root_session_id
            )
            if (
                root is None
                or not root.archived
                or root.archive_revision != intent.archive_revision
                or root.archive_close_requested_revision != intent.archive_revision
            ):
                await asyncio.to_thread(self._intent_store.cancel, intent.id)
                return
            if not await self._claim_root_for_target(intent, root_token, now):
                return

        claim_token = secrets.token_hex(16)
        claimed = await asyncio.to_thread(
            self._intent_store.claim,
            intent.id,
            claim_token,
            claimed_at=now,
            stale_before=now - _INTENT_CLAIM_STALE_AFTER_S,
        )
        if not claimed:
            if intent.reason == "archive" and intent.archive_revision is not None:
                await asyncio.to_thread(
                    self._conversation_store.release_archive_close_claim,
                    intent.root_session_id,
                    intent.archive_revision,
                    root_token,
                )
            return

        owner_task = asyncio.current_task()
        if owner_task is None:
            raise RuntimeError("CLI release requires an asyncio task owner")
        heartbeat = asyncio.create_task(
            self._lease_heartbeat(intent.id, claim_token, owner_task),
            name=f"cli-release-lease:{intent.id}",
        )
        root_heartbeat = (
            asyncio.create_task(
                self._root_lease_heartbeat(intent.root_session_id, root_token, owner_task),
                name=f"archive-close-lease:{intent.root_session_id}",
            )
            if intent.reason == "archive"
            else None
        )
        from omnigent.server.routes._sessions.orchestration import _archive_close_intents

        if intent.reason == "archive":
            _archive_close_intents.add(intent.root_session_id)
        try:
            await self._ensure_intent_lease(intent.id, claim_token)
            if intent.reason == "archive":
                await self._ensure_root_lease(intent.root_session_id, root_token)
            outcome = await self._execute_intent(intent)
            await self._ensure_intent_lease(intent.id, claim_token)
            if intent.reason == "archive":
                await self._ensure_root_lease(intent.root_session_id, root_token)
            # The next write consumes the intent claim. Stop its heartbeat
            # first so a successful status transition is not mistaken for
            # lease loss while archive finalization continues under root lease.
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
            if outcome == "completed":
                mutated = await asyncio.to_thread(
                    self._intent_store.complete, intent.id, claim_token
                )
            elif outcome == "cancelled":
                mutated = await asyncio.to_thread(
                    self._intent_store.cancel_claimed, intent.id, claim_token
                )
            else:
                mutated = await asyncio.to_thread(
                    self._intent_store.retry,
                    intent.id,
                    claim_token,
                    error=outcome,
                    next_attempt_at=int(time.time())
                    + _retry_delay_seconds(intent, int(time.time())),
                )
            if not mutated:
                raise asyncio.CancelledError
            if intent.reason == "archive" and intent.archive_revision is not None:
                if await asyncio.to_thread(
                    self._intent_store.archive_targets_complete,
                    intent.root_session_id,
                    intent.archive_revision,
                ):
                    await self._ensure_root_lease(intent.root_session_id, root_token)
                    await asyncio.to_thread(
                        self._conversation_store.finalize_archive_close,
                        intent.root_session_id,
                        intent.archive_revision,
                    )
        except asyncio.CancelledError:
            await asyncio.shield(
                asyncio.to_thread(
                    self._intent_store.retry,
                    intent.id,
                    claim_token,
                    error="worker_cancelled",
                    next_attempt_at=int(time.time()),
                )
            )
            raise
        except Exception as exc:  # noqa: BLE001 - durable pending retry owns recovery.
            _logger.warning("CLI release intent %s failed", intent.id, exc_info=True)
            await asyncio.to_thread(
                self._intent_store.retry,
                intent.id,
                claim_token,
                error=type(exc).__name__,
                next_attempt_at=int(time.time()) + _retry_delay_seconds(intent, int(time.time())),
            )
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
            if root_heartbeat is not None:
                root_heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await root_heartbeat
            if intent.reason == "archive" and intent.archive_revision is not None:
                await asyncio.to_thread(
                    self._conversation_store.release_archive_close_claim,
                    intent.root_session_id,
                    intent.archive_revision,
                    root_token,
                )
                _archive_close_intents.discard(intent.root_session_id)

        if intent.reason == "archive":
            self._trigger_next_root_target(intent.root_session_id)

    async def _execute_intent(self, intent: CliReleaseIntent) -> str:
        target = await asyncio.to_thread(
            self._conversation_store.get_conversation, intent.target_session_id
        )
        if target is None:
            return "completed"
        # Address the binding captured when the intent was materialized. A newer
        # DB binding must never redirect old destructive work to a new Runner.
        target = dataclasses.replace(
            target,
            host_id=intent.host_id,
            runner_id=intent.runner_id,
        )
        if intent.reason == "archive":
            from omnigent.server.routes._sessions.orchestration import _archive_stop_one

            stop_host_runner = False
            if (
                intent.archive_revision is not None
                and intent.host_id is not None
                and intent.runner_id is not None
            ):
                stop_host_runner = await asyncio.to_thread(
                    self._intent_store.archive_binding_ready_to_stop,
                    root_session_id=intent.root_session_id,
                    revision=intent.archive_revision,
                    current_intent_id=intent.id,
                    host_id=intent.host_id,
                    runner_id=intent.runner_id,
                )

            closed = await _archive_stop_one(
                intent.target_session_id,
                target,
                self._runner_router,
                self._host_registry,
                archive_scope_id=intent.root_session_id,
                archive_revision=intent.archive_revision,
                stop_host_runner=stop_host_runner,
            )
            if closed:
                return "completed"
            if intent.host_id is not None and self._host_store is not None:
                host = await asyncio.to_thread(self._host_store.get_host, intent.host_id)
                if host is None and (
                    self._host_registry is None or self._host_registry.get(intent.host_id) is None
                ):
                    # The host row is gone and no tunnel can carry the stop, so
                    # nothing will ever acknowledge it.
                    return "completed"
            return "runner_or_host_unavailable"

        if intent.host_id is None or intent.policy_revision is None:
            return "cancelled"
        host = await asyncio.to_thread(self._host_store.get_host, intent.host_id)
        if (
            host is None
            or host.cli_retention_policy is None
            or host.cli_retention_revision != intent.policy_revision
        ):
            return "cancelled"
        if self._host_lock_provider is not None:
            async with self._host_lock_provider(intent.host_id) as lease:
                return await self._execute_idle_intent_locked(intent, target, lease=lease)
        return await self._execute_idle_intent_locked(intent, target)

    async def _execute_idle_intent_locked(
        self,
        intent: CliReleaseIntent,
        target: Any,
        *,
        lease: Any = None,
    ) -> str:
        """Validate current policy and call Runner under the owner-replica lock."""
        assert intent.host_id is not None
        assert intent.policy_revision is not None
        host = await asyncio.to_thread(self._host_store.get_host, intent.host_id)
        if (
            host is None
            or host.cli_retention_policy is None
            or host.cli_retention_revision != intent.policy_revision
        ):
            return "cancelled"
        if lease is not None:
            await lease.ensure_owned()
        try:
            routed = self._runner_router.client_for_session_resources(
                intent.target_session_id,
                conversation=target,
            )
            response = await routed.client.post(
                f"/v1/sessions/{intent.target_session_id}/cli-retention/release",
                json={
                    "reason": "idle_pool_overflow",
                    "idle_threshold_seconds": intent.idle_threshold_seconds,
                    "expected_activity_token": intent.activity_token,
                    "runtime_generation": intent.runtime_generation,
                    "host_id": intent.host_id,
                    "policy_revision": intent.policy_revision,
                },
                timeout=10.0,
            )
        except Exception:  # noqa: BLE001 - tunnel recovery retries the same intent.
            return "runner_unavailable"
        payload = response.json() if response.status_code < 500 else {}
        status = payload.get("status") if isinstance(payload, dict) else None
        if response.status_code < 400 and status in {"released", "absent"}:
            return "completed"
        if status in {
            "busy",
            "not_eligible",
            "stale",
            "stale_generation",
            "stale_policy",
            "unsupported",
        }:
            return "cancelled"
        return "runner_release_failed"

    async def shutdown(self) -> None:
        """Cancel local workers; claimed DB work becomes reclaimable after its lease."""
        if self._scan_task is not None:
            self._scan_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._scan_task
            self._scan_task = None
        tasks = list(self._root_tasks.values()) + list(self._intent_tasks.values())
        self._root_tasks.clear()
        self._intent_tasks.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def wait_for_idle(self) -> None:
        """Test seam: drain work currently owned by this replica."""
        while self._root_tasks or self._intent_tasks:
            tasks = list(self._root_tasks.values()) + list(self._intent_tasks.values())
            if tasks:
                await asyncio.gather(*tasks)
