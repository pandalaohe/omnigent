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
  shortcutAriaKeys,
  shortcutBindingLabels,
  shortcutChordFromEvent,
  writeShortcutPreference,
  type ShortcutActionId,
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
    const commandN = new KeyboardEvent("keydown", {
      code: "KeyN",
      key: "n",
      metaKey: true,
      altKey: true,
    });
    const controlN = new KeyboardEvent("keydown", {
      code: "KeyN",
      key: "n",
      ctrlKey: true,
      altKey: true,
    });

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

  it("registers the upstream hotkey defaults", () => {
    const expected: [ShortcutActionId, ShortcutChord[]][] = [
      ["openSettings", [{ code: "Comma", modifiers: ["primary", "alt"] }]],
      ["findSession", [{ code: "KeyS", modifiers: ["primary", "alt"] }]],
      ["openModelPicker", [{ code: "KeyM", modifiers: ["control", "shift"] }]],
      ["focusComposer", [{ code: "KeyL", modifiers: ["control", "shift"] }]],
      ["recentSessions", [{ code: "Tab", modifiers: ["control"] }]],
      ["previousSession", [{ code: "BracketLeft", modifiers: ["primary"] }]],
      ["nextSession", [{ code: "BracketRight", modifiers: ["primary"] }]],
      ["toggleViewMode", [{ code: "Backslash", modifiers: ["primary", "alt"] }]],
      ["selectWorkspaceTab1", []],
      ["selectWorkspaceTab2", []],
      ["selectWorkspaceTab3", []],
      ["selectWorkspaceTab4", []],
      ["newBrowserTab", [{ code: "KeyB", modifiers: ["primary", "alt"] }]],
      ["newShell", [{ code: "KeyT", modifiers: ["primary", "alt"] }]],
    ];

    for (const [actionId, bindings] of expected) {
      expect(resolveShortcutBindings(actionId, "macos")).toEqual(bindings);
    }
  });

  it("registers the question-card action defaults", () => {
    const expected: [ShortcutActionId, ShortcutChord[]][] = [
      ["focusQuestionCard", [{ code: "KeyF", modifiers: ["control", "shift"] }]],
      ["questionCardPreviousOption", [{ code: "ArrowUp", modifiers: [] }]],
      ["questionCardNextOption", [{ code: "ArrowDown", modifiers: [] }]],
      ["questionCardSelectOption", [{ code: "Space", modifiers: [] }]],
      ["questionCardNextOrSubmit", [{ code: "Enter", modifiers: ["primary"] }]],
      ["questionCardPreviousQuestion", [{ code: "ArrowLeft", modifiers: [] }]],
      ["questionCardNextQuestion", [{ code: "ArrowRight", modifiers: [] }]],
      ["questionCardLeave", [{ code: "Escape", modifiers: [] }]],
      ["questionCardCancel", []],
      ["questionCardCancelAndInterrupt", []],
    ];

    for (const [actionId, bindings] of expected) {
      expect(DEFAULT_SHORTCUT_DEFINITIONS[actionId].group).toBe("questionCard");
      expect(DEFAULT_SHORTCUT_DEFINITIONS[actionId].scope).toBe(
        actionId === "focusQuestionCard" ? "global" : "questionCard",
      );
      expect(resolveShortcutBindings(actionId, "macos")).toEqual(bindings);
      expect(resolveShortcutBindings(actionId, "windows")).toEqual(bindings);
    }
  });

  it("matches a key-only Space event against the Space chord", () => {
    // Some synthetic and IME paths deliver key without code; the card's
    // "select" binding must still match.
    const event = new KeyboardEvent("keydown", { key: " " });
    expect(eventMatchesShortcut(event, { code: "Space", modifiers: [] })).toBe(true);
  });

  it("lets card keys coexist with composer, suggestions and approvePrompt", () => {
    // Card actions fire only with focus inside the card; the composer's recall
    // keys and the approval verdict can bind the same keys without stealing
    // each other's events.
    expect(
      findShortcutConflicts("questionCardPreviousOption", [{ code: "ArrowUp", modifiers: [] }]),
    ).toEqual([]);
    expect(
      findShortcutConflicts("questionCardNextOrSubmit", [
        { code: "Enter", modifiers: ["primary"] },
      ]),
    ).not.toContain("approvePrompt");
  });

  it("still rejects card collisions with globals and other card actions", () => {
    expect(
      findShortcutConflicts("questionCardSelectOption", [
        { code: "KeyN", modifiers: ["primary", "alt"] },
      ]),
    ).toContain("newSession");
    // Recording ⌘K for a card action collides with the command palette.
    expect(
      findShortcutConflicts("questionCardNextOrSubmit", [{ code: "KeyK", modifiers: ["primary"] }]),
    ).toContain("commandPalette");
    expect(
      findShortcutConflicts("questionCardNextOption", [{ code: "ArrowUp", modifiers: [] }]),
    ).toContain("questionCardPreviousOption");
  });

  it("keeps an action with an empty default safe to resolve and conflict-check", () => {
    expect(resolveShortcutBindings("selectWorkspaceTab1", "macos")).toEqual([]);
    expect(findShortcutConflicts("selectWorkspaceTab1", [], "macos")).toEqual([]);
  });

  it("labels the macOS control modifier and punctuation keys", () => {
    expect(
      shortcutBindingLabels({ code: "KeyL", modifiers: ["control", "shift"] }, "macos"),
    ).toEqual(["⌃", "⇧", "L"]);
    expect(
      shortcutBindingLabels({ code: "KeyL", modifiers: ["control", "shift"] }, "windows"),
    ).toEqual(["Ctrl", "⇧", "L"]);
    expect(shortcutBindingLabels({ code: "Backslash", modifiers: [] }, "macos")).toEqual(["\\"]);
    expect(shortcutBindingLabels({ code: "Comma", modifiers: [] }, "macos")).toEqual([","]);
  });

  it("builds aria-keyshortcuts strings from the resolved modifier flags", () => {
    const binding: ShortcutChord = { code: "KeyN", modifiers: ["primary", "alt"] };
    expect(shortcutAriaKeys(binding, "macos")).toEqual("Meta+Alt+N");
    expect(shortcutAriaKeys(binding, "windows")).toEqual("Control+Alt+N");
    expect(shortcutAriaKeys({ code: "Comma", modifiers: ["primary", "alt"] }, "macos")).toEqual(
      "Meta+Alt+,",
    );
  });

  it("names punctuation and space keys in aria-keyshortcuts", () => {
    expect(shortcutAriaKeys({ code: "Semicolon", modifiers: ["primary"] }, "windows")).toEqual(
      "Control+;",
    );
    expect(shortcutAriaKeys({ code: "Quote", modifiers: ["primary"] }, "windows")).toEqual(
      "Control+'",
    );
    expect(shortcutAriaKeys({ code: "Minus", modifiers: ["primary"] }, "windows")).toEqual(
      "Control+-",
    );
    expect(shortcutAriaKeys({ code: "Equal", modifiers: ["primary"] }, "windows")).toEqual(
      "Control+=",
    );
    expect(shortcutAriaKeys({ code: "Space", modifiers: [] }, "windows")).toEqual("Space");
  });
});
