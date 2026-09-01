import { beforeEach, describe, expect, it, vi } from "vitest";
import { COMPOSER_SEND_SHORTCUT_STORAGE_KEY } from "./composerSendShortcutPreferences";
import { CONTEXT_INDICATOR_STORAGE_KEY } from "./contextIndicatorPreferences";
import { KEYBOARD_SHORTCUTS_STORAGE_KEY } from "./keyboardShortcutPreferences";
import { MOBILE_ASSISTANT_STORAGE_KEY } from "./mobileAssistantPreferences";
import {
  SESSION_NAVIGATION_CHANGED_EVENT,
  SESSION_NAVIGATION_STORAGE_KEY,
} from "./sessionNavigationPreferences";
import { applyImportedSettings, collectSettings } from "./settingsPortability";

beforeEach(() => localStorage.clear());

describe("composer shortcut portability", () => {
  it("exports, imports, and clears the device-local preference", () => {
    localStorage.setItem(COMPOSER_SEND_SHORTCUT_STORAGE_KEY, "true");
    expect(collectSettings()?.settings[COMPOSER_SEND_SHORTCUT_STORAGE_KEY]).toBe("true");

    applyImportedSettings({ version: 1, settings: {} });
    expect(localStorage.getItem(COMPOSER_SEND_SHORTCUT_STORAGE_KEY)).toBeNull();

    applyImportedSettings({
      version: 1,
      settings: { [COMPOSER_SEND_SHORTCUT_STORAGE_KEY]: "true" },
    });
    expect(localStorage.getItem(COMPOSER_SEND_SHORTCUT_STORAGE_KEY)).toBe("true");
  });
});

describe("custom control portability", () => {
  it("exports and clears keyboard and mobile assistant preferences", () => {
    localStorage.setItem(KEYBOARD_SHORTCUTS_STORAGE_KEY, "keyboard");
    localStorage.setItem(MOBILE_ASSISTANT_STORAGE_KEY, "mobile");
    localStorage.setItem(SESSION_NAVIGATION_STORAGE_KEY, "navigation");
    localStorage.setItem(CONTEXT_INDICATOR_STORAGE_KEY, "compact");

    expect(collectSettings()?.settings).toMatchObject({
      [KEYBOARD_SHORTCUTS_STORAGE_KEY]: "keyboard",
      [MOBILE_ASSISTANT_STORAGE_KEY]: "mobile",
      [SESSION_NAVIGATION_STORAGE_KEY]: "navigation",
      [CONTEXT_INDICATOR_STORAGE_KEY]: "compact",
    });

    applyImportedSettings({ version: 1, settings: {} });
    expect(localStorage.getItem(KEYBOARD_SHORTCUTS_STORAGE_KEY)).toBeNull();
    expect(localStorage.getItem(MOBILE_ASSISTANT_STORAGE_KEY)).toBeNull();
    expect(localStorage.getItem(SESSION_NAVIGATION_STORAGE_KEY)).toBeNull();
    expect(localStorage.getItem(CONTEXT_INDICATOR_STORAGE_KEY)).toBeNull();
  });

  it("notifies same-tab session navigation consumers after import", () => {
    const changed = vi.fn();
    window.addEventListener(SESSION_NAVIGATION_CHANGED_EVENT, changed);
    try {
      applyImportedSettings({
        version: 1,
        settings: { [SESSION_NAVIGATION_STORAGE_KEY]: "navigation" },
      });
      expect(changed).toHaveBeenCalledTimes(1);
    } finally {
      window.removeEventListener(SESSION_NAVIGATION_CHANGED_EVENT, changed);
    }
  });
});
