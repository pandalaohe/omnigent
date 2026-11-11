// ⌃` (Ctrl+` on Win/Linux) opens a new shell in the workspace rail, launching
// the remembered default type — the keyboard path for the tab-strip "+" menu,
// whose row launches on click. Bound ONCE at the app shell, where the
// shell-launch wiring lives.
//
// ⌃` is the terminal chord most editors use. It is literal Control on every
// platform, so it never meets macOS window cycling (⌘`), and it is no editing
// key in a terminal or editor, so it also works from a focused shell.

import { useEffect, useRef } from "react";

import { isMacPlatform } from "@/lib/hotkeys";
import {
  eventMatchesShortcutAction,
  hasCustomShortcutBindings,
  isShortcutActionEnabled,
  isShortcutRecordingActive,
} from "@/lib/keyboardShortcutPreferences";

/** Editor/terminal surfaces that own their own keystrokes; the chord defers. */

/** True for Cmd+Alt+T on Apple platforms or Ctrl+Alt+T elsewhere. */
export function isNewShellHotkey(e: globalThis.KeyboardEvent, _isMac = isMacPlatform()): boolean {
  // AltGr reports as Ctrl+Alt on Windows/Linux, so an ordinary AltGr+T
  // keystroke on an international layout would otherwise match this chord (and
  // get swallowed). Bail so intl typing never launches a shell — same guard the
  // sibling hotkeys carry.
  if (typeof e.getModifierState === "function" && e.getModifierState("AltGraph")) return false;
  if (isShortcutRecordingActive() || !isShortcutActionEnabled("newShell")) return false;
  if (!hasCustomShortcutBindings("newShell")) {
    return e.ctrlKey && !e.metaKey && !e.altKey && !e.shiftKey && e.code === "Backquote";
  }
  return eventMatchesShortcutAction(e, "newShell");
}

/**
 * Bind ⌃` to open a new shell. Bind ONCE at the app shell. It fires only when
 * `enabled` (the caller gates on the session declaring shell access and being
 * reachable).
 *
 * @param onLaunch Open the default shell (no-op if the caller can't right now).
 * @param enabled  Pass `false` to disable (no session, no shell access, or an
 *   offline session the browser can't reconnect). Defaults to enabled.
 */
export function useNewShellHotkey(
  onLaunch: () => void,
  enabled = true,
  isMac = isMacPlatform(),
): void {
  // Held in a ref so the bound handler always calls the latest closure without
  // re-registering on every render.
  const latest = useRef(onLaunch);
  latest.current = onLaunch;

  useEffect(() => {
    if (!enabled) return;
    const handler = (e: globalThis.KeyboardEvent): void => {
      // Ignore auto-repeat: holding the chord would spawn a shell per tick.
      if (e.repeat || !isNewShellHotkey(e, isMac)) return;
      e.preventDefault();
      e.stopPropagation();
      latest.current();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [enabled, isMac]);
}

/** Compatibility parser name used by desktop terminal command consumers. */
export const isTerminalHotkey = isNewShellHotkey;
