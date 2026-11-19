import { describe, expect, it, vi } from "vitest";
import { logicalKeyEvent, TERMINAL_MODIFIER_PRESETS } from "./keyboardShortcutPreferences";
import {
  terminalHardwareKeyAction,
  type TerminalKeyboardPreferences,
} from "./terminalKeyboardPreferences";

function down(init: KeyboardEventInit): KeyboardEvent {
  return new KeyboardEvent("keydown", init);
}

describe("terminal hardware keyboard", () => {
  it("derives Ctrl-letter, control punctuation and named-key bytes from a custom Linux map", () => {
    const preferences = {
      v: 1,
      modifierMap: { linux: { ctrl: "ctrl", alt: "ctrl", meta: "cmd" } },
    } satisfies TerminalKeyboardPreferences;
    const action = (key: string, code: string) =>
      terminalHardwareKeyAction(down({ key, code, altKey: true }), preferences, "linux");
    expect(action("b", "KeyB")).toEqual({ bytes: "\x02" });
    expect(action("[", "BracketLeft")).toEqual({ bytes: "\x1b" });
    expect(action("ArrowLeft", "ArrowLeft")).toEqual({ bytes: "\x1b[1;5D" });
  });

  it("swaps Mac Cmd and Option across line keys, letters, digits and punctuation", () => {
    const preferences = {
      v: 1,
      modifierMap: { macos: TERMINAL_MODIFIER_PRESETS.swapAltCmd },
    } satisfies TerminalKeyboardPreferences;
    const action = (init: KeyboardEventInit) =>
      terminalHardwareKeyAction(down(init), preferences, "macos");
    expect(action({ key: "b", code: "KeyB", metaKey: true })).toEqual({ bytes: "\x1bb" });
    expect(action({ key: "ArrowLeft", code: "ArrowLeft", altKey: true })).toEqual({
      bytes: "\x01",
    });
    expect(action({ key: "∫", code: "KeyB", altKey: true })).toEqual({ bytes: "" });
    expect(action({ key: "3", code: "Digit3", metaKey: true })).toEqual({ bytes: "\x1b3" });
    expect(action({ key: "?", code: "Slash", shiftKey: true, metaKey: true })).toEqual({
      bytes: "\x1b?",
    });
    expect(action({ key: "B", code: "KeyB", shiftKey: true })).toBeNull();
  });

  it("routes Windows Alt shortcuts to Command or back to xterm and leaves AltGr alone", () => {
    const action = (init: KeyboardEventInit, hasSelection = true) =>
      terminalHardwareKeyAction(down({ altKey: true, ...init }), { v: 1 }, "windows", {
        hasSelection: () => hasSelection,
      });
    expect(action({ key: "ArrowLeft", code: "ArrowLeft" })).toEqual({ bytes: "\x01" });
    expect(action({ key: "b", code: "KeyB" })).toBeNull();
    expect(action({ key: "v", code: "KeyV" })).toEqual({ clipboard: "paste" });
    expect(action({ key: "c", code: "KeyC" })).toEqual({ clipboard: "copy" });
    expect(action({ key: "c", code: "KeyC" }, false)).toBeNull();
    expect(action({ key: "@", code: "KeyQ", ctrlKey: true })).toBeNull();
    expect(action({ key: "Process", code: "KeyV" })).toBeNull();
  });

  it("ignores IME and AltGraph before normalizing an Option-generated character", () => {
    const map = TERMINAL_MODIFIER_PRESETS.swapAltCmd;
    expect(
      logicalKeyEvent(down({ key: "Process", keyCode: 229, altKey: true }), map, "macos"),
    ).toEqual({ passthrough: "ime" });
    const altGraph = down({ key: "@", code: "KeyQ", altKey: true });
    vi.spyOn(altGraph, "getModifierState").mockImplementation((key) => key === "AltGraph");
    expect(logicalKeyEvent(altGraph, map, "linux")).toEqual({ passthrough: "altgr" });
    expect(
      logicalKeyEvent(
        down({ key: "”", code: "BracketLeft", altKey: true, shiftKey: true }),
        map,
        "macos",
      ),
    ).toEqual({
      key: "{",
      code: "BracketLeft",
      ctrl: false,
      alt: false,
      cmd: true,
      shift: true,
      remapped: true,
    });
  });

  it("matches a rebound line action only on its configured platform and gives it precedence over paste", () => {
    const preferences = {
      v: 1,
      bindings: {
        lineStart: {
          platformOverrides: {
            windows: [{ code: "KeyA", modifiers: ["control", "shift"] }],
            macos: [{ code: "KeyV", modifiers: ["meta"] }],
          },
        },
      },
    } satisfies TerminalKeyboardPreferences;
    const rebound = down({ key: "A", code: "KeyA", ctrlKey: true, shiftKey: true });
    expect(terminalHardwareKeyAction(rebound, preferences, "windows")).toEqual({ bytes: "\x01" });
    expect(terminalHardwareKeyAction(rebound, preferences, "linux")).toBeNull();
    expect(
      terminalHardwareKeyAction(down({ key: "ArrowLeft", metaKey: true }), preferences, "windows"),
    ).toBeNull();
    expect(
      terminalHardwareKeyAction(down({ key: "v", metaKey: true }), preferences, "macos", {
        hasSelection: () => false,
      }),
    ).toEqual({ bytes: "\x01" });
  });
});
