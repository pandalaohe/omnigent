import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  MOBILE_MENU_PINS_CHANGED_EVENT,
  MOBILE_MENU_PINS_STORAGE_KEY,
  canonicalMobileMenuPins,
  readMobileMenuPinsPreferences,
  toggleMobileMenuPin,
  writeMobileMenuPins,
} from "./mobileMenuPinPreferences";
import { queueUserPreferencePatch } from "./userPreferencesSync";

vi.mock("./userPreferencesSync", () => ({ queueUserPreferencePatch: vi.fn() }));

beforeEach(() => {
  localStorage.clear();
  vi.mocked(queueUserPreferencePatch).mockClear();
});

describe("mobileMenuPinPreferences", () => {
  it("pins Shells on a device that never edited the list", () => {
    expect(readMobileMenuPinsPreferences()).toEqual({
      version: 1,
      pinnedMenuItems: ["shells"],
    });
  });

  it("falls back to the stock choice for anything unrecognisable", () => {
    for (const value of [null, 7, "shells", [], { version: 2, pinnedMenuItems: ["files"] }]) {
      expect(canonicalMobileMenuPins(value)).toEqual({ version: 1, pinnedMenuItems: ["shells"] });
    }
    expect(canonicalMobileMenuPins({ version: 1, pinnedMenuItems: "files" })).toEqual({
      version: 1,
      pinnedMenuItems: ["shells"],
    });
  });

  it("drops ids this build does not know and keeps an explicit empty list", () => {
    expect(
      canonicalMobileMenuPins({
        version: 1,
        pinnedMenuItems: ["files", "retired-row", "files", "shells"],
      }),
    ).toEqual({ version: 1, pinnedMenuItems: ["files", "shells"] });
    expect(canonicalMobileMenuPins({ version: 1, pinnedMenuItems: [] })).toEqual({
      version: 1,
      pinnedMenuItems: [],
    });
  });

  it("stores the whole namespace value and queues that namespace", () => {
    const changed = vi.fn();
    window.addEventListener(MOBILE_MENU_PINS_CHANGED_EVENT, changed);

    writeMobileMenuPins(["files", "shells"]);

    expect(JSON.parse(localStorage.getItem(MOBILE_MENU_PINS_STORAGE_KEY)!)).toEqual({
      version: 1,
      pinnedMenuItems: ["files", "shells"],
    });
    expect(queueUserPreferencePatch).toHaveBeenCalledWith("mobile_menu_pins", {
      version: 1,
      pinnedMenuItems: ["files", "shells"],
    });
    expect(changed).toHaveBeenCalled();
    window.removeEventListener(MOBILE_MENU_PINS_CHANGED_EVENT, changed);
  });

  it("appends a new pin and removes an existing one in place", () => {
    toggleMobileMenuPin("files");
    expect(readMobileMenuPinsPreferences().pinnedMenuItems).toEqual(["shells", "files"]);

    toggleMobileMenuPin("shells");
    expect(readMobileMenuPinsPreferences().pinnedMenuItems).toEqual(["files"]);
    expect(queueUserPreferencePatch).toHaveBeenLastCalledWith("mobile_menu_pins", {
      version: 1,
      pinnedMenuItems: ["files"],
    });
  });

  it("reads a malformed stored value as the stock choice", () => {
    localStorage.setItem(MOBILE_MENU_PINS_STORAGE_KEY, "{broken");
    expect(readMobileMenuPinsPreferences().pinnedMenuItems).toEqual(["shells"]);
  });
});
