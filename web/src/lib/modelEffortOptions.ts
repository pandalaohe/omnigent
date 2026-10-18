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

export function effortLevelsFor(
  harness: string | null | undefined,
  rows: readonly NativeModelOption[],
  model: string | null | undefined,
): readonly string[] | null {
  if (harness === "acp" || harness?.startsWith("acp:")) {
    const selected = model
      ? (rows.find((row) => row.id === model) ?? rows.find((row) => row.id === "__default__"))
      : rows.find((row) => row.isDefault);
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
      const selected = rows.find((row) => row.id === model);
      const efforts =
        selected?.efforts ??
        selected?.supportedReasoningEfforts?.map((option) => option.reasoningEffort) ??
        [];
      return ["low", "medium", "high"].filter((effort) => efforts.includes(effort));
    }
    case "pi-native":
      return PI_NATIVE_EFFORT_LEVELS;
    default:
      return null;
  }
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
