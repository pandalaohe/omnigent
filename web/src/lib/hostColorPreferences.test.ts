import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  HOST_COLORS_STORAGE_KEY,
  normalizeHostColorPreferences,
  patchHostColor,
  readHostColorPreferences,
} from "./hostColorPreferences";

const { queuePatchMock } = vi.hoisted(() => ({ queuePatchMock: vi.fn() }));

vi.mock("./userPreferencesSync", () => ({
  queueUserPreferencePatch: queuePatchMock,
}));

beforeEach(() => {
  localStorage.clear();
  queuePatchMock.mockReset();
});

describe("hostColorPreferences", () => {
  it("drops unknown keys, unknown palette values, nulls and non-strings, keeps auto", () => {
    expect(
      normalizeHostColorPreferences({
        h1: "purple",
        h2: "chartreuse",
        h3: null,
        h4: 7,
        h5: "auto",
        "": "blue",
      }),
    ).toEqual({ h1: "purple", h5: "auto" });
    expect(normalizeHostColorPreferences(null)).toEqual({});
    expect(normalizeHostColorPreferences(["purple"])).toEqual({});
  });

  it("queues the full local map and mirrors it locally", () => {
    patchHostColor("h1", "purple");
    expect(queuePatchMock).toHaveBeenLastCalledWith("host_colors", { h1: "purple" });

    patchHostColor("h2", "green");
    expect(queuePatchMock).toHaveBeenLastCalledWith("host_colors", {
      h1: "purple",
      h2: "green",
    });
    expect(JSON.parse(localStorage.getItem(HOST_COLORS_STORAGE_KEY) ?? "null")).toEqual({
      h1: "purple",
      h2: "green",
    });
  });

  it("resets one host to the auto tombstone in the patch and the mirror", () => {
    patchHostColor("h1", "purple");
    patchHostColor("h1", null);

    expect(queuePatchMock).toHaveBeenLastCalledWith("host_colors", { h1: "auto" });
    expect(readHostColorPreferences()).toEqual({ h1: "auto" });
    expect(JSON.parse(localStorage.getItem(HOST_COLORS_STORAGE_KEY) ?? "null")).toEqual({
      h1: "auto",
    });
  });

  it("sanitizes malformed stored values on read", () => {
    localStorage.setItem(
      HOST_COLORS_STORAGE_KEY,
      JSON.stringify({ h1: "purple", h2: null, h3: "nope", h4: "auto" }),
    );
    expect(readHostColorPreferences()).toEqual({ h1: "purple", h4: "auto" });
  });
});
