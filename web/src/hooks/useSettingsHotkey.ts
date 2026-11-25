import { useEffect } from "react";

import { isMacPlatform } from "@/lib/hotkeys";
import { type Chord, chordMatches } from "@/lib/shortcuts/chord";
import { supportsDesktopCommands } from "@/lib/nativeBridge";
import { useNavigate } from "@/lib/routing";
import {
  eventMatchesShortcutAction,
  hasCustomShortcutBindings,
  isShortcutActionEnabled,
  isShortcutRecordingActive,
} from "@/lib/keyboardShortcutPreferences";

const SETTINGS_CHORD: Chord = { mod: true, alt: true, code: "Comma" };

/**
 * True for Cmd+Alt+, on Apple platforms or Ctrl+Alt+, elsewhere. Never in the
 * desktop app, whose Settings menu item owns Cmd/Ctrl+, instead.
 */
export function isSettingsHotkey(
  event: globalThis.KeyboardEvent,
  isMac = isMacPlatform(),
  desktopMenu = supportsDesktopCommands(),
): boolean {
  if (typeof event.getModifierState === "function" && event.getModifierState("AltGraph")) {
    return false;
  }
  if (isShortcutRecordingActive() || !isShortcutActionEnabled("openSettings")) return false;
  if (!hasCustomShortcutBindings("openSettings")) {
    return !desktopMenu && chordMatches(SETTINGS_CHORD, event, isMac);
  }
  return eventMatchesShortcutAction(event, "openSettings");
}

/** Navigate to Settings from anywhere in the app. */
export function useSettingsHotkey(enabled = true, isMac = isMacPlatform()): void {
  const navigate = useNavigate();

  useEffect(() => {
    if (!enabled) return;
    const handler = (event: globalThis.KeyboardEvent): void => {
      if (event.repeat || !isSettingsHotkey(event, isMac)) return;
      event.preventDefault();
      event.stopPropagation();
      navigate("/settings");
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [enabled, isMac, navigate]);
}
