import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  DEFAULT_SHORTCUT_DEFINITIONS,
  KEYBOARD_SHORTCUTS_STORAGE_KEY,
  SHORTCUT_ACTION_IDS,
  deleteShortcutPlatformOverride,
  eventMatchesShortcut,
  eventMatchesShortcutAction,
  findShortcutConflicts,
  defaultShortcutBindings,
  macWindowsKeyPositionNotes,
  readKeyboardShortcutPreferences,
  readMacWindowsKeyPositions,
  resetShortcutPreference,
  resolveShortcutBindings,
  resolvedShortcutChords,
  shortcutAriaKeys,
  shortcutBindingLabels,
  shortcutChordFromEvent,
  writeMacWindowsKeyPositions,
  writeShortcutPreference,
  type ShortcutActionId,
  type ShortcutChord,
  type ShortcutPlatform,
} from "./keyboardShortcutPreferences";
import { queueUserPreferencePatch } from "./userPreferencesSync";

vi.mock("./userPreferencesSync", () => ({ queueUserPreferencePatch: vi.fn() }));

const ctrlN: ShortcutChord = {
  code: "KeyN",
  modifiers: ["control"],
};

const altN: ShortcutChord = {
  code: "KeyN",
  modifiers: ["alt"],
};

const ALL_PLATFORMS: ShortcutPlatform[] = ["macos", "windows", "linux"];

