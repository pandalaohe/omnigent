"""SDK permission labels and presets.

Kept dependency-free so the server routes and runner agree on session modes.
"""

CODEX_SDK_TURN_POLICIES: dict[str, tuple[str, str]] = {
    "default": ("on-request", "workspaceWrite"),
    "full-access": ("never", "dangerFullAccess"),
    "read-only": ("on-request", "readOnly"),
}
CODEX_SDK_APPROVAL_MODES: frozenset[str] = frozenset(CODEX_SDK_TURN_POLICIES)
CLAUDE_SDK_PERMISSION_MODE_LABEL_KEY = "omnigent.claude_sdk.permission_mode"
CODEX_SDK_APPROVAL_MODE_LABEL_KEY = "omnigent.codex_sdk.approval_mode"
