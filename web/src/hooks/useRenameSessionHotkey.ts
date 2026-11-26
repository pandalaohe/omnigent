import { useCurrentSessionShortcut } from "@/hooks/useCurrentSessionShortcut";
import { isMacPlatform } from "@/lib/hotkeys";
import { eventMatchesShortcutAction } from "@/lib/keyboardShortcutPreferences";

/** Cmd/Ctrl+Alt+R. */
export function isRenameSessionHotkey(
  event: globalThis.KeyboardEvent,
  isMac = isMacPlatform(),
): boolean {
  return (
    !event.getModifierState("AltGraph") &&
    eventMatchesShortcutAction(event, "renameSession", isMac ? "macos" : "windows")
  );
}

/** Bind alongside the owner-only title editor so unavailable sessions keep their keys. */
export function useRenameSessionHotkey(
  onRename: () => void,
  enabled = true,
  isMac = isMacPlatform(),
): void {
  useCurrentSessionShortcut("session.rename", onRename, enabled, isMac);
}
