import { describe, expect, it } from "vitest";

import { unescapeFromDisplay } from "./keySequence";
import { recordTerminalKey } from "./terminalKeyRecording";
import {
  terminalHardwareKeyAction,
  type TerminalKeyboardPreferences,
} from "./terminalKeyboardPreferences";
import { encodeChord, encodeSend } from "./terminalKeys";
import { EMPTY_STICKY_MODIFIERS } from "./terminalKeyBar";

const altAsControl: TerminalKeyboardPreferences = {
  v: 1,
  modifierMap: { macos: { ctrl: "ctrl", alt: "ctrl", meta: "cmd" } },
};

function keyEvent(key: string, code: string, init?: KeyboardEventInit) {
  return new KeyboardEvent("keydown", { ...init, key, code });
}

describe("recordTerminalKey", () => {
  it.each([
    ["[", "BracketLeft", "\\e", "\x1b"],
    ["Backspace", "Backspace", "^W", "\x17"],
    ["b", "KeyB", "^B", "\x02"],
  ])(
    "replays the hardware bytes for Alt+%s when Alt is mapped to Ctrl",
    (key, code, send, bytes) => {
      const event = keyEvent(key, code, { altKey: true });
      expect(terminalHardwareKeyAction(event, altAsControl, "macos")).toEqual({ bytes });
      expect(recordTerminalKey(event, altAsControl, "macos")).toMatchObject({
        action: { type: "send", send },
      });
      expect(encodeSend(send, false, EMPTY_STICKY_MODIFIERS, false)).toEqual([bytes]);
    },
  );

  it.each([
    ["b", "KeyB"],
    ["B", "KeyB"],
    ["z", "KeyY"],
    ["😀", "KeyA"],
    ["^", "Digit6"],
    ["\\", "Backslash"],
  ])("replays the literal %s typed without modifiers", (key, code) => {
    const event = keyEvent(key, code);
    expect(terminalHardwareKeyAction(event, altAsControl, "macos")).toBeNull();
    const recording = recordTerminalKey(event, altAsControl, "macos");
    expect(recording).toMatchObject({ action: { type: "send" } });
    if (recording && "action" in recording && recording.action.type === "send") {
      expect(unescapeFromDisplay(recording.action.send)).toBe(key);
    }
  });

  it("keeps a logical Cmd chord for replay, including Alt-as-Cmd on Windows", () => {
    for (const [platform, init] of [
      ["macos", { metaKey: true }],
      ["windows", { altKey: true }],
    ] as const) {
      expect(
        recordTerminalKey(keyEvent("ArrowLeft", "ArrowLeft", init), { v: 1 }, platform),
      ).toMatchObject({
        action: { type: "combo", chord: { code: "ArrowLeft", modifiers: ["meta"] } },
      });
    }
  });

  it.each([
    ["v", "Period", { clipboard: "paste" }],
    ["C", "KeyJ", { clipboard: "copy" }],
    ["k", "KeyV", { bytes: "\x1bk" }],
    ["é", "KeyC", { bytes: "\x1bé" }],
  ])("replays logical Cmd+%s when the physical code is %s", (key, code, output) => {
    const recording = recordTerminalKey(keyEvent(key, code, { metaKey: true }), { v: 1 }, "macos");
    if (!recording || !("action" in recording) || recording.action.type !== "combo") {
      throw new Error("Expected a recorded Command chord");
    }
    expect(recording.action.chord).toEqual({ code: key, modifiers: ["meta"] });
    expect(encodeChord(recording.action.chord, EMPTY_STICKY_MODIFIERS, false)).toEqual(output);
  });

  it("uses the logical key and modifiers produced by a remap", () => {
    const preferences = {
      v: 1,
      modifierMap: { macos: { ctrl: "cmd", alt: "alt", meta: "ctrl" } },
    } satisfies TerminalKeyboardPreferences;
    const recording = recordTerminalKey(
      keyEvent("V", "Period", { ctrlKey: true, shiftKey: true }),
      preferences,
      "macos",
    );
    if (!recording || !("action" in recording) || recording.action.type !== "combo") {
      throw new Error("Expected a recorded Command chord");
    }
    expect(recording.action.chord).toEqual({ code: "V", modifiers: ["meta", "shift"] });
    expect(encodeChord(recording.action.chord, EMPTY_STICKY_MODIFIERS, false)).toEqual({
      clipboard: "paste",
    });
  });

  it("ignores repeats, modifiers and composition, and rejects AltGr and unsupported keys", () => {
    const preferences = { v: 1 } as const;
    for (const event of [
      keyEvent("b", "KeyB", { repeat: true }),
      keyEvent("Shift", "ShiftLeft", { shiftKey: true }),
      keyEvent("Process", "KeyB", { isComposing: true }),
    ])
      expect(recordTerminalKey(event, preferences, "windows")).toBeNull();
    expect(
      recordTerminalKey(
        keyEvent("q", "KeyQ", { altKey: true, ctrlKey: true }),
        preferences,
        "windows",
      ),
    ).toEqual({ refused: "altgr" });
    for (const key of ["MediaPlayPause", "Dead", "Unidentified"]) {
      expect(recordTerminalKey(keyEvent(key, key), preferences, "macos")).toEqual({
        refused: "no-sequence",
      });
    }
  });
});
