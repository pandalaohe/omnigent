"""Single owner for a Runner session's runtime generation and reclaim lock."""

from __future__ import annotations

import asyncio
import secrets
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Literal

RuntimePhase = Literal["absent", "starting", "live", "reclaiming"]


class SessionRuntimeLock:
    """Shared/exclusive lock over one session's CLI runtime.

    Session-init, a background turn and terminal creation all *use* the
    runtime, so they hold it shared and may overlap: a parent reconnecting
    mid-turn has to get its handshake answered instead of waiting the turn out.
    CLI retention's reclaim and the archive fence *replace or destroy* the
    runtime, so they hold it exclusive and wait for every user to leave.

    Waiters are granted in arrival order, which keeps a stream of overlapping
    turns from starving a pending reclaim and a stream of reclaims from
    starving a turn. Hand-rolled on futures because asyncio ships no
    reader/writer lock; the release path is deliberately synchronous, so a
    cancelled turn — which is how archive teardown reclaims the runtime —
    cannot lose its release to a second cancellation at an ``await``.
    """

    def __init__(self) -> None:
        self._shared_holders = 0
        self._exclusive_held = False
        # (exclusive?, future) in arrival order.
        self._waiters: deque[tuple[bool, asyncio.Future[None]]] = deque()

    def locked(self) -> bool:
        """Whether the runtime is held at all, in either mode."""
        return self._exclusive_held or self._shared_holders > 0

    @asynccontextmanager
    async def shared(self) -> AsyncIterator[None]:
        """Hold the runtime as a user, concurrently with other users."""
        await self._acquire(exclusive=False)
        try:
            yield
        finally:
            self._release(exclusive=False)

    @asynccontextmanager
    async def exclusive(self, *, timeout: float | None = None) -> AsyncIterator[None]:
        """Hold the runtime as its owner, with no user overlapping."""
        if timeout is None:
            await self._acquire(exclusive=True)
        else:
            async with asyncio.timeout(timeout):
                await self._acquire(exclusive=True)
        try:
            yield
        finally:
            self._release(exclusive=True)

    def _grantable(self, exclusive: bool) -> bool:
        if self._exclusive_held:
            return False
        return not exclusive or self._shared_holders == 0

    async def _acquire(self, *, exclusive: bool) -> None:
        if not self._waiters and self._grantable(exclusive):
            self._take(exclusive)
            return
        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters.append((exclusive, waiter))
        try:
            await waiter
        except BaseException:
            # Cancelled after the grant already landed: the hold was taken on
            # this task's behalf, so hand it back rather than leave the runtime
            # held by a task that is unwinding.
            if waiter.done() and not waiter.cancelled():
                self._release(exclusive=exclusive)
            else:
                self._drain()
            raise

    def _take(self, exclusive: bool) -> None:
        if exclusive:
            self._exclusive_held = True
        else:
            self._shared_holders += 1

    def _release(self, *, exclusive: bool) -> None:
        if exclusive:
            self._exclusive_held = False
        else:
            self._shared_holders -= 1
        self._drain()

    def _drain(self) -> None:
        """Grant the queue head, plus the shared run behind a shared head."""
        while self._waiters:
            exclusive, waiter = self._waiters[0]
            if waiter.done():  # cancelled while queued
                self._waiters.popleft()
                continue
            if not self._grantable(exclusive):
                return
            self._waiters.popleft()
            self._take(exclusive)
            waiter.set_result(None)
            if exclusive:
                return


@dataclass
class _SessionRuntimeState:
    generation: int = 0
    phase: RuntimePhase = "absent"
    policy_host_id: str | None = None
    policy_revision: int | None = None
    archive_states: dict[str, tuple[int, bool]] = field(default_factory=dict)


