// Native-harness permission / approval / execution mode tables, shared by the
// new-session dialog and the fork dialog so the option lists, CLI-flag
// mappings, and default values can't drift between the two surfaces.
//
// Each entry's `args` are the `terminal_launch_args` the runner passes to the
// native CLI; a mode whose `args` are empty sends no flags (the CLI keeps its
// own configured default). Keep in sync with each CLI's `--help`.

import { CLAUDE_NATIVE_DEFAULT_PERMISSION_MODE } from "@/lib/claudePermissionMode";
import { CODEX_APPROVAL_PRESETS } from "@/lib/codexApprovalMode";

/** One selectable mode: value + label + blurb + the CLI flags it maps to. */
export interface NativeHarnessMode {
  value: string;
  label: string;
  description: string;
  args: string[];
}

export type PermissionModeConcept =
  "manual" | "automatic" | "edit-automatic" | "read-only" | "deny" | "full-access" | "default";

const PERMISSION_MODE_CONCEPTS: Record<string, Record<string, PermissionModeConcept>> = {
  "claude-native": {
    default: "manual",
    auto: "automatic",
    acceptEdits: "edit-automatic",
    plan: "read-only",
    dontAsk: "deny",
    bypassPermissions: "full-access",
  },
  "codex-native": {
    default: "automatic",
    "ask-for-approval": "automatic",
    "approve-for-me": "automatic",
    "full-access": "full-access",
    "read-only": "read-only",
    bypass: "full-access",
  },
  "cursor-native": {
    default: "manual",
    "auto-review": "automatic",
    plan: "read-only",
    ask: "read-only",
    yolo: "full-access",
  },
  "antigravity-native": {
    ask: "manual",
    plan: "read-only",
    "accept-edits": "edit-automatic",
    auto: "automatic",
    full: "full-access",
  },
  pi: {
    ask: "manual",
    plan: "read-only",
    "accept-edits": "edit-automatic",
    auto: "automatic",
    full: "full-access",
  },
  "pi-native": {
    ask: "manual",
    plan: "read-only",
    "accept-edits": "edit-automatic",
    auto: "automatic",
    full: "full-access",
  },
};

const NATIVE_HARNESS_ALIASES: Record<string, string> = {
  "native-claude": "claude-native",
  "native-codex": "codex-native",
  "native-pi": "pi-native",
  "native-cursor": "cursor-native",
  "native-antigravity": "antigravity-native",
  "agy-native": "antigravity-native",
  "native-agy": "antigravity-native",
};

export function canonicalNativeHarness(harness: string): string {
  return NATIVE_HARNESS_ALIASES[harness] ?? harness;
}

/** Shared permission concept for presentation across native harness vocabularies. */
export function permissionModeConcept(
  harness: string | null | undefined,
  value: string | null | undefined,
): PermissionModeConcept {
  if (!harness || !value) return "default";
  const canonicalHarness = canonicalNativeHarness(harness);
  return (
    PERMISSION_MODE_CONCEPTS[canonicalHarness]?.[
      canonicalHarness === "antigravity-native"
        ? (normalizeAgyPermissionTier(value) ?? value)
        : value
    ] ?? "default"
  );
}

// The server owns tier launch flags; create sends the tier label instead.
export const AGY_NATIVE_PERMISSION_TIER_LABEL_KEY = "omnigent.antigravity_native.permission_tier";
export const AGY_NATIVE_DEFAULT_PERMISSION_TIER = "auto";
export const AGY_NATIVE_FULL_PERMISSION_TIER = "full";
export const AGY_RESTRICTED_HOOK_RULE =
  'Restricted Antigravity modes check third-party PreToolUse hooks before launch. Enabled hooks or unreadable configuration block launch and identify the hook and file. Choose Full access or set the hook to "enabled": false in its source file. Plugin filters do not bypass this check. Other events do not block launch. Independent Omnigent policies still apply in every mode.';
export const AGY_NATIVE_PERMISSION_TIERS: NativeHarnessMode[] = [
  { value: "ask", label: "Ask every time", description: "Ask before each tool call", args: [] },
  { value: "plan", label: "Plan", description: "Planning and read-only tools only", args: [] },
  {
    value: "accept-edits",
    label: "Accept edits",
    description: "Approve workspace edits automatically; ask for other actions",
    args: [],
  },
  {
    value: "auto",
    label: "Auto",
    description: "Allow tools except actions blocked by fixed risk rules; no OS sandbox",
    args: [],
  },
  {
    value: "full",
    label: "Full access",
    description: "Skip tier approvals and rules",
    args: [],
  },
];
export const PI_PERMISSION_TIERS = AGY_NATIVE_PERMISSION_TIERS;
export const PI_DEFAULT_PERMISSION_TIER = AGY_NATIVE_DEFAULT_PERMISSION_TIER;

export function agyPermissionTierFromSession(
  session:
    | { labels?: Record<string, string | null> | null; terminalLaunchArgs?: string[] | null }
    | null
    | undefined,
): string {
  const tier = session?.labels?.[AGY_NATIVE_PERMISSION_TIER_LABEL_KEY];
  const normalized = normalizeAgyPermissionTier(tier);
  if (AGY_NATIVE_PERMISSION_TIERS.some((option) => option.value === normalized)) return normalized!;
  return session?.terminalLaunchArgs?.includes("--dangerously-skip-permissions") ? "full" : "ask";
}

export function normalizeAgyPermissionTier(
  value: string | null | undefined,
): string | null | undefined {
  // Remove legacy values two local/host-custom releases after this one.
  if (value === "default") return "ask";
  if (value === "skip") return "full";
  if (["auto-strict", "auto-standard", "auto-relaxed"].includes(value ?? "")) return "auto";
  return value;
}

