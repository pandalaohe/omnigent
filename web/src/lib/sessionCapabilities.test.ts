import { describe, expect, it } from "vitest";

import {
  effortLevelsForFamily,
  SANDBOX_FORK_UNSUPPORTED,
  SANDBOX_ONLY_FORK_RECOVERY_UNSUPPORTED,
  SANDBOX_SWITCH_HOST_UNSUPPORTED,
  sessionActionRestrictions,
  supportsEffortControl,
  permissionTierSwitchDisabledReason,
  PERMISSION_TIER_BLOCKED,
  workspaceRecoveryDisabledReason,
} from "./sessionCapabilities";

describe("permission tier switch gate", () => {
  const ready = {
    permissionTier: "auto",
    permissionTierRevision: 3,
    permissionTierState: "ready" as const,
    permissionTiersCapability: true,
    status: "idle",
  };
  it("allows only a confirmed idle revision", () => {
    expect(permissionTierSwitchDisabledReason(ready)).toBeUndefined();
    expect(
      permissionTierSwitchDisabledReason({ ...ready, permissionTiersCapability: false }),
    ).toBeDefined();
    expect(
      permissionTierSwitchDisabledReason({ ...ready, permissionTierRevision: null }),
    ).toBeDefined();
    expect(permissionTierSwitchDisabledReason({ ...ready, status: "running" })).toBeDefined();
    expect(
      permissionTierSwitchDisabledReason({ ...ready, pendingElicitations: [{}] }),
    ).toBeDefined();
  });
  it("allows a confirmed idle full tier and names blocked recovery", () => {
    expect(
      permissionTierSwitchDisabledReason({ ...ready, permissionTier: "full" }),
    ).toBeUndefined();
    expect(permissionTierSwitchDisabledReason({ ...ready, permissionTierState: "blocked" })).toBe(
      PERMISSION_TIER_BLOCKED,
    );
  });
});

describe("session action restrictions", () => {
  it("recognizes the managed snapshot without access to its host record", () => {
    expect(sessionActionRestrictions({ labels: { "omnigent.host_type": "managed" } })).toEqual({
      forkDisabledReason: SANDBOX_FORK_UNSUPPORTED,
      switchHostDisabledReason: SANDBOX_SWITCH_HOST_UNSUPPORTED,
    });
  });

  it("recognizes the lakebox provider when the session has no synthetic label", () => {
    expect(sessionActionRestrictions({ labels: {} }, { sandbox_provider: "lakebox" })).toEqual({
      forkDisabledReason: SANDBOX_FORK_UNSUPPORTED,
      switchHostDisabledReason: SANDBOX_SWITCH_HOST_UNSUPPORTED,
    });
  });

  it.each<Record<string, string>>([{}, { "omnigent.host_type": "managed" }])(
    "forks an Arclet session only onto a fresh Arclet with labels %j",
    (labels) => {
      expect(sessionActionRestrictions({ labels }, { sandbox_provider: "arclet" })).toEqual({
        forkDisabledReason: undefined,
        forkSandboxProvider: "arclet",
        switchHostDisabledReason: SANDBOX_SWITCH_HOST_UNSUPPORTED,
      });
    },
  );

  it("preserves forks from supported sandbox providers, including repository labels", () => {
    const result = sessionActionRestrictions(
      { labels: { "omnigent.sandbox.repo": "https://github.com/org/repo" } },
      { sandbox_provider: "modal" },
    );
    expect(result.forkDisabledReason).toBeUndefined();
    expect(result.forkSandboxProvider).toBeUndefined();
    expect(result.switchHostDisabledReason).toBe(SANDBOX_SWITCH_HOST_UNSUPPORTED);
  });

  it("honors the embedded restriction even for a normally forkable provider", () => {
    expect(
      sessionActionRestrictions(
        { labels: { "omnigent.host_type": "managed" } },
        { sandbox_provider: "modal" },
      ),
    ).toEqual({
      forkDisabledReason: SANDBOX_FORK_UNSUPPORTED,
      switchHostDisabledReason: SANDBOX_SWITCH_HOST_UNSUPPORTED,
    });
  });

  it("keeps both actions available for ordinary hosts", () => {
    expect(sessionActionRestrictions({ labels: {} }, { sandbox_provider: null })).toEqual({
      forkDisabledReason: undefined,
      switchHostDisabledReason: undefined,
    });
  });

  it("blocks host-directory recovery only for sandbox-only forks", () => {
    expect(workspaceRecoveryDisabledReason("arclet")).toBe(SANDBOX_ONLY_FORK_RECOVERY_UNSUPPORTED);
    expect(workspaceRecoveryDisabledReason(undefined)).toBeUndefined();
  });
});

