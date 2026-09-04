import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  DEFAULT_SHORTCUT_DEFINITIONS,
  KEYBOARD_SHORTCUTS_STORAGE_KEY,
  deleteShortcutPlatformOverride,
  eventMatchesShortcut,
  eventMatchesShortcutAction,
  findShortcutConflicts,
  defaultShortcutBindings,
  readKeyboardShortcutPreferences,
  resolveShortcutBindings,
  shortcutChordFromEvent,
  writeShortcutPreference,
  type ShortcutChord,
} from "./keyboardShortcutPreferences";

const ctrlN: ShortcutChord = {
  code: "KeyN",
  modifiers: ["control"],
};

const altN: ShortcutChord = {
  code: "KeyN",
  modifiers: ["alt"],
};

describe("keyboardShortcutPreferences", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("falls back to the action default when no preference exists", () => {
    expect(resolveShortcutBindings("newSession", "windows")).toEqual(
      DEFAULT_SHORTCUT_DEFINITIONS.newSession.defaultBindings,
    );
  });

  it("uses a common recording on every platform without an override", () => {
    writeShortcutPreference("newSession", { common: [ctrlN], platformOverrides: {} });

    expect(resolveShortcutBindings("newSession", "windows")).toEqual([ctrlN]);
    expect(resolveShortcutBindings("newSession", "macos")).toEqual([ctrlN]);
    expect(resolveShortcutBindings("newSession", "linux")).toEqual([ctrlN]);
  });

  it("prefers a platform recording and returns to common after deletion", () => {
    writeShortcutPreference("newSession", {
      common: [ctrlN],
      platformOverrides: { windows: [altN] },
    });

    expect(resolveShortcutBindings("newSession", "windows")).toEqual([altN]);
    deleteShortcutPlatformOverride("newSession", "windows");
    expect(resolveShortcutBindings("newSession", "windows")).toEqual([ctrlN]);
  });

  it("ignores malformed persisted records instead of breaking shortcuts", () => {
    localStorage.setItem(
      KEYBOARD_SHORTCUTS_STORAGE_KEY,
      JSON.stringify({ version: 1, actions: { newSession: { common: [{ code: 42 }] } } }),
    );

    expect(readKeyboardShortcutPreferences()).toEqual({ version: 1, actions: {} });
    expect(resolveShortcutBindings("newSession", "windows")).toEqual(
      DEFAULT_SHORTCUT_DEFINITIONS.newSession.defaultBindings,
    );
  });

  it("records physical key codes and exact modifiers", () => {
    const event = new KeyboardEvent("keydown", {
      code: "Backquote",
      key: "~",
      altKey: true,
      shiftKey: true,
    });

    expect(shortcutChordFromEvent(event)).toEqual({
      code: "Backquote",
      modifiers: ["alt", "shift"],
    });
  });

  it("matches a recorded chord without accepting extra modifiers", () => {
    const chord: ShortcutChord = { code: "KeyW", modifiers: ["alt"] };

    expect(
      eventMatchesShortcut(new KeyboardEvent("keydown", { code: "KeyW", altKey: true }), chord),
    ).toBe(true);
    expect(
      eventMatchesShortcut(
        new KeyboardEvent("keydown", { code: "KeyW", altKey: true, shiftKey: true }),
        chord,
      ),
    ).toBe(false);
  });

  it("matches default primary shortcuts only on the selected platform", () => {
    const commandN = new KeyboardEvent("keydown", { code: "KeyN", key: "n", metaKey: true });
    const controlN = new KeyboardEvent("keydown", { code: "KeyN", key: "n", ctrlKey: true });

    expect(eventMatchesShortcutAction(commandN, "newSession", "macos")).toBe(true);
    expect(eventMatchesShortcutAction(controlN, "newSession", "macos")).toBe(false);
    expect(eventMatchesShortcutAction(controlN, "newSession", "windows")).toBe(true);
    expect(eventMatchesShortcutAction(commandN, "newSession", "windows")).toBe(false);
  });

  it("reports conflicts only within the same platform-effective bindings", () => {
    writeShortcutPreference("newSession", { common: [ctrlN], platformOverrides: {} });
    writeShortcutPreference("commandPalette", {
      common: [{ code: "KeyK", modifiers: ["control"] }],
      platformOverrides: { windows: [ctrlN] },
    });

    expect(findShortcutConflicts("commandPalette", [ctrlN], "windows")).toEqual(["newSession"]);
    expect(findShortcutConflicts("commandPalette", [ctrlN], "macos")).toEqual(["newSession"]);
  });

  it("rejects cross-scope collisions that can share a real key event", () => {
    expect(
      findShortcutConflicts("applySuggestion", [{ code: "Enter", modifiers: [] }], "windows"),
    ).toContain("sendMessage");
    expect(
      findShortcutConflicts("archiveSession", [{ code: "Enter", modifiers: [] }], "windows"),
    ).toEqual(expect.arrayContaining(["sendMessage", "applySuggestion"]));
  });

  it("keeps disabled actions reserved so re-enabling cannot create a collision", () => {
    writeShortcutPreference("commandPalette", { enabled: false });

    expect(
      findShortcutConflicts("newSession", [{ code: "KeyK", modifiers: ["primary"] }]),
    ).toContain("commandPalette");
  });

  it("resolves composer and pinned defaults for the current runtime context", () => {
    expect(defaultShortcutBindings("sendMessage", { submitWithModEnter: true })).toEqual([
      { code: "Enter", modifiers: ["primary"] },
    ]);
    expect(defaultShortcutBindings("newLine", { submitWithModEnter: true })).toEqual([
      { code: "Enter", modifiers: [] },
    ]);
    expect(defaultShortcutBindings("pinnedSession", { nativeShell: true })).toEqual([
      { code: "Digit*", modifiers: ["primary"] },
    ]);
  });

  it("notifies live consumers after a preference write", async () => {
    const listener = vi.fn();
    window.addEventListener("omnigent:keyboard-shortcuts-changed", listener);

    writeShortcutPreference("newSession", { common: [altN], platformOverrides: {} });

    expect(listener).toHaveBeenCalledOnce();
    window.removeEventListener("omnigent:keyboard-shortcuts-changed", listener);
  });
});
