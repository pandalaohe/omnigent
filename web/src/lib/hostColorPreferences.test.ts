import { beforeEach, describe, expect, it, vi } from "vitest";

import type * as HostColorPreferencesModule from "./hostColorPreferences";

const { queuePatchMock } = vi.hoisted(() => ({ queuePatchMock: vi.fn() }));

vi.mock("./userPreferencesSync", () => ({
  queueUserPreferencePatch: queuePatchMock,
}));

let hostColorPreferences: typeof HostColorPreferencesModule;

beforeEach(async () => {
  localStorage.clear();
  queuePatchMock.mockReset();
  // `patchHostColor` remembers this page session's changed keys, so each test
  // needs a fresh module rather than the previous test's union.
  vi.resetModules();
  hostColorPreferences = await import("./hostColorPreferences");
});

describe("hostColorPreferences", () => {
  it("drops unknown keys, unknown palette values, nulls and non-strings", () => {
    expect(
      hostColorPreferences.normalizeHostColorPreferences({
        h1: "purple",
        h2: "chartreuse",
        h3: null,
        h4: 7,
        "": "blue",
      }),
    ).toEqual({ h1: "purple" });
    expect(hostColorPreferences.normalizeHostColorPreferences(null)).toEqual({});
    expect(hostColorPreferences.normalizeHostColorPreferences(["purple"])).toEqual({});
  });

  it("queues the union of changed keys and mirrors the full map locally", () => {
    hostColorPreferences.patchHostColor("h1", "purple");
    expect(queuePatchMock).toHaveBeenLastCalledWith("host_colors", { h1: "purple" });

    hostColorPreferences.patchHostColor("h2", "green");
    expect(queuePatchMock).toHaveBeenLastCalledWith("host_colors", {
      h1: "purple",
      h2: "green",
    });
    expect(
      JSON.parse(localStorage.getItem(hostColorPreferences.HOST_COLORS_STORAGE_KEY) ?? "null"),
    ).toEqual({
      h1: "purple",
      h2: "green",
    });
  });

  it("resets one host with an explicit null patch and drops it from the mirror", () => {
    hostColorPreferences.patchHostColor("h1", "purple");
    hostColorPreferences.patchHostColor("h1", null);

    expect(queuePatchMock).toHaveBeenLastCalledWith("host_colors", { h1: null });
    expect(hostColorPreferences.readHostColorPreferences()).toEqual({});
    expect(localStorage.getItem(hostColorPreferences.HOST_COLORS_STORAGE_KEY)).toBeNull();
  });

  it("sanitizes malformed stored values on read", () => {
    localStorage.setItem(
      hostColorPreferences.HOST_COLORS_STORAGE_KEY,
      JSON.stringify({ h1: "purple", h2: null, h3: "nope" }),
    );
    expect(hostColorPreferences.readHostColorPreferences()).toEqual({ h1: "purple" });
  });
});
