import {
  CLAUDE_NATIVE_PERMISSION_MODES,
  type ClaudePermissionModeOption,
} from "@/lib/claudePermissionMode";
import type { CodexRuntimeApprovalPreset } from "@/lib/codexApprovalMode";
import { isSdkHarnessSession } from "@/lib/sessionCapabilities";

const CLAUDE_SDK_PERMISSION_MODE_LABEL_KEY = "omnigent.claude_sdk.permission_mode";
const CODEX_SDK_APPROVAL_MODE_LABEL_KEY = "omnigent.codex_sdk.approval_mode";

const CODEX_SDK_APPROVAL_PRESETS: CodexRuntimeApprovalPreset[] = [
  {
    value: "default",
    label: "Default",
    description: "Asks before commands outside the workspace; edits within it",
  },
  {
    value: "full-access",
    label: "Full access",
    description: "No approval prompts or sandbox",
  },
  {
    value: "read-only",
    label: "Read only",
    description: "Reads only; asks before edits and commands",
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
  if (harness === "codex") return "default";
  return null;
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
  return session.labels?.[labelKey] ?? "";
}
