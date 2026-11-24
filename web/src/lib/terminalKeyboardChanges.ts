import type { ModifierMap, ShortcutChord, ShortcutPlatform } from "./keyboardShortcutPreferences";
import type { TerminalBindingId, TerminalPasteBoxMode } from "./terminalKeyboardPreferences";

export type TerminalKeyboardChange = { platform: ShortcutPlatform } & (
  | { field: "modifierMap"; value: ModifierMap }
  | { field: "pasteBoxMode"; value: TerminalPasteBoxMode }
  | { field: TerminalBindingId; value: ShortcutChord[] | null }
);

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

export function applyTerminalKeyboardChanges(stored: unknown, changes: TerminalKeyboardChange[]) {
  const previous = record(stored);
  const keyboard: Record<string, unknown> = previous.v === 1 ? { ...previous } : { v: 1 };
  for (const { platform, field, value } of changes) {
    if (field === "modifierMap" || field === "pasteBoxMode") {
      keyboard[field] = { ...record(keyboard[field]), [platform]: value };
    } else {
      const bindings = record(keyboard.bindings);
      const binding = record(bindings[field]);
      const overrides = { ...record(binding.platformOverrides) };
      if (value === null) Reflect.deleteProperty(overrides, platform);
      else overrides[platform] = value;
      keyboard.bindings = { ...bindings, [field]: { ...binding, platformOverrides: overrides } };
    }
  }
  return keyboard;
}
