import {
  eventMatchesShortcut,
  eventMatchesShortcutAction,
  hasCustomShortcutBindings,
  isShortcutActionEnabled,
  resolveShortcutBindings,
} from "./keyboardShortcutPreferences";

export const COMPOSER_SEND_SHORTCUT_STORAGE_KEY = "omnigent:composer-submit-with-mod-enter";

export const DEFAULT_SUBMIT_WITH_MOD_ENTER = false;

interface ComposerSendKeyEvent {
  key: string;
  code?: string;
  shiftKey?: boolean;
  metaKey?: boolean;
  ctrlKey?: boolean;
  altKey?: boolean;
  isComposing?: boolean;
}

export function parseSubmitWithModEnter(value: unknown): boolean {
  return value === "true";
}

export function readSubmitWithModEnter(): boolean {
  if (typeof window === "undefined") return DEFAULT_SUBMIT_WITH_MOD_ENTER;
  try {
    return parseSubmitWithModEnter(window.localStorage.getItem(COMPOSER_SEND_SHORTCUT_STORAGE_KEY));
  } catch {
    return DEFAULT_SUBMIT_WITH_MOD_ENTER;
  }
}

export function writeSubmitWithModEnter(value: boolean): void {
  if (typeof window === "undefined") return;
  try {
    if (value === DEFAULT_SUBMIT_WITH_MOD_ENTER) {
      window.localStorage.removeItem(COMPOSER_SEND_SHORTCUT_STORAGE_KEY);
    } else {
      window.localStorage.setItem(COMPOSER_SEND_SHORTCUT_STORAGE_KEY, "true");
    }
  } catch {
    // A storage failure must not make the composer unusable.
  }
}

export function isComposerSendKey(
  event: ComposerSendKeyEvent,
  submitWithModEnter: boolean,
  isMobile: boolean,
): boolean {
  if (isMobile || event.isComposing) {
    return false;
  }

  if (!isShortcutActionEnabled("sendMessage")) return false;
  if (hasCustomShortcutBindings("sendMessage")) {
    return eventMatchesShortcutAction(
      {
        code: event.code ?? "",
        key: event.key,
        ctrlKey: event.ctrlKey ?? false,
        metaKey: event.metaKey ?? false,
        altKey: event.altKey ?? false,
        shiftKey: event.shiftKey ?? false,
      },
      "sendMessage",
    );
  }

  if (event.key !== "Enter" || event.shiftKey || event.altKey) return false;

  const hasMod = event.metaKey === true || event.ctrlKey === true;
  return submitWithModEnter ? hasMod : true;
}

export type ComposerNewLineDisposition = "none" | "insert" | "block";

/**
 * Resolve both the legacy textarea-owned newline and a recorded replacement.
 * Returning `block` lets callers suppress the browser's native Shift+Enter
 * insertion when the action is disabled.
 */
export function composerNewLineDisposition(
  event: ComposerSendKeyEvent,
  submitWithModEnter: boolean,
  isMobile: boolean,
): ComposerNewLineDisposition {
  if (isMobile || event.isComposing) return "none";

  const normalized = {
    code: event.code ?? "",
    key: event.key,
    ctrlKey: event.ctrlKey ?? false,
    metaKey: event.metaKey ?? false,
    altKey: event.altKey ?? false,
    shiftKey: event.shiftKey ?? false,
  };
  const matches = hasCustomShortcutBindings("newLine")
    ? resolveShortcutBindings("newLine").some((binding) =>
        eventMatchesShortcut(normalized, binding),
      )
    : event.key === "Enter" &&
      !event.altKey &&
      !event.metaKey &&
      !event.ctrlKey &&
      Boolean(event.shiftKey) !== submitWithModEnter;
  if (!matches) return "none";
  return isShortcutActionEnabled("newLine") ? "insert" : "block";
}
