import { useEffect, useRef } from "react";

import {
  eventMatchesShortcutAction,
  type ShortcutActionId,
} from "@/lib/keyboardShortcutPreferences";

export const SELECT_WORKSPACE_TAB_ACTION_EVENT = "omnigent:action:select-workspace-tab";

export function dispatchSelectWorkspaceTab(tabNumber: 1 | 2 | 3 | 4): void {
  if (typeof window !== "undefined") {
    window.dispatchEvent(
      new CustomEvent(SELECT_WORKSPACE_TAB_ACTION_EVENT, { detail: { tabNumber } }),
    );
  }
}

const TAB_ACTION_IDS: Record<1 | 2 | 3 | 4, ShortcutActionId> = {
  1: "selectWorkspaceTab1",
  2: "selectWorkspaceTab2",
  3: "selectWorkspaceTab3",
  4: "selectWorkspaceTab4",
};

/**
 * Bind the per-tab shortcuts (unset by default) plus the software-keyboard
 * action event that dispatches them; both paths are inert while `enabled` is
 * false (e.g. the rail is still pending).
 */
export function useWorkspaceTabHotkeys(
  onSelect: (tabNumber: 1 | 2 | 3 | 4) => void,
  enabled: boolean,
): void {
  const latest = useRef(onSelect);
  latest.current = onSelect;

  useEffect(() => {
    if (!enabled) return;
    const onKeyDown = (event: globalThis.KeyboardEvent): void => {
      if (event.repeat) return;
      for (const tabNumber of [1, 2, 3, 4] as const) {
        if (!eventMatchesShortcutAction(event, TAB_ACTION_IDS[tabNumber])) continue;
        event.preventDefault();
        event.stopPropagation();
        latest.current(tabNumber);
        return;
      }
    };
    const onAction = (event: Event): void => {
      const tabNumber = (event as CustomEvent<{ tabNumber?: unknown }>).detail?.tabNumber;
      if (tabNumber === 1 || tabNumber === 2 || tabNumber === 3 || tabNumber === 4) {
        latest.current(tabNumber);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    window.addEventListener(SELECT_WORKSPACE_TAB_ACTION_EVENT, onAction);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener(SELECT_WORKSPACE_TAB_ACTION_EVENT, onAction);
    };
  }, [enabled]);
}
