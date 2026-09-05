"""Read-only status proof for Claude-native sub-agents.

The repair UI uses this module to distinguish a child with durable, structured
terminal evidence from one whose activity merely cannot be observed.  Probing
never starts a Claude process, advances a forwarder cursor, or writes bridge
state.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypedDict, cast

from omnigent.claude_native_bridge import (
    bridge_dir_for_bridge_id,
    read_active_session_id,
    read_bridge_id,
    read_claude_session_id,
    read_transcript_items_from_offset,
    read_transcript_path,
)
from omnigent.claude_native_forwarder import (
    _read_subagent_forward_state,
    _read_subagent_meta,
    _structured_terminal_evidence,
    _subagents_dir_for_transcript,
)

NativeSubagentProbeStatus = Literal["terminal", "unverified"]
NativeSubagentTerminalStatus = Literal["completed", "failed", "stopped", "killed"]


class NativeSubagentEvidence(TypedDict):
    """Opaque proof fields the Server can carry into its guarded update."""

    claude_session_id: str
    parent_complete_byte_offset: int
    subagent_id: str
    meta_tool_use_id: str | None
    evidence_key: str


class NativeSubagentProbeChild(TypedDict):
    """One Server child and the strongest status the Host can prove."""

    server_session_id: str
    subagent_id: str
    tool_use_id: str | None
    status: NativeSubagentProbeStatus
    terminal_status: NativeSubagentTerminalStatus | None
    reason: str
    evidence: NativeSubagentEvidence


class NativeSubagentProbeResult(TypedDict):
    """JSON response for a successful, identity-stable parent probe."""

    parent_session_id: str
    claude_session_id: str
    parent_complete_byte_offset: int
    children: list[NativeSubagentProbeChild]


class NativeSubagentProbeError(RuntimeError):
    """A safe error returned when a current, stable proof cannot be read."""

    def __init__(self, *, http_status: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.http_status = http_status
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class _ParentIdentity:
    active_session_id: str | None
    bridge_id: str | None
    claude_session_id: str | None
    transcript_path: Path | None


def _read_optional_bytes(path: Path) -> bytes | None:
    """Read one evidence file without treating absence as an empty file."""
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _read_parent_identity(bridge_dir: Path) -> _ParentIdentity:
    return _ParentIdentity(
        active_session_id=read_active_session_id(bridge_dir),
        bridge_id=read_bridge_id(bridge_dir),
        claude_session_id=read_claude_session_id(bridge_dir),
        transcript_path=read_transcript_path(bridge_dir),
    )


def _evidence_key(
    *,
    parent_session_id: str,
    claude_session_id: str,
    parent_complete_byte_offset: int,
    subagent_id: str,
    meta_tool_use_id: str | None,
    terminal_status: str | None,
) -> str:
    """Return a deterministic token for one exact Host evidence snapshot."""
    fields = (
        parent_session_id,
        claude_session_id,
        str(parent_complete_byte_offset),
        subagent_id,
        meta_tool_use_id or "",
        terminal_status or "",
    )
    return hashlib.sha256("\0".join(fields).encode("utf-8")).hexdigest()


def probe_native_subagent_status(
    *,
    parent_session_id: str,
    bridge_id: str,
) -> NativeSubagentProbeResult:
    """Inspect one existing Claude-native parent without changing runtime state.

    Only a current ``agent-*.meta.json`` correlation and terminal evidence in a
    frozen, complete prefix of the current parent transcript produce
    ``status=terminal``.  Missing process visibility, quiet files, and stale
    forwarder status remain ``unverified``.

    :raises NativeSubagentProbeError: When the requested parent is not the
        bridge's current identity or the proof source is unavailable/changes
        during the scan.
    """
    bridge_dir = bridge_dir_for_bridge_id(bridge_id)
    bridge_config_path = bridge_dir / "bridge.json"
    bridge_state_path = bridge_dir / "state.json"
    bridge_config_bytes = _read_optional_bytes(bridge_config_path)
    bridge_state_bytes = _read_optional_bytes(bridge_state_path)
    before = _read_parent_identity(bridge_dir)
    if (
        _read_optional_bytes(bridge_config_path) != bridge_config_bytes
        or _read_optional_bytes(bridge_state_path) != bridge_state_bytes
    ):
        raise NativeSubagentProbeError(
            http_status=409,
            code="native_parent_identity_changed",
            detail="The Claude-native parent identity changed during the status probe.",
        )
    if before.active_session_id is None or before.transcript_path is None:
        raise NativeSubagentProbeError(
            http_status=404,
            code="native_parent_not_found",
            detail="No current Claude-native parent transcript is available on this Runner.",
        )
    if before.active_session_id != parent_session_id or before.bridge_id != bridge_id:
        raise NativeSubagentProbeError(
            http_status=409,
            code="native_parent_identity_mismatch",
            detail="The Claude-native bridge is currently bound to another session.",
        )
    if before.claude_session_id is None:
        raise NativeSubagentProbeError(
            http_status=404,
            code="native_parent_not_found",
            detail="The current Claude transcript identity is not available on this Runner.",
        )

    transcript_path = before.transcript_path
    try:
        parent_stat_before = transcript_path.stat()
        raw_end = parent_stat_before.st_size
        parent_result = read_transcript_items_from_offset(
            transcript_path,
            0,
            start_line=0,
            agent_name="Claude Code",
            include_sidechains=False,
            end_offset=raw_end,
        )
        parent_stat_after = transcript_path.stat()
    except OSError as exc:
        raise NativeSubagentProbeError(
            http_status=404,
            code="native_parent_not_found",
            detail="The current Claude parent transcript could not be read.",
        ) from exc

    after = _read_parent_identity(bridge_dir)
    same_file = (
        parent_stat_before.st_dev == parent_stat_after.st_dev
        and parent_stat_before.st_ino == parent_stat_after.st_ino
        and parent_stat_before.st_size == parent_stat_after.st_size
        and parent_stat_before.st_mtime_ns == parent_stat_after.st_mtime_ns
    )
    if before != after or not same_file:
        raise NativeSubagentProbeError(
            http_status=409,
            code="native_parent_identity_changed",
            detail="The Claude-native parent identity changed during the status probe.",
        )

    complete_offset = parent_result.byte_offset
    terminal_evidence = _structured_terminal_evidence(parent_result)
    subagent_state_path = bridge_dir / "subagent_forwarder.json"
    subagent_state_bytes = _read_optional_bytes(subagent_state_path)
    subagent_state = _read_subagent_forward_state(bridge_dir)
    if _read_optional_bytes(subagent_state_path) != subagent_state_bytes:
        raise NativeSubagentProbeError(
            http_status=409,
            code="native_parent_identity_changed",
            detail="The Claude-native sub-agent registration changed during the status probe.",
        )
    subagents_dir = _subagents_dir_for_transcript(transcript_path)
    children: list[NativeSubagentProbeChild] = []
    meta_snapshots: dict[Path, bytes | None] = {}

    for entry in sorted(
        subagent_state.subagents.values(),
        key=lambda candidate: (candidate.child_conversation_id, candidate.subagent_id),
    ):
        if not entry.child_conversation_id:
            continue
        meta_path = subagents_dir / f"agent-{entry.subagent_id}.meta.json"
        meta_bytes = _read_optional_bytes(meta_path)
        meta = _read_subagent_meta(meta_path)
        if _read_optional_bytes(meta_path) != meta_bytes:
            raise NativeSubagentProbeError(
                http_status=409,
                code="native_parent_identity_changed",
                detail="A Claude-native child identity changed during the status probe.",
            )
        meta_snapshots[meta_path] = meta_bytes
        meta_tool_use_id = meta["toolUseId"] if meta is not None else None
        terminal_status: NativeSubagentTerminalStatus | None = None
        if entry.tool_use_id is None:
            reason = "missing_tool_use_id"
        elif meta is None:
            reason = "missing_or_invalid_meta"
        elif meta_tool_use_id != entry.tool_use_id:
            reason = "tool_use_id_mismatch"
        else:
            observed = terminal_evidence.get(entry.tool_use_id)
            if observed is None:
                reason = "no_structured_terminal_evidence"
            else:
                terminal_status = cast(NativeSubagentTerminalStatus, observed[0])
                reason = "structured_parent_terminal_evidence"

        status: NativeSubagentProbeStatus = (
            "terminal" if terminal_status is not None else "unverified"
        )
        evidence: NativeSubagentEvidence = {
            "claude_session_id": before.claude_session_id,
            "parent_complete_byte_offset": complete_offset,
            "subagent_id": entry.subagent_id,
            "meta_tool_use_id": meta_tool_use_id,
            "evidence_key": _evidence_key(
                parent_session_id=parent_session_id,
                claude_session_id=before.claude_session_id,
                parent_complete_byte_offset=complete_offset,
                subagent_id=entry.subagent_id,
                meta_tool_use_id=meta_tool_use_id,
                terminal_status=terminal_status,
            ),
        }
        children.append(
            {
                "server_session_id": entry.child_conversation_id,
                "subagent_id": entry.subagent_id,
                "tool_use_id": entry.tool_use_id,
                "status": status,
                "terminal_status": terminal_status,
                "reason": reason,
                "evidence": evidence,
            }
        )

    final_identity = _read_parent_identity(bridge_dir)
    try:
        parent_stat_final = transcript_path.stat()
    except OSError as exc:
        raise NativeSubagentProbeError(
            http_status=409,
            code="native_parent_identity_changed",
            detail="The Claude-native parent identity changed during the status probe.",
        ) from exc
    if (
        final_identity != before
        or _read_optional_bytes(bridge_config_path) != bridge_config_bytes
        or _read_optional_bytes(bridge_state_path) != bridge_state_bytes
        or parent_stat_final.st_dev != parent_stat_before.st_dev
        or parent_stat_final.st_ino != parent_stat_before.st_ino
        or parent_stat_final.st_size != parent_stat_before.st_size
        or parent_stat_final.st_mtime_ns != parent_stat_before.st_mtime_ns
        or _read_optional_bytes(subagent_state_path) != subagent_state_bytes
        or any(
            _read_optional_bytes(meta_path) != meta_bytes
            for meta_path, meta_bytes in meta_snapshots.items()
        )
    ):
        raise NativeSubagentProbeError(
            http_status=409,
            code="native_parent_identity_changed",
            detail="The Claude-native parent identity changed during the status probe.",
        )

    return {
        "parent_session_id": parent_session_id,
        "claude_session_id": before.claude_session_id,
        "parent_complete_byte_offset": complete_offset,
        "children": children,
    }
