"""Replacement creates must not replay server-owned labels."""

import pytest

from omnigent.errors import OmnigentError
from omnigent.native._native_session_labels import replacement_session_labels
from omnigent.server.routes._sessions.helpers import (
    _reject_reserved_cost_control_label_seed,
    _reject_server_reserved_label_seed,
)


@pytest.mark.parametrize(
    "key",
    [
        "omnigent.codex_native.permission_observation",
        "OMNIGENT.CODEX_NATIVE.PERMISSION_OBSERVATION",
        "omnigent.codex_native.permissión_observation",
        "omnigent.startup.registration",
        "omnigent.startup.create_preparation",
        "omnigent.permission_tier.state",
        "OMNIGENT.PERMISSION_TIER.CAPABILITY",
        "omnigent.pi.permission_tier",
        "omnigent.antigravity_native.permission_tier",
        "omnigent.handoff.state",
        "omnigent.member.0",
        "omnigent.mcp_service.test",
        "omnigent.side_chat.source_id",
        "omnigent.git.worktree_root_sha256",
        "omnigent.turn_actor",
        "omnigent.archive_locked",
        "omnigent.archived_at",
        "omnigent.archive_stop_when_idle",
        "omnigent.archive_delete_worktree",
        "omnigent.archive_cascade",
        "omnigent.archive_cascade.0",
        "omnigent.archive_keep_worktree",
        "omnigent.archive_worktree_mode",
        "omnigent.archive_removed_worktree",
        "omnigent.archive_worktree_admission_fence",
        "omnigent.archive_worktree_admission_fence.alias",
        "omnigent.project_archive",
        "omnigent.project_archive.batch",
        "omnigent.pinned.other",
        "omnigent.touched.other",
        "omnigent.sandbox.repo",
        "omnigent.codex_native.subagent_thread_id",
        "OMNIGENT.ARTIFACT_LINK_KEY",
        "omnigent.rotate_requested",
        "cost_control.policy",
    ],
)
def test_replacement_labels_drop_server_owned_state(key: str) -> None:
    with pytest.raises(OmnigentError):
        _reject_reserved_cost_control_label_seed({key: "old state"})
        _reject_server_reserved_label_seed({key: "old state"})
    configuration = {
        "team": "example",
        "omnigent.wrapper": "codex-native-ui",
        "omnigent.ui": "terminal",
        "omnigent.codex_native.bridge_id": "shared_bridge",
        "omnigent.codex_native.approval_mode": "default",
    }
    original = {**configuration, key: "old state"}
    assert replacement_session_labels(original) == configuration
    assert original[key] == "old state"
