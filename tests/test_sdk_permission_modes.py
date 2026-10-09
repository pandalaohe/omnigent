"""Drift guard: SDK turn policies must cover Codex's ``/permissions`` presets."""

from __future__ import annotations

from omnigent.codex_approval_modes import CODEX_NATIVE_PERMISSION_VALUES
from omnigent.sdk_permission_modes import CODEX_SDK_TURN_POLICIES


def test_sdk_turn_policies_cover_codex_permission_presets() -> None:
    assert set(CODEX_SDK_TURN_POLICIES) == set(CODEX_NATIVE_PERMISSION_VALUES) | {"default"}, (
        "Codex's /permissions preset list changed. Add an SDK mapping "
        "(approvalPolicy, approvalsReviewer, sandboxPolicy type) for each new value "
        "in omnigent/sdk_permission_modes.py."
    )


def test_legacy_default_entry_matches_ask_for_approval() -> None:
    assert CODEX_SDK_TURN_POLICIES["default"] == CODEX_SDK_TURN_POLICIES["ask-for-approval"]
