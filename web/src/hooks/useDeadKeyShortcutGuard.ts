import { useEffect } from "react";

import { installDeadKeyShortcutGuard } from "@/lib/deadKeyShortcutGuard";
import {
  SHORTCUT_ACTION_IDS,
  currentShortcutPlatform,
  eventMatchesShortcutAction,
} from "@/lib/keyboardShortcutPreferences";

/** Swallows the accent an IME emits when a dead-key shortcut (e.g. ⌥`) fires. */
export function useDeadKeyShortcutGuard(): void {
  useEffect(() => {
    // Only Chromium on macOS hands the dead key to the input method before
    // keydown, so no other platform needs the guard.
    if (currentShortcutPlatform() !== "macos") return;
    return installDeadKeyShortcutGuard(window, (event) =>
      SHORTCUT_ACTION_IDS.some((actionId) => eventMatchesShortcutAction(event, actionId)),
    );
  }, []);
}
