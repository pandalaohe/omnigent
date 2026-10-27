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


class StartupPermissionUnavailable(RuntimeError):
    """A refused cold boundary must preserve any existing runtime."""


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
        raise StartupPermissionUnavailable("Startup permission unavailable")
    if not await owner.claim(owner.attempt, boundary):
        raise StartupPermissionUnavailable("Startup permission unavailable")


def opening_settled() -> None:
    """The owning local handoff returned; this says nothing about vendor work."""
    owner = _scope.get()
    if owner is not None and owner.attempt is not None and owner.attempt.mode == "startup_only":
        owner.emit(
            StartupObservation(
                attempt=owner.attempt,
                phase="opening_settled",
                source="runner",
            )
        )


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


class ClaudeTurnEvidence:
    """Bind a cold SDK query to the CLI prompt hook and its main response stream."""

    def __init__(self, prompt: str, *, tool_bridge_installed: bool = True) -> None:
        self._owner = _scope.get()
        self._prompt = prompt
        self._tool_bridge_installed = tool_bridge_installed
        self._session_id: str | None = None
        self._working = False
        if (
            self._owner is not None
            and self._owner.attempt is not None
            and self._owner.attempt.input_id is None
        ):
            raise RuntimeError("Claude SDK startup proof unavailable")

    @property
    def enabled(self) -> bool:
        return (
            self._owner is not None
            and self._owner.attempt is not None
            and self._owner.attempt.mode != "startup_only"
        )

    def _emit(
        self,
        phase: Literal[
            "runtime_ready", "tools_ready", "input_dispatched", "input_accepted", "work_started"
        ],
    ) -> None:
        owner = self._owner
        if (
            owner is None
            or owner.attempt is None
            or owner.attempt.input_id is None
            or _scope.get() is not owner
            or self._session_id is None
        ):
            return
        owner.emit(
            StartupObservation(
                attempt=owner.attempt,
                phase=phase,
                source="claude-sdk",
                vendor_session_id=self._session_id,
                # A cold client has exactly one admitted startup query. Its
                # CLI session id identifies that query until its first result.
                vendor_turn_id=self._session_id,
            )
        )

    def prompt_submitted(self, payload: Mapping[str, Any]) -> None:
        if not self.enabled or self._session_id is not None:
            return
        session_id = payload.get("session_id")
        if (
            payload.get("hook_event_name") != "UserPromptSubmit"
            or payload.get("prompt") != self._prompt
            or not isinstance(session_id, str)
            or not session_id
            or payload.get("agent_id") is not None
        ):
            return
        self._session_id = session_id
        self._prompt = ""
        phases = (
            ("input_dispatched", "runtime_ready", "tools_ready")
            if self._tool_bridge_installed
            else ("input_dispatched", "runtime_ready")
        )
        for phase in phases:
            self._emit(phase)

    def message(self, message: object) -> None:
        if self._working or self._session_id is None:
            return
        if (
            getattr(message, "session_id", None) != self._session_id
            or getattr(message, "parent_tool_use_id", None) is not None
        ):
            return
        event = getattr(message, "event", None)
        work = False
        if isinstance(event, dict):
            if event.get("type") == "content_block_start":
                block = event.get("content_block")
                work = (
                    isinstance(block, dict)
                    and block.get("type") == "tool_use"
                    and bool(block.get("id"))
                )
            elif event.get("type") == "content_block_delta":
                delta = event.get("delta")
                work = isinstance(delta, dict) and (
                    (delta.get("type") == "text_delta" and bool(delta.get("text")))
                    or (delta.get("type") == "thinking_delta" and bool(delta.get("thinking")))
                )
        else:
            content = getattr(message, "content", None)
            if isinstance(content, list):
                if getattr(message, "error", None) is None:
                    work = any(
                        bool(getattr(block, "text", None))
                        or bool(getattr(block, "thinking", None))
                        or (
                            bool(getattr(block, "id", None)) and bool(getattr(block, "name", None))
                        )
                        for block in content
                    )
            elif getattr(message, "is_error", None) is False:
                num_turns = getattr(message, "num_turns", None)
                work = (
                    isinstance(num_turns, int)
                    and num_turns > 0
                    and bool(getattr(message, "result", None))
                )
        if work:
            self._emit("input_accepted")
            self._emit("work_started")
            self._working = True
