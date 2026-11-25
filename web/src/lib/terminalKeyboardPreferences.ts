import {
  currentShortcutPlatform,
  eventMatchesShortcut,
  logicalKeyEvent,
  normalizeActionPreference,
  shortcutChordFromEvent,
  shortcutEventFromLogicalKey,
  terminalModifierPreset,
  TERMINAL_MODIFIER_PRESETS,
  type ModifierMap,
  type ModifierRole,
  type ShortcutActionPreference,
  type ShortcutChord,
  type ShortcutPlatform,
} from "./keyboardShortcutPreferences";
import { keyEventToSequence } from "./keySequence";
import { readMobileAssistantPreferences, terminalDataForChord } from "./mobileAssistantPreferences";
import type { TerminalKeyboardChange } from "./terminalKeyboardChanges";
import { writeTerminalKeyPreferences } from "./terminalKeys";

export const TERMINAL_BINDINGS = {
  lineStart: {
    label: "Move to start of line",
    chord: { code: "ArrowLeft", modifiers: ["meta"] },
    bytes: "\x01",
  },
  lineEnd: {
    label: "Move to end of line",
    chord: { code: "ArrowRight", modifiers: ["meta"] },
    bytes: "\x05",
  },
  killLine: {
    label: "Delete to start of line",
    chord: { code: "Backspace", modifiers: ["meta"] },
    bytes: "\x15",
  },
  newline: {
    label: "Send Shift+Enter",
    chord: { code: "Enter", modifiers: ["shift"] },
    bytes: "\x1b[13;2u",
  },
} satisfies Record<string, { label: string; chord: ShortcutChord; bytes: string }>;

const DEFAULT_NEWLINE_CHORDS: ShortcutChord[] = [
  TERMINAL_BINDINGS.newline.chord,
  { code: "NumpadEnter", modifiers: ["shift"] },
];

export type TerminalBindingId = keyof typeof TERMINAL_BINDINGS;
export const TERMINAL_BINDING_IDS = Object.keys(TERMINAL_BINDINGS) as TerminalBindingId[];
const PLATFORMS = ["macos", "windows", "linux"] as const;

export type TerminalPasteBoxMode = "always" | "whenBlocked" | "never";
export const DEFAULT_TERMINAL_PASTE_BOX_MODE: TerminalPasteBoxMode = "whenBlocked";

type PlatformShortcutValues = Pick<ShortcutActionPreference, "common" | "platformOverrides">;

export interface TerminalKeyboardPreferences {
  v: 1;
  modifierMap?: Partial<Record<ShortcutPlatform, ModifierMap>>;
  pasteBoxMode?: Partial<Record<ShortcutPlatform, TerminalPasteBoxMode>>;
  bindings?: Partial<Record<TerminalBindingId, PlatformShortcutValues>>;
}

export function defaultTerminalModifierMap(platform: ShortcutPlatform): ModifierMap {
  return platform === "windows"
    ? TERMINAL_MODIFIER_PRESETS.altAsCmd
    : TERMINAL_MODIFIER_PRESETS.byName;
}

function isRole(value: unknown): value is ModifierRole {
  return value === "ctrl" || value === "alt" || value === "cmd";
}

function normalizeTerminalKeyboardPreferences(value: unknown): TerminalKeyboardPreferences {
  const preferences: TerminalKeyboardPreferences = { v: 1 };
  if (!value || typeof value !== "object" || !("v" in value) || value.v !== 1) return preferences;
  if ("modifierMap" in value && value.modifierMap && typeof value.modifierMap === "object") {
    const modifierMap: NonNullable<TerminalKeyboardPreferences["modifierMap"]> = {};
    for (const platform of PLATFORMS) {
      const map = (value.modifierMap as Record<string, Partial<ModifierMap> | undefined>)[platform];
      if (isRole(map?.ctrl) && isRole(map?.alt) && isRole(map?.meta))
        modifierMap[platform] = map as ModifierMap;
    }
    if (Object.keys(modifierMap).length) preferences.modifierMap = modifierMap;
  }
  if ("pasteBoxMode" in value && value.pasteBoxMode && typeof value.pasteBoxMode === "object") {
    const pasteBoxMode: NonNullable<TerminalKeyboardPreferences["pasteBoxMode"]> = {};
    for (const platform of PLATFORMS) {
      const mode = (value.pasteBoxMode as Record<string, unknown>)[platform];
      if (mode === "always" || mode === "whenBlocked" || mode === "never") {
        pasteBoxMode[platform] = mode;
      }
    }
    if (Object.keys(pasteBoxMode).length) preferences.pasteBoxMode = pasteBoxMode;
  }
  if ("bindings" in value && value.bindings && typeof value.bindings === "object") {
    const bindings: NonNullable<TerminalKeyboardPreferences["bindings"]> = {};
    for (const id of TERMINAL_BINDING_IDS) {
      const preference = normalizeActionPreference((value.bindings as Record<string, unknown>)[id]);
      if (preference)
        bindings[id] = {
          common: preference.common,
          platformOverrides: preference.platformOverrides,
        };
    }
    if (Object.keys(bindings).length) preferences.bindings = bindings;
  }
  return preferences;
}

