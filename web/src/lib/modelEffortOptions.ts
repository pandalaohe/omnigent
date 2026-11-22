import { codexEffortLevelsForModel } from "@/lib/codexNativeModels";
import type { NativeModelOption } from "@/lib/types";

/** Claude effort ladder (matches ANTHROPIC_EFFORTS in reasoning_effort.py). */
const CLAUDE_EFFORT_LEVELS = ["low", "medium", "high", "xhigh", "max"] as const;

/** Pi thinking ladder (matches PI_EFFORTS in reasoning_effort.py; ``ultra`` aliases to ``max`` on Pi so omitted). */
const PI_NATIVE_EFFORT_LEVELS = [
  "none",
  "minimal",
  "low",
  "medium",
  "high",
  "xhigh",
  "max",
] as const;

const AGY_DISPLAY_EFFORT_SUFFIX_RE = /\s*\((?:low|medium|high|xhigh|max)\)$/i;
const AGY_SLUG_EFFORT_SUFFIX_RE = /-(?:low|medium|high|xhigh|max)$/i;

function normalizeAntigravityModelKey(value: string): string {
  const withoutDisplayEffort = value.trim().replace(AGY_DISPLAY_EFFORT_SUFFIX_RE, "");
  const withoutSlugEffort = withoutDisplayEffort.replace(AGY_SLUG_EFFORT_SUFFIX_RE, "");
  return withoutSlugEffort.toLowerCase().replace(/[^a-z0-9]/g, "");
}

function findAntigravityModelRow(
  rows: readonly NativeModelOption[],
  model: string | null | undefined,
): NativeModelOption | undefined {
  if (!model) return undefined;
  const exact = rows.find((row) => row.id === model);
  if (exact) return exact;
  const target = normalizeAntigravityModelKey(model);
  if (!target) return undefined;
  return rows.find(
    (row) =>
      normalizeAntigravityModelKey(row.id) === target ||
      (row.displayName != null && normalizeAntigravityModelKey(row.displayName) === target),
  );
}

export function effortLevelsFor(
  harness: string | null | undefined,
  rows: readonly NativeModelOption[],
  model: string | null | undefined,
): readonly string[] | null {
  if (harness === "acp" || harness?.startsWith("acp:")) {
    const selected = model
      ? (rows.find((row) => row.id === model) ?? rows.find((row) => row.id === "__default__"))
      : rows.find((row) => row.isDefault || row.isActive);
    return (
      selected?.efforts ??
      selected?.supportedReasoningEfforts?.map((option) => option.reasoningEffort) ??
      []
    );
  }
  switch (harness) {
    case "claude-native":
    case "claude-sdk":
      return CLAUDE_EFFORT_LEVELS;
    case "codex-native":
    case "codex":
    case "devin-native":
      // Devin encodes effort as a model-variant suffix, and the rung set is
      // PER MODEL (swe-2 exposes only medium/high/max; `swe-2-low` is a different
      // Fusion model), so derive it from the selected model's catalog entry —
      // its `supportedReasoningEfforts` — rather than a fixed ladder.
      return codexEffortLevelsForModel(rows, model);
    case "antigravity-native": {
      const selected = findAntigravityModelRow(rows, model);
      const efforts =
        selected?.efforts ??
        selected?.supportedReasoningEfforts?.map((option) => option.reasoningEffort) ??
        [];
      return ["low", "medium", "high", "xhigh"].filter((effort) => efforts.includes(effort));
    }
    case "pi-native":
    case "pi":
      return PI_NATIVE_EFFORT_LEVELS;
    default:
      return null;
  }
}

/** Efforts advertised only at startup are available before creating a chat. */
export function startEffortLevelsFor(
  harness: string | null | undefined,
  rows: readonly NativeModelOption[],
  model: string | null | undefined,
): readonly string[] {
  const live = effortLevelsFor(harness, rows, model) ?? [];
  if (live.length || !(harness === "acp" || harness?.startsWith("acp:"))) return live;
  const selected = model
    ? (rows.find((row) => row.id === model) ?? rows.find((row) => row.id === "__default__"))
    : rows.find((row) => row.isDefault || row.isActive);
  return selected?.start_efforts ?? [];
}

export function reconcileEffortOnModelChange(
  harness: string | null | undefined,
  rows: readonly NativeModelOption[],
  model: string | null | undefined,
  effort: string | null,
): string | null {
  // Devin's model switch keeps the selected effort even if the new model omits it.
  if (
    (harness === "codex-native" ||
      harness === "codex" ||
      harness === "antigravity-native" ||
      harness === "acp" ||
      harness?.startsWith("acp:")) &&
    effort !== null
  ) {
    return effortLevelsFor(harness, rows, model)?.includes(effort) ? effort : null;
  }
  return effort;
}

export function reconcileStartEffortOnModelChange(
  harness: string | null | undefined,
  rows: readonly NativeModelOption[],
  model: string | null | undefined,
  effort: string | null,
): string | null {
  if (harness === "acp" || harness?.startsWith("acp:")) {
    return effort !== null && startEffortLevelsFor(harness, rows, model).includes(effort)
      ? effort
      : null;
  }
  return reconcileEffortOnModelChange(harness, rows, model, effort);
}
