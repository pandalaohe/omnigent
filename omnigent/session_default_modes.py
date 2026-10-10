"""Session calling-default vocabularies shared by the server and runners."""

import re

from omnigent.codex_approval_modes import CODEX_NATIVE_PERMISSION_VALUES

SPEED_TIER_LABEL_KEY = "omnigent.speed_tier"
SPEED_TIER_VALUES = frozenset({"standard", "fast"})
_SERVICE_TIER_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")


def valid_speed_tier(value: object, harness: str | None) -> bool:
    """Accept legacy aliases and safe future Codex catalog tier ids."""
    if not isinstance(value, str):
        return False
    if harness in {"codex", "codex-native"}:
        return bool(_SERVICE_TIER_ID.fullmatch(value))
    return harness in {"claude-native", "claude-sdk"} and value in SPEED_TIER_VALUES


def codex_service_tier(value: str) -> str:
    """Translate stored legacy aliases to Codex's wire/config vocabulary."""
    if not valid_speed_tier(value, "codex"):
        raise ValueError("invalid Codex service tier")
    return {"standard": "default", "fast": "priority"}.get(value, value)


PERMISSION_DEFAULT_VALUES: dict[str, frozenset[str]] = {
    "codex": CODEX_NATIVE_PERMISSION_VALUES,
    "codex-native": CODEX_NATIVE_PERMISSION_VALUES,
    "claude-native": frozenset({"acceptEdits", "auto", "plan", "dontAsk", "bypassPermissions"}),
    "claude-sdk": frozenset({"acceptEdits", "auto", "plan", "dontAsk", "bypassPermissions"}),
}
CODEX_NATIVE_PERMISSION_DEFAULT_ARGS: dict[str, list[str]] = {
    "ask-for-approval": [
        "--ask-for-approval",
        "on-request",
        "--sandbox",
        "workspace-write",
        "-c",
        'approvals_reviewer="user"',
    ],
    "approve-for-me": ["--approve-for-me"],
    "full-access": ["--sandbox", "danger-full-access", "--ask-for-approval", "never"],
    "read-only": ["--sandbox", "read-only", "--ask-for-approval", "on-request"],
}
CLAUDE_FAST_MODE_SETTINGS: dict[str, bool] = {"fast": True, "standard": False}
