import { beforeEach, describe, expect, it, vi } from "vitest";

const { queuePatchMock } = vi.hoisted(() => ({ queuePatchMock: vi.fn() }));

vi.mock("./userPreferencesSync", () => ({
  queueUserPreferencePatch: queuePatchMock,
}));

import {
  HOST_COLORS_STORAGE_KEY,
  normalizeHostColorPreferences,
  patchHostColor,
  readHostColorPreferences,
} from "./hostColorPreferences";

beforeEach(() => {
  localStorage.clear();
  queuePatchMock.mockReset();
});

describe("hostColorPreferences", () => {
  it("drops unknown keys, unknown palette values, nulls and non-strings", () => {
    expect(
      normalizeHostColorPreferences({
        h1: "purple",
        h2: "chartreuse",
        h3: null,
        h4: 7,
        "": "blue",
      }),
    ).toEqual({ h1: "purple" });
    expect(normalizeHostColorPreferences(null)).toEqual({});
    expect(normalizeHostColorPreferences(["purple"])).toEqual({});
  });

  it("writes a one-key patch and mirrors the full map locally", () => {
    patchHostColor("h1", "purple");
    expect(queuePatchMock).toHaveBeenLastCalledWith("host_colors", { h1: "purple" });

    patchHostColor("h2", "green");
    expect(queuePatchMock).toHaveBeenLastCalledWith("host_colors", { h2: "green" });
    expect(JSON.parse(localStorage.getItem(HOST_COLORS_STORAGE_KEY) ?? "null")).toEqual({
      h1: "purple",
      h2: "green",
    });
  });

  it("resets one host with an explicit null patch and drops it from the mirror", () => {
    patchHostColor("h1", "purple");
    patchHostColor("h1", null);

    expect(queuePatchMock).toHaveBeenLastCalledWith("host_colors", { h1: null });
    expect(readHostColorPreferences()).toEqual({});
    expect(localStorage.getItem(HOST_COLORS_STORAGE_KEY)).toBeNull();
  });

  it("sanitizes malformed stored values on read", () => {
    localStorage.setItem(
      HOST_COLORS_STORAGE_KEY,
      JSON.stringify({ h1: "purple", h2: null, h3: "nope" }),
    );
    expect(readHostColorPreferences()).toEqual({ h1: "purple" });
  });
});
