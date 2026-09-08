# LOCAL_MODS — pandalaohe/omnigent `local/host-custom`

Fork-local modifications on top of upstream `omnigent-ai/omnigent`. One entry per modification; each carries its exit condition.

| # | File | Change | Why | Exit condition |
|---|------|--------|-----|----------------|
| 1 | `omnigent/claude_native_bridge.py` `ask_uq_hook` | PreToolUse `AskUserQuestion` command-hook `timeout` 10 → 86400 | In `bypassPermissions` mode the web-UI card had 10 s before the hook fell through to Claude's TUI picker, which a web-UI user never sees; the `PermissionRequest` fallback then denied the call. Waiting a day matches the hook's own long-poll budget (`_PERMISSION_TIMEOUT_S`) and the sibling `PermissionRequest` hook. Accepted trade-off, same as the sibling: a terminal-only bypass session, or one whose web client is unreachable, waits for the web answer instead of getting the TUI picker. | Remove when upstream makes the ask-user-question wait configurable, or when the hook itself checks for an attached web client before long-polling. |
