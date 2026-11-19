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

type PlatformShortcutValues = Pick<ShortcutActionPreference, "common" | "platformOverrides">;

export interface TerminalKeyboardPreferences {
  v: 1;
  modifierMap?: Partial<Record<ShortcutPlatform, ModifierMap>>;
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
  const binding = TERMINAL_BINDING_IDS.find((id) =>
    terminalBindingsFor(preferences, id, platform).some((shortcut) =>
      eventMatchesShortcut(input, shortcut, platform),
    ),
  );
  if (binding) return { bytes: TERMINAL_BINDINGS[binding].bytes };

  if (
    logical.remapped &&
    logical.cmd &&
    !logical.ctrl &&
    !logical.alt &&
    !logical.shift &&
    clipboard
  ) {
    if (logical.key.toLowerCase() === "v") return { clipboard: "paste" };
    if (logical.key.toLowerCase() === "c" && clipboard.hasSelection()) {
      return { clipboard: "copy" };
    }
  }
  if (!logical.remapped) return null;
  if (logical.cmd) {
    return platform === "windows" && terminalModifierPreset(map) === "altAsCmd"
      ? null
      : { bytes: "" };
  }
  // Reuse the chord encoder for the full set of Ctrl control bytes. For printable
  // keys, resolve the chord from the logical character rather than the US key position.
  const characterChord = shortcutChordFromEvent({ ...input, code: "" });
  const controlBytes = logical.ctrl && characterChord && terminalDataForChord(characterChord);
  return { bytes: controlBytes || keyEventToSequence(input) };
}
