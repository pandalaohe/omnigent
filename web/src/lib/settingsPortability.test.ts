import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { COMPOSER_SEND_SHORTCUT_STORAGE_KEY } from "./composerSendShortcutPreferences";
import { CONTEXT_INDICATOR_STORAGE_KEY } from "./contextIndicatorPreferences";
import { AGENT_BADGE_STORAGE_KEY } from "./agentBadgePreferences";
import { KEYBOARD_SHORTCUTS_STORAGE_KEY } from "./keyboardShortcutPreferences";
import { MOBILE_ASSISTANT_STORAGE_KEY } from "./mobileAssistantPreferences";
import {
  SESSION_NAVIGATION_CHANGED_EVENT,
  SESSION_NAVIGATION_STORAGE_KEY,
} from "./sessionNavigationPreferences";
import { applyImportedSettings, collectSettings } from "./settingsPortability";
import {
  readAlwaysUseWorktree,
  writeAlwaysUseWorktree,
  WORKTREE_DEFAULTS_STORAGE_KEY,
} from "./worktreeDefaultPreferences";
import {
  initializeUserPreferencesSync,
  resetUserPreferencesSyncForTests,
} from "./userPreferencesSync";

beforeEach(() => {
  localStorage.clear();
  resetUserPreferencesSyncForTests();
});
afterEach(() => {
  resetUserPreferencesSyncForTests();
  vi.useRealTimers();
});

describe("worktree default portability", () => {
  it("exports and restores the account preference and clears it with a baseline", () => {
    writeAlwaysUseWorktree(true);
    const exported = collectSettings()!;
    expect(exported.settings[WORKTREE_DEFAULTS_STORAGE_KEY]).toBe(
      JSON.stringify({ alwaysUseWorktree: true }),
    );
    applyImportedSettings({ version: 1, settings: {} });
    expect(readAlwaysUseWorktree()).toBe(false);
    applyImportedSettings(exported);
    expect(readAlwaysUseWorktree()).toBe(true);
  });

  it("translates old backups before patching the server", async () => {
    vi.useFakeTimers();
    const fetcher = vi.fn().mockResolvedValue(new Response(null, { status: 200 }));
    await initializeUserPreferencesSync(
      { version: 1, settings: { worktree_defaults: { alwaysUseWorktree: false } } },
      fetcher,
    );
    applyImportedSettings({ version: 1, settings: { "omnigent:always-use-worktree": "true" } });
    expect(readAlwaysUseWorktree()).toBe(true);
    expect(localStorage.getItem("omnigent:always-use-worktree")).toBeNull();
    await vi.advanceTimersByTimeAsync(1000);
    expect(fetcher).toHaveBeenCalledWith(
      "/v1/me/preferences/worktree_defaults",
      expect.objectContaining({ body: JSON.stringify({ value: { alwaysUseWorktree: true } }) }),
    );
  });

  it("prefers the current value in a backup carrying both keys", () => {
    applyImportedSettings({
      version: 1,
      settings: {
        "omnigent:always-use-worktree": "true",
        [WORKTREE_DEFAULTS_STORAGE_KEY]: JSON.stringify({ alwaysUseWorktree: false }),
      },
    });
    expect(readAlwaysUseWorktree()).toBe(false);
    expect(localStorage.getItem("omnigent:always-use-worktree")).toBeNull();
  });
});

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
    localStorage.setItem(AGENT_BADGE_STORAGE_KEY, "badges");

    expect(collectSettings()?.settings).toMatchObject({
      [KEYBOARD_SHORTCUTS_STORAGE_KEY]: "keyboard",
      [MOBILE_ASSISTANT_STORAGE_KEY]: "mobile",
      [SESSION_NAVIGATION_STORAGE_KEY]: "navigation",
      [CONTEXT_INDICATOR_STORAGE_KEY]: "compact",
      [AGENT_BADGE_STORAGE_KEY]: "badges",
    });

    applyImportedSettings({ version: 1, settings: {} });
    expect(localStorage.getItem(KEYBOARD_SHORTCUTS_STORAGE_KEY)).toBeNull();
    expect(localStorage.getItem(MOBILE_ASSISTANT_STORAGE_KEY)).toBeNull();
    expect(localStorage.getItem(SESSION_NAVIGATION_STORAGE_KEY)).toBeNull();
    expect(localStorage.getItem(CONTEXT_INDICATOR_STORAGE_KEY)).toBeNull();
    expect(localStorage.getItem(AGENT_BADGE_STORAGE_KEY)).toBeNull();
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
