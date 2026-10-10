"""Drift guard: SDK turn policies must cover Codex's ``/permissions`` presets."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from omnigent.codex_approval_modes import (
    CODEX_NATIVE_PERMISSION_PRESETS,
    CODEX_NATIVE_PERMISSION_VALUES,
)
from omnigent.sdk_permission_modes import CODEX_SDK_TURN_POLICIES
from omnigent.session_default_modes import (
    CODEX_NATIVE_PERMISSION_DEFAULT_ARGS,
    PERMISSION_DEFAULT_VALUES,
)


def test_sdk_turn_policies_cover_codex_permission_presets() -> None:
    assert set(CODEX_SDK_TURN_POLICIES) == set(CODEX_NATIVE_PERMISSION_VALUES) | {"default"}, (
        "Codex's /permissions preset list changed. Add an SDK mapping "
        "(approvalPolicy, approvalsReviewer, sandboxPolicy type) for each new value "
        "in omnigent/sdk_permission_modes.py."
    )


def test_legacy_default_entry_matches_ask_for_approval() -> None:
    assert CODEX_SDK_TURN_POLICIES["default"] == CODEX_SDK_TURN_POLICIES["ask-for-approval"]


def test_codex_launch_and_calling_defaults_share_permission_presets() -> None:
    for harness in ("codex", "codex-native"):
        assert PERMISSION_DEFAULT_VALUES[harness] == CODEX_NATIVE_PERMISSION_VALUES
    assert set(CODEX_NATIVE_PERMISSION_DEFAULT_ARGS) == CODEX_NATIVE_PERMISSION_VALUES


def test_web_and_backend_codex_permission_presets_agree() -> None:
    source = Path(__file__).resolve().parents[1] / "web/src/lib/codexApprovalMode.ts"
    script = (
        f"import({json.dumps(source.as_uri())}).then(m => "
        "process.stdout.write(JSON.stringify(m.CODEX_APPROVAL_PRESETS.map("
        "({value,label}) => ({value,label})))))"
    )
    output = subprocess.run(
        ["node", "--experimental-strip-types", "--input-type=module", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    web = json.loads(output)
    backend = [
        {"value": preset.value, "label": preset.label}
        for preset in CODEX_NATIVE_PERMISSION_PRESETS
    ]
    assert web == backend
