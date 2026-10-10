import { queueUserPreferencePatch } from "./userPreferencesSync";
import { useSyncExternalStore } from "react";

export const WORKTREE_DEFAULTS_CHANGED_EVENT = "omnigent:worktree-defaults-changed";
export const WORKTREE_DEFAULTS_STORAGE_KEY = "omnigent:worktree-defaults";

/**
 * Read the global "always use a worktree" default. `false` on a server render
 * (no `window`), when nothing is stored, or when storage is inaccessible —
 * never throws. Only a boolean true reads as on, so a stale/hand-
 * edited value can't accidentally enable it.
 */
export function readAlwaysUseWorktree(): boolean {
  if (typeof window === "undefined") return false;
  try {
    const value = JSON.parse(
      window.localStorage.getItem(WORKTREE_DEFAULTS_STORAGE_KEY) ?? "null",
    ) as unknown;
    return (
      !!value &&
      typeof value === "object" &&
      (value as { alwaysUseWorktree?: unknown }).alwaysUseWorktree === true
    );
  } catch {
    return false;
  }
}

function subscribe(onChange: () => void): () => void {
  window.addEventListener(WORKTREE_DEFAULTS_CHANGED_EVENT, onChange);
  return () => window.removeEventListener(WORKTREE_DEFAULTS_CHANGED_EVENT, onChange);
}

export function useAlwaysUseWorktree(): boolean {
  return useSyncExternalStore(subscribe, readAlwaysUseWorktree, () => false);
}

/**
 * Persist the user default through the shared server preference sync.
 * localStorage is its offline cache; false must be stored to sync an opt-out.
 */
export function writeAlwaysUseWorktree(on: boolean): void {
  if (typeof window === "undefined") return;
  try {
    const value = { alwaysUseWorktree: on };
    window.localStorage.setItem(WORKTREE_DEFAULTS_STORAGE_KEY, JSON.stringify(value));
    window.dispatchEvent(new Event(WORKTREE_DEFAULTS_CHANGED_EVENT));
    queueUserPreferencePatch("worktree_defaults", value);
  } catch {
    // localStorage quota or access errors shouldn't break settings.
  }
}
