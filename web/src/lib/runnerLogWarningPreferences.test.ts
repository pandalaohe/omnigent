import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  RUNNER_LOG_WARNINGS_STORAGE_KEY,
  dismissRunnerLogWarning,
  normalizeRunnerLogWarningPreferences,
  readDismissedRunnerLogWarnings,
} from "./runnerLogWarningPreferences";

const { queuePatchMock } = vi.hoisted(() => ({ queuePatchMock: vi.fn() }));

vi.mock("./userPreferencesSync", () => ({
  queueUserPreferencePatch: queuePatchMock,
}));

const FLAG_A = "2026-09-23T09:25:00+00:00";
const FLAG_B = "2026-09-23T10:25:00+00:00";

beforeEach(() => {
  localStorage.clear();
  queuePatchMock.mockReset();
});

describe("runnerLogWarningPreferences", () => {
  it("drops junk, de-duplicates and keeps only the 50 newest instants", () => {
    const instants = Array.from({ length: 60 }, (_, index) => `instant-${index}`);
    const normalized = normalizeRunnerLogWarningPreferences({
      dismissed: ["a", "", 7, null, "a", ...instants],
    });
    expect(normalized).toHaveLength(50);
    expect(normalized[0]).toBe("instant-10");
    expect(normalized.at(-1)).toBe("instant-59");
  });

  it("returns an empty list for junk shapes", () => {
    expect(normalizeRunnerLogWarningPreferences(null)).toEqual([]);
    expect(normalizeRunnerLogWarningPreferences(["a"])).toEqual([]);
    expect(normalizeRunnerLogWarningPreferences({ dismissed: "a" })).toEqual([]);
  });

  it("reads back a dismissed flag from localStorage", () => {
    expect(readDismissedRunnerLogWarnings()).toEqual([]);
    dismissRunnerLogWarning(FLAG_A);
    expect(readDismissedRunnerLogWarnings()).toEqual([FLAG_A]);
    expect(JSON.parse(localStorage.getItem(RUNNER_LOG_WARNINGS_STORAGE_KEY) ?? "null")).toEqual({
      dismissed: [FLAG_A],
    });
  });

  it("no-ops for an empty flag", () => {
    dismissRunnerLogWarning("");
    expect(readDismissedRunnerLogWarnings()).toEqual([]);
    expect(queuePatchMock).not.toHaveBeenCalled();
  });

  it("queues the full dismissed list on each dismissal", () => {
    dismissRunnerLogWarning(FLAG_A);
    expect(queuePatchMock).toHaveBeenLastCalledWith("runner_log_warnings", {
      dismissed: [FLAG_A],
    });

    dismissRunnerLogWarning(FLAG_B);
    expect(queuePatchMock).toHaveBeenLastCalledWith("runner_log_warnings", {
      dismissed: [FLAG_A, FLAG_B],
    });
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
});
