"""Coalesced host-load advice, composed only when a parent can receive it.

Parents whose runners are on another replica get no push; use sys_host_load and timer wakes.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from omnigent.db.db_models import workspace_scope
from omnigent.entities import Conversation
from omnigent.server.peer_sweeper import PeerSweeper
from omnigent.server.routes._sessions.helpers import (
    Placement,
    _effective_placement,
    _inherited_placement,
)
from omnigent.server.schemas import SessionEventInput
from omnigent.server.system_status import SystemStatusHub
from omnigent.stores import ConversationStore
from omnigent.util.session_lifecycle import is_session_closed

_logger = logging.getLogger(__name__)
_HOLDS = {"heavy_hold", "new_session_hold"}


def child_host_ids(store: ConversationStore, children: list[Conversation]) -> set[str]:
    """Resolve inherited host placement without filtering on nullable child host_id."""
    hosts: set[str] = set()
    for child in children:
        inherited = (
            _inherited_placement(store, child.parent_conversation_id)
            if child.parent_conversation_id
            else Placement(None, None, None)
        )
        host = _effective_placement(child, inherited).host_id
        if host:
            hosts.add(host)
    return hosts


def hosts_line(hub: SystemStatusHub, workspace_id: int, hosts: set[str], now: float) -> str:
    rows = []
    for host in sorted(hosts):
        load = hub.host_load(workspace_id, host, now=now)
        value = load["load_per_core"]
        reading = f"{value:.2f}" if value is not None else "unknown"
        rows.append(f"{hub.host_name(workspace_id, host)} {reading}/core {load['level']}")
    return "Hosts: " + "; ".join(rows)


def _notice(hub: SystemStatusHub, workspace: int, host: str, now: float) -> str:
    load = hub.host_load(workspace, host, now=now)
    level = load["level"]
    value = load["load_per_core"]
    reading = f"{value:.2f}" if value is not None else "unknown"
    prefix = f"[System: host load] Host {hub.host_name(workspace, host)} "
    if level == "heavy_hold":
        if load["reason"] == "memory":
            memory = load["mem_pct"]
            memory_text = f"{memory:.1f}" if memory is not None else "unknown"
            threshold = hub.effective_load_lines(workspace, host).mem_pct
            detail = f"memory {memory_text}% ≥ {threshold:g}%"
        else:
            detail = f"{reading} per core ≥ {load['lines']['heavy']:g}"
        return (
            prefix + f"is above its heavy-work line ({detail}). New heavy test runs on this "
            "host wait. Do not dispatch new heavy work to it until a later notice "
            "says it is clear."
        )
    if level == "new_session_hold":
        action = (
            "New child sessions created on this host are queued and start automatically "
            "when it drops below the line; heavy test runs wait."
            if hub.get_settings(workspace)["load_lines"]["queue_enabled"]
            else "New child-session queuing is disabled. Do not dispatch new child sessions "
            "or heavy test runs to this host until it is clear."
        )
        return (
            prefix + f"is above its new-session line ({reading} per core ≥ "
            f"{load['lines']['new_session']:g}). {action}"
        )
    if level in {"normal", "spare"}:
        return (
            prefix + f"is clear ({reading} per core, below {load['lines']['heavy']:g}). "
            "Heavy work may resume."
        )
    return (
        prefix + "load is unknown. Query sys_host_load for a fresh reading before scheduling work."
    )


@dataclass
class _Delivery:
    level: str | None = None
    at: float | None = None
    pending: bool = False


class HostLoadNotices:
    def __init__(
        self,
        *,
        conversation_store: ConversationStore,
        owner_for: Callable[[Conversation], str | None],
        owns_parent: Callable[[Conversation], bool],
        true_state: Callable[[Conversation], Awaitable[tuple[str, bool | None]]],
        post_event: Callable[..., Awaitable[Any]],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = conversation_store
        self._owner_for = owner_for
        self._owns_parent = owns_parent
        self._true_state = true_state
        self._post_event = post_event
        self._clock = clock
        self._deliveries: dict[tuple[int, str, str], _Delivery] = {}
        self._tasks: dict[tuple[int, str, str], asyncio.Task[None]] = {}
        self._slots = asyncio.Semaphore(4)

    def _recipients(self, host: str) -> set[str]:
        recipients: set[str] = set()
        after = None
        while True:
            page = self._store.list_conversations(
                kind="sub_agent", include_archived=False, limit=200, after=after
            )
            for child in page.data:
                if child.parent_conversation_id is None:
                    continue
                parent = self._store.get_conversation(child.parent_conversation_id)
                if (
                    parent is None
                    or parent.archived
                    or is_session_closed(parent.labels, parent.title)
                ):
                    continue
                if host in child_host_ids(self._store, [child, parent]):
                    recipients.add(parent.id)
            if not page.has_more or page.last_id is None:
                return recipients
            after = page.last_id

    async def tick(self, hub: SystemStatusHub, app: Any) -> None:
        for change in hub.drain_level_changes():
            # Spare/normal changes alone are visible in queries and check-ins.
            if change.old not in _HOLDS and change.new not in _HOLDS:
                continue
            with workspace_scope(change.workspace_id):
                recipients = await asyncio.to_thread(self._recipients, change.host_id)
            for parent in recipients:
                key = (change.workspace_id, parent, change.host_id)
                self._deliveries.setdefault(key, _Delivery()).pending = True
        for key, delivery in list(self._deliveries.items()):
            if not delivery.pending or key in self._tasks:
                continue
            self._tasks[key] = asyncio.create_task(
                self._run_delivery(hub, app, key, delivery), name="host-load-notice"
            )

    async def shutdown(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_delivery(
        self, hub: SystemStatusHub, app: Any, key: tuple[int, str, str], delivery: _Delivery
    ) -> None:
        workspace, parent_id, host = key
        try:
            async with self._slots:
                with workspace_scope(workspace):
                    await self._deliver(hub, app, key, delivery)
        except Exception:
            _logger.exception("Host-load notice failed for parent %s, host %s", parent_id, host)
        finally:
            self._tasks.pop(key, None)

    async def _deliver(
        self, hub: SystemStatusHub, app: Any, key: tuple[int, str, str], delivery: _Delivery
    ) -> None:
        workspace, parent_id, host = key
        parent = await asyncio.to_thread(self._store.get_conversation, parent_id)
        if parent is None or parent.archived or is_session_closed(parent.labels, parent.title):
            self._deliveries.pop(key, None)
            return
        if not self._owns_parent(parent):
            return
        now = self._clock()
        if delivery.at is not None and now - delivery.at < 300:
            return
        state, _online = await self._true_state(parent)
        if state not in {"idle", "steerable"}:
            return
        fresh = await asyncio.to_thread(self._store.get_conversation, parent_id)
        if (
            fresh is None
            or fresh.archived
            or is_session_closed(fresh.labels, fresh.title)
            or fresh.runner_id != parent.runner_id
            or fresh.archive_revision != parent.archive_revision
            or not self._owns_parent(fresh)
        ):
            return
        owner = await asyncio.to_thread(self._owner_for, fresh)
        now = self._clock()
        level = hub.host_load(workspace, host, now=now)["level"]
        if level == delivery.level or (
            level in {"normal", "spare"} and delivery.level not in _HOLDS
        ):
            delivery.pending = False
            return
        text = _notice(hub, workspace, host, now)
        previous = (delivery.level, delivery.at)
        # A lost response may already have woken the parent; spend the budget first.
        delivery.level, delivery.at, delivery.pending = level, now, False
        result = await asyncio.wait_for(
            self._post_event(
                PeerSweeper._synthetic_request(parent_id, app),
                parent_id,
                SessionEventInput(
                    type="message",
                    data={"role": "user", "content": [{"type": "input_text", "text": text}]},
                ),
                acting_user_id=owner,
            ),
            timeout=120,
        )
        if isinstance(result, dict) and result.get("forwarded") is False:
            delivery.level, delivery.at = previous
            delivery.pending = True
