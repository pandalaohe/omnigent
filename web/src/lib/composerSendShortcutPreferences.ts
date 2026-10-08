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

/** Enter or Shift+Enter with no Ctrl/Meta/Alt: the chords a textarea itself turns into a newline. */
export function isTextareaNewlineKey(event: ComposerSendKeyEvent): boolean {
  return event.key === "Enter" && !event.altKey && !event.metaKey && !event.ctrlKey;
}

/**
 * Resolve both the legacy textarea-owned newline and a recorded replacement.
 * Defaults are Shift+Enter (Enter in alternate mode) plus Alt/Option+Enter; a
 * recorded replacement replaces them; `block` lets callers suppress the
 * browser's native newline when the action is disabled.
 */
export function composerNewLineDisposition(
  event: ComposerSendKeyEvent,
  submitWithModEnter: boolean,
  isMobile: boolean,
): ComposerNewLineDisposition {
  if (event.isComposing) return "none";
  // Touch keyboards already turn Enter and Shift+Enter into a newline; never intercept them there.
  if (isMobile && isTextareaNewlineKey(event)) return "none";

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
    : isComposerAltNewlineKey(event) ||
      (isTextareaNewlineKey(event) && Boolean(event.shiftKey) !== submitWithModEnter);
  if (!matches) return "none";
  return isShortcutActionEnabled("newLine") ? "insert" : "block";
}

/**
 * Alt/Option+Enter inserts a line break like Shift+Enter, matching most harness
 * TUIs. Browsers insert nothing for this chord, so the composer does it itself.
 */
export function isComposerAltNewlineKey(event: ComposerSendKeyEvent): boolean {
  return (
    event.key === "Enter" &&
    event.altKey === true &&
    event.metaKey !== true &&
    event.ctrlKey !== true &&
    !event.isComposing
  );
}

/**
 * The "steer everything now" chord: Mod+Enter (Cmd on macOS, Ctrl elsewhere).
 * When Mod+Enter is already the send chord, Mod+Shift+Enter takes over so the
 * modifier keeps one meaning per mode.
 */
export function isComposerSteerAllKey(
  event: ComposerSendKeyEvent,
  submitWithModEnter: boolean,
  isMobile: boolean,
): boolean {
  if (isMobile || event.key !== "Enter" || event.isComposing || event.altKey) return false;
  const hasMod = event.metaKey === true || event.ctrlKey === true;
  if (!hasMod) return false;
  return submitWithModEnter ? event.shiftKey === true : event.shiftKey !== true;
}
