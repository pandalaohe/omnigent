// User-synced display preference for the composer's context ring.

import { queueUserPreferencePatch } from "./userPreferencesSync";

export const CONTEXT_INDICATOR_STORAGE_KEY = "omnigent:context-indicator-mode";
export const CONTEXT_INDICATOR_CHANGED_EVENT = "omnigent:context-indicator-mode-changed";

export const contextIndicatorModes = ["context", "compact"] as const;
export type ContextIndicatorMode = (typeof contextIndicatorModes)[number];
export const CONTEXT_INDICATOR_DEFAULT: ContextIndicatorMode = "context";

export function readContextIndicatorMode(): ContextIndicatorMode {
  if (typeof window === "undefined") return CONTEXT_INDICATOR_DEFAULT;
  try {
    return window.localStorage.getItem(CONTEXT_INDICATOR_STORAGE_KEY) === "compact"
      ? "compact"
      : CONTEXT_INDICATOR_DEFAULT;
  } catch {
    return CONTEXT_INDICATOR_DEFAULT;
  }
}

export function writeContextIndicatorMode(value: ContextIndicatorMode): void {
  if (typeof window === "undefined") return;
  try {
    if (value === CONTEXT_INDICATOR_DEFAULT) {
      window.localStorage.removeItem(CONTEXT_INDICATOR_STORAGE_KEY);
    } else {
      window.localStorage.setItem(CONTEXT_INDICATOR_STORAGE_KEY, value);
    }
    window.dispatchEvent(new Event(CONTEXT_INDICATOR_CHANGED_EVENT));
    queueUserPreferencePatch(
      "context_indicator",
      value === CONTEXT_INDICATOR_DEFAULT ? null : value,
    );
  } catch {
    // A display preference must never break the chat surface.
  }
}
