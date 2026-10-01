import { beforeEach, describe, expect, it, vi } from "vitest";

import { queueUserPreferencePatch } from "./userPreferencesSync";

vi.mock("./userPreferencesSync", () => ({ queueUserPreferencePatch: vi.fn() }));

import {
  DEFAULT_USAGE_CONTEXT_PREFERENCES,
  deleteUsageContextOverrides,
  patchUsageContextOverrides,
  readUsageContextPreferences,
  providerUsageLimitsForSource,
  resolveUsageContextLimits,
  usageContextSourceFromKey,
  usageContextSourceKey,
  writeUsageContextOverride,
  writeUsageContextPreferences,
  writeLastProviderUsageLimits,
  type UsageContextPreferences,
} from "./usageContextPreferences";

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

  it("falls back to the reported Compact limit when the buffer meets the context total", () => {
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
      autoCompactTokenLimit: 240_000,
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
});
