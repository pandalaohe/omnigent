import { describe, expect, it } from "vitest";
import type { NativeModelOption } from "@/lib/types";
import {
  effortLevelsFor,
  startEffortLevelsFor,
  reconcileEffortOnModelChange,
  reconcileStartEffortOnModelChange,
} from "./modelEffortOptions";

const rows: NativeModelOption[] = [
  {
    id: "model-a",
    supportedReasoningEfforts: [{ reasoningEffort: "low" }, { reasoningEffort: "high" }],
  },
  { id: "model-b", supportedReasoningEfforts: [{ reasoningEffort: "medium" }] },
];

describe("startEffortLevelsFor", () => {
  it("offers startup-only ACP efforts without exposing them to running sessions", () => {
    const models = [
      { id: "agent-model", isActive: true, efforts: [], start_efforts: ["low", "high"] },
    ];
    expect(startEffortLevelsFor("acp:agent", models, null)).toEqual(["low", "high"]);
    expect(effortLevelsFor("acp", models, null)).toEqual([]);
    expect(
      startEffortLevelsFor("acp:agent", [{ ...models[0], efforts: ["medium"] }], null),
    ).toEqual(["medium"]);
  });
});

describe("effortLevelsFor", () => {
  it("keeps ACP effort-only data usable without adopting a model shortlist", () => {
    const models = [{ id: "__default__", isDefault: true, efforts: ["precise"] }];
    expect(effortLevelsFor("acp:neutral", models, "agent-current-model")).toEqual(["precise"]);
  });
  it("uses opaque ACP advertised values without guessing a ladder", () => {
    const models = [
      { id: "a", isDefault: true, efforts: ["quick", "deliberate"] },
      { id: "b", efforts: [] },
    ];
    expect(effortLevelsFor("acp:neutral", models, "a")).toEqual(["quick", "deliberate"]);
    expect(effortLevelsFor("acp:neutral", models, "b")).toEqual([]);
    expect(effortLevelsFor("acp:neutral", models, null)).toEqual(["quick", "deliberate"]);
    expect(effortLevelsFor("acp:neutral", [], null)).toEqual([]);
  });
  it.each([
    ["claude-native", ["low", "medium", "high", "xhigh", "max"]],
    ["codex-native", ["low", "high"]],
    ["devin-native", ["low", "high"]],
    ["pi-native", ["none", "minimal", "low", "medium", "high", "xhigh", "max"]],
    ["pi", ["none", "minimal", "low", "medium", "high", "xhigh", "max"]],
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

describe("reconcileStartEffortOnModelChange", () => {
  const models = [
    { id: "a", isActive: true, efforts: [], start_efforts: ["high"] },
    { id: "b", efforts: [], start_efforts: ["high"] },
    { id: "c", efforts: [], start_efforts: ["low"] },
    { id: "d", efforts: ["low"], start_efforts: ["high"] },
  ];
  it.each([
    ["a", "high"],
    ["b", "high"],
    ["c", null],
    ["d", null],
    [null, "high"],
  ])("reconciles startup effort for model %s without enabling live effort", (model, expected) => {
    expect(reconcileStartEffortOnModelChange("acp:neutral", models, model, "high")).toBe(expected);
    expect(reconcileEffortOnModelChange("acp:neutral", models, model, "high")).toBeNull();
  });
  it("keeps non-ACP model reconciliation unchanged", () => {
    expect(reconcileStartEffortOnModelChange("codex", rows, "model-a", "high")).toBe("high");
    expect(reconcileStartEffortOnModelChange("codex", rows, "model-b", "high")).toBeNull();
    expect(reconcileStartEffortOnModelChange("claude-sdk", rows, "model-b", "high")).toBe("high");
  });
});

it("filters Antigravity efforts per model and clears an unsupported pick", () => {
  const models = [
    { id: "claude-sonnet-5-5", efforts: ["low", "medium", "high", "max"] },
    { id: "gpt-oss", efforts: ["medium"] },
  ];
  expect(effortLevelsFor("antigravity-native", models, "claude-sonnet-5-5")).toEqual([
    "low",
    "medium",
    "high",
  ]);
  expect(effortLevelsFor("antigravity-native", models, "gpt-oss")).toEqual(["medium"]);
  expect(effortLevelsFor("antigravity-native", rows, "model-b")).toEqual(["medium"]);
  expect(effortLevelsFor("antigravity-native", models, "unknown")).toEqual([]);
  expect(reconcileEffortOnModelChange("antigravity-native", models, "gpt-oss", "high")).toBeNull();
});
