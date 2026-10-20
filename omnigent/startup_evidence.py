"""Adapter-owned startup evidence carries control identities, never input text."""

from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Literal

from omnigent.startup_protocol import StartupAttemptRef, StartupObservation


@dataclass(frozen=True)
class _EvidenceScope:
    attempt: StartupAttemptRef | None
    emit: Callable[[StartupObservation], None]
    claim: Callable[[StartupAttemptRef, Literal["executor_prepare"]], Awaitable[bool]] | None
    required: bool


_scope: ContextVar[_EvidenceScope | None] = ContextVar("startup_evidence", default=None)


@contextmanager
def startup_evidence_scope(
    attempt: StartupAttemptRef | None,
    emit: Callable[[StartupObservation], None],
    claim: Callable[[StartupAttemptRef, Literal["executor_prepare"]], Awaitable[bool]]
    | None = None,
    *,
    required: bool = False,
) -> Iterator[None]:
    token = _scope.set(
        _EvidenceScope(attempt, emit, claim, required) if attempt is not None or required else None
    )
    try:
        yield
    finally:
        _scope.reset(token)


async def claim_startup_boundary(boundary: Literal["executor_prepare"]) -> None:
    owner = _scope.get()
    if owner is None:
        return
    if owner.attempt is None or owner.claim is None:
        raise RuntimeError("Startup permission unavailable")
    if not await owner.claim(owner.attempt, boundary):
        raise RuntimeError("Startup permission unavailable")


class CodexTurnEvidence:
    """The owning turn/start RPC supplies immutable thread and turn identities."""

    def __init__(self, thread_id: str, turn_id: str, *, source: str) -> None:
        if not thread_id or not turn_id or source not in ("codex", "codex-native"):
            raise ValueError("Invalid startup vendor binding")
        self.thread_id, self.turn_id, self.source = thread_id, turn_id, source
        self._owner = _scope.get()
        self._accepted = self._working = False

    def ready(self, *, tool_bridge_installed: bool) -> None:
        owner = self._owner
        if owner is None or owner.attempt is None or _scope.get() is not owner:
            return
        if owner.attempt.input_id is not None:
            owner.emit(
                StartupObservation(
                    attempt=owner.attempt,
                    phase="input_dispatched",
                    source=self.source,
                    vendor_session_id=self.thread_id,
                    vendor_turn_id=self.turn_id,
                )
            )
        phases = ("runtime_ready", "tools_ready") if tool_bridge_installed else ("runtime_ready",)
        for phase in phases:
            owner.emit(
                StartupObservation(
                    attempt=owner.attempt,
                    phase=phase,
                    source=self.source,
                    vendor_session_id=self.thread_id,
                    vendor_turn_id=self.turn_id,
                )
            )

    def notification(self, method: str, params: Mapping[str, Any]) -> None:
        owner = self._owner
        if (
            owner is None
            or owner.attempt is None
            or _scope.get() is not owner
            or owner.attempt.input_id is None
            or params.get("threadId") != self.thread_id
            or params.get("turnId") != self.turn_id
        ):
            return
        phase = None
        item = params.get("item")
        if (
            not self._accepted
            and method == "item/completed"
            and isinstance(item, dict)
            and item.get("type") == "userMessage"
            and isinstance(item.get("id"), str)
            and item["id"]
        ):
            phase = "input_accepted"
            self._accepted = True
        elif not self._working:
            delta = params.get("delta")
            if (
                (
                    method in ("item/started", "item/completed")
                    and isinstance(item, dict)
                    and isinstance(item.get("id"), str)
                    and item["id"]
                    and item.get("type")
                    in (
                        "commandExecution",
                        "fileChange",
                        "dynamicToolCall",
                        "webSearch",
                        "imageView",
                        "imageGeneration",
                    )
                )
                or (
                    method
                    in (
                        "item/commandExecution/requestApproval",
                        "item/fileChange/requestApproval",
                        "item/permissions/requestApproval",
                    )
                    and isinstance(params.get("itemId"), str)
                    and params["itemId"]
                )
                or (
                    method == "item/completed"
                    and isinstance(item, dict)
                    and item.get("type") == "agentMessage"
                    and isinstance(item.get("text"), str)
                    and item["text"]
                )
                or (
                    method
                    in (
                        "item/agentMessage/delta",
                        "item/plan/delta",
                        "item/reasoning/textDelta",
                        "item/reasoning/summaryTextDelta",
                        "item/commandExecution/outputDelta",
                    )
                    and isinstance(delta, str)
                    and delta
                )
                or (
                    method == "item/tool/call"
                    and isinstance(params.get("callId"), str)
                    and params["callId"]
                    and isinstance(params.get("tool"), str)
                    and params["tool"]
                )
            ):
                phase = "work_started"
            self._working = phase is not None
        if phase is not None:
            owner.emit(
                StartupObservation(
                    attempt=owner.attempt,
                    phase=phase,
                    source=self.source,
                    vendor_session_id=self.thread_id,
                    vendor_turn_id=self.turn_id,
                )
            )
