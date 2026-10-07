import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  RUNNER_LOG_WARNING_RETOUCH_MS,
  RUNNER_LOG_WARNINGS_STORAGE_KEY,
  dismissRunnerLogWarning,
  normalizeRunnerLogWarningPreferences,
  readDismissedRunnerLogWarnings,
  touchRunnerLogWarningDismissals,
} from "./runnerLogWarningPreferences";

const { queuePatchMock } = vi.hoisted(() => ({ queuePatchMock: vi.fn() }));

vi.mock("./userPreferencesSync", () => ({
  queueUserPreferencePatch: queuePatchMock,
}));

const FLAG_A = "2026-09-23T09:25:00+00:00";
const FLAG_B = "2026-09-23T10:25:00+00:00";
const DISMISSED_AT_A = Date.parse("2026-10-07T06:22:16Z");
const DISMISSED_AT_B = Date.parse("2026-10-07T06:23:00Z");

beforeEach(() => {
  localStorage.clear();
  queuePatchMock.mockReset();
  vi.useRealTimers();
});

describe("runnerLogWarningPreferences", () => {
  it("normalizes to a map, dropping junk keys and non-number values", () => {
    expect(
      normalizeRunnerLogWarningPreferences({
        [FLAG_A]: DISMISSED_AT_A,
        "": 2000,
        [FLAG_B]: "3000",
        missing: null,
        nan: Number.NaN,
        infinite: Number.POSITIVE_INFINITY,
        list: [1, 2],
      }),
    ).toEqual({ [FLAG_A]: DISMISSED_AT_A });
  });

  it("returns an empty map for junk shapes", () => {
    expect(normalizeRunnerLogWarningPreferences(null)).toEqual({});
    expect(normalizeRunnerLogWarningPreferences(["a"])).toEqual({});
    expect(normalizeRunnerLogWarningPreferences({ dismissed: [FLAG_A] })).toEqual({});
  });

  it("keeps every dismissal — no count cap", () => {
    const all: Record<string, number> = {};
    for (let index = 0; index < 60; index += 1) {
      all[`instant-${index}`] = index;
    }
    expect(normalizeRunnerLogWarningPreferences(all)).toEqual(all);
  });

  it("reads back a dismissed flag from localStorage", () => {
    vi.useFakeTimers();
    vi.setSystemTime(DISMISSED_AT_A);
    expect(readDismissedRunnerLogWarnings()).toEqual({});

    dismissRunnerLogWarning(FLAG_A);
    expect(readDismissedRunnerLogWarnings()).toEqual({ [FLAG_A]: DISMISSED_AT_A });
    expect(JSON.parse(localStorage.getItem(RUNNER_LOG_WARNINGS_STORAGE_KEY) ?? "null")).toEqual({
      [FLAG_A]: DISMISSED_AT_A,
    });
  });

  it("no-ops for an empty flag", () => {
    dismissRunnerLogWarning("");
    expect(readDismissedRunnerLogWarnings()).toEqual({});
    expect(queuePatchMock).not.toHaveBeenCalled();
  });

  it("queues the full map on each dismissal", () => {
    vi.useFakeTimers();
    vi.setSystemTime(DISMISSED_AT_A);
    dismissRunnerLogWarning(FLAG_A);
    expect(queuePatchMock).toHaveBeenLastCalledWith("runner_log_warnings", {
      [FLAG_A]: DISMISSED_AT_A,
    });

    vi.setSystemTime(DISMISSED_AT_B);
    dismissRunnerLogWarning(FLAG_B);
    expect(queuePatchMock).toHaveBeenLastCalledWith("runner_log_warnings", {
      [FLAG_A]: DISMISSED_AT_A,
      [FLAG_B]: DISMISSED_AT_B,
    });
  });

  it("treats a repeat dismissal of the same flag as a no-op", () => {
    vi.useFakeTimers();
    vi.setSystemTime(DISMISSED_AT_A);
    dismissRunnerLogWarning(FLAG_A);
    queuePatchMock.mockClear();

    vi.setSystemTime(DISMISSED_AT_B);
    dismissRunnerLogWarning(FLAG_A);
    expect(queuePatchMock).not.toHaveBeenCalled();
    expect(readDismissedRunnerLogWarnings()).toEqual({ [FLAG_A]: DISMISSED_AT_A });
  });

  it("announces the change to listening components", () => {
    const changed = vi.fn();
    window.addEventListener("omnigent:runner-log-warnings-changed", changed);
    try {
      dismissRunnerLogWarning(FLAG_A);
      expect(changed).toHaveBeenCalledTimes(1);
    } finally {
      window.removeEventListener("omnigent:runner-log-warnings-changed", changed);
    }
  });

  it("re-touches a dismissal older than a day and queues the full map", () => {
    const now = DISMISSED_AT_A + 2 * RUNNER_LOG_WARNING_RETOUCH_MS;
    localStorage.setItem(
      RUNNER_LOG_WARNINGS_STORAGE_KEY,
      JSON.stringify({ [FLAG_A]: DISMISSED_AT_A, [FLAG_B]: now - 60_000 }),
    );

    expect(touchRunnerLogWarningDismissals([FLAG_A], now)).toBe(true);
    expect(readDismissedRunnerLogWarnings()).toEqual({
      [FLAG_A]: now,
      [FLAG_B]: now - 60_000,
    });
    expect(queuePatchMock).toHaveBeenLastCalledWith("runner_log_warnings", {
      [FLAG_A]: now,
      [FLAG_B]: now - 60_000,
    });
  });

  it("leaves a dismissal touched within the last day alone and queues nothing", () => {
    const now = DISMISSED_AT_A + 2 * RUNNER_LOG_WARNING_RETOUCH_MS;
    const fresh = now - RUNNER_LOG_WARNING_RETOUCH_MS;
    localStorage.setItem(RUNNER_LOG_WARNINGS_STORAGE_KEY, JSON.stringify({ [FLAG_A]: fresh }));

    expect(touchRunnerLogWarningDismissals([FLAG_A], now)).toBe(false);
    expect(readDismissedRunnerLogWarnings()).toEqual({ [FLAG_A]: fresh });
    expect(queuePatchMock).not.toHaveBeenCalled();
  });

  it("ignores flags that were never dismissed", () => {
    localStorage.setItem(
      RUNNER_LOG_WARNINGS_STORAGE_KEY,
      JSON.stringify({ [FLAG_A]: DISMISSED_AT_A }),
    );

    expect(
      touchRunnerLogWarningDismissals([FLAG_B], DISMISSED_AT_A + 2 * RUNNER_LOG_WARNING_RETOUCH_MS),
    ).toBe(false);
    expect(readDismissedRunnerLogWarnings()).toEqual({ [FLAG_A]: DISMISSED_AT_A });
    expect(queuePatchMock).not.toHaveBeenCalled();
  });

  it("announces and returns false when nothing needs a touch", () => {
    const changed = vi.fn();
    window.addEventListener("omnigent:runner-log-warnings-changed", changed);
    try {
      expect(touchRunnerLogWarningDismissals([FLAG_A], DISMISSED_AT_A)).toBe(false);
      expect(changed).not.toHaveBeenCalled();
    } finally {
      window.removeEventListener("omnigent:runner-log-warnings-changed", changed);
    }
  });
});