// The Auto Harness's Permissions vocabulary: Default only. No cross-harness
// permission mapping exists, so the row stays locked and the create call sends
// no override — each CLI keeps the machine's own configuration.
export const AUTO_PERMISSION_MODE = {
  value: CLAUDE_NATIVE_DEFAULT_PERMISSION_MODE,
  label: "Default",
  description: "The picked harness keeps its own configured permissions",
} as const;
export const AUTO_PERMISSION_MODE_OPTIONS = [AUTO_PERMISSION_MODE] as const;

// Cursor execution modes. "default" sends no flags; other values map to CLI
// args passed via terminal_launch_args. Keep in sync with `cursor-agent --help`.
export const CURSOR_NATIVE_DEFAULT_EXEC_MODE = "default";
export const CURSOR_NATIVE_EXEC_MODES: NativeHarnessMode[] = [
  {
    value: "default",
    label: "Default",
    description: "Normal agent mode; prompts before running commands",
    args: [],
  },
  {
    value: "auto-review",
    label: "Auto-review",
    description: "Smart Auto: auto-runs safe tool calls and prompts for the rest",
    args: ["--auto-review"],
  },
  {
    value: "plan",
    label: "Plan",
    description: "Read-only planning; analyzes and proposes plans, no edits",
    args: ["--mode", "plan"],
  },
  {
    value: "ask",
    label: "Ask",
    description: "Q&A style; explains and answers questions (read-only)",
    args: ["--mode", "ask"],
  },
  {
    value: "yolo",
    label: "Yolo",
    description: "Runs everything without prompts or safety checks",
    args: ["--yolo"],
  },
];

// Devin's own permission vocabulary (`devin --help`), deliberately NOT Claude's:
// Devin's rungs differ, and it reads them from `--permission-mode`. "auto" is
// Devin's own default, so it sends no flag. Keep in sync with `devin --help`.
// Values are Devin's CANONICAL names (it also accepts `auto` for `normal` and
// `bypass`/`yolo` for `dangerous`), because a mid-session switch stores the mode
// the pane confirmed — which is always the canonical one.
export const DEVIN_NATIVE_DEFAULT_PERMISSION_MODE = "normal";
export const DEVIN_NATIVE_PERMISSION_MODES: NativeHarnessMode[] = [
  {
    value: "normal",
    label: "Normal",
    description: "Auto-approves read-only tools; prompts for anything else",
    args: [],
  },
  {
    value: "accept-edits",
    label: "Accept edits",
    description: "Also auto-approves edits inside the workspace",
    args: ["--permission-mode", "accept-edits"],
  },
  {
    value: "smart",
    label: "Smart",
    description: "Also auto-runs actions a fast model judges safe",
    args: ["--permission-mode", "smart"],
  },
  {
    value: "dangerous",
    label: "Dangerous",
    description: "Auto-approves every tool, with no prompts",
    args: ["--permission-mode", "dangerous"],
  },
];

// Codex approval presets matching the `/permissions` TUI popup.
// Each preset bundles a sandbox profile + approval policy, mirroring
// codex-rs/utils/approval-presets/src/lib.rs. Ask for approval explicitly
// selects the user reviewer so a host config cannot silently change it.
// Keep in sync with `codex --help` and
// https://developers.openai.com/codex/agent-approvals-security
export const CODEX_NATIVE_DEFAULT_APPROVAL_MODE = "ask-for-approval";
export const CODEX_NATIVE_APPROVAL_MODES: NativeHarnessMode[] = CODEX_APPROVAL_PRESETS.map(
  (preset) => ({
    ...preset,
    args:
      preset.value === "ask-for-approval"
        ? [
            "--ask-for-approval",
            "on-request",
            "--sandbox",
            "workspace-write",
            "-c",
            'approvals_reviewer="user"',
          ]
        : preset.value === "approve-for-me"
          ? ["--approve-for-me"]
          : preset.value === "full-access"
            ? ["--sandbox", "danger-full-access", "--ask-for-approval", "never"]
            : preset.value === "read-only"
              ? ["--sandbox", "read-only", "--ask-for-approval", "on-request"]
              : [],
  }),
);

// Conversation-label key for the DANGEROUS codex full-bypass opt-in. When
// set to "1" the runner launches Codex with
// `--dangerously-bypass-approvals-and-sandbox` (no approval prompts, no
// command sandbox) — see omnigent.stores.conversation_store
// CODEX_NATIVE_BYPASS_SANDBOX_LABEL_KEY. Stored as a label (cheap thread
// metadata) so it survives reload. Mutually exclusive in spirit with the
// approval-mode presets above: when bypass is on the runner strips any
// `--sandbox` / `--ask-for-approval` flags those presets would emit.
export const CODEX_NATIVE_BYPASS_SANDBOX_LABEL_KEY = "omnigent.codex_native.bypass_sandbox";
// Bypass is the most-permissive Codex approval stance — presented after the four
// option in the Codex approval dropdown (Codex only; OpenCode shares the
// presets above but has no bypass). It rides as a conversation label, not
// terminal_launch_args, so its `args` are empty and it's handled specially.
export const CODEX_NATIVE_BYPASS_APPROVAL_VALUE = "bypass";
export const CODEX_NATIVE_BYPASS_APPROVAL_OPTION: NativeHarnessMode = {
  value: CODEX_NATIVE_BYPASS_APPROVAL_VALUE,
  label: "Bypass approvals & sandbox",
  description: "Runs Codex with no approval prompts and no command sandbox",
  args: [],
};
