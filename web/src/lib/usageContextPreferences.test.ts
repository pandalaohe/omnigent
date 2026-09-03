import { beforeEach, describe, expect, it, vi } from "vitest";

import { queueUserPreferencePatch } from "./userPreferencesSync";

vi.mock("./userPreferencesSync", () => ({ queueUserPreferencePatch: vi.fn() }));

import {
  DEFAULT_USAGE_CONTEXT_PREFERENCES,
  readUsageContextPreferences,
  providerUsageLimitsForSource,
  resolveUsageContextLimits,
  usageContextSourceFromKey,
  usageContextSourceKey,
  writeUsageContextOverride,
  writeUsageContextPreferences,
  writeLastProviderUsageLimits,
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

  it("uses a manual context total and Compact threshold when supplied", () => {
    writeUsageContextOverride(
      {
        version: 4,
        showProviderUsageLimits: false,
        overrides: {},
        lastProviderUsageLimits: {},
      },
      source,
      {
        contextWindowTokens: 330_000,
        autoCompactThresholdPercent: 93,
      },
    );
    const preferences = readUsageContextPreferences();
    expect(preferences.showProviderUsageLimits).toBe(false);
    expect(resolveUsageContextLimits(preferences, source, 258_400, 240_000)).toEqual({
      contextWindow: 330_000,
      autoCompactTokenLimit: 306_900,
    });
  });

  it("does not leak a manual override into a different Host or model", () => {
    writeUsageContextPreferences({
      version: 4,
      showProviderUsageLimits: false,
      overrides: {
        [source]: { contextWindowTokens: 330_000, autoCompactThresholdPercent: 93 },
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
          [source]: { contextWindowTokens: -1, autoCompactThresholdPercent: 101 },
        },
      }),
    );
    expect(readUsageContextPreferences()).toEqual(DEFAULT_USAGE_CONTEXT_PREFERENCES);
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
});