export function readTerminalKeyboardPreferences(): TerminalKeyboardPreferences {
  return normalizeTerminalKeyboardPreferences(readMobileAssistantPreferences().terminalKeyboard);
}

export function writeTerminalKeyboardPreferences(preferences: TerminalKeyboardPreferences) {
  return writeTerminalKeyPreferences({ terminalKeyboard: preferences });
}

export function writeTerminalKeyboardChange(change: TerminalKeyboardChange) {
  return writeTerminalKeyPreferences({}, change);
}

export function terminalPasteBoxModeFor(
  preferences: TerminalKeyboardPreferences,
  platform = currentShortcutPlatform(),
): TerminalPasteBoxMode {
  return preferences.pasteBoxMode?.[platform] ?? DEFAULT_TERMINAL_PASTE_BOX_MODE;
}

export function terminalBindingsFor(
  preferences: TerminalKeyboardPreferences,
  id: TerminalBindingId,
  platform: ShortcutPlatform,
): ShortcutChord[] {
  const binding = preferences.bindings?.[id];
  return (
    binding?.platformOverrides?.[platform] ??
    binding?.common ??
    (id === "newline" ? DEFAULT_NEWLINE_CHORDS : [TERMINAL_BINDINGS[id].chord])
  );
}

type TerminalHardwareKeyAction = { bytes: string } | { clipboard: "copy" | "paste" } | null;

type LogicalShortcutEvent = ReturnType<typeof shortcutEventFromLogicalKey>;

export function terminalBindingBytes(
  input: LogicalShortcutEvent,
  preferences: TerminalKeyboardPreferences,
  platform: ShortcutPlatform,
): string | undefined {
  const binding = TERMINAL_BINDING_IDS.find((id) =>
    terminalBindingsFor(preferences, id, platform).some((shortcut) =>
      eventMatchesShortcut(input, shortcut, platform),
    ),
  );
  return binding && TERMINAL_BINDINGS[binding].bytes;
}

export function encodeLogicalTerminalKey(input: LogicalShortcutEvent): string {
  // Resolve control characters and named keys from the logical character. Keep
  // the actual character for printable input on non-US layouts and Caps Lock.
  const chord = shortcutChordFromEvent({ ...input, code: "" });
  const bytes = chord && terminalDataForChord(chord);
  const printable = !input.ctrlKey && Array.from(input.key).length === 1;
  const expected = input.altKey ? `\x1b${input.key}` : input.key;
  if (bytes !== null && (!printable || bytes === expected)) return bytes;
  return keyEventToSequence(input);
}

/** A null action lets xterm interpret the physical input. Empty bytes claim it without sending. */
export function terminalHardwareKeyAction(
  event: KeyboardEvent,
  preferences: TerminalKeyboardPreferences,
  platform = currentShortcutPlatform(),
  clipboard?: { hasSelection: () => boolean },
): TerminalHardwareKeyAction {
  const map = preferences.modifierMap?.[platform] ?? defaultTerminalModifierMap(platform);
  const logical = logicalKeyEvent(event, map, platform);
  if ("passthrough" in logical) return null;
  const input = shortcutEventFromLogicalKey(logical);
  const chord = shortcutChordFromEvent(input);
  if (!chord) return null;
  const bindingBytes = terminalBindingBytes(input, preferences, platform);
  if (bindingBytes !== undefined) return { bytes: bindingBytes };

  if (logical.cmd && !logical.ctrl && !logical.alt && !logical.shift && clipboard) {
    const key = logical.key.toLowerCase();
    // Preserve browser-native Cmd+V on Mac unless the user opted into the editor.
    if (
      key === "v" &&
      (logical.remapped ||
        (platform === "macos" && terminalPasteBoxModeFor(preferences, platform) === "always"))
    ) {
      return { clipboard: "paste" };
    }
    if (logical.remapped && key === "c" && clipboard.hasSelection()) {
      return { clipboard: "copy" };
    }
  }
  if (!logical.remapped) return null;
  if (logical.cmd) {
    return platform === "windows" && terminalModifierPreset(map) === "altAsCmd"
      ? null
      : { bytes: "" };
  }
  return { bytes: encodeLogicalTerminalKey(input) };
}
