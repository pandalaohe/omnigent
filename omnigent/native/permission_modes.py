"""Native permission modes accepted by automations and their launch arguments."""

# Keep these mappings aligned with the web permission constants in
# claudePermissionMode.ts and nativeHarnessModes.ts.
PERMISSION_LAUNCH_ARGS: dict[str, dict[str, list[str]]] = {
    "claude-native": {
        "default": ["--permission-mode", "default"],
        "auto": ["--permission-mode", "auto"],
        "acceptEdits": ["--permission-mode", "acceptEdits"],
        "plan": ["--permission-mode", "plan"],
        "dontAsk": ["--permission-mode", "dontAsk"],
        "bypassPermissions": ["--permission-mode", "bypassPermissions"],
    },
    "codex-native": {
        "default": [],
        "ask-for-approval": [
            "--sandbox",
            "workspace-write",
            "--ask-for-approval",
            "on-request",
            "-c",
            'approvals_reviewer="user"',
        ],
        "approve-for-me": [
            "--sandbox",
            "workspace-write",
            "--ask-for-approval",
            "on-request",
            "-c",
            'approvals_reviewer="auto_review"',
        ],
        "full-access": ["--sandbox", "danger-full-access", "--ask-for-approval", "never"],
        "read-only": ["--sandbox", "read-only", "--ask-for-approval", "on-request"],
        "bypass": [],
    },
    "cursor-native": {
        "default": [],
        "auto-review": ["--auto-review"],
        "plan": ["--mode", "plan"],
        "ask": ["--mode", "ask"],
        "yolo": ["--yolo"],
    },
    "antigravity-native": {"default": [], "skip": ["--dangerously-skip-permissions"]},
    "devin-native": {
        "normal": [],
        "accept-edits": ["--permission-mode", "accept-edits"],
        "smart": ["--permission-mode", "smart"],
        "dangerous": ["--permission-mode", "dangerous"],
    },
}

# Saved automations retain every registered native and SDK permission choice.
UNATTENDED_DISALLOWED_PERMISSION_MODES: frozenset[str] = frozenset()
# Shared by every rejection of an automation's permission_mode.
AGENT_DEFAULT_PERMISSION_MODE_HINT = "JSON null (not the string 'null') for the agent default"

# Modes that skip the approval prompt (``dontAsk`` denies instead). Registered
# selections remain valid for task overrides, SDK defaults and continued sessions.
# Copied history receives fresh permission authority under the creation rules.
APPROVAL_SKIPPING_PERMISSION_MODES: frozenset[str] = frozenset(
    {
        "bypassPermissions",  # claude-native, claude-sdk, acp
        "dontAsk",  # claude-native
        "full-access",  # codex-native
        "bypass",  # codex-native
        "yolo",  # cursor-native
        "skip",  # antigravity-native
        "dangerous",  # devin-native
    }
)
