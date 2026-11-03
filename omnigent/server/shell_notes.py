"""Transient next-message notes for user-created shells, gated per harness."""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from omnigent.db.workspace_cache import WorkspaceScopedCache
from omnigent.member_snapshot import member_entries_from_labels

# A family is added only after the native transcript round-trip is verified.
SHELL_NOTE_HARNESSES: frozenset[str] = frozenset()


@dataclass
class _Note:
    terminal_id: str
    instance_id: str
    line: str
    claimed: bool = False


@dataclass(frozen=True)
class Taken:
    """Instance-keyed lines retained for rollback until a forward is accepted."""

    entries: tuple[_Note, ...]

    @property
    def prefix(self) -> str:
        return "\n".join(note.line for note in self.entries) + "\n\n"


_held: WorkspaceScopedCache[str, dict[str, _Note]] = WorkspaceScopedCache()
_lock = threading.Lock()


def hold(session_id: str, terminal_id: str, instance_id: str, line: str) -> None:
    """Keep at most five distinct instances; repeated creates are idempotent."""
    with _lock:
        entries = _held.setdefault(session_id, {})
        entries.setdefault(instance_id, _Note(terminal_id, instance_id, line))
        while len(entries) > 5:
            entries.pop(next(iter(entries)))


def drop(session_id: str, terminal_id: str) -> None:
    """Forget notes for a terminal closed by the user."""
    with _lock:
        entries = _held.get(session_id, {})
        for instance_id, note in list(entries.items()):
            if note.terminal_id == terminal_id:
                entries.pop(instance_id)
        if not entries:
            _held.pop(session_id, None)


def take(session_id: str) -> Taken | None:
    """Claim the current lines for one message."""
    with _lock:
        entries = tuple(note for note in _held.get(session_id, {}).values() if not note.claimed)
        for note in entries:
            note.claimed = True
        return Taken(entries) if entries else None


def restore(session_id: str, taken: Taken) -> None:
    """Restore a refused forward ahead of any newly opened shells."""
    with _lock:
        entries = _held.get(session_id, {})
        for note in taken.entries:
            if entries.get(note.instance_id) is note:
                note.claimed = False


def release(session_id: str, taken: Taken) -> None:
    """Spend accepted notes without touching replacements or newer opens."""
    with _lock:
        entries = _held.get(session_id, {})
        for note in taken.entries:
            if entries.get(note.instance_id) is note:
                entries.pop(note.instance_id)
        if not entries:
            _held.pop(session_id, None)


def eligible(event_type: str, data: Mapping[str, Any], labels: Mapping[str, str] | None) -> bool:
    """Only a person's plain message in a single-member session carries a note."""
    if event_type != "message" or data.get("role") != "user":
        return False
    if data.get("peer_message_id") or data.get("peer_delivery"):
        return False
    if len(member_entries_from_labels(labels)) >= 2:
        return False
    content = data.get("content")
    if not isinstance(content, list):
        return False
    text = "\n".join(
        block.get("text", "")
        for block in content
        if isinstance(block, dict)
        and block.get("type") in {"text", "input_text"}
        and isinstance(block.get("text"), str)
    ).lstrip()
    if text.startswith("[System:") and not text.startswith("[System: shell]"):
        return False
    # Shell notes are only forwarded prefixes, never runtime posts. The user
    # can paste the same line as their own message and must keep those words.
    return bool(text) and not text.startswith(("/", "!", "[Peer message"))


def prepend(content: list[dict[str, Any]], prefix: str) -> list[dict[str, Any]]:
    """Copy the content with a separate note block, leaving stored text intact."""
    return [{"type": "input_text", "text": prefix}, *content]
