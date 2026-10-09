import {
  CLAUDE_NATIVE_PERMISSION_MODES,
  type ClaudePermissionModeOption,
} from "@/lib/claudePermissionMode";
import {
  CODEX_NATIVE_RUNTIME_APPROVAL_PRESETS,
  type CodexRuntimeApprovalPreset,
} from "@/lib/codexApprovalMode";
import { isSdkHarnessSession } from "@/lib/sessionCapabilities";

const CLAUDE_SDK_PERMISSION_MODE_LABEL_KEY = "omnigent.claude_sdk.permission_mode";
const CODEX_SDK_APPROVAL_MODE_LABEL_KEY = "omnigent.codex_sdk.approval_mode";

const CODEX_SDK_APPROVAL_PRESETS: CodexRuntimeApprovalPreset[] = [
  ...CODEX_NATIVE_RUNTIME_APPROVAL_PRESETS,
  {
    value: "read-only",
    label: "Read Only",
    description: "Read workspace files, with approval required for edits or internet access",
  },
];

export function sdkPermissionOptions(
  harness: string | null | undefined,
): readonly ClaudePermissionModeOption[] | null {
  if (harness === "claude-sdk") return CLAUDE_NATIVE_PERMISSION_MODES;
  if (harness === "codex") return CODEX_SDK_APPROVAL_PRESETS;
  return null;
}

export function sdkInitialPermissionMode(harness: string | null | undefined): string | null {
  if (harness === "claude-sdk") return "auto";
  if (harness === "codex") return "ask-for-approval";
  return null;
}

// Older Codex sessions and scheduled tasks stored the legacy "default".
export function normalizeSdkPermissionMode(
  harness: string | null | undefined,
  mode: string,
): string {
  return harness === "codex" && mode === "default" ? "ask-for-approval" : mode;
}

export function sdkPermissionModeFromSession(
  session: Parameters<typeof isSdkHarnessSession>[0],
  harness: "claude-sdk" | "codex",
): string | null {
  if (!session || !isSdkHarnessSession(session) || session.harness !== harness) return null;
  const labelKey =
    harness === "claude-sdk"
      ? CLAUDE_SDK_PERMISSION_MODE_LABEL_KEY
      : CODEX_SDK_APPROVAL_MODE_LABEL_KEY;
  const mode = session.labels?.[labelKey] ?? "";
  return normalizeSdkPermissionMode(harness, mode);
}