describe("effortLevelsForFamily", () => {
  it("mirrors the server ladders for each declared family", () => {
    expect(effortLevelsForFamily("anthropic")).toEqual(["low", "medium", "high", "xhigh", "max"]);
    expect(effortLevelsForFamily("openai")).toEqual([
      "none",
      "minimal",
      "low",
      "medium",
      "high",
      "xhigh",
    ]);
    expect(effortLevelsForFamily("gemini")).toEqual(["low", "medium", "high"]);
    expect(effortLevelsForFamily("copilot")).toEqual(["low", "medium", "high", "xhigh"]);
    expect(effortLevelsForFamily("pi")).toEqual([
      "none",
      "minimal",
      "low",
      "medium",
      "high",
      "xhigh",
      "max",
    ]);
  });

  it("offers no ladder for none, codex-native (per-model), unknown, or absent", () => {
    // codex-native's ladder comes from the model catalog, not a fixed family.
    for (const family of ["none", "codex-native", "made-up", null, undefined]) {
      expect(effortLevelsForFamily(family)).toEqual([]);
    }
  });
});

describe("supportsEffortControl with a declared effort family", () => {
  it("shows the dial for a top-level label-less SDK session whose family has a ladder", () => {
    // WHY: claude-sdk / codex carry no wrapper label; their effort reaches the
    // executor through the session's reasoning_effort, so the family is the gate.
    expect(supportsEffortControl({ labels: {}, harness: "claude-sdk" }, "anthropic")).toBe(true);
    expect(supportsEffortControl({ labels: {}, harness: "codex" }, "openai")).toBe(true);
  });

  it("still fails closed without a family, or with one that has no ladder", () => {
    expect(supportsEffortControl({ labels: {}, harness: "claude-sdk" })).toBe(false);
    expect(supportsEffortControl({ labels: {}, harness: "claude-sdk" }, null)).toBe(false);
    expect(supportsEffortControl({ labels: {}, harness: "cursor-native" }, "none")).toBe(false);
  });

  it("never gives a sub-agent child a dial, even with a ladder", () => {
    expect(
      supportsEffortControl(
        { labels: {}, harness: "claude-sdk", parentSessionId: "parent" },
        "anthropic",
      ),
    ).toBe(false);
  });

  it("keeps configured inference outside the SDK effort controls", () => {
    expect(
      supportsEffortControl(
        { labels: {}, harness: "claude-sdk", inferenceConfigured: true },
        "anthropic",
      ),
    ).toBe(false);
  });

  it("leaves the native wrapper-label gate exactly as before", () => {
    expect(supportsEffortControl({ labels: { "omnigent.wrapper": "claude-code-native-ui" } })).toBe(
      true,
    );
    expect(supportsEffortControl({ labels: { "omnigent.wrapper": "cursor-native-ui" } })).toBe(
      false,
    );
    // A labelled native row ignores the family entirely.
    expect(
      supportsEffortControl({ labels: { "omnigent.wrapper": "cursor-native-ui" } }, "anthropic"),
    ).toBe(false);
    expect(supportsEffortControl(null, "anthropic")).toBe(false);
  });
});

it("gates ACP effort on the live session advertisement", () => {
  const session = {
    harness: "acp:neutral",
    codexModelOptions: [{ id: "model-a", isDefault: true, efforts: ["deliberate"] }],
  };
  expect(supportsEffortControl({ harness: "acp:neutral" })).toBe(false);
  expect(supportsEffortControl(session)).toBe(true);
  expect(
    supportsEffortControl(session, null, [{ id: "model-b", isDefault: true, efforts: [] }]),
  ).toBe(false);
});

it("uses the active ACP model without changing the reset default", () => {
  const session = { harness: "acp:neutral" };
  const rows = [
    { id: "a", isDefault: true, isActive: false, efforts: ["quick"] },
    { id: "b", isDefault: false, isActive: true, efforts: [] },
  ];
  expect(supportsEffortControl(session, null, rows)).toBe(false);
  rows[1].efforts = ["deliberate"];
  expect(supportsEffortControl(session, null, rows)).toBe(true);
});
