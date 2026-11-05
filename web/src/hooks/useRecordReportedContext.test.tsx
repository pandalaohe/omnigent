import { renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  DEFAULT_USAGE_CONTEXT_PREFERENCES,
  readUsageContextPreferences,
  usageContextSourceKey,
  type UsageContextPreferences,
} from "@/lib/usageContextPreferences";

const store = vi.hoisted(() => ({
  contextWindow: 1_000_000 as number | null,
  autoCompactTokenLimit: 550_000 as number | null,
}));

vi.mock("@/store/chatStore", () => ({
  useChatStore: (selector: (state: typeof store) => unknown) => selector(store),
}));

import { useRecordReportedContext } from "./useRecordReportedContext";

const SOURCE = {
  hostId: "host-a",
  agentName: "claude",
  harness: "claude-native",
  model: "claude-opus-5-5",
};

function record(preferences: UsageContextPreferences, key: string) {
  renderHook(() => useRecordReportedContext(preferences, key));
}

describe("useRecordReportedContext", () => {
  beforeEach(() => {
    localStorage.clear();
    store.contextWindow = 1_000_000;
    store.autoCompactTokenLimit = 550_000;
  });

  it("records the session's figures under its exact source", () => {
    const key = usageContextSourceKey(SOURCE);
    record(DEFAULT_USAGE_CONTEXT_PREFERENCES, key);
    expect(readUsageContextPreferences().reportedContext?.[key]).toMatchObject({
      contextWindow: 1_000_000,
      autoCompactTokenLimit: 550_000,
    });
  });

  it.each([
    ["host", { ...SOURCE, hostId: "" }],
    ["agent", { ...SOURCE, agentName: "" }],
    ["harness", { ...SOURCE, harness: "" }],
    ["model", { ...SOURCE, model: "" }],
  ])("waits for a source with no %s", (_part, source) => {
    record(DEFAULT_USAGE_CONTEXT_PREFERENCES, usageContextSourceKey(source));
    expect(readUsageContextPreferences().reportedContext).toBeUndefined();
  });

  it("records nothing until the session reports a figure", () => {
    store.contextWindow = null;
    store.autoCompactTokenLimit = null;
    record(DEFAULT_USAGE_CONTEXT_PREFERENCES, usageContextSourceKey(SOURCE));
    expect(readUsageContextPreferences().reportedContext).toBeUndefined();
  });
});
