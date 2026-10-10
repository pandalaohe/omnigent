import { CLAUDE_NATIVE_PERMISSION_MODES } from "@/lib/claudePermissionMode";

export const SPEED_TIER_HARNESSES = [
  "codex",
  "codex-native",
  "claude-native",
  "claude-sdk",
] as const;
export const SPEED_TIER_OPTIONS = [
  { value: "standard", label: "Standard" },
  { value: "fast", label: "Fast" },
];
const CODEX_PERMISSION_OPTIONS = [
  { value: "ask-for-approval", label: "Ask for approval" },
  { value: "approve-for-me", label: "Approve for me" },
  { value: "full-access", label: "Full access" },
  { value: "read-only", label: "Read only" },
];
const CLAUDE_PERMISSION_OPTIONS = CLAUDE_NATIVE_PERMISSION_MODES.filter(
  (option) => option.value !== "default",
);
export const PERMISSION_DEFAULT_OPTIONS: Record<
  string,
  readonly { value: string; label: string }[]
> = {
  codex: CODEX_PERMISSION_OPTIONS,
  "codex-native": CODEX_PERMISSION_OPTIONS,
  "claude-native": CLAUDE_PERMISSION_OPTIONS,
  "claude-sdk": CLAUDE_PERMISSION_OPTIONS,
};

export function sessionDefaultModeOptions(harness: string, field: "speed" | "permission") {
  if (field === "permission") return PERMISSION_DEFAULT_OPTIONS[harness] ?? [];
  return SPEED_TIER_HARNESSES.some((value) => value === harness) ? SPEED_TIER_OPTIONS : [];
}
