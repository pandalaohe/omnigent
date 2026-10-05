import { describe, expect, it } from "vitest";
import type { KeepWarmStatus } from "@/hooks/useConversations";
import {
  formatKeepWarmCost,
  formatKeepWarmCounters,
  keepWarmLastReturnLabel,
  keepWarmStopReason,
  keepWarmTooltipLine,
} from "./keepWarmStatus";

const NOW_MS = 1_700_100_000_000;

function status(partial: Partial<KeepWarmStatus> = {}): KeepWarmStatus {
  return {
    state: "on",
    stop_reason: null,
    episode: { pings: 0, cost_usd: 0, estimated: false, started_at: null },
    total: { pings: 0, cost_usd: 0, estimated: false },
    last_return: null,
    last_reason: null,
    ...partial,
  };
}

describe("formatKeepWarmCost", () => {
  it("formats dollars to cents", () => {
    expect(formatKeepWarmCost(0, false)).toBe("$0.00");
    expect(formatKeepWarmCost(0.038, false)).toBe("$0.04");
  });

  it("prefixes estimated costs with ≈", () => {
    expect(formatKeepWarmCost(0.038, true)).toBe("≈$0.04");
  });
});

describe("formatKeepWarmCounters", () => {
  it("singularizes one ping", () => {
    expect(formatKeepWarmCounters(1, 0.01, false)).toBe("1 ping $0.01");
  });

  it("pluralizes and keeps the estimate marker", () => {
    expect(formatKeepWarmCounters(4, 0.038, true)).toBe("4 pings ≈$0.04");
  });
});

describe("keepWarmStopReason", () => {
  it("returns null when no reason is stored", () => {
    expect(keepWarmStopReason(status())).toBeNull();
  });

  it("reports a cap stop as 'cap reached' regardless of episode age", () => {
    // `started_at` predates any cold time after the stop, so it must not feed
    // an elapsed-hours label.
    const startedAt = NOW_MS / 1000 - 4 * 3600;
    expect(
      keepWarmStopReason(
        status({
          stop_reason: "cap",
          episode: { pings: 4, cost_usd: 0, estimated: false, started_at: startedAt },
        }),
      ),
    ).toBe("cap reached");
    expect(keepWarmStopReason(status({ stop_reason: "cap" }))).toBe("cap reached");
  });

  it("maps misses to cache misses", () => {
    expect(keepWarmStopReason(status({ stop_reason: "misses" }))).toBe("cache misses");
  });

  it("names the last failure code when one is stored", () => {
    expect(
      keepWarmStopReason(status({ stop_reason: "failures", last_reason: "btw_unavailable" })),
    ).toBe("failures (btw_unavailable)");
    expect(keepWarmStopReason(status({ stop_reason: "failures" }))).toBe("failures");
  });

  it("maps card, host, switch_off and runner_version", () => {
    expect(keepWarmStopReason(status({ stop_reason: "card" }))).toBe("pending card");
    expect(keepWarmStopReason(status({ stop_reason: "host" }))).toBe("host offline");
    expect(keepWarmStopReason(status({ stop_reason: "switch_off" }))).toBe("switched off");
    expect(keepWarmStopReason(status({ stop_reason: "runner_version" }))).toBe("runner too old");
  });
});

describe("keepWarmTooltipLine", () => {
  it("renders a null status as a likely-cold cache", () => {
    expect(keepWarmTooltipLine(null)).toBe("Prompt cache likely cold");
  });

  it("renders a stopped cap with counters, estimate included", () => {
    const startedAt = NOW_MS / 1000 - 4 * 3600;
    expect(
      keepWarmTooltipLine(
        status({
          state: "stopped",
          stop_reason: "cap",
          episode: { pings: 4, cost_usd: 0.038, estimated: true, started_at: startedAt },
        }),
      ),
    ).toBe("Keep-warm stopped: cap reached · 4 pings ≈$0.04");
  });

  it("renders a paused failure with the raw reason and its cost", () => {
    expect(
      keepWarmTooltipLine(
        status({
          state: "paused",
          stop_reason: "failures",
          last_reason: "btw_unavailable",
          episode: { pings: 3, cost_usd: 0, estimated: false, started_at: null },
        }),
      ),
    ).toBe("Keep-warm paused: failures (btw_unavailable) · 3 pings $0.00");
  });

  it("renders an active episode's counters", () => {
    expect(
      keepWarmTooltipLine(
        status({
          episode: { pings: 2, cost_usd: 0.02, estimated: false, started_at: null },
        }),
      ),
    ).toBe("Keep-warm on · 2 pings $0.02");
  });

  it("renders off without counters", () => {
    expect(keepWarmTooltipLine(status({ state: "off" }))).toBe("Keep-warm off");
  });

  it("carries a stop reason on an off state", () => {
    expect(keepWarmTooltipLine(status({ state: "off", stop_reason: "switch_off" }))).toBe(
      "Keep-warm off: switched off",
    );
  });
});

describe("keepWarmLastReturnLabel", () => {
  it("shows the result and relative time", () => {
    expect(keepWarmLastReturnLabel({ at: NOW_MS / 1000 - 7200, result: "hit" }, NOW_MS)).toBe(
      "hit · 2h",
    );
  });
});
