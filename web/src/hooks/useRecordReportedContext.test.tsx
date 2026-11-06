import { renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  DEFAULT_USAGE_CONTEXT_PREFERENCES,
  readUsageContextPreferences,
  usageContextSourceKey,
  writeReportedContext,
  type UsageContextPreferences,
} from "@/lib/usageContextPreferences";

const store = vi.hoisted(() => ({
  conversationId: "session-1" as string | null,
  contextWindow: 1_000_000 as number | null,
  autoCompactTokenLimit: 550_000 as number | null,
  reportedContextReading: {
    model: "claude-opus-5-5",
    contextWindow: 1_000_000 as number | null,
    autoCompactTokenLimit: 550_000 as number | null,
  },
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
    store.conversationId = "session-1";
    store.contextWindow = 1_000_000;
    store.autoCompactTokenLimit = 550_000;
    store.reportedContextReading = {
      model: SOURCE.model,
      contextWindow: 1_000_000,
      autoCompactTokenLimit: 550_000,
    };
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
    store.reportedContextReading = {
      model: SOURCE.model,
      contextWindow: null,
      autoCompactTokenLimit: null,
    };
    record(DEFAULT_USAGE_CONTEXT_PREFERENCES, usageContextSourceKey(SOURCE));
    expect(readUsageContextPreferences().reportedContext).toBeUndefined();
  });

  it("does not write its reading back over another tab's for the same source", () => {
    const key = usageContextSourceKey(SOURCE);
    const { rerender } = renderHook(
      ({ preferences }) => useRecordReportedContext(preferences, key),
      { initialProps: { preferences: DEFAULT_USAGE_CONTEXT_PREFERENCES } },
    );
    // Another tab's session on the same source reports a different compact point.
    writeReportedContext(readUsageContextPreferences(), key, {
      contextWindow: 1_000_000,
      autoCompactTokenLimit: 367_000,
    });
    rerender({ preferences: readUsageContextPreferences() });
    expect(readUsageContextPreferences().reportedContext?.[key]?.autoCompactTokenLimit).toBe(
      367_000,
    );
    store.reportedContextReading = { ...store.reportedContextReading };
    rerender({ preferences: readUsageContextPreferences() });
    expect(readUsageContextPreferences().reportedContext?.[key]?.autoCompactTokenLimit).toBe(
      550_000,
    );
  });

  it("keeps the old model's reading when the model switches before the new reading", () => {
    const opus = usageContextSourceKey(SOURCE);
    const sonnet = usageContextSourceKey({ ...SOURCE, model: "claude-sonnet-5-5" });
    const { rerender } = renderHook(
      ({ key }) => useRecordReportedContext(readUsageContextPreferences(), key),
      { initialProps: { key: opus } },
    );
    rerender({ key: sonnet });
    expect(readUsageContextPreferences().reportedContext?.[sonnet]).toBeUndefined();

    store.contextWindow = 200_000;
    store.autoCompactTokenLimit = 167_000;
    store.reportedContextReading = {
      model: "claude-sonnet-5-5",
      contextWindow: 200_000,
      autoCompactTokenLimit: 167_000,
    };
    rerender({ key: sonnet });
    const reported = readUsageContextPreferences().reportedContext;
    expect(reported?.[sonnet]).toMatchObject({
      contextWindow: 200_000,
      autoCompactTokenLimit: 167_000,
    });
    expect(reported?.[opus]).toMatchObject({
      contextWindow: 1_000_000,
      autoCompactTokenLimit: 550_000,
    });
  });

  it("records another session's source even when its figures match the last one", () => {
    const first = usageContextSourceKey(SOURCE);
    const second = usageContextSourceKey({ ...SOURCE, hostId: "host-b" });
    const { rerender } = renderHook(
      ({ key }) => useRecordReportedContext(readUsageContextPreferences(), key),
      { initialProps: { key: first } },
    );
    store.conversationId = "session-2";
    rerender({ key: second });
    expect(readUsageContextPreferences().reportedContext?.[second]).toMatchObject({
      contextWindow: 1_000_000,
      autoCompactTokenLimit: 550_000,
    });
  });

  it.each([true, false])(
    "attributes usage arriving before the model change (same figures: %s)",
    (sameFigures) => {
      const opus = usageContextSourceKey(SOURCE);
      const sonnet = usageContextSourceKey({ ...SOURCE, model: "claude-sonnet-5-5" });
      const { rerender } = renderHook(
        ({ key }) => useRecordReportedContext(readUsageContextPreferences(), key),
        { initialProps: { key: opus } },
      );
      const reading = {
        model: "claude-sonnet-5-5",
        contextWindow: sameFigures ? 1_000_000 : 200_000,
        autoCompactTokenLimit: sameFigures ? 550_000 : 167_000,
      };
      store.contextWindow = reading.contextWindow;
      store.autoCompactTokenLimit = reading.autoCompactTokenLimit;
      store.reportedContextReading = reading;
      rerender({ key: opus });
      expect(readUsageContextPreferences().reportedContext?.[sonnet]).toMatchObject({
        contextWindow: reading.contextWindow,
        autoCompactTokenLimit: reading.autoCompactTokenLimit,
      });
      expect(readUsageContextPreferences().reportedContext?.[opus]).toMatchObject({
        contextWindow: 1_000_000,
        autoCompactTokenLimit: 550_000,
      });
      rerender({ key: sonnet });
      expect(Object.keys(readUsageContextPreferences().reportedContext ?? {})).toHaveLength(2);
    },
  );

  it("records a new identical reading after switching models in the same session", () => {
    const opus = usageContextSourceKey(SOURCE);
    const sonnet = usageContextSourceKey({ ...SOURCE, model: "claude-sonnet-5-5" });
    const { rerender } = renderHook(
      ({ key }) => useRecordReportedContext(readUsageContextPreferences(), key),
      { initialProps: { key: opus } },
    );
    rerender({ key: sonnet });
    expect(readUsageContextPreferences().reportedContext?.[sonnet]).toBeUndefined();
    store.reportedContextReading = { ...store.reportedContextReading, model: "claude-sonnet-5-5" };
    rerender({ key: sonnet });
    expect(readUsageContextPreferences().reportedContext?.[sonnet]).toMatchObject({
      contextWindow: 1_000_000,
      autoCompactTokenLimit: 550_000,
    });
  });
});
