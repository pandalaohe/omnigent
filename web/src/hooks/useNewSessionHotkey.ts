import { useEffect } from "react";

import { useIsEmbedded } from "@/lib/embedded";
import { getEmbedRoot } from "@/lib/host";
import { isMacPlatform } from "@/lib/hotkeys";
import { type Chord, chordMatches } from "@/lib/shortcuts/chord";
import { isNativeShell } from "@/lib/nativeBridge";
import { useNavigate } from "@/lib/routing";
import { useNewSessionTarget } from "@/hooks/useNewSessionTarget";
import {
  eventMatchesShortcutAction,
  hasCustomShortcutBindings,
  isShortcutActionEnabled,
  isShortcutRecordingActive,
} from "@/lib/keyboardShortcutPreferences";

const BROWSER_NEW_SESSION_CHORD: Chord = { mod: true, shift: true, key: "o" };
const NATIVE_NEW_SESSION_CHORD: Chord = { mod: true, alt: true, code: "KeyN" };

/** Use the platform default unless the person configured a binding. */
export function isNewSessionHotkey(
  e: globalThis.KeyboardEvent,
  isMac = isMacPlatform(),
  native = isNativeShell(),
): boolean {
  if (typeof e.getModifierState === "function" && e.getModifierState("AltGraph")) return false;
  if (isShortcutRecordingActive() || !isShortcutActionEnabled("newSession")) return false;
  if (!hasCustomShortcutBindings("newSession")) {
    return chordMatches(native ? NATIVE_NEW_SESSION_CHORD : BROWSER_NEW_SESSION_CHORD, e, isMac);
  }
  return eventMatchesShortcutAction(e, "newSession");
}

/** Navigate to the same new-session route used by the command palette. */
export function useNewSessionHotkey(enabled = true, isMac = isMacPlatform()): void {
  const navigate = useNavigate();
  const { route } = useNewSessionTarget();
  const isEmbedded = useIsEmbedded();

  useEffect(() => {
    if (!enabled) return;
    const handler = (e: globalThis.KeyboardEvent): void => {
      if (e.repeat || !isNewSessionHotkey(e, isMac)) return;
      const embedRoot = getEmbedRoot();
      if (isEmbedded && (!embedRoot || !e.composedPath().includes(embedRoot))) return;
      e.preventDefault();
      e.stopPropagation();
      navigate(route);
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [enabled, isMac, isEmbedded, navigate, route]);
}
