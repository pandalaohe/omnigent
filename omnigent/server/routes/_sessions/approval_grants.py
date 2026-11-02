"""One-shot grants for deferred ("deny now, approve later") approvals."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

from omnigent.server.routes._sessions.helpers import _canonical_tool_input
from omnigent.server.schemas import ElicitationResult


def claude_grant_key(
    tool_name: str,
    tool_input: dict[str, Any] | None,
    cwd: str | None,
) -> tuple[str, ...]:
    """
    Build the match key for a Claude-native deferred approval.

    ``Bash`` keys on the command and payload cwd only: the model retypes
    the description and timeout when it re-issues the call, so those
    fields must not break the match. Every other tool keys on the
    canonicalized full input plus cwd: the same input under a different
    working directory is a different call.

    :param tool_name: Gated tool from the PermissionRequest payload.
    :param tool_input: Tool input from the payload, or ``None``.
    :param cwd: Payload cwd, or ``None`` when absent.
    :returns: Hashable key scoped to one session.
    """
    if tool_name == "Bash":
        command = tool_input.get("command") if isinstance(tool_input, dict) else None
        return ("Bash", command if isinstance(command, str) else "", cwd or "")
    return (
        tool_name,
        json.dumps(_canonical_tool_input(tool_input), sort_keys=True),
        cwd or "",
    )


def codex_command_grant_key(
    raw_command: Any,
    cwd: str | None,
) -> tuple[str, ...]:
    """
    Build the match key for a Codex command approval.

    The protocol's raw ``params.command`` is used as sent — a string, or
    an argv list with its boundaries kept — never the display preview:
    ``_codex_command_preview`` joins argv with spaces, so
    ``["printf", "%s", "a b"]`` and ``["printf", "%s", "a", "b"]``
    would collide.

    :param raw_command: Codex ``params.command`` as received.
    :param cwd: Codex ``params.cwd``, or ``None``.
    :returns: Hashable key scoped to one session.
    """
    return ("codex_command", json.dumps(raw_command), cwd or "")


class ApprovalGrants:
    """
    In-memory single-use grants, keyed by ``(session_id, key)``.

    A grant is written when the user accepts a deferred approval card
    and consumed by the next hook call carrying the exact same key; a
    session can never consume another session's grant. Expired entries
    are pruned on access. Server startup restores grants from their durable
    card records after the in-memory cache is lost.

    :param clock: Monotonic clock; injectable for tests.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._grants: dict[
            tuple[str, tuple[str, ...]], tuple[ElicitationResult, float, str | None, int]
        ] = {}

    def put(
        self,
        session_id: str,
        key: tuple[str, ...],
        verdict: ElicitationResult,
        ttl_s: float,
        *,
        source_id: str | None = None,
        answered_at: int | None = None,
    ) -> None:
        """
        Store one grant unless a later answer already superseded it.

        :param session_id: Session the approved call belongs to.
        :param key: Match key from :func:`claude_grant_key` /
            :func:`codex_command_grant_key`.
        :param verdict: The accepted web verdict; its content drives the
            grant-hit decision (remember / allow-all-edits / auto mode).
        :param ttl_s: Lifetime in seconds, e.g. ``86400``.
        :param source_id: Durable card id backing this grant, when available.
        :param answered_at: Durable answer sequence, or a new local order when omitted.
        :returns: None.
        """
        self._prune()
        now = self._clock()
        previous = self._grants.get((session_id, key))
        if answered_at is None:
            answered_at = previous[3] + 1 if previous is not None else 0
        if previous is not None and answered_at < previous[3]:
            return
        self._grants[(session_id, key)] = (verdict, now + ttl_s, source_id, answered_at)

    def discard_if_source(self, session_id: str, key: tuple[str, ...], source_id: str) -> None:
        """Revoke only a grant backed by this card, preserving a newer grant."""
        entry = self._grants.get((session_id, key))
        if entry is not None and entry[2] == source_id:
            self._grants.pop((session_id, key), None)

    def discard_older(self, session_id: str, key: tuple[str, ...], answered_at: int) -> None:
        """Apply a denial without revoking a later acceptance."""
        entry = self._grants.get((session_id, key))
        if entry is not None and entry[3] < answered_at:
            self._grants.pop((session_id, key), None)

    def consume_with_source(
        self, session_id: str, key: tuple[str, ...]
    ) -> tuple[ElicitationResult, str | None] | None:
        """Consume a live grant along with its durable card id."""
        self._prune()
        entry = self._grants.pop((session_id, key), None)
        if entry is None:
            return None
        verdict, expires_at, source_id, _answered_at = entry
        if expires_at <= self._clock():
            return None
        return verdict, source_id

    def consume(
        self,
        session_id: str,
        key: tuple[str, ...],
    ) -> ElicitationResult | None:
        """
        Return and drop a live grant, or ``None`` when there is none.

        :param session_id: Session the incoming call belongs to.
        :param key: Match key of the incoming call.
        :returns: The stored verdict, or ``None`` when absent/expired.
        """
        entry = self.consume_with_source(session_id, key)
        return entry[0] if entry is not None else None

    def clear(self) -> None:
        """Drop every grant; test isolation."""
        self._grants.clear()

    def _prune(self) -> None:
        """Drop every expired grant; called on each access."""
        now = self._clock()
        expired = [
            grant_key
            for grant_key, (_verdict, expires_at, _source_id, _answered_at) in self._grants.items()
            if expires_at <= now
        ]
        for grant_key in expired:
            del self._grants[grant_key]


#: Process-wide grant registry, read by the Claude and Codex hooks.
approval_grants = ApprovalGrants()
