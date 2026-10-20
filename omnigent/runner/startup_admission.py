"""Runner-side cold-start permission for server-issued startup attempts."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

import httpx

from omnigent.startup_protocol import StartupAttemptRef, StartupObservation


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
        self._attempts: dict[str, StartupAttemptRef | None] = {}
        self._observations: dict[str, dict[str, StartupObservation]] = {}
        if root_attempt is not None:
            if not root_session_id:
                raise ValueError("Startup attempt requires a bound root session")
            self.register(root_session_id, root_attempt)

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
            if self._attempts.get(session_id) != attempt:
                self._observations.pop(session_id, None)
            self._attempts[session_id] = attempt
        else:
            self.forget(session_id)

    def forget(self, session_id: str) -> None:
        self._attempts.pop(session_id, None)
        self.clear_observations(session_id)

    def clear_observations(self, session_id: str) -> None:
        self._observations.pop(session_id, None)

    def record_observation(self, session_id: str, observation: StartupObservation) -> bool:
        """Keep one typed Codex observation per phase for the current attempt."""
        attempt = self._attempts.get(session_id)
        if attempt is None or attempt != observation.attempt or observation.source != "codex":
            return False
        metadata = observation.model_copy(update={"reason": None})
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
        return session_id in self._attempts

    def native_admitter(self, session_id: str) -> Callable[[], Awaitable[None]]:
        async def admit() -> None:
            if self.required(session_id):
                # None of the native adapters has an exclusive first-input/work
                # proof contract yet. Refuse before claiming or booting it.
                raise RuntimeError("startup_proof_unavailable")

        return admit

    async def admit_harness(self, session_id: str, harness: str) -> None:
        if self.required(session_id) and harness != "codex":
            raise RuntimeError("startup_proof_unavailable")
        await self.claim(session_id, "harness_prepare")

    async def claim(self, session_id: str, boundary: str) -> None:
        if session_id not in self._attempts:
            return
        attempt = self._attempts[session_id]
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
