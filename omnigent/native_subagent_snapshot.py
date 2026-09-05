"""Bounded, observation-only native child inventory and delivery protocol."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import quote

import httpx

NATIVE_SUBAGENT_SNAPSHOT_EVENT = "external_native_subagent_snapshot"
NATIVE_SUBAGENT_HEARTBEAT_INTERVAL_S = 15.0
NATIVE_SUBAGENT_ACTIVE_STATUSES = frozenset({"running", "waiting", "activity_unverified"})
NATIVE_SUBAGENT_STATUSES = NATIVE_SUBAGENT_ACTIVE_STATUSES | {
    "idle",
    "completed",
    "failed",
    "stopped",
    "killed",
}
MAX_NATIVE_SNAPSHOT_CHILDREN = 512


@dataclass(frozen=True)
class NativeSubagentSnapshot:
    """A forwarder's inventory, never proof that an omitted child ended.

    Generation is the Host monotonic clock at forwarder creation. It orders
    overlapping forwarders on one current runner binding without wall-clock
    skew; a Host reboot requires a new runner binding. Sequence orders posts
    within that generation. Neither identity grants task-status authority.
    """

    generation: int
    sequence: int
    children: dict[str, str]
    complete: bool = True
    retired: bool = False

    @property
    def active_child_ids(self) -> frozenset[str]:
        return frozenset(
            child
            for child, status in self.children.items()
            if status in NATIVE_SUBAGENT_ACTIVE_STATUSES
        )


def parse_native_subagent_snapshot(payload: object) -> NativeSubagentSnapshot:
    """Reject malformed/oversized inventories instead of silently truncating."""
    if not isinstance(payload, Mapping):
        raise ValueError("snapshot data must be an object")
    generation, sequence = payload.get("generation"), payload.get("sequence")
    for name, value in (("generation", generation), ("sequence", sequence)):
        if type(value) is not int or not 0 < value < 2**63:
            raise ValueError(f"snapshot {name} must be a positive 63-bit integer")
    complete, retired = payload.get("complete", True), payload.get("retired", False)
    if not isinstance(complete, bool) or not isinstance(retired, bool):
        raise ValueError("snapshot complete/retired must be booleans")
    raw = payload.get("children")
    if not isinstance(raw, list) or len(raw) > MAX_NATIVE_SNAPSHOT_CHILDREN:
        raise ValueError("snapshot children must be a list of at most 512 entries")
    children: dict[str, str] = {}
    for row in raw:
        if not isinstance(row, Mapping):
            raise ValueError("snapshot child must be an object")
        child, status = row.get("session_id"), row.get("status")
        if not isinstance(child, str) or not child or len(child) > 512:
            raise ValueError("invalid snapshot child session_id")
        if child in children:
            raise ValueError("duplicate snapshot child session_id")
        if not isinstance(status, str) or status not in NATIVE_SUBAGENT_STATUSES:
            raise ValueError("invalid snapshot child status")
        children[child] = status
    if retired and children:
        raise ValueError("retired snapshot must have no children")
    assert isinstance(generation, int) and isinstance(sequence, int)
    return NativeSubagentSnapshot(generation, sequence, children, complete, retired)


@dataclass
class _PendingInventory:
    children: tuple[tuple[str, str], ...]
    complete: bool
    retired: bool
    changed_at: float
    sent: bool = False
    next_attempt: float = 0.0


class NativeSubagentSnapshotPublisher:
    """One bounded sender with heartbeat and retries, including final empties.

    The caller owns the HTTP client and cancellation lifetime. Unsupported old
    Servers disable this optional protocol; they retain normal status events.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        heartbeat_s: float = 15.0,
        retry_s: float = 5.0,
        observation_timeout_s: float | None = None,
    ) -> None:
        self._client = client
        self._heartbeat_s = heartbeat_s
        self._retry_s = retry_s
        self._observation_timeout_s = observation_timeout_s
        self._generation = time.monotonic_ns()
        self._sequence = 0
        self._inventories: dict[str, _PendingInventory] = {}
        self._changed = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._disabled = False

    async def __aenter__(self) -> NativeSubagentSnapshotPublisher:
        self._task = asyncio.create_task(self._run(), name="native-child-snapshots")
        return self

    async def __aexit__(self, *_args: object) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    def update(
        self,
        parent_id: str,
        children: Mapping[str, str],
        *,
        retired: bool = False,
    ) -> None:
        if self._disabled:
            return
        # An oversized inventory is explicitly partial. Never pretend a
        # truncated list proves the remaining children disappeared.
        complete = len(children) <= MAX_NATIVE_SNAPSHOT_CHILDREN
        rows = tuple(sorted(children.items()))[:MAX_NATIVE_SNAPSHOT_CHILDREN]
        now = time.monotonic()
        old = self._inventories.get(parent_id)
        if old is None and not rows and not retired:
            # No child has ever been observed: do not arm the optional
            # protocol or add requests to an ordinary native conversation.
            return
        if old and (old.children, old.complete, old.retired) == (rows, complete, retired):
            old.changed_at = now
            return
        if old is None and len(self._inventories) >= 64:
            # Only retired acknowledgements should normally remain here.
            # Refuse excess bookkeeping rather than grow with /clear forever.
            return
        self._inventories[parent_id] = _PendingInventory(
            rows,
            complete,
            retired,
            now,
            next_attempt=old.next_attempt if old else 0.0,
        )
        self._changed.set()

    async def _run(self) -> None:
        while not self._disabled:
            self._changed.clear()
            next_due = float("inf")
            for parent_id, pending in list(self._inventories.items()):
                now = time.monotonic()
                active = any(
                    status in NATIVE_SUBAGENT_ACTIVE_STATUSES for _, status in pending.children
                )
                if pending.sent and not active:
                    continue
                if (
                    pending.sent
                    and active
                    and self._observation_timeout_s is not None
                    and now - pending.changed_at > self._observation_timeout_s
                ):
                    # A stuck Claude poll must not be hidden by cached heartbeats.
                    # Terminal/empty/retired observations remain true after the
                    # poll moves on, so their delivery retry cannot expire here.
                    next_due = min(next_due, now + self._heartbeat_s)
                    continue
                if pending.next_attempt > now:
                    next_due = min(next_due, pending.next_attempt)
                    continue
                self._sequence += 1
                try:
                    response = await self._client.post(
                        f"/v1/sessions/{quote(parent_id, safe='')}/events",
                        json={
                            "type": NATIVE_SUBAGENT_SNAPSHOT_EVENT,
                            "data": {
                                "generation": self._generation,
                                "sequence": self._sequence,
                                "children": [
                                    {"session_id": child, "status": status}
                                    for child, status in pending.children
                                ],
                                "complete": pending.complete,
                                "retired": pending.retired,
                            },
                        },
                        timeout=10.0,
                    )
                    if response.status_code in {400, 403, 404, 422, 501}:
                        # This internal event did not exist on older Servers.
                        self._disabled = True
                        return
                    accepted = 200 <= response.status_code < 300
                except (httpx.HTTPError, ConnectionError):
                    accepted = False
                current = self._inventories.get(parent_id)
                if current is not pending:
                    if current is not None and not accepted:
                        # A state change during an outage must not bypass the
                        # failure backoff and turn vendor churn into hot POSTs.
                        current.next_attempt = max(
                            current.next_attempt,
                            time.monotonic() + self._retry_s,
                        )
                    continue
                if accepted:
                    pending.sent = True
                    if pending.retired:
                        self._inventories.pop(parent_id, None)
                        continue
                pending.next_attempt = time.monotonic() + (
                    self._heartbeat_s if accepted else self._retry_s
                )
                next_due = min(next_due, pending.next_attempt)
            delay = max(0.0, next_due - time.monotonic())
            if delay == float("inf"):
                await self._changed.wait()
            else:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._changed.wait(), timeout=delay)
