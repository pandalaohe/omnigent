"""Session calling-default vocabularies shared by the server and runners."""

SPEED_TIER_LABEL_KEY = "omnigent.speed_tier"
SPEED_TIER_VALUES = frozenset({"standard", "fast"})
SPEED_TIER_HARNESSES = frozenset({"codex", "codex-native"})
PERMISSION_DEFAULT_VALUES: dict[str, frozenset[str]] = {
    "codex": frozenset({"ask-for-approval", "approve-for-me", "full-access", "read-only"}),
    "codex-native": frozenset({"ask-for-approval", "approve-for-me", "full-access", "read-only"}),
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
CODEX_APP_SERVER_SERVICE_TIERS: dict[str, str] = {"fast": "priority", "standard": "default"}
