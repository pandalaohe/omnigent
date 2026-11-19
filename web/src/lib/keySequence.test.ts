import { describe, expect, it } from "vitest";
import {
  escapeForDisplay,
  escapeLiteral,
  keyEventToLabel,
  keyEventToSequence,
  unescapeFromDisplay,
} from "./keySequence";

function event(key: string, options: KeyboardEventInit = {}) {
  return new KeyboardEvent("keydown", { key, ...options });
}

describe("Dinotty key sequence codec", () => {
  it.each([
    [event("b", { code: "KeyB", altKey: true }), "\\eb", "opt+b"],
    [event("b", { code: "KeyB", ctrlKey: true }), "^B", "ctrl+b"],
    [event("ArrowUp", { ctrlKey: true, shiftKey: true }), "\\e[1;6A", "ctrl+shift+↑"],
    [event("F2", { altKey: true }), "\\e[1;3Q", "opt+F2"],
    [event("F12"), "\\e[24~", "F12"],
    [event("Backspace"), "\\x7f", "⌫"],
    [event("Enter"), "\\r", "↵"],
    [event("Tab", { shiftKey: true }), "\\e[Z", "shift+tab"],
  ])("encodes recorded keys and formats their labels", (input, display, label) => {
    expect(escapeForDisplay(keyEventToSequence(input))).toBe(display);
    expect(keyEventToLabel(input)).toBe(label);
    expect(unescapeFromDisplay(display)).toBe(keyEventToSequence(input));
  });

  it.each(["^C", String.raw`echo a\nb`, String.raw`\x03`, "\x03\r", "\\^\x1b", "你好🧪"])(
    "converts explicit legacy text reversibly",
    (text) => expect(unescapeFromDisplay(escapeLiteral(text))).toBe(text),
  );
});
