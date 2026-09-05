"""Identity-fenced native inventory liveness; never a task-status writer."""

from __future__ import annotations

import asyncio
import contextlib
import time
import weakref
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from omnigent.native_subagent_snapshot import NativeSubagentSnapshot

_watchdogs: weakref.WeakSet[NativeSubagentWatchdog] = weakref.WeakSet()


def disarm_native_subagent_watchdogs(parent_id: str, *, retire: bool = True) -> None:
    """Invalidate leases at the existing stop/archive/delete/rebind owners."""
    for watchdog in list(_watchdogs):
        watchdog.disarm(parent_id, retire=retire)


def native_subagent_activity_unverified(parent_id: str, child_id: str) -> bool:
    return any(watchdog.is_unverified(parent_id, child_id) for watchdog in list(_watchdogs))


@dataclass
class _Watch:
    binding: str
    snapshot: NativeSubagentSnapshot
    heartbeat: float
    missing: frozenset[str]
    wake: asyncio.Event
    task: asyncio.Task[None] | None = None
    unverified: bool = False
    attempts: int = 0


class NativeSubagentWatchdog:
    """At most one bounded, read-only verification flight per native parent.

    ``verify`` may GET existing Host evidence only. It cannot resume a runner,
    launch a pane, inject input or mutate task state. Results do not settle tasks.
    Old Hosts opt out by never sending a snapshot. Only liveness display is
    transient; native task terminal labels remain the durable source of truth.
    """

    def __init__(
        self,
        *,
        verify: Callable[[str, str], Awaitable[bool]],
        changed: Callable[[frozenset[str]], None],
        heartbeat_timeout_s: float = 35.0,
        retry_s: float = 10.0,
    ) -> None:
        self._verify = verify
        self._changed = changed
        self._timeout = heartbeat_timeout_s
        self._retry = retry_s
        self._states: dict[str, _Watch] = {}
        self._floors: OrderedDict[tuple[str, str], tuple[int, int]] = OrderedDict()
        _watchdogs.add(self)

    def heartbeat(self, parent_id: str, binding: str, snapshot: NativeSubagentSnapshot) -> bool:
        key = (parent_id, binding)
        incoming = (snapshot.generation, snapshot.sequence)
        if incoming <= self._floors.get(key, (0, 0)):
            return False
        old = self._states.get(parent_id)
        if (
            old is not None
            and old.binding == binding
            and incoming
            <= (
                old.snapshot.generation,
                old.snapshot.sequence,
            )
        ):
            return False
        if old is None and len(self._states) >= 256:
            return False
        self._floors[key] = incoming
        self._floors.move_to_end(key)
        while len(self._floors) > 1024:
            self._floors.popitem(last=False)
        if old is not None and old.binding != binding:
            self.disarm(parent_id)
            old = None
        if snapshot.retired:
            self._floors[key] = (snapshot.generation, 2**63 - 1)
            self.disarm(parent_id)
            return True
        previous = (old.snapshot.active_child_ids | old.missing) if old else frozenset()
        # Keep only bounded missing evidence. Overflow still means unknown
        # inventory; it never causes a terminal transition or a task clear.
        missing = frozenset(sorted(previous - snapshot.children.keys())[:512])
        if not snapshot.active_child_ids and not missing and snapshot.complete:
            self.disarm(parent_id)
            return True
        if old is not None:
            affected = old.snapshot.active_child_ids | old.missing | snapshot.active_child_ids
            content_changed = (old.snapshot.children, old.missing) != (snapshot.children, missing)
            was_unverified = old.unverified
            old.snapshot, old.heartbeat, old.missing = snapshot, time.monotonic(), missing
            old.unverified = bool(missing) or not snapshot.complete
            if content_changed or (was_unverified and not old.unverified):
                old.attempts = 0
            old.wake.set()
            if was_unverified != old.unverified:
                self._changed(affected)
            state = old
        else:
            state = _Watch(binding, snapshot, time.monotonic(), missing, asyncio.Event())
            state.unverified = bool(missing) or not snapshot.complete
            self._states[parent_id] = state
            if state.unverified:
                self._changed(snapshot.active_child_ids | missing)
        if state.task is None or state.task.done():
            state.task = asyncio.create_task(
                self._run(parent_id, state), name="native-child-liveness"
            )
        return True

    def is_unverified(self, parent_id: str, child_id: str) -> bool:
        state = self._states.get(parent_id)
        return bool(
            state
            and state.unverified
            and (child_id in state.snapshot.children or child_id in state.missing)
        )

    def disarm(self, parent_id: str, *, retire: bool = False) -> None:
        if retire:
            # A completed inventory may already have disarmed its worker;
            # Stop still retires that source so a delayed active POST cannot
            # arm it again after the user's lifecycle decision.
            for key, (generation, _sequence) in list(self._floors.items()):
                if key[0] == parent_id:
                    self._floors[key] = (generation, 2**63 - 1)
        state = self._states.pop(parent_id, None)
        if state is None:
            return
        if retire:
            self._floors[(parent_id, state.binding)] = (state.snapshot.generation, 2**63 - 1)
        if state.task is not None:
            try:
                current = asyncio.current_task()
            except RuntimeError:
                current = None
            if state.task is not current:
                state.task.get_loop().call_soon_threadsafe(state.task.cancel)
        if state.unverified:
            self._changed(state.snapshot.active_child_ids | state.missing)

    async def close(self) -> None:
        tasks = [state.task for state in self._states.values() if state.task is not None]
        for parent_id in list(self._states):
            self.disarm(parent_id)
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._floors.clear()
        _watchdogs.discard(self)

    async def _run(self, parent_id: str, state: _Watch) -> None:
        while self._states.get(parent_id) is state:
            state.wake.clear()
            stale = time.monotonic() - state.heartbeat >= self._timeout
            if stale or state.missing or not state.snapshot.complete:
                if not state.unverified:
                    state.unverified = True
                    self._changed(state.snapshot.active_child_ids | state.missing)
                if state.attempts >= 3:
                    return
                # Capturing the full source revision also fences a same-status
                # heartbeat arriving while an old Host GET is suspended.
                observed = state.snapshot
                state.attempts += 1
                try:
                    eligible = await self._verify(parent_id, state.binding)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - a failed read consumes one bounded attempt.
                    eligible = True
                if self._states.get(parent_id) is not state or state.snapshot is not observed:
                    continue
                if not eligible:
                    self.disarm(parent_id)
                    return
                delay = self._retry
            else:
                delay = max(0.0, self._timeout - (time.monotonic() - state.heartbeat))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(state.wake.wait(), timeout=delay)
