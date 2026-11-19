import {
  logicalKeyEvent,
  shortcutChordFromEvent,
  shortcutEventFromLogicalKey,
  type ShortcutPlatform,
} from "./keyboardShortcutPreferences";
import { escapeLiteral, keyEventToLabel } from "./keySequence";
import { applyCmd, CMD_KEY_MODE, type KeyAction } from "./terminalKeys";
import {
  defaultTerminalModifierMap,
  encodeLogicalTerminalKey,
  terminalBindingBytes,
  type TerminalKeyboardPreferences,
} from "./terminalKeyboardPreferences";

type RecordingResult =
  | { action: Extract<KeyAction, { type: "send" | "combo" }>; label: string }
  | { refused: "altgr" | "no-sequence" }
  | null;

export function recordTerminalKey(
  event: KeyboardEvent,
  preferences: TerminalKeyboardPreferences,
  platform: ShortcutPlatform,
): RecordingResult {
  if (event.repeat || ["Shift", "Control", "Alt", "Meta"].includes(event.key)) return null;
  const map = preferences.modifierMap?.[platform] ?? defaultTerminalModifierMap(platform);
  const logical = logicalKeyEvent(event, map, platform);
  if ("passthrough" in logical) {
    return logical.passthrough === "ime" ? null : { refused: "altgr" };
  }
  if (["Dead", "Unidentified", ""].includes(logical.key)) return { refused: "no-sequence" };

  const input = shortcutEventFromLogicalKey(logical);
  const label = keyEventToLabel(input);
  if (logical.cmd) {
    // The terminal needs the character produced by the layout, even for C/V.
    const chord = shortcutChordFromEvent({ ...input, code: logical.key });
    return !chord || "refused" in applyCmd({ chord }, CMD_KEY_MODE)
      ? { refused: "no-sequence" }
      : { action: { type: "combo", chord }, label };
  }
  const bytes =
    terminalBindingBytes(input, preferences, platform) ?? encodeLogicalTerminalKey(input);
  return bytes
    ? { action: { type: "send", send: escapeLiteral(bytes) }, label }
    : { refused: "no-sequence" };
}
