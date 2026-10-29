import { useEffect, useRef } from "react";

import { useIsEmbedded } from "@/lib/embedded";
import { getEmbedRoot } from "@/lib/host";
import { isMacPlatform } from "@/lib/hotkeys";
import { eventMatchesShortcutAction } from "@/lib/keyboardShortcutPreferences";

/** Cmd/Ctrl+Alt+R; match the physical key because Option changes its character. */
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
  const latest = useRef(onRename);
  latest.current = onRename;
  const isEmbedded = useIsEmbedded();

  useEffect(() => {
    if (!enabled) return;
    const handler = (event: globalThis.KeyboardEvent): void => {
      if (event.defaultPrevented || event.repeat || event.isComposing) return;
      if (!isRenameSessionHotkey(event, isMac)) return;
      const embedRoot = getEmbedRoot();
      if (isEmbedded && (!embedRoot || !event.composedPath().includes(embedRoot))) return;
      const focused = document.activeElement;
      if (
        focused instanceof Element &&
        focused.closest(
          '.xterm, .monaco-editor, [role="dialog"], [role="alertdialog"], [role="menu"]',
        )
      ) {
        return;
      }
      event.preventDefault();
      event.stopPropagation();
      latest.current();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [enabled, isMac, isEmbedded]);
}
