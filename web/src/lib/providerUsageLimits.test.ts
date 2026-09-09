import { describe, expect, it } from "vitest";

import {
  formatProviderUsageLimits,
  providerUsageLimitsFromCodex,
  providerUsageLimitsMatchesSource,
  providerUsageLimitsFromWire,
} from "./providerUsageLimits";

describe("provider usage limits", () => {
  it("parses Claude's provider-neutral wire snapshot", () => {
    expect(
      providerUsageLimitsFromWire({
        provider: "Claude",
        scope: "Claude plan",
        captured_at: 1_900_000_000,
        windows: [
          {
            label: "5h",
            aria_label: "5 hour",
            used_percent: 11.4,
            duration_mins: 300,
          },
        ],
      }),
    ).toEqual({
      provider: "Claude",
      scope: "Claude plan",
      capturedAt: 1_900_000_000,
      windows: [{ label: "5h", ariaLabel: "5 hour", usedPercent: 11.4, durationMinutes: 300 }],
    });
  });

  it("adapts and formats Codex without inventing a missing monthly window", () => {
    const snapshot = providerUsageLimitsFromCodex(
      {
        captured_at: 1_900_000_000,
        limits: [
          {
            limit_id: "codex",
            limit_name: "Codex",
            windows: [
              { kind: "primary", used_percent: 11.4, window_duration_mins: 300 },
              { kind: "secondary", used_percent: 6, window_duration_mins: 10_080 },
            ],
          },
        ],
      },
      "gpt-5.6",
    );
    expect(formatProviderUsageLimits(snapshot, 1_900_000_010)).toMatchObject({
      text: "5h:11% w:6%",
      ariaLabel: "Codex usage: 5 hour 11% used, weekly 6% used",
    });
  });

  it("prefers the most specific Codex model bucket", () => {
    const snapshot = providerUsageLimitsFromCodex(
      {
        captured_at: 1_900_000_000,
        limits: [
          {
            limit_id: "codex",
            limit_name: "Codex",
            windows: [{ kind: "primary", used_percent: 11, window_duration_mins: 300 }],
          },
          {
            limit_id: "codex-spark",
            limit_name: "Codex Spark",
            windows: [{ kind: "primary", used_percent: 32, window_duration_mins: 300 }],
          },
        ],
      },
      "gpt-5.3-codex-spark",
    );

    expect(formatProviderUsageLimits(snapshot, 1_900_000_010)?.text).toBe("5h:32%");
  });

  it("falls back to a case-insensitive canonical bucket with displayable windows", () => {
    const snapshot = providerUsageLimitsFromCodex(
      {
        captured_at: 1_900_000_000,
        limits: [
          {
            limit_id: "special",
            limit_name: "Model One",
            windows: [{ kind: "primary", used_percent: 32, window_duration_mins: 60 }],
          },
          {
            limit_id: "CoDeX",
            windows: [{ kind: "primary", used_percent: 8, window_duration_mins: 300 }],
          },
        ],
      },
      "model-one",
    );

    expect(formatProviderUsageLimits(snapshot, 1_900_000_010)?.text).toBe("5h:8%");
  });

  it("hides malformed or unmatched Codex buckets instead of throwing or guessing", () => {
    expect(() =>
      providerUsageLimitsFromCodex(
        { captured_at: 1_900_000_000, limits: [null] } as never,
        "model-one",
      ),
    ).not.toThrow();
    expect(
      providerUsageLimitsFromCodex(
        {
          captured_at: 1_900_000_000,
          limits: [
            {
              limit_id: "unrelated",
              windows: [{ kind: "primary", used_percent: 9, window_duration_mins: 300 }],
            },
          ],
        },
        "model-one",
      ),
    ).toBeNull();
  });

  it("stops presenting a cached reading as current after one hour", () => {
    const snapshot = providerUsageLimitsFromWire({
      provider: "Claude",
      captured_at: 1_900_000_000,
      windows: [{ label: "5h", aria_label: "5 hour", used_percent: 7 }],
    });
    expect(formatProviderUsageLimits(snapshot, 1_900_003_601)).toBeNull();
  });

  it("rejects a provider snapshot from another native agent family", () => {
    const claude = providerUsageLimitsFromWire({
      provider: "Claude",
      captured_at: 1_900_000_000,
      windows: [{ label: "5h", aria_label: "5 hour", used_percent: 7 }],
    });
    expect(providerUsageLimitsMatchesSource(claude, { agentName: "codex", harness: "codex" })).toBe(
      false,
    );
    expect(
      providerUsageLimitsMatchesSource(claude, {
        agentName: "claude-native",
        harness: "claude-native",
      }),
    ).toBe(true);
  });
});
