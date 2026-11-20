"""Session labels used during native rotation."""

import json
from collections.abc import Mapping
from typing import Any

from omnigent.server.permission_tiers import is_permission_label_key
from omnigent.stores import conversation_store

CLAUDE_TRANSFER_INTENT_LABEL = "omnigent.auto_rotation_state.claude_transfer"


def displaced_claude_candidate_label(target_id: str) -> str:
    """Source label retaining the expired attempt for a replaced candidate."""
    return f"omnigent.auto_rotation_state.displaced_claude_candidate.{target_id}"


def parse_claude_transfer_intent(raw: str) -> dict[str, Any] | None:
    """Read the server's recorded transfer identity from a session snapshot."""
    try:
        intent = json.loads(raw) if raw else None
    except ValueError:
        return None
    return intent if isinstance(intent, dict) and "target" in intent and "id" in intent else None


def replacement_session_labels(labels: Mapping[str, str]) -> dict[str, str]:
    """Drop server-owned state before submitting a replacement session create."""
    reserved = {
        conversation_store.ARCHIVE_LOCK_LABEL_KEY,
        conversation_store.ARCHIVED_AT_LABEL_KEY,
        conversation_store.ARCHIVE_STOP_WHEN_IDLE_LABEL_KEY,
        conversation_store.ARCHIVE_DELETE_WORKTREE_LABEL_KEY,
        conversation_store.ARCHIVE_CASCADE_LABEL_KEY,
        conversation_store.ARCHIVE_KEEP_WORKTREE_LABEL_KEY,
        conversation_store.ARCHIVE_WORKTREE_MODE_LABEL_KEY,
        conversation_store.ARCHIVE_REMOVED_WORKTREE_LABEL_KEY,
        conversation_store.ARCHIVE_WORKTREE_ADMISSION_FENCE_LABEL_KEY,
        conversation_store.PROJECT_ARCHIVE_LABEL_KEY,
        conversation_store.SIDE_CHAT_SOURCE_LABEL_KEY,
        "omnigent.startup.registration",
        "omnigent.startup.create_preparation",
        "omnigent.git.worktree_root_sha256",
        "omnigent.turn_actor",
        "omnigent.codex_native.subagent_thread_id",
    } | conversation_store.SUCCESSION_OPERATION_LABEL_KEYS
    prefixes = (
        "omnigent.auto_rotation_state.",
        "cost_control.",
        "omnigent.handoff.",
        "omnigent.member.",
        "omnigent.mcp_service.",
        "omnigent.sandbox.",
        "omnigent.standing_approval",
        "omnigent.standing_pass",
        f"{conversation_store.PINNED_LABEL_KEY}.",
        f"{conversation_store.ARCHIVE_CASCADE_LABEL_KEY}.",
        f"{conversation_store.PROJECT_ARCHIVE_LABEL_KEY}.",
        conversation_store.ARCHIVE_WORKTREE_ADMISSION_FENCE_ALIAS_PREFIX,
    )
    return {
        key: value
        for key, value in labels.items()
        if key not in reserved
        and not key.startswith(prefixes)
        and not conversation_store.is_codex_permission_observation_label_key(key)
        and not conversation_store.is_server_secret_label_key(key)
        and not is_permission_label_key(key)
    }
