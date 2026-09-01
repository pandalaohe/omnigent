import { beforeEach, describe, expect, it } from "vitest";

import {
  MAX_SESSION_POLLING_WINDOW_HOURS,
  readSessionNavigationPreferences,
  SESSION_NAVIGATION_STORAGE_KEY,
  shouldHideNativeServerSwitcher,
  isSessionInsidePollingWindow,
  writeSessionNavigationPreferences,
} from "./sessionNavigationPreferences";

describe("sessionNavigationPreferences", () => {
  beforeEach(() => localStorage.clear());

  it("keeps official navigation behavior when unset or corrupt", () => {
    expect(readSessionNavigationPreferences()).toEqual({
      pollingActiveWindowHours: null,
      nativeMobileHeaderMode: "server",
    });
    localStorage.setItem(SESSION_NAVIGATION_STORAGE_KEY, "{broken");
    expect(readSessionNavigationPreferences()).toEqual({
      pollingActiveWindowHours: null,
      nativeMobileHeaderMode: "server",
    });
  });

  it("normalizes stored hours and preserves title mode", () => {
    localStorage.setItem(
      SESSION_NAVIGATION_STORAGE_KEY,
      JSON.stringify({
        pollingActiveWindowHours: MAX_SESSION_POLLING_WINDOW_HOURS + 10,
        nativeMobileHeaderMode: "conversation-title",
      }),
    );
    expect(readSessionNavigationPreferences()).toEqual({
      pollingActiveWindowHours: MAX_SESSION_POLLING_WINDOW_HOURS,
      nativeMobileHeaderMode: "conversation-title",
    });
  });

  it("removes storage when preferences return to official defaults", () => {
    writeSessionNavigationPreferences({
      pollingActiveWindowHours: 24,
      nativeMobileHeaderMode: "conversation-title",
    });
    expect(localStorage.getItem(SESSION_NAVIGATION_STORAGE_KEY)).not.toBeNull();

    writeSessionNavigationPreferences({
      pollingActiveWindowHours: null,
      nativeMobileHeaderMode: "server",
    });
    expect(localStorage.getItem(SESSION_NAVIGATION_STORAGE_KEY)).toBeNull();
  });
});

describe("isSessionInsidePollingWindow", () => {
  const nowMs = 1_800_000_000_000;
  const nowSeconds = nowMs / 1000;

  it("accepts every timestamp when the window is unset", () => {
    expect(isSessionInsidePollingWindow(0, null, nowMs)).toBe(true);
  });

  it("uses updated_at seconds and includes the exact boundary", () => {
    expect(isSessionInsidePollingWindow(nowSeconds - 2 * 60 * 60, 2, nowMs)).toBe(true);
    expect(isSessionInsidePollingWindow(nowSeconds - 2 * 60 * 60 - 1, 2, nowMs)).toBe(false);
  });
});

describe("shouldHideNativeServerSwitcher", () => {
  it("keeps the official switcher on the frontmost chat", () => {
    expect(
      shouldHideNativeServerSwitcher({ frontmost: true, sidebarOpen: false, headerMode: "server" }),
    ).toBe(false);
    expect(
      shouldHideNativeServerSwitcher({ frontmost: false, sidebarOpen: true, headerMode: "server" }),
    ).toBe(true);
  });

  it("moves the switcher off the chat and onto the open sidebar in title mode", () => {
    expect(
      shouldHideNativeServerSwitcher({
        frontmost: true,
        sidebarOpen: false,
        headerMode: "conversation-title",
      }),
    ).toBe(true);
    expect(
      shouldHideNativeServerSwitcher({
        frontmost: false,
        sidebarOpen: true,
        headerMode: "conversation-title",
      }),
    ).toBe(false);
  });
});
