"""Runner-side cold-start permission for server-issued startup attempts."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

import httpx

from omnigent.startup_protocol import (
    QUEUE_STARTUP_SOURCES,
    SDK_STARTUP_SOURCES,
    StartupAttemptRef,
    StartupObservation,
)


class StartupAdmission:
    """Keep startup attempts bound to their own sessions in a shared runner."""

    def __init__(
        self,
        server_client: httpx.AsyncClient,
        *,
        root_session_id: str | None = None,
        root_attempt: StartupAttemptRef | None = None,
    ) -> None:
        self._server_client = server_client
        self._enabled = False
        self._queue_mode = False
        self._recreate_exempt: set[str] = set()
        self._unrequired: set[str] = set()
        self._control_versions: dict[str, int] = {}
        self._attempts: dict[str, StartupAttemptRef | None] = {}
        self._observations: dict[str, dict[str, StartupObservation]] = {}
        if root_attempt is not None:
            if not root_session_id:
                raise ValueError("Startup attempt requires a bound root session")
            self.register(root_session_id, root_attempt)

    def set_policy(self, enabled: bool) -> None:
        """Replace process policy from a trusted server initialization snapshot."""
        if self._enabled == enabled:
            return
        for session_id in list(self._control_versions):
            self.forget(session_id)
        self._enabled = enabled
        self._queue_mode = False

    def register(
        self,
        session_id: str,
        attempt: StartupAttemptRef | None,
        *,
        required: bool = True,
    ) -> None:
        """Install a reference from trusted runner control input, including missing-ref refusal."""
        if not session_id:
            raise ValueError("Startup session id is required")
        if required:
            self._control_versions[session_id] = self._control_versions.get(session_id, 0) + 1
            self._recreate_exempt.discard(session_id)
            self._unrequired.discard(session_id)
            self._enabled = True
            if attempt is not None and attempt.mode == "startup_only":
                self._queue_mode = True
            if self._attempts.get(session_id) != attempt:
                self._observations.pop(session_id, None)
            self._attempts[session_id] = attempt
        else:
            self.forget(session_id)
            self._unrequired.add(session_id)

    def forget(self, session_id: str) -> None:
        self._control_versions[session_id] = self._control_versions.get(session_id, 0) + 1
        self._recreate_exempt.discard(session_id)
        self._unrequired.discard(session_id)
        self._attempts.pop(session_id, None)
        self.clear_observations(session_id)

    def clear_observations(self, session_id: str) -> None:
        self._observations.pop(session_id, None)

    def record_observation(self, session_id: str, observation: StartupObservation) -> bool:
        """Keep one typed SDK observation per phase for the current attempt."""
        attempt = self._attempts.get(session_id)
        if (
            attempt is None
            or attempt != observation.attempt
            or not (
                observation.source in SDK_STARTUP_SOURCES
                or (
                    attempt.mode == "startup_only"
                    and observation.source == "runner"
                    and observation.phase == "opening_settled"
                )
            )
            or any(
                item.source != observation.source
                and item.phase != "opening_settled"
                and observation.phase != "opening_settled"
                for item in self._observations.get(session_id, {}).values()
            )
        ):
            return False
        metadata = (
            observation
            if observation.phase == "opening_settled"
            else observation.model_copy(update={"reason": None})
        )
        self._observations.setdefault(session_id, {}).setdefault(observation.phase, metadata)
        return True

    def observations_for(
        self, session_id: str, attempt: StartupAttemptRef
    ) -> list[StartupObservation] | None:
        """Return metadata only when the caller names the current registered attempt."""
        current = self._attempts.get(session_id)
        if current is None or current != attempt:
            return None
        return list(self._observations.get(session_id, {}).values())

    def required(self, session_id: str) -> bool:
        return session_id in self._attempts and session_id not in self._recreate_exempt

    def recreate_is_exempt(self, session_id: str) -> bool:
        return session_id in self._recreate_exempt

    def suppress_initialization_recovery(self, session_id: str) -> bool:
        """The startup-only input owner, rather than initialization, starts execution."""
        attempt = self._attempts.get(session_id)
        return (
            self._queue_mode
            and session_id in self._attempts
            and (attempt is None or attempt.mode == "startup_only")
        )

    async def refresh_exemption(self, session_id: str) -> bool:
        """Only the live server can exempt an already opened session from cold admission."""
        self._recreate_exempt.discard(session_id)
        version = self._control_versions.get(session_id, 0) + 1
        self._control_versions[session_id] = version
        if not self._queue_mode or session_id in self._unrequired:
            return False
        try:
            response = await self._server_client.post(
                f"/v1/sessions/{session_id}/startup/recreate-exemption",
                timeout=5.0,
            )
            if (
                response.status_code == 200
                and response.json() == {"exempt": True}
                and self._control_versions.get(session_id) == version
            ):
                self._recreate_exempt.add(session_id)
                return True
        except (httpx.HTTPError, ValueError, RuntimeError):
            pass
        return False

    def native_admitter(self, session_id: str) -> Callable[[], Awaitable[None]]:
        async def admit() -> None:
            if self._enabled and session_id not in self._unrequired:
                version = self._control_versions.get(session_id, 0)
                exempt = await self.refresh_exemption(session_id)
                if self._control_versions.get(session_id) != version + 1:
                    raise RuntimeError("Startup control changed during exemption check")
                if exempt:
                    return
                attempt = self._attempts.get(session_id)
                if attempt is None or attempt.mode != "startup_only":
                    raise RuntimeError("startup_proof_unavailable")
                await self.claim(session_id, "native_prepare")

        return admit

    async def admit_harness(self, session_id: str, harness: str) -> None:
        if not self._enabled or session_id in self._unrequired:
            return
        version = self._control_versions.get(session_id, 0)
        exempt = await self.refresh_exemption(session_id)
        if self._control_versions.get(session_id) != version + 1:
            raise RuntimeError("Startup control changed during exemption check")
        if exempt:
            return
        attempt = self._attempts.get(session_id)
        sources = (
            QUEUE_STARTUP_SOURCES
            if attempt and attempt.mode == "startup_only"
            else SDK_STARTUP_SOURCES
        )
        if self._enabled and harness not in sources:
            raise RuntimeError("startup_proof_unavailable")
        await self.claim(session_id, "harness_prepare")

    async def opening_settled(
        self, session_id: str, expected: StartupAttemptRef | None, *, waiting_input: bool = False
    ) -> bool:
        """Cache completion from the owning awaited local setup/handoff path."""
        attempt = self._attempts.get(session_id)
        if session_id in self._recreate_exempt:
            return False
        if attempt is None or attempt != expected or attempt.mode != "startup_only":
            return False
        if attempt.input_id is not None:
            return False
        observation = StartupObservation(
            attempt=attempt,
            phase="opening_settled",
            source="runner",
            reason="waiting_input" if waiting_input else None,
        )
        if not self.record_observation(session_id, observation):
            return False
        try:
            response = await self._server_client.post(
                f"/v1/sessions/{session_id}/startup/receipts",
                json=observation.model_dump(),
                timeout=5.0,
            )
            return response.status_code == 200 and response.json() == {"accepted": True}
        except (httpx.HTTPError, ValueError):
            return False

    def empty_settler(self, session_id: str) -> Callable[[], Awaitable[None]]:
        expected = self._attempts.get(session_id)

        async def settle() -> None:
            await self.opening_settled(session_id, expected)

        return settle

    async def claim(self, session_id: str, boundary: str) -> None:
        if not self._enabled or session_id in self._unrequired:
            return
        attempt = self._attempts.get(session_id)
        if attempt is None:
            raise RuntimeError("Startup permission unavailable")
        try:
            response = await self._server_client.post(
                f"/v1/sessions/{session_id}/startup/claims",
                json={
                    "attempt": attempt.model_dump(),
                    "boundary": boundary,
                    "claim_id": uuid.uuid4().hex,
                },
                timeout=5.0,
            )
            if response.status_code == 200 and response.json() == {"granted": True}:
                return
        except (httpx.HTTPError, ValueError, RuntimeError):
            pass
        raise RuntimeError("Startup permission unavailable")
