"""SDK permission labels and presets.

Kept dependency-free so the server routes and runner agree on session modes.
"""

CODEX_SDK_TURN_POLICIES: dict[str, tuple[str, str, str]] = {
    "ask-for-approval": ("on-request", "user", "workspaceWrite"),
    "approve-for-me": ("on-request", "auto_review", "workspaceWrite"),
    "full-access": ("never", "user", "dangerFullAccess"),
    "read-only": ("on-request", "user", "readOnly"),
    # Legacy value stored by older sessions / scheduled tasks.
    "default": ("on-request", "user", "workspaceWrite"),
}
CODEX_SDK_APPROVAL_MODES: frozenset[str] = frozenset(CODEX_SDK_TURN_POLICIES)
CLAUDE_SDK_PERMISSION_MODE_LABEL_KEY = "omnigent.claude_sdk.permission_mode"
CODEX_SDK_APPROVAL_MODE_LABEL_KEY = "omnigent.codex_sdk.approval_mode"
ACP_SESSION_MODE_LABEL_KEY = "omnigent.acp.session_mode"
ACP_SESSION_MODE_SELECTION_LABEL_KEY = "omnigent.acp.session_mode_selection"


def codex_approval_mode_tightens(previous: str | None, requested: str | None) -> bool:
    """Treat unknown authority as requiring helper revocation too."""
    ranks = {
        "read-only": 0,
        "default": 1,
        "ask-for-approval": 1,
        "approve-for-me": 2,
        "full-access": 3,
    }
    previous_rank = ranks.get(previous) if previous is not None else None
    requested_rank = ranks.get(requested) if requested is not None else None
    if previous_rank is None or requested_rank is None:
        return True
    return requested_rank < previous_rank
