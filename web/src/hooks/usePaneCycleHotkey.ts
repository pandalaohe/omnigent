// ⌘⌥⇧[ / ⌘⌥⇧] (Ctrl+Alt+Shift on Win/Linux) focus the previous / next pane: grid
// cells or split terminal panes (grid cells never split, so one chord serves both).

import { useEffect, useRef } from "react";

import { isFocusInDialogOrMenu, isMacPlatform } from "@/lib/hotkeys";
import { eventMatchesShortcutAction } from "@/lib/keyboardShortcutPreferences";

/** -1 for Cmd/Ctrl+Alt+Shift+[, 1 for Cmd/Ctrl+Alt+Shift+], 0 for anything else. */
export function paneCycleHotkeyStep(
  event: globalThis.KeyboardEvent,
  isMac = isMacPlatform(),
): -1 | 0 | 1 {
  if (event.isComposing || event.defaultPrevented) {
    return 0;
  }
  if (typeof event.getModifierState === "function" && event.getModifierState("AltGraph")) {
    return 0;
  }
  // Match the physical key: Alt and Shift change the produced character.
  if (eventMatchesShortcutAction(event, "nextPane", isMac ? "macos" : "windows")) return 1;
  if (eventMatchesShortcutAction(event, "previousPane", isMac ? "macos" : "windows")) return -1;
  return 0;
}

interface PaneCycleHotkeyOptions {
  enabled: boolean;
  /** Panes in cycle order; must include ``focusedKey``. */
  paneKeys: readonly string[];
  focusedKey: string | undefined;
  onFocusPane: (key: string) => void;
}

/** Bind the pane-cycle chord, wrapping at either end of ``paneKeys``. */
export function usePaneCycleHotkey(options: PaneCycleHotkeyOptions, isMac = isMacPlatform()): void {
  const latest = useRef(options);
  latest.current = options;

  useEffect(() => {
    if (!options.enabled) return;

    const handler = (event: globalThis.KeyboardEvent): void => {
      const step = paneCycleHotkeyStep(event, isMac);
      if (step === 0 || isFocusInDialogOrMenu()) return;
      event.preventDefault();
      event.stopPropagation();
      // Claim auto-repeat too, so a held chord never reaches the terminal.
      if (event.repeat) return;
      const { paneKeys, focusedKey, onFocusPane } = latest.current;
      const index = paneKeys.indexOf(focusedKey ?? "");
      onFocusPane(paneKeys[(index + step + paneKeys.length) % paneKeys.length]);
    };

    // Capture before a focused xterm can forward the chord to its PTY.
    window.addEventListener("keydown", handler, true);
    return () => window.removeEventListener("keydown", handler, true);
  }, [isMac, options.enabled]);
}
