import { beforeEach, describe, expect, it, vi } from "vitest";

import { queueUserPreferencePatch } from "./userPreferencesSync";

vi.mock("./userPreferencesSync", () => ({ queueUserPreferencePatch: vi.fn() }));

import {
  DEFAULT_USAGE_CONTEXT_PREFERENCES,
  deleteUsageContextOverrides,
  patchUsageContextOverrides,
  readUsageContextPreferences,
  providerUsageLimitsForSource,
  resolveUsageContext,
  usageContextLevelKey,
  usageContextRecordFromKey,
  proposeUsageContextCollapse,
  usageContextSourceFromKey,
  usageContextSourceKey,
  writeUsageContextOverride,
  writeUsageContextPreferences,
  writeLastProviderUsageLimits,
  type UsageContextPreferences,
} from "./usageContextPreferences";

function resolveUsageContextLimits(
  preferences: UsageContextPreferences,
  sourceKey: string,
  contextWindow: number | null,
  autoCompactTokenLimit: number | null,
) {
  const result = resolveUsageContext(preferences, sourceKey, {
    contextWindow,
    autoCompactTokenLimit,
  });
  return {
    contextWindow: result.contextWindow.value,
    autoCompactTokenLimit: result.autoCompactTokenLimit,
  };
}

beforeEach(() => {
  localStorage.clear();
  vi.mocked(queueUserPreferencePatch).mockClear();
});

