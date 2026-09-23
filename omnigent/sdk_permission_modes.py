"""SDK permission labels and presets.

Kept dependency-free so the server routes and runner agree on session modes.
"""

CODEX_SDK_APPROVAL_MODES: frozenset[str] = frozenset({"default", "full-access", "read-only"})
CLAUDE_SDK_PERMISSION_MODE_LABEL_KEY = "omnigent.claude_sdk.permission_mode"
CODEX_SDK_APPROVAL_MODE_LABEL_KEY = "omnigent.codex_sdk.approval_mode"
