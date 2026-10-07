import { useEffect, useRef } from "react";

import { hasCommandModifier, isMacPlatform } from "@/lib/hotkeys";
import {
  eventMatchesShortcutAction,
  hasCustomShortcutBindings,
  isShortcutActionEnabled,
  isShortcutRecordingActive,
} from "@/lib/keyboardShortcutPreferences";

const TEXT_ENTRY_SURFACE = ".monaco-editor, .xterm";

/** True for Cmd+Alt+B on Apple platforms or Ctrl+Alt+B elsewhere. */
export function isNewBrowserHotkey(
  event: globalThis.KeyboardEvent,
  isMac = isMacPlatform(),
): boolean {
  if (typeof event.getModifierState === "function" && event.getModifierState("AltGraph")) {
    return false;
  }
  if (isShortcutRecordingActive() || !isShortcutActionEnabled("newBrowserTab")) return false;
  if (!hasCustomShortcutBindings("newBrowserTab")) {
    if (!hasCommandModifier(event, isMac) || !event.altKey || event.shiftKey) return false;
    return event.code === "KeyB";
  }
  return eventMatchesShortcutAction(event, "newBrowserTab");
}

/** Bind the new-browser-tab shortcut while the workspace supports Browser. */
export function useNewBrowserHotkey(
  onOpen: () => void,
  enabled = true,
  isMac = isMacPlatform(),
): void {
  const latest = useRef(onOpen);
  latest.current = onOpen;

  useEffect(() => {
    if (!enabled) return;
    const handler = (event: globalThis.KeyboardEvent): void => {
      if (event.repeat || !isNewBrowserHotkey(event, isMac)) return;
      const active = document.activeElement;
      if (active instanceof Element && active.closest(TEXT_ENTRY_SURFACE) !== null) return;
      event.preventDefault();
      event.stopPropagation();
      latest.current();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [enabled, isMac]);
}
