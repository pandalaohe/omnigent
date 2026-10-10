import { describe, expect, it } from "vitest";

import { CODEX_NATIVE_APPROVAL_MODES, permissionModeConcept } from "./nativeHarnessModes";
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

  it("falls back safely for missing or future values", () => {
    expect(permissionModeConcept(null, "default")).toBe("default");
    expect(permissionModeConcept("codex-native", "future-mode")).toBe("default");
  });
});