describe("keyboardShortcutPreferences", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.mocked(queueUserPreferencePatch).mockReset();
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
      { code: "Enter", modifiers: ["alt"] },
    ]);
    expect(defaultShortcutBindings("pinnedSession", { nativeShell: true })).toEqual([
      { code: "Digit*", modifiers: ["primary"] },
    ]);
  });

  it("defaults the composer newline to Shift+Enter and Alt+Enter", () => {
    expect(DEFAULT_SHORTCUT_DEFINITIONS.newLine.defaultBindings).toEqual([
      { code: "Enter", modifiers: ["shift"] },
      { code: "Enter", modifiers: ["alt"] },
    ]);
    expect(resolveShortcutBindings("newLine", "windows")).toEqual([
      { code: "Enter", modifiers: ["shift"] },
      { code: "Enter", modifiers: ["alt"] },
    ]);
  });

  it("flags the default Alt+Enter newline as a conflict on other composer actions", () => {
    expect(
      findShortcutConflicts("sendMessage", [{ code: "Enter", modifiers: ["alt"] }], "windows"),
    ).toContain("newLine");
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
      ["toggleAnnotationMode", [{ code: "Period", modifiers: ["primary", "shift"] }]],
    ];

    for (const [actionId, bindings] of expected) {
      expect(resolveShortcutBindings(actionId, "macos")).toEqual(bindings);
    }
  });

  it("resolves action chords to concrete platform modifier flags", () => {
    expect(resolvedShortcutChords("toggleAnnotationMode", "macos")).toEqual([
      { code: "Period", ctrl: false, meta: true, alt: false, shift: true },
    ]);
    expect(resolvedShortcutChords("toggleAnnotationMode", "windows")).toEqual([
      { code: "Period", ctrl: true, meta: false, alt: false, shift: true },
    ]);

    writeShortcutPreference("toggleAnnotationMode", { enabled: false });
    expect(resolvedShortcutChords("toggleAnnotationMode", "windows")).toEqual([]);
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

  describe("Windows key positions on macOS", () => {
    it("leaves every default unchanged while the switch is off", () => {
      for (const actionId of SHORTCUT_ACTION_IDS) {
        for (const platform of ALL_PLATFORMS) {
          expect(resolveShortcutBindings(actionId, platform)).toEqual(
            DEFAULT_SHORTCUT_DEFINITIONS[actionId].defaultBindings,
          );
        }
      }
      expect(resolveShortcutBindings("sendMessage", "macos", { submitWithModEnter: true })).toEqual(
        [{ code: "Enter", modifiers: ["primary"] }],
      );
      expect(resolveShortcutBindings("pinnedSession", "macos", { nativeShell: true })).toEqual([
        { code: "Digit*", modifiers: ["primary"] },
      ]);
    });

    it("maps macOS defaults onto Windows key positions when enabled", () => {
      writeMacWindowsKeyPositions(true);

      expect(resolveShortcutBindings("pollSessions", "macos")).toEqual([
        { code: "Backquote", modifiers: ["meta"] },
      ]);
      expect(resolveShortcutBindings("archiveSession", "macos")).toEqual([
        { code: "KeyW", modifiers: ["alt"] },
      ]);
      expect(resolveShortcutBindings("newLine", "macos")).toEqual([
        { code: "Enter", modifiers: ["shift"] },
        { code: "Enter", modifiers: ["alt"] },
      ]);
      expect(resolveShortcutBindings("newLine", "macos", { submitWithModEnter: true })).toEqual([
        { code: "Enter", modifiers: [] },
        { code: "Enter", modifiers: ["alt"] },
      ]);
      expect(resolveShortcutBindings("newSession", "macos")).toEqual([
        { code: "KeyN", modifiers: ["control", "meta"] },
      ]);
      expect(resolveShortcutBindings("pinnedSession", "macos")).toEqual([
        { code: "Digit*", modifiers: ["control", "meta"] },
      ]);
      expect(resolveShortcutBindings("commandPalette", "macos")).toEqual([
        { code: "KeyK", modifiers: ["control"] },
      ]);
      expect(resolveShortcutBindings("approvePrompt", "macos")).toEqual([
        { code: "Enter", modifiers: ["control"] },
      ]);
      // Pure-Control defaults do not change on macOS.
      expect(resolveShortcutBindings("openModelPicker", "macos")).toEqual([
        { code: "KeyM", modifiers: ["control", "shift"] },
      ]);
    });

    it("labels the mapped Poll and new-session defaults", () => {
      writeMacWindowsKeyPositions(true);

      expect(
        shortcutBindingLabels(resolveShortcutBindings("pollSessions", "macos")[0], "macos"),
      ).toEqual(["⌘", "~"]);
      expect(
        shortcutBindingLabels(resolveShortcutBindings("newSession", "macos")[0], "macos"),
      ).toEqual(["⌃", "⌘", "N"]);
    });

    it("leaves Windows and Linux defaults unchanged while enabled", () => {
      writeMacWindowsKeyPositions(true);

      for (const actionId of SHORTCUT_ACTION_IDS) {
        for (const platform of ["windows", "linux"] as const) {
          expect(resolveShortcutBindings(actionId, platform)).toEqual(
            DEFAULT_SHORTCUT_DEFINITIONS[actionId].defaultBindings,
          );
        }
      }
    });

    it("returns recorded bindings unmapped", () => {
      writeMacWindowsKeyPositions(true);
      writeShortcutPreference("pollSessions", {
        common: [{ code: "Backquote", modifiers: ["control"] }],
      });
      writeShortcutPreference("archiveSession", {
        common: [ctrlN],
        platformOverrides: { macos: [{ code: "KeyW", modifiers: ["primary"] }] },
      });

      expect(resolveShortcutBindings("pollSessions", "macos")).toEqual([
        { code: "Backquote", modifiers: ["control"] },
      ]);
      expect(resolveShortcutBindings("archiveSession", "macos")).toEqual([
        { code: "KeyW", modifiers: ["primary"] },
      ]);
    });

    it("keeps the macOS conflict set identical to the switch-off set", () => {
      const conflictsWhileOff = new Map(
        SHORTCUT_ACTION_IDS.map((actionId) => [
          actionId,
          findShortcutConflicts(actionId, resolveShortcutBindings(actionId, "macos"), "macos"),
        ]),
      );
      writeMacWindowsKeyPositions(true);

      for (const actionId of SHORTCUT_ACTION_IDS) {
        expect(
          findShortcutConflicts(actionId, resolveShortcutBindings(actionId, "macos"), "macos"),
        ).toEqual(conflictsWhileOff.get(actionId));
      }
    });

    it("matches ⌘` for Poll only while the switch is on", () => {
      const commandBackquote = new KeyboardEvent("keydown", {
        code: "Backquote",
        key: "`",
        metaKey: true,
      });
      const optionBackquote = new KeyboardEvent("keydown", {
        code: "Backquote",
        key: "`",
        altKey: true,
      });

      expect(eventMatchesShortcutAction(commandBackquote, "pollSessions", "macos")).toBe(false);
      expect(eventMatchesShortcutAction(optionBackquote, "pollSessions", "macos")).toBe(true);

      writeMacWindowsKeyPositions(true);

      expect(eventMatchesShortcutAction(commandBackquote, "pollSessions", "macos")).toBe(true);
      expect(eventMatchesShortcutAction(optionBackquote, "pollSessions", "macos")).toBe(false);
    });

    it("persists the switch through the keyboard-shortcuts namespace", () => {
      writeMacWindowsKeyPositions(true);

      expect(readMacWindowsKeyPositions()).toBe(true);
      expect(JSON.parse(localStorage.getItem(KEYBOARD_SHORTCUTS_STORAGE_KEY) ?? "null")).toEqual({
        version: 1,
        actions: {},
        macWindowsKeyPositions: true,
      });
      expect(queueUserPreferencePatch).toHaveBeenLastCalledWith("keyboard_shortcuts", {
        version: 1,
        actions: {},
        macWindowsKeyPositions: true,
      });
    });

    it("removes the stored record when the switch is turned off with no actions", () => {
      writeMacWindowsKeyPositions(true);
      writeMacWindowsKeyPositions(false);

      expect(readMacWindowsKeyPositions()).toBe(false);
      expect(localStorage.getItem(KEYBOARD_SHORTCUTS_STORAGE_KEY)).toBeNull();
      expect(queueUserPreferencePatch).toHaveBeenLastCalledWith("keyboard_shortcuts", null);
    });

    it("keeps the switch through preference edits and override deletion", () => {
      writeMacWindowsKeyPositions(true);
      writeShortcutPreference("newSession", { common: [altN] });
      writeShortcutPreference("archiveSession", {
        common: [ctrlN],
        platformOverrides: { macos: [{ code: "KeyW", modifiers: ["primary"] }] },
      });

      resetShortcutPreference("newSession");
      deleteShortcutPlatformOverride("archiveSession", "macos");

      expect(readMacWindowsKeyPositions()).toBe(true);
      expect(readKeyboardShortcutPreferences().actions.archiveSession).toEqual({
        common: [ctrlN],
        platformOverrides: {},
      });
    });

    it("treats a stored non-boolean flag as off", () => {
      localStorage.setItem(
        KEYBOARD_SHORTCUTS_STORAGE_KEY,
        JSON.stringify({ version: 1, actions: {}, macWindowsKeyPositions: "true" }),
      );

      expect(readMacWindowsKeyPositions()).toBe(false);
      expect(readKeyboardShortcutPreferences()).toEqual({ version: 1, actions: {} });
    });

    it("explains the taken keys while enabled on macOS", () => {
      writeMacWindowsKeyPositions(true);

      expect(macWindowsKeyPositionNotes("pollSessions", {}, "macos")).toEqual([
        "macOS uses ⌘` to move focus to the next window. To use it here, turn off System Settings → Keyboard → Keyboard Shortcuts → Keyboard → “Move focus to next window”.",
      ]);
      expect(macWindowsKeyPositionNotes("archiveSession", {}, "macos")).toEqual([
        "Keeps ⌥W: ⌘W closes the window or tab.",
      ]);
      expect(macWindowsKeyPositionNotes("newLine", {}, "macos")).toEqual([
        "Keeps ⌥↵: ⌘↵ is the composer's send-all key.",
      ]);
    });

    it("shows no notes off macOS or while the switch is off", () => {
      writeMacWindowsKeyPositions(true);
      expect(macWindowsKeyPositionNotes("pollSessions", {}, "windows")).toEqual([]);
      expect(macWindowsKeyPositionNotes("pollSessions", {}, "linux")).toEqual([]);

      writeMacWindowsKeyPositions(false);
      expect(macWindowsKeyPositionNotes("pollSessions", {}, "macos")).toEqual([]);
    });
  });
});
