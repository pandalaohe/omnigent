import { CLAUDE_NATIVE_PERMISSION_MODES } from "@/lib/claudePermissionMode";
import { CODEX_APPROVAL_PRESETS } from "@/lib/codexApprovalMode";
import { speedOptionsForModel } from "@/lib/speedTiers";
import type { NativeModelOption } from "@/lib/types";

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
const CLAUDE_PERMISSION_OPTIONS = CLAUDE_NATIVE_PERMISSION_MODES.filter(
  (option) => option.value !== "default",
);
export const PERMISSION_DEFAULT_OPTIONS: Record<
  string,
  readonly { value: string; label: string }[]
> = {
  codex: CODEX_APPROVAL_PRESETS,
  "codex-native": CODEX_APPROVAL_PRESETS,
  "claude-native": CLAUDE_PERMISSION_OPTIONS,
  "claude-sdk": CLAUDE_PERMISSION_OPTIONS,
};

export function sessionDefaultModeOptions(
  harness: string,
  field: "speed" | "permission",
  models: readonly NativeModelOption[] = [],
  model: string | null = null,
) {
  if (field === "permission") return PERMISSION_DEFAULT_OPTIONS[harness] ?? [];
  if (harness === "codex" || harness === "codex-native") {
    return speedOptionsForModel(models, model);
  }
  return SPEED_TIER_HARNESSES.some((value) => value === harness) ? SPEED_TIER_OPTIONS : [];
}
