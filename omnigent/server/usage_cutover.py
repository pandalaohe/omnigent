"""Server-side usage cutover gate shared by every turn admission path.

One predicate decides whether a session may start a new turn: while any domain
of the session's host is held, the gate refuses and writes nothing. An allowed
turn writes its admission before the input reaches the runner, so every call the
turn produces starts at or after the recorded bound.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from functools import lru_cache

from omnigent.entities import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.usage_export.cutover import UsageCutoverStore
from omnigent.usage_export.store import UsageExportError

# Session harness names to the four feed names.
_FEED_HARNESS = {
    "codex": "codex-sdk",
    "codex-sdk": "codex-sdk",
    "codex-native": "codex-native",
    "claude-code-native": "claude-native",
    "claude-native": "claude-native",
    "claude-sdk": "claude-sdk",
    "claude-code-sdk": "claude-sdk",
}


@lru_cache(maxsize=8)
def _store(storage_location: str) -> UsageCutoverStore:
    return UsageCutoverStore(storage_location)


def feed_harness(
    conv: Conversation, resolve_harness: Callable[[Conversation], str | None] | None
) -> str | None:
    """Resolve the session's feed harness, or ``None`` while it is undecided."""
    harness = conv.harness_override
    if not harness and resolve_harness is not None:
        harness = resolve_harness(conv)
    if not harness:
        return None
    return _FEED_HARNESS.get(harness)


def active_hold(
    storage_location: str,
    conv: Conversation,
    resolve_harness: Callable[[Conversation], str | None] | None,
) -> dict[str, object] | None:
    """The active hold refusing this session, if any."""
    if not conv.host_id:
        return None
    return _store(storage_location).active_hold(conv.host_id, feed_harness(conv, resolve_harness))


def admit_turn(
    storage_location: str,
    conv: Conversation,
    *,
    source: str,
    resolve_harness: Callable[[Conversation], str | None] | None,
    owner_id: str | None = None,
    admission_id: str | None = None,
) -> str | None:
    """Write the admission for an allowed turn, or refuse it with 409.

    Returns the admission id (``None`` when the session has no host yet, which
    no gate can evaluate). Raises :class:`OmnigentError` with
    ``usage_cutover_hold`` while the domain is held.
    """
    if not conv.host_id:
        return None
    store = _store(storage_location)
    harness = feed_harness(conv, resolve_harness)
    if store.active_hold(conv.host_id, harness) is not None:
        raise OmnigentError(
            "This session's host is in a usage cutover hold and cannot start new turns.",
            code=ErrorCode.USAGE_CUTOVER_HOLD,
        )
    try:
        record = store.admit_turn(
            owner_id or "local",
            admission_id=admission_id or uuid.uuid4().hex,
            session_id=conv.id,
            host_id=conv.host_id,
            harness=harness,
            source=source,
        )
    except UsageExportError as exc:
        if exc.code == "usage_cutover_hold":
            raise OmnigentError(
                "This session's host is in a usage cutover hold and cannot start new turns.",
                code=ErrorCode.USAGE_CUTOVER_HOLD,
            ) from exc
        raise
    return str(record["admission_id"])


def refuse_if_held(
    storage_location: str,
    conv: Conversation,
    *,
    resolve_harness: Callable[[Conversation], str | None] | None,
) -> None:
    """Refuse a non-turn control action (compact, keep-warm, skill command)."""
    if active_hold(storage_location, conv, resolve_harness) is not None:
        raise OmnigentError(
            "This session's host is in a usage cutover hold and cannot start new turns.",
            code=ErrorCode.USAGE_CUTOVER_HOLD,
        )