class SessionRuntimeLifecycle:
    """Linearize runtime creation/reclaim and fence stale destructive work."""

    def __init__(self, *, boot_id: str | None = None) -> None:
        self._boot_id = boot_id or secrets.token_hex(16)
        self._locks: dict[str, SessionRuntimeLock] = {}
        self._states: dict[str, _SessionRuntimeState] = {}

    @property
    def boot_id(self) -> str:
        return self._boot_id

    def lock_for(self, session_id: str) -> SessionRuntimeLock:
        """Return the stable process-lifetime lock for one session."""
        return self._locks.setdefault(session_id, SessionRuntimeLock())

    def _state(self, session_id: str) -> _SessionRuntimeState:
        return self._states.setdefault(session_id, _SessionRuntimeState())

    def runtime_token(self, session_id: str) -> str:
        state = self._state(session_id)
        return f"{self._boot_id}:{state.generation}"

    def runtime_token_matches(self, session_id: str, token: str) -> bool:
        """Return whether queued work still targets the current generation."""
        return token == self.runtime_token(session_id)

    def observe_archive_state(
        self,
        session_id: str,
        *,
        scope_id: str,
        revision: int,
        archived: bool,
    ) -> bool:
        """Apply a versioned archive fence and reject superseded messages."""
        state = self._state(session_id)
        current = state.archive_states.get(scope_id)
        if current is not None:
            current_revision, current_archived = current
            if revision < current_revision:
                return False
            if revision == current_revision and archived != current_archived:
                return False
        changed = current != (revision, archived)
        state.archive_states[scope_id] = (revision, archived)
        if changed:
            # Invalidate work queued before either archive or unarchive won.
            state.generation += 1
        return True

    def runtime_start_allowed(self, session_id: str) -> bool:
        """Return whether the Server has left this runtime unfenced."""
        return not any(
            archived for _revision, archived in self._state(session_id).archive_states.values()
        )

    def archive_revision(self, session_id: str, scope_id: str) -> int:
        """Return the latest revision observed for one archive-operation scope."""
        return self._state(session_id).archive_states.get(scope_id, (-1, False))[0]

    def archive_fence_matches(
        self,
        session_id: str,
        *,
        scope_id: str,
        revision: int,
    ) -> bool:
        """Confirm a delayed close still owns the same active archive fence."""
        return self._state(session_id).archive_states.get(scope_id) == (revision, True)

    def observe_policy(self, session_id: str, *, host_id: str, revision: int) -> bool:
        """Remember the exact Host policy scope applied by the latest snapshot."""
        state = self._state(session_id)
        if state.policy_host_id != host_id:
            state.policy_host_id = host_id
            state.policy_revision = revision
            return True
        if state.policy_revision is not None and revision < state.policy_revision:
            return False
        if state.policy_revision is None or revision > state.policy_revision:
            state.policy_revision = revision
        return True

    def observe_reset(self, session_id: str, *, host_id: str, revision: int) -> bool:
        """Accept a reset only from the host currently owning this policy scope."""
        state = self._state(session_id)
        if state.policy_host_id is not None and state.policy_host_id != host_id:
            return False
        return self.observe_policy(session_id, host_id=host_id, revision=revision)

    def policy_matches(self, session_id: str, *, host_id: str, revision: int) -> bool:
        state = self._state(session_id)
        return state.policy_host_id == host_id and state.policy_revision == revision

    def mark_starting(self, session_id: str) -> None:
        state = self._state(session_id)
        # A create/ensure request is the linearization point for a new runtime
        # incarnation. Advancing here fences an idle intent captured before
        # the request even when the ensure path reuses some underlying pieces.
        state.generation += 1
        state.phase = "starting"

    def mark_live(self, session_id: str) -> None:
        self._state(session_id).phase = "live"

    def claim_reclaim(
        self,
        session_id: str,
        *,
        expected_runtime_token: str | None = None,
        policy_host_id: str | None = None,
        policy_revision: int | None = None,
    ) -> bool:
        """Advance the generation and enter reclaiming after all fences match."""
        state = self._state(session_id)
        if expected_runtime_token is not None and expected_runtime_token != self.runtime_token(
            session_id
        ):
            return False
        if policy_host_id is not None and policy_revision is not None:
            if not self.policy_matches(
                session_id, host_id=policy_host_id, revision=policy_revision
            ):
                return False
        if state.phase == "reclaiming":
            return False
        state.phase = "reclaiming"
        state.generation += 1
        return True

    def finish_reclaim(self, session_id: str, *, present: bool = False) -> None:
        self._state(session_id).phase = "live" if present else "absent"

    def phase(self, session_id: str) -> RuntimePhase:
        return self._state(session_id).phase
