"""Durable intent for continuing a turn after its host loses the runner."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from omnigent.errors import ErrorCode, OmnigentError

if TYPE_CHECKING:
    from omnigent.entities.conversation import MessageData

RECOVERY_LABEL_PREFIX = "omnigent.recovery."
RECOVERY_NOTICE = "[System: session resumed]"
RECOVERY_PROMPT = (
    f"{RECOVERY_NOTICE}\n"
    "Your runner was interrupted while this task was active. "
    "Continue the existing task from its current state. "
    "Check any interrupted operation's outcome before repeating it."
)
MAX_RECOVERY_ATTEMPTS = 3
RECOVERY_MAX_AGE_S = 86400
RECOVERY_RETRY_S = 30
RECOVERY_ATTEMPT_S = 120


def is_native_recovery_echo(message: MessageData) -> bool:
    """Recognize a recovery prompt shape; the caller must also check its durable claim."""
    if message.role != "user" or message.is_meta or message.user_authored:
        return False
    if len(message.content) != 1 or message.content[0].get("type") != "input_text":
        return False
    text = message.content[0].get("text")
    return (
        isinstance(text, str)
        and text.startswith(f"{RECOVERY_NOTICE}\n")
        and bool(text[len(RECOVERY_NOTICE) :].strip())
    )


def reject_recovery_labels(keys: Iterable[str]) -> None:
    """Only the recovery store may mutate its private labels."""
    if any(key.lower().startswith(RECOVERY_LABEL_PREFIX) for key in keys):
        raise OmnigentError(
            "Session recovery labels are server-internal", code=ErrorCode.INVALID_INPUT
        )


@dataclass(frozen=True)
class SessionRecovery:
    """One interruption episode; transport retries retain the attempt identity."""

    host_id: str
    runner_id: str
    interrupted_at: int
    phase: Literal[
        "active", "pending", "launching", "dispatching", "submitted", "stopped", "failed"
    ] = "pending"
    attempts: int = 0
    attempt_id: str | None = None
    retry_at: int = 0
    launch_runner_id: str | None = None
    response_id: str | None = None
    # A witnessed native start awaiting its matching input echo.
    next_response_id: str | None = None

    @property
    def accepts_native_echo(self) -> bool:
        return bool(self.attempt_id) and self.phase in {
            "dispatching",
            "submitted",
            "failed",
            "stopped",
        }
