"""Single owner for a Runner session's runtime generation and reclaim lock."""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass, field
from typing import Literal

RuntimePhase = Literal["absent", "starting", "live", "reclaiming"]


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
        self._locks: dict[str, asyncio.Lock] = {}
        self._states: dict[str, _SessionRuntimeState] = {}

    @property
    def boot_id(self) -> str:
        return self._boot_id

    def lock_for(self, session_id: str) -> asyncio.Lock:
        """Return the stable process-lifetime lock for one session."""
        return self._locks.setdefault(session_id, asyncio.Lock())

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
