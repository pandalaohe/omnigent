import { describe, expect, it } from "vitest";

import {
  DEFAULT_MOBILE_MENU_PINS,
  MOBILE_MENU_PIN_IDS,
  normalizeMobileMenuPins,
  toggledMobileMenuPins,
} from "./mobileMenuPins";

describe("mobileMenuPins", () => {
  it("keeps the pinned order and drops ids this build doesn't know", () => {
    expect(normalizeMobileMenuPins(["shells", "gone", "files", "shells"])).toEqual([
      "shells",
      "files",
    ]);
    expect(normalizeMobileMenuPins([7, null, "agents"])).toEqual(["agents"]);
  });

  it("treats a missing value as never edited and an empty list as a real choice", () => {
    expect(normalizeMobileMenuPins(undefined)).toBeUndefined();
    expect(normalizeMobileMenuPins("shells")).toBeUndefined();
    expect(normalizeMobileMenuPins([])).toEqual([]);
  });

  it("pins Shells out of the box", () => {
    expect(DEFAULT_MOBILE_MENU_PINS).toEqual(["shells"]);
    expect(MOBILE_MENU_PIN_IDS).toContain("shells");
  });

  it("appends a new pin and removes an existing one", () => {
    expect(toggledMobileMenuPins(["shells"], "files")).toEqual(["shells", "files"]);
    expect(toggledMobileMenuPins(["shells", "files"], "shells")).toEqual(["files"]);
    expect(toggledMobileMenuPins([], "shells")).toEqual(["shells"]);
  });
});
