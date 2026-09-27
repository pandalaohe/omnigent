import { describe, expect, it } from "vitest";
import type { NativeModelOption } from "@/lib/types";
import { effortLevelsFor, reconcileEffortOnModelChange } from "./modelEffortOptions";

const rows: NativeModelOption[] = [
  {
    id: "model-a",
    supportedReasoningEfforts: [{ reasoningEffort: "low" }, { reasoningEffort: "high" }],
  },
  { id: "model-b", supportedReasoningEfforts: [{ reasoningEffort: "medium" }] },
];

describe("effortLevelsFor", () => {
  it.each([
    ["claude-native", ["low", "medium", "high", "xhigh", "max"]],
    ["codex-native", ["low", "high"]],
    ["devin-native", ["low", "high"]],
    ["pi-native", ["none", "minimal", "low", "medium", "high", "xhigh", "max"]],
    ["claude-sdk", ["low", "medium", "high", "xhigh", "max"]],
    ["codex", ["low", "high"]],
    [null, null],
  ])("returns the ladder for %s", (harness, expected) => {
    expect(effortLevelsFor(harness, rows, "model-a")).toEqual(expected);
  });

  it("uses the selected model's efforts and leaves unknown models empty", () => {
    expect(effortLevelsFor("codex-native", rows, "model-b")).toEqual(["medium"]);
    expect(effortLevelsFor("codex", rows, "model-b")).toEqual(["medium"]);
    expect(effortLevelsFor("codex", rows, null)).toEqual([]);
    expect(effortLevelsFor("devin-native", rows, null)).toEqual([]);
  });
});

describe("reconcileEffortOnModelChange", () => {
  it.each([
    ["codex-native", "model-a", "high", "high"],
    ["codex-native", "model-b", "high", null],
    ["codex-native", null, "high", null],
    ["codex-native", "model-b", null, null],
    ["codex", "model-a", "high", "high"],
    ["codex", "model-b", "high", null],
    ["codex", null, "high", null],
    ["devin-native", "model-b", "high", "high"],
    ["claude-native", "model-b", "high", "high"],
    ["pi-native", "model-b", "high", "high"],
    ["claude-sdk", "model-b", "high", "high"],
    [null, "model-b", "high", "high"],
  ])("reconciles %s on %s", (harness, model, effort, expected) => {
    expect(reconcileEffortOnModelChange(harness, rows, model, effort)).toBe(expected);
  });
});
