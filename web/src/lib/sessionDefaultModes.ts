import { CLAUDE_NATIVE_PERMISSION_MODES } from "@/lib/claudePermissionMode";
import { CODEX_APPROVAL_PRESETS } from "@/lib/codexApprovalMode";
import { NON_FULL_PERMISSION_TIERS } from "@/lib/nativeHarnessModes";
import { speedOptionsForModel } from "@/lib/speedTiers";
import type { HarnessCapability, SessionModeSet } from "@/lib/agentLabels";
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
  "antigravity-native": NON_FULL_PERMISSION_TIERS,
  pi: NON_FULL_PERMISSION_TIERS,
  "pi-native": NON_FULL_PERMISSION_TIERS,
  codex: CODEX_APPROVAL_PRESETS,
  "codex-native": CODEX_APPROVAL_PRESETS,
  "claude-native": CLAUDE_PERMISSION_OPTIONS,
  "claude-sdk": CLAUDE_PERMISSION_OPTIONS,
};

export type SessionModeContext = "default" | "create";
export type HarnessCapabilityMap = Readonly<Record<string, HarnessCapability>>;

/** `undefined` means an older catalog without this declaration; `null` is an explicit no-mode. */
function declaredModeSet(
  harness: string,
  field: "speed" | "permission",
  capabilities: HarnessCapabilityMap | undefined,
): SessionModeSet | null | undefined {
  const capability =
    capabilities?.[harness] ?? (harness.startsWith("acp:") ? capabilities?.acp : undefined);
  if (!capability || !("session_modes" in capability)) return undefined;
  return capability.session_modes?.[field] ?? null;
}

/** The handler selects the create transport; old catalogs keep their prior UI path. */
export function sessionModeHandler(
  harness: string,
  field: "speed" | "permission",
  capabilities?: HarnessCapabilityMap,
): string | null {
  const declared = declaredModeSet(harness, field, capabilities);
  if (declared !== undefined) return declared?.handler ?? null;
  if (field === "permission") {
    if (["pi", "pi-native", "antigravity-native"].includes(harness)) return "agy-tier";
    if (harness === "codex-native") return "codex-approval-preset";
    if (harness === "codex") return "codex-sdk-approval";
    if (["claude-native", "claude-sdk"].includes(harness)) return "claude-permission-mode";
  } else {
    if (["codex-native", "codex"].includes(harness)) return "codex-service-tier";
    if (["claude-native", "claude-sdk"].includes(harness)) return "claude-fast-mode";
  }
  return null;
}

export function sessionDefaultModeOptions(
  harness: string,
  field: "speed" | "permission",
  models: readonly NativeModelOption[] = [],
  model: string | null = null,
  context: SessionModeContext = "default",
  capabilities?: HarnessCapabilityMap,
) {
  const declared = declaredModeSet(harness, field, capabilities);
  if (declared !== undefined) {
    if (!declared) return [];
    if (declared.options === "per-model") return speedOptionsForModel(models, model);
    return declared.options
      .filter((option) => option.contexts.includes(context))
      .map(({ value, label }) => ({ value, label }));
  }
  if (field === "permission") return PERMISSION_DEFAULT_OPTIONS[harness] ?? [];
  if (harness === "codex" || harness === "codex-native") {
    return speedOptionsForModel(models, model);
  }
  return SPEED_TIER_HARNESSES.some((value) => value === harness) ? SPEED_TIER_OPTIONS : [];
}
