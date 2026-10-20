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
import {
  type SessionWorkspaceState,
  readSessionWorkspaceState,
  writeSessionWorkspaceState,
} from "./sessionWorkspaceState";
import {
  PINNED_WORKSPACE_TABS_STORAGE_KEY,
  readInitialWorkspaceTab,
  readPinnedWorkspaceTabs,
  writeDefaultWorkspaceTab,
  writePinnedWorkspaceTabs,
} from "./workspaceTabPreferences";

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

it("round-trips workspace pins and default without restoring stale selections", () => {
  writePinnedWorkspaceTabs(["changes", "subagents"]);
  writeDefaultWorkspaceTab("changes");
  const exported = collectSettings()!;
  applyImportedSettings({ version: 1, settings: {} });
  expect(readPinnedWorkspaceTabs()).toHaveLength(4);
  expect(readInitialWorkspaceTab()).toBe("files");

  writeSessionWorkspaceState("conv", { open: true, rightRailTab: "files" });
  applyImportedSettings(exported);
  expect(readPinnedWorkspaceTabs()).toEqual(["changes", "subagents"]);
  expect(readInitialWorkspaceTab()).toBe("changes");
  expect(readSessionWorkspaceState("conv")).toEqual({ open: true });
});

it("preserves unrelated session selections when importing changed pins without a new default", () => {
  writePinnedWorkspaceTabs(["files", "changes", "subagents"]);
  writeSessionWorkspaceState("files", {
    rightRailTab: "files",
    openFiles: ["README.md"],
    selectedFilePath: "README.md",
  });
  const openedPage: SessionWorkspaceState = { rightRailTab: "github", openPanels: ["github"] };
  writeSessionWorkspaceState("opened", openedPage);
  const browser: SessionWorkspaceState = {
    rightRailTab: "browser",
    selectedBrowserId: "one",
    openBrowsers: ["one"],
  };
  writeSessionWorkspaceState("browser", browser);

  applyImportedSettings({
    version: 1,
    settings: {
      [PINNED_WORKSPACE_TABS_STORAGE_KEY]: '["changes","subagents"]',
      "omnigent:default-workspace-tab": "files",
    },
  });
  expect(readSessionWorkspaceState("files")).toEqual({
    openFiles: ["README.md"],
    selectedFilePath: "README.md",
  });
  expect(readSessionWorkspaceState("opened")).toEqual(openedPage);
  expect(readSessionWorkspaceState("browser")).toEqual(browser);

  applyImportedSettings({
    version: 1,
    settings: {
      [PINNED_WORKSPACE_TABS_STORAGE_KEY]: '["changes","github","subagents"]',
      "omnigent:default-workspace-tab": "files",
    },
  });
  expect(readSessionWorkspaceState("opened")).toEqual(openedPage);
  expect(readSessionWorkspaceState("browser")).toEqual(browser);

  applyImportedSettings({
    version: 1,
    settings: {
      [PINNED_WORKSPACE_TABS_STORAGE_KEY]: '["changes","github","subagents"]',
      "omnigent:default-workspace-tab": "subagents",
    },
  });
  expect(readSessionWorkspaceState("opened")).toEqual({ openPanels: ["github"] });
  expect(readSessionWorkspaceState("browser")).toEqual({ openBrowsers: ["one"] });
});

it.each([
  ['["files","changes","subagents"]', "changes"],
  ['["changes","subagents"]', "subagents"],
] as const)("applies an explicit imported default with pins %s", (pins, defaultTab) => {
  writePinnedWorkspaceTabs(["files", "changes"]);
  writeSessionWorkspaceState("browser", {
    rightRailTab: "browser",
    openBrowsers: ["one"],
    selectedBrowserId: "one",
  });

  applyImportedSettings({
    version: 1,
    settings: {
      [PINNED_WORKSPACE_TABS_STORAGE_KEY]: pins,
      "omnigent:default-workspace-tab": defaultTab,
    },
  });

  expect(readInitialWorkspaceTab()).toBe(defaultTab);
  expect(readSessionWorkspaceState("browser")).toEqual({ openBrowsers: ["one"] });
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
