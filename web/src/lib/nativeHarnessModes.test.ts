import { describe, expect, it } from "vitest";

import {
  AGY_NATIVE_PERMISSION_TIERS,
  AGY_NATIVE_DEFAULT_PERMISSION_TIER,
  agyPermissionTierFromSession,
  normalizeAgyPermissionTier,
  CODEX_NATIVE_APPROVAL_MODES,
  permissionModeConcept,
  canonicalNativeHarness,
} from "./nativeHarnessModes";
import { CODEX_APPROVAL_PRESETS, CODEX_NATIVE_RUNTIME_APPROVAL_PRESETS } from "./codexApprovalMode";
import { sdkPermissionOptions } from "./sdkPermissionModes";
import { sessionDefaultModeOptions } from "./sessionDefaultModes";

it("keeps Codex native launch, runtime, SDK and defaults on the four current presets", () => {
  const expected = ["ask-for-approval", "approve-for-me", "full-access", "read-only"];
  expect(CODEX_APPROVAL_PRESETS.map((mode) => mode.value)).toEqual(expected);
  expect(CODEX_NATIVE_APPROVAL_MODES.map((mode) => mode.value)).toEqual(expected);
  expect(CODEX_NATIVE_RUNTIME_APPROVAL_PRESETS.map((mode) => mode.value)).toEqual(expected);
  expect(sdkPermissionOptions("codex")?.map((mode) => mode.value)).toEqual(expected);
  expect(sessionDefaultModeOptions("codex-native", "permission").map((mode) => mode.value)).toEqual(
    expected,
  );
  expect(sessionDefaultModeOptions("codex", "permission").map((mode) => mode.value)).toEqual(
    expected,
  );
  expect(
    Object.fromEntries(CODEX_NATIVE_APPROVAL_MODES.map((mode) => [mode.value, mode.args])),
  ).toEqual({
    "ask-for-approval": [
      "--ask-for-approval",
      "on-request",
      "--sandbox",
      "workspace-write",
      "-c",
      'approvals_reviewer="user"',
    ],
    "approve-for-me": ["--approve-for-me"],
    "full-access": ["--sandbox", "danger-full-access", "--ask-for-approval", "never"],
    "read-only": ["--sandbox", "read-only", "--ask-for-approval", "on-request"],
  });
});

describe("permissionModeConcept", () => {
  it("maps Claude and Codex vocabularies onto shared concepts", () => {
    expect(permissionModeConcept("claude-native", "default")).toBe("manual");
    expect(permissionModeConcept("claude-native", "plan")).toBe("read-only");
    expect(permissionModeConcept("codex-native", "default")).toBe("automatic");
    expect(permissionModeConcept("codex-native", "read-only")).toBe("read-only");
    expect(permissionModeConcept("codex-native", "full-access")).toBe("full-access");
    expect(permissionModeConcept("codex-native", "bypass")).toBe("full-access");
  });

  it("supports native harness aliases", () => {
    expect(permissionModeConcept("native-codex", "read-only")).toBe("read-only");
    expect(permissionModeConcept("native-agy", "skip")).toBe("full-access");
  });

  it("canonicalizes reversed native harness aliases", () => {
    expect(canonicalNativeHarness("native-claude")).toBe("claude-native");
    expect(canonicalNativeHarness("native-codex")).toBe("codex-native");
    expect(canonicalNativeHarness("native-pi")).toBe("pi-native");
    expect(canonicalNativeHarness("opencode-native")).toBe("opencode-native");
  });

  it("falls back safely for missing or future values", () => {
    expect(permissionModeConcept(null, "default")).toBe("default");
    expect(permissionModeConcept("codex-native", "future-mode")).toBe("default");
  });
});

describe("shared Pi and Antigravity tiers", () => {
  it("offers five English tiers for creation and defaults without client launch flags", () => {
    expect(AGY_NATIVE_DEFAULT_PERMISSION_TIER).toBe("auto");
    expect(
      AGY_NATIVE_PERMISSION_TIERS.map(({ value, label, args }) => [value, label, args]),
    ).toEqual([
      ["ask", "Ask every time", []],
      ["plan", "Plan", []],
      ["accept-edits", "Accept edits", []],
      ["auto", "Auto", []],
      ["full", "Full access", []],
    ]);
    expect(
      sessionDefaultModeOptions("antigravity-native", "permission").map((row) => row.value),
    ).toEqual(["ask", "plan", "accept-edits", "auto", "full"]);
    expect(sessionDefaultModeOptions("pi", "permission").map((row) => row.value)).toEqual([
      "ask",
      "plan",
      "accept-edits",
      "auto",
      "full",
    ]);
    expect(sessionDefaultModeOptions("pi-native", "permission").map((row) => row.value)).toEqual([
      "ask",
      "plan",
      "accept-edits",
      "auto",
      "full",
    ]);
    expect(sessionDefaultModeOptions("antigravity-native", "speed")).toEqual([]);
  });
  it.each([
    ["default", "ask"],
    ["skip", "full"],
    ["auto-standard", "auto"],
    ["auto-strict", "auto"],
    ["auto-relaxed", "auto"],
  ])("migrates %s", (value, expected) => {
    expect(normalizeAgyPermissionTier(value)).toBe(expected);
  });
  it("resolves server labels before legacy launch arguments", () => {
    expect(
      agyPermissionTierFromSession({
        labels: { "omnigent.antigravity_native.permission_tier": "plan" },
        terminalLaunchArgs: ["--dangerously-skip-permissions"],
      }),
    ).toBe("plan");
    expect(
      agyPermissionTierFromSession({ terminalLaunchArgs: ["--dangerously-skip-permissions"] }),
    ).toBe("full");
    expect(
      agyPermissionTierFromSession({
        labels: { "omnigent.antigravity_native.permission_tier": "invalid" },
      }),
    ).toBe("ask");
    expect(agyPermissionTierFromSession(null)).toBe("ask");
  });
  it.each([
    ["ask", "manual"],
    ["plan", "read-only"],
    ["accept-edits", "edit-automatic"],
    ["auto", "automatic"],
    ["full", "full-access"],
  ])("presents %s with its shared concept", (tier, concept) => {
    expect(permissionModeConcept("antigravity-native", tier)).toBe(concept);
  });
});
