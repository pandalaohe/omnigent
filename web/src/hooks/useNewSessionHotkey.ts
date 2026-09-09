import { useEffect } from "react";

import { hasCommandModifier, isMacPlatform } from "@/lib/hotkeys";
import { useNavigate } from "@/lib/routing";
import { useNewSessionTarget } from "@/hooks/useNewSessionTarget";
import {
  eventMatchesShortcutAction,
  hasCustomShortcutBindings,
  isShortcutActionEnabled,
  isShortcutRecordingActive,
} from "@/lib/keyboardShortcutPreferences";

/** True for Cmd+N on Apple platforms or Ctrl+N elsewhere, without extra modifiers. */
export function isNewSessionHotkey(e: globalThis.KeyboardEvent, isMac = isMacPlatform()): boolean {
  if (typeof e.getModifierState === "function" && e.getModifierState("AltGraph")) return false;
  if (isShortcutRecordingActive() || !isShortcutActionEnabled("newSession")) return false;
  if (!hasCustomShortcutBindings("newSession")) {
    if (!hasCommandModifier(e, isMac) || e.altKey || e.shiftKey) return false;
    return e.key === "n" || e.key === "N";
  }
  return eventMatchesShortcutAction(e, "newSession");
}

/** Navigate to the same new-session route used by the command palette. */
export function useNewSessionHotkey(enabled = true, isMac = isMacPlatform()): void {
  const navigate = useNavigate();
  const { route } = useNewSessionTarget();

  useEffect(() => {
    if (!enabled) return;
    const handler = (e: globalThis.KeyboardEvent): void => {
      if (e.repeat || !isNewSessionHotkey(e, isMac)) return;
      e.preventDefault();
      e.stopPropagation();
      navigate(route);
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [enabled, isMac, navigate, route]);
}