describe("usage/context preferences", () => {
  const source = usageContextSourceKey({
    hostId: "host-a",
    agentName: "codex",
    harness: "codex",
    model: "gpt-5.6",
  });

  it("round-trips the four-field source used for saved profiles", () => {
    expect(usageContextSourceFromKey(source)).toEqual({
      hostId: "host-a",
      agentName: "codex",
      harness: "codex",
      model: "gpt-5.6",
    });
    expect(usageContextSourceFromKey("not-json")).toBeNull();
  });

  it("follows reported Host and model values by default", () => {
    expect(readUsageContextPreferences()).toEqual(DEFAULT_USAGE_CONTEXT_PREFERENCES);
    expect(
      resolveUsageContextLimits(DEFAULT_USAGE_CONTEXT_PREFERENCES, source, 258_400, 240_000),
    ).toEqual({ contextWindow: 258_400, autoCompactTokenLimit: 240_000 });
  });

  it("inherits the host × agent cell for another exact model", () => {
    const preferences = {
      ...DEFAULT_USAGE_CONTEXT_PREFERENCES,
      overrides: {
        '["host-a","codex","codex","","cell"]': {
          contextWindowTokens: 400_000,
          autoCompactBufferTokens: null,
        },
      },
    };
    expect(resolveUsageContextLimits(preferences, source, 258_400, 240_000).contextWindow).toBe(
      400_000,
    );
  });

  it("recomputes the compact budget from the effective cell total and reported buffer", () => {
    const preferences = {
      ...DEFAULT_USAGE_CONTEXT_PREFERENCES,
      overrides: {
        [usageContextLevelKey(usageContextSourceFromKey(source)!, "cell")]: {
          contextWindowTokens: 400_000,
          autoCompactBufferTokens: null,
        },
      },
    };
    expect(
      resolveUsageContext(preferences, source, {
        contextWindow: 258_400,
        autoCompactTokenLimit: 240_000,
      }),
    ).toEqual({
      contextWindow: { value: 400_000, level: "cell" },
      buffer: { mode: "tokens", value: 18_400, level: "reported" },
      autoCompactTokenLimit: 381_600,
      ignored: [],
    });
  });

  it.each([18_400, 10_000])(
    "rejects a reported buffer that meets or exceeds the effective context %i",
    (contextWindowTokens) => {
      const preferences = {
        ...DEFAULT_USAGE_CONTEXT_PREFERENCES,
        overrides: { [source]: { contextWindowTokens, autoCompactBufferTokens: null } },
      };
      const resolved = resolveUsageContext(preferences, source, {
        contextWindow: 258_400,
        autoCompactTokenLimit: 240_000,
      });
      expect(resolved.buffer).toEqual({ mode: "tokens", value: null, level: "reported" });
      expect(resolved.autoCompactTokenLimit).toBeNull();
    },
  );

  it("uses a manual context total and Compact buffer when supplied", () => {
    writeUsageContextOverride(
      {
        version: 5,
        showProviderUsageLimits: false,
        overrides: {},
        lastProviderUsageLimits: {},
      },
      source,
      {
        contextWindowTokens: 330_000,
        autoCompactBufferTokens: 33_000,
      },
    );
    const preferences = readUsageContextPreferences();
    expect(preferences.showProviderUsageLimits).toBe(false);
    expect(resolveUsageContextLimits(preferences, source, 258_400, 240_000)).toEqual({
      contextWindow: 330_000,
      autoCompactTokenLimit: 297_000,
    });
  });

  it("uses a saved percentage as the compact denominator and retains zero", () => {
    const preferences = {
      ...DEFAULT_USAGE_CONTEXT_PREFERENCES,
      overrides: {
        [source]: {
          contextWindowTokens: 100_000,
          autoCompactBufferTokens: 15_000,
          autoCompactBufferPercent: 20,
          compactBufferMode: "percent" as const,
        },
      },
    };
    writeUsageContextPreferences(preferences);
    const saved = readUsageContextPreferences();
    expect(saved.overrides[source]).toEqual(preferences.overrides[source]);
    expect(queueUserPreferencePatch).toHaveBeenCalledWith("usage_context", saved);
    expect(resolveUsageContextLimits(saved, source, 90_000, 72_000)).toEqual({
      contextWindow: 100_000,
      autoCompactTokenLimit: 80_000,
    });
    writeUsageContextOverride(saved, source, {
      ...saved.overrides[source],
      autoCompactBufferPercent: 10,
    });
    expect(
      resolveUsageContextLimits(readUsageContextPreferences(), source, 90_000, 72_000)
        .autoCompactTokenLimit,
    ).toBe(90_000);
    writeUsageContextOverride(readUsageContextPreferences(), source, {
      ...readUsageContextPreferences().overrides[source],
      autoCompactBufferPercent: 0,
      autoCompactBufferTokens: 0,
    });
    expect(readUsageContextPreferences().overrides[source]).toMatchObject({
      autoCompactBufferPercent: 0,
      autoCompactBufferTokens: 0,
    });
    expect(
      resolveUsageContextLimits(readUsageContextPreferences(), source, 90_000, 72_000)
        .autoCompactTokenLimit,
    ).toBe(100_000);
  });

  it("migrates a token-only source without changing its compact behavior", () => {
    localStorage.setItem(
      "omnigent:usage-context-preferences",
      JSON.stringify({
        version: 5,
        showProviderUsageLimits: true,
        overrides: { [source]: { contextWindowTokens: 100_000, autoCompactBufferTokens: 20_000 } },
        lastProviderUsageLimits: {},
      }),
    );
    const preferences = readUsageContextPreferences();
    expect(preferences.overrides[source]).toEqual({
      contextWindowTokens: 100_000,
      autoCompactBufferTokens: 20_000,
    });
    expect(resolveUsageContextLimits(preferences, source, 90_000, 70_000)).toEqual({
      contextWindow: 100_000,
      autoCompactTokenLimit: 80_000,
    });
  });

  it("drops invalid percentages without erasing a valid token buffer", () => {
    localStorage.setItem(
      "omnigent:usage-context-preferences",
      JSON.stringify({
        version: 5,
        overrides: {
          [source]: {
            contextWindowTokens: 100_000,
            autoCompactBufferTokens: 20_000,
            autoCompactBufferPercent: 100,
            compactBufferMode: "tokens",
          },
        },
      }),
    );
    expect(readUsageContextPreferences().overrides[source]).toEqual({
      contextWindowTokens: 100_000,
      autoCompactBufferTokens: 20_000,
      compactBufferMode: "tokens",
    });
  });

  it("does not leak a manual override into a different Host or model", () => {
    writeUsageContextPreferences({
      version: 5,
      showProviderUsageLimits: false,
      overrides: {
        [source]: { contextWindowTokens: 330_000, autoCompactBufferTokens: 33_000 },
      },
      lastProviderUsageLimits: {},
    });
    const preferences = readUsageContextPreferences();
    const other = usageContextSourceKey({
      hostId: "host-b",
      agentName: "codex",
      harness: "codex",
      model: "gpt-5.6-mini",
    });
    expect(resolveUsageContextLimits(preferences, other, 128_000, 115_000)).toEqual({
      contextWindow: 128_000,
      autoCompactTokenLimit: 115_000,
    });
  });

  it("rejects invalid manual values and retains safe defaults", () => {
    localStorage.setItem(
      "omnigent:usage-context-preferences",
      JSON.stringify({
        version: 2,
        overrides: {
          [source]: { contextWindowTokens: -1, autoCompactBufferTokens: -5 },
        },
      }),
    );
    expect(readUsageContextPreferences()).toEqual(DEFAULT_USAGE_CONTEXT_PREFERENCES);
  });

  it("migrates a legacy Compact percentage into a token buffer", () => {
    localStorage.setItem(
      "omnigent:usage-context-preferences",
      JSON.stringify({
        version: 4,
        overrides: {
          [source]: { contextWindowTokens: 390_000, autoCompactThresholdPercent: 92.5 },
        },
      }),
    );
    expect(readUsageContextPreferences().overrides[source]).toEqual({
      contextWindowTokens: 390_000,
      autoCompactBufferTokens: 29_250,
    });
  });

  it("drops a legacy Compact percentage without a context total", () => {
    localStorage.setItem(
      "omnigent:usage-context-preferences",
      JSON.stringify({
        version: 4,
        overrides: {
          [source]: { contextWindowTokens: null, autoCompactThresholdPercent: 90 },
        },
      }),
    );
    expect(readUsageContextPreferences().overrides).toEqual({});
  });

  it("recomputes from the reported buffer when the saved buffer meets the context total", () => {
    writeUsageContextPreferences({
      version: 5,
      showProviderUsageLimits: true,
      overrides: {
        [source]: { contextWindowTokens: 30_000, autoCompactBufferTokens: 33_000 },
      },
      lastProviderUsageLimits: {},
    });
    const preferences = readUsageContextPreferences();
    expect(resolveUsageContextLimits(preferences, source, 258_400, 240_000)).toEqual({
      contextWindow: 30_000,
      autoCompactTokenLimit: 11_600,
    });
  });

  it("retains provider usage only for the exact Host, agent, harness, and model", () => {
    const snapshot = {
      provider: "Codex",
      scope: "Codex",
      capturedAt: 1_900_000_000,
      windows: [{ label: "5h", ariaLabel: "5 hour", usedPercent: 11 }],
    };
    writeLastProviderUsageLimits(DEFAULT_USAGE_CONTEXT_PREFERENCES, source, snapshot);
    const preferences = readUsageContextPreferences();
    expect(providerUsageLimitsForSource(preferences, source)).toEqual(snapshot);

    const claudeSource = usageContextSourceKey({
      hostId: "host-a",
      agentName: "claude",
      harness: "claude-native",
      model: "opus",
    });
    expect(providerUsageLimitsForSource(preferences, claudeSource)).toBeNull();
  });

  it("refreshes an unchanged provider reading locally without syncing it again", () => {
    const first = {
      provider: "Codex",
      scope: "Codex",
      capturedAt: 1_900_000_000,
      windows: [{ label: "5h", ariaLabel: "5 hour", usedPercent: 11 }],
    };
    writeLastProviderUsageLimits(DEFAULT_USAGE_CONTEXT_PREFERENCES, source, first);
    expect(queueUserPreferencePatch).toHaveBeenCalledTimes(1);

    const preferences = readUsageContextPreferences();
    writeLastProviderUsageLimits(preferences, source, {
      ...first,
      capturedAt: first.capturedAt + 60_000,
    });

    expect(providerUsageLimitsForSource(readUsageContextPreferences(), source)?.capturedAt).toBe(
      first.capturedAt + 60_000,
    );
    expect(queueUserPreferencePatch).toHaveBeenCalledTimes(1);
  });

  it("does not sync provider changes that render identically", () => {
    const first = {
      provider: "Claude",
      scope: "Account",
      capturedAt: 1_900_000_000,
      windows: [
        {
          label: "5h",
          ariaLabel: "5 hour",
          usedPercent: 11.1,
          durationMinutes: 300,
          resetsAt: 1_900_010_000,
        },
      ],
    };
    writeLastProviderUsageLimits(DEFAULT_USAGE_CONTEXT_PREFERENCES, source, first);
    writeLastProviderUsageLimits(readUsageContextPreferences(), source, {
      ...first,
      capturedAt: first.capturedAt + 60_000,
      windows: [
        {
          ...first.windows[0]!,
          usedPercent: 11.4,
          resetsAt: 1_900_020_000,
        },
      ],
    });

    expect(queueUserPreferencePatch).toHaveBeenCalledTimes(1);
    expect(providerUsageLimitsForSource(readUsageContextPreferences(), source)?.capturedAt).toBe(
      first.capturedAt + 60_000,
    );
  });

  it("patches a batch in one write, keeping each row's absent field", () => {
    const second = usageContextSourceKey({
      hostId: "host-a",
      agentName: "claude",
      harness: "claude-native",
      model: "opus",
    });
    const preferences: UsageContextPreferences = {
      version: 5,
      showProviderUsageLimits: true,
      overrides: {
        [source]: { contextWindowTokens: 100_000, autoCompactBufferTokens: 10_000 },
        [second]: { contextWindowTokens: null, autoCompactBufferTokens: 20_000 },
      },
      lastProviderUsageLimits: {},
    };

    patchUsageContextOverrides(preferences, [source, second], {
      autoCompactBufferTokens: 33_000,
    });

    const saved = readUsageContextPreferences().overrides;
    expect(saved[source]).toEqual({
      contextWindowTokens: 100_000,
      autoCompactBufferTokens: 33_000,
    });
    expect(saved[second]).toEqual({ contextWindowTokens: null, autoCompactBufferTokens: 33_000 });
    expect(queueUserPreferencePatch).toHaveBeenCalledTimes(1);
  });

  it("clears a patched field to Auto and drops a row left all-Auto", () => {
    const preferences: UsageContextPreferences = {
      version: 5,
      showProviderUsageLimits: true,
      overrides: {
        [source]: { contextWindowTokens: 100_000, autoCompactBufferTokens: 10_000 },
      },
      lastProviderUsageLimits: {},
    };

    patchUsageContextOverrides(preferences, [source], { contextWindowTokens: null });
    expect(readUsageContextPreferences().overrides[source]).toEqual({
      contextWindowTokens: null,
      autoCompactBufferTokens: 10_000,
    });

    patchUsageContextOverrides(readUsageContextPreferences(), [source], {
      autoCompactBufferTokens: null,
    });
    expect(readUsageContextPreferences().overrides[source]).toBeUndefined();
    expect(queueUserPreferencePatch).toHaveBeenCalledTimes(2);
  });

  it("does not write a patch without source keys", () => {
    patchUsageContextOverrides(DEFAULT_USAGE_CONTEXT_PREFERENCES, [], { contextWindowTokens: 1 });

    expect(queueUserPreferencePatch).not.toHaveBeenCalled();
    expect(localStorage.getItem("omnigent:usage-context-preferences")).toBeNull();
  });

  it("deletes only the given sources in one write", () => {
    const second = usageContextSourceKey({
      hostId: "host-a",
      agentName: "claude",
      harness: "claude-native",
      model: "opus",
    });
    const keep = usageContextSourceKey({
      hostId: "host-b",
      agentName: "codex",
      harness: "codex",
      model: "gpt-5.6",
    });
    const preferences: UsageContextPreferences = {
      version: 5,
      showProviderUsageLimits: true,
      overrides: {
        [source]: { contextWindowTokens: 100_000, autoCompactBufferTokens: 10_000 },
        [second]: { contextWindowTokens: null, autoCompactBufferTokens: 20_000 },
        [keep]: { contextWindowTokens: 50_000, autoCompactBufferTokens: null },
      },
      lastProviderUsageLimits: {},
    };

    deleteUsageContextOverrides(preferences, [source, second]);

    const saved = readUsageContextPreferences().overrides;
    expect(saved[source]).toBeUndefined();
    expect(saved[second]).toBeUndefined();
    expect(saved[keep]).toEqual({ contextWindowTokens: 50_000, autoCompactBufferTokens: null });
    expect(queueUserPreferencePatch).toHaveBeenCalledTimes(1);
  });

  it("does not write a delete without a matching source", () => {
    deleteUsageContextOverrides(DEFAULT_USAGE_CONTEXT_PREFERENCES, ["missing"]);
    deleteUsageContextOverrides(DEFAULT_USAGE_CONTEXT_PREFERENCES, []);

    expect(queueUserPreferencePatch).not.toHaveBeenCalled();
    expect(localStorage.getItem("omnigent:usage-context-preferences")).toBeNull();
  });

  it("resolves each field independently and labels the level actually used", () => {
    const identity = usageContextSourceFromKey(source)!;
    const column = usageContextLevelKey(identity, "column");
    const cell = usageContextLevelKey(identity, "cell");
    const preferences = {
      ...DEFAULT_USAGE_CONTEXT_PREFERENCES,
      overrides: {
        [column]: { contextWindowTokens: 400_000, autoCompactBufferTokens: 50_000 },
        [cell]: { contextWindowTokens: null, autoCompactBufferTokens: 20_000 },
        [source]: { contextWindowTokens: 300_000, autoCompactBufferTokens: null },
      },
    };
    const reported = { contextWindow: 128_000, autoCompactTokenLimit: 100_000 };
    expect(resolveUsageContext(preferences, source, reported)).toEqual({
      contextWindow: { value: 300_000, level: "model" },
      buffer: { mode: "tokens", value: 20_000, level: "cell" },
      autoCompactTokenLimit: 280_000,
      ignored: [],
    });
    const other = usageContextSourceKey({ ...identity, hostId: "host-b", model: "other" });
    expect(resolveUsageContext(preferences, other, reported).contextWindow).toEqual({
      value: 400_000,
      level: "column",
    });
    expect(providerUsageLimitsForSource(preferences, other)).toBeNull();
  });

  it("ignores invalid fixed buffers at each level and reports their true fallback", () => {
    const identity = usageContextSourceFromKey(source)!;
    const cell = usageContextLevelKey(identity, "cell");
    const column = usageContextLevelKey(identity, "column");
    const preferences = {
      ...DEFAULT_USAGE_CONTEXT_PREFERENCES,
      overrides: {
        [source]: { contextWindowTokens: 100_000, autoCompactBufferTokens: 100_000 },
        [cell]: { contextWindowTokens: null, autoCompactBufferTokens: 150_000 },
        [column]: { contextWindowTokens: 400_000, autoCompactBufferTokens: 25_000 },
      },
    };
    const result = resolveUsageContext(preferences, source, {
      contextWindow: 200_000,
      autoCompactTokenLimit: 190_000,
    });
    expect(result.buffer).toEqual({ mode: "tokens", value: 25_000, level: "column" });
    expect(result.autoCompactTokenLimit).toBe(75_000);
    expect(result.ignored).toEqual([
      { level: "model", reason: "buffer ≥ context" },
      { level: "cell", reason: "buffer ≥ context" },
    ]);
    preferences.overrides[column].autoCompactBufferTokens = 150_000;
    const fallback = resolveUsageContext(preferences, source, {
      contextWindow: 200_000,
      autoCompactTokenLimit: 190_000,
    });
    expect(fallback.buffer).toEqual({ mode: "tokens", value: 10_000, level: "reported" });
    expect(fallback.autoCompactTokenLimit).toBe(90_000);
  });

  it("treats buffer mode and its value as one inherited unit, including zero percent", () => {
    const identity = usageContextSourceFromKey(source)!;
    const cell = usageContextLevelKey(identity, "cell");
    const preferences: UsageContextPreferences = {
      ...DEFAULT_USAGE_CONTEXT_PREFERENCES,
      overrides: {
        [source]: {
          contextWindowTokens: 100_000,
          autoCompactBufferTokens: 50_000,
          compactBufferMode: "percent",
        },
        [cell]: { contextWindowTokens: null, autoCompactBufferTokens: 20_000 },
      },
    };
    const reported = { contextWindow: 90_000, autoCompactTokenLimit: 70_000 };
    expect(resolveUsageContext(preferences, source, reported).buffer.level).toBe("cell");
    preferences.overrides[source]!.autoCompactBufferPercent = 0;
    expect(resolveUsageContext(preferences, source, reported).autoCompactTokenLimit).toBe(100_000);
  });

  it("keeps literal * models exact and preserves unknown levels through all writers", () => {
    const identity = { ...usageContextSourceFromKey(source)!, model: "*" };
    const exact = usageContextSourceKey(identity);
    const unknown = JSON.stringify([
      identity.hostId,
      identity.agentName,
      identity.harness,
      "",
      "future",
    ]);
    expect(usageContextLevelKey(identity, "cell")).toBe('["host-a","codex","codex","","cell"]');
    expect(usageContextSourceFromKey(usageContextLevelKey(identity, "cell"))).toBeNull();
    expect(usageContextRecordFromKey(unknown)).toBeNull();
    writeUsageContextPreferences({
      ...DEFAULT_USAGE_CONTEXT_PREFERENCES,
      overrides: {
        [exact]: { contextWindowTokens: 123_000, autoCompactBufferTokens: null },
        [unknown]: { contextWindowTokens: 999_000, autoCompactBufferTokens: null },
      },
    });
    const reported = { contextWindow: 90_000, autoCompactTokenLimit: null };
    expect(
      resolveUsageContext(readUsageContextPreferences(), exact, reported).contextWindow.value,
    ).toBe(123_000);
    expect(
      resolveUsageContext(readUsageContextPreferences(), source, reported).contextWindow.value,
    ).toBe(90_000);
    patchUsageContextOverrides(readUsageContextPreferences(), [source], {
      contextWindowTokens: 100_000,
    });
    deleteUsageContextOverrides(readUsageContextPreferences(), [exact]);
    expect(readUsageContextPreferences().version).toBe(6);
    expect(readUsageContextPreferences().overrides[unknown]?.contextWindowTokens).toBe(999_000);
  });

  it("proposes 7 model records → 2 cells for the nine-record fixture without writing", () => {
    const overrides = Object.fromEntries([
      ...["opus", "opus[1m]", "sonnet", "sonnet[1m]", "haiku"].map((model) => [
        usageContextSourceKey({
          hostId: "host-a",
          agentName: "claude",
          harness: "claude-native",
          model,
        }),
        { contextWindowTokens: 550_000, autoCompactBufferTokens: 32_000 },
      ]),
      ...["gpt-x", "gpt-y"].map((model) => [
        usageContextSourceKey({
          hostId: "host-a",
          agentName: "codex",
          harness: "codex-native",
          model,
        }),
        { contextWindowTokens: 400_000, autoCompactBufferTokens: 0 },
      ]),
      [
        usageContextSourceKey({
          hostId: "host-a",
          agentName: "opencode",
          harness: "opencode",
          model: "",
        }),
        { contextWindowTokens: 200_000, autoCompactBufferTokens: null },
      ],
      [
        usageContextSourceKey({
          hostId: "host-b",
          agentName: "codex",
          harness: "codex",
          model: "gpt-x",
        }),
        { contextWindowTokens: 300_000, autoCompactBufferTokens: null },
      ],
    ]);
    const preferences = { ...DEFAULT_USAGE_CONTEXT_PREFERENCES, overrides };
    const proposal = proposeUsageContextCollapse(preferences);
    expect(proposal.removed).toHaveLength(7);
    expect(Object.keys(proposal.added)).toHaveLength(2);
    expect(Object.keys(preferences.overrides)).toHaveLength(9);
    expect(queueUserPreferencePatch).not.toHaveBeenCalled();
    writeLastProviderUsageLimits(preferences, source, {
      provider: "Codex",
      capturedAt: 1_900_000_000,
      windows: [{ label: "5h", ariaLabel: "5 hour", usedPercent: 11 }],
    });
    expect(readUsageContextPreferences().overrides).toEqual(overrides);
    const merged = { ...overrides, ...proposal.added };
    for (const key of proposal.removed) Reflect.deleteProperty(merged, key);
    expect(Object.keys(merged)).toHaveLength(4);
  });

  it("preserves an unknown level's empty record and future fields while ignoring it", () => {
    const unknown = '["host-a","codex","codex","","future"]';
    const entry = { contextWindowTokens: null, autoCompactBufferTokens: null, futureBuffer: 123 };
    writeUsageContextPreferences({
      ...DEFAULT_USAGE_CONTEXT_PREFERENCES,
      overrides: { [unknown]: entry },
    });
    const saved = readUsageContextPreferences();
    expect(saved.overrides[unknown]).toEqual(entry);
    expect(
      resolveUsageContext(saved, source, { contextWindow: 100_000, autoCompactTokenLimit: 90_000 })
        .contextWindow,
    ).toEqual({ value: 100_000, level: "reported" });
    patchUsageContextOverrides(saved, [source], { contextWindowTokens: 200_000 });
    expect(readUsageContextPreferences().overrides[unknown]).toEqual(entry);
  });

  it("does not propose mixed values, singleton groups, or groups with an existing cell", () => {
    const identity = usageContextSourceFromKey(source)!;
    const second = usageContextSourceKey({ ...identity, model: "other" });
    const value = { contextWindowTokens: 100_000, autoCompactBufferTokens: null };
    const preferences = { ...DEFAULT_USAGE_CONTEXT_PREFERENCES, overrides: { [source]: value } };
    expect(proposeUsageContextCollapse(preferences).removed).toEqual([]);
    preferences.overrides[second] = { ...value, contextWindowTokens: 200_000 };
    expect(proposeUsageContextCollapse(preferences).removed).toEqual([]);
    preferences.overrides[second] = value;
    preferences.overrides[usageContextLevelKey(identity, "cell")] = value;
    expect(proposeUsageContextCollapse(preferences).removed).toEqual([]);
  });
});
