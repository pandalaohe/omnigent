import { useEffect, useReducer } from "react";
import type { ReactNode } from "react";

export { CompactKbd, CompactShortcutKeys } from "@/components/ui/kbd";
import { TooltipContent } from "@/components/ui/tooltip";
import {
  KEYBOARD_SHORTCUTS_CHANGED_EVENT,
  hasCustomShortcutBindings,
  isShortcutActionEnabled,
  resolveShortcutBindings,
  shortcutAriaKeys,
  shortcutBindingLabels,
  type ShortcutActionId,
} from "@/lib/keyboardShortcutPreferences";
import { isMacPlatform } from "@/lib/hotkeys";
import { cn } from "@/lib/utils";

const IS_MAC = isMacPlatform();

export const MOD_KEY = IS_MAC ? "⌘" : "Ctrl";
export const ALT_KEY = IS_MAC ? "⌥" : "Alt";
export const ENTER_KEY = "↵";
export const SHIFT_KEY = "⇧";
export const ARIA_MOD_KEY = IS_MAC ? "Meta" : "Control";

/** Labels for an action's first binding, or `[]` when disabled/unbound. */
export function shortcutKeys(actionId: ShortcutActionId): string[] {
  if (!isShortcutActionEnabled(actionId)) return [];
  const binding = resolveShortcutBindings(actionId)[0];
  return binding ? shortcutBindingLabels(binding) : [];
}

/** Re-render whenever a shortcut preference changes (local write or sync). */
export function useKeyboardShortcutsVersion(): number {
  const [version, refresh] = useReducer((previous: number) => previous + 1, 0);
  useEffect(() => {
    const onChanged = () => refresh();
    window.addEventListener(KEYBOARD_SHORTCUTS_CHANGED_EVENT, onChanged);
    window.addEventListener("storage", onChanged);
    return () => {
      window.removeEventListener(KEYBOARD_SHORTCUTS_CHANGED_EVENT, onChanged);
      window.removeEventListener("storage", onChanged);
    };
  }, []);
  return version;
}

/**
 * Live hint for a shortcut action: the first binding's labels and its
 * `aria-keyshortcuts` value, kept in sync with preference writes.
 */
export function useShortcutHint(actionId: ShortcutActionId): {
  keys: string[];
  aria: string | undefined;
} {
  useKeyboardShortcutsVersion();
  const binding = isShortcutActionEnabled(actionId)
    ? resolveShortcutBindings(actionId)[0]
    : undefined;
  return {
    keys: shortcutKeys(actionId),
    aria: binding ? shortcutAriaKeys(binding) : undefined,
  };
}

export function composerSendShortcutKeys(submitWithModEnter: boolean): string[] {
  if (hasCustomShortcutBindings("sendMessage")) {
    return resolveShortcutBindings("sendMessage").flatMap((binding) =>
      shortcutBindingLabels(binding),
    );
  }
  return submitWithModEnter ? [MOD_KEY, ENTER_KEY] : [ENTER_KEY];
}

export function composerSteerAllShortcutKeys(submitWithModEnter: boolean): string[] {
  return submitWithModEnter ? [MOD_KEY, SHIFT_KEY, ENTER_KEY] : [MOD_KEY, ENTER_KEY];
}

export function composerNewLineShortcutKeys(submitWithModEnter: boolean): string[] {
  if (!isShortcutActionEnabled("newLine")) return [];
  if (hasCustomShortcutBindings("newLine")) {
    const binding = resolveShortcutBindings("newLine")[0];
    return binding ? shortcutBindingLabels(binding) : [];
  }
  return submitWithModEnter ? [ENTER_KEY] : [SHIFT_KEY, ENTER_KEY];
}

export function composerNewLineAlternateKeys(submitWithModEnter: boolean): string[] | undefined {
  if (hasCustomShortcutBindings("newLine")) {
    const binding = resolveShortcutBindings("newLine")[1];
    return binding ? shortcutBindingLabels(binding) : undefined;
  }
  // Alt+Enter is a newline in both modes; plain Enter already is in alternate mode.
  return submitWithModEnter ? undefined : [ALT_KEY, ENTER_KEY];
}

export function Kbd({
  children,
  variant = "default",
}: {
  children: ReactNode;
  variant?: "default" | "dark";
}) {
  return (
    <kbd
      data-slot="kbd"
      className={cn(
        "inline-flex h-6 min-w-6 items-center justify-center rounded-md border border-border bg-muted px-1.5 font-sans text-sm font-medium text-muted-foreground",
        variant === "dark" && "border-slate-600 bg-slate-700 text-slate-300",
      )}
    >
      {children}
    </kbd>
  );
}

export function KeyboardShortcutTooltipContent({ label, keys }: { label: string; keys: string[] }) {
  return (
    <TooltipContent
      side="top"
      shortcut={keys}
      className="border border-slate-700 bg-slate-900 text-slate-100 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100"
    >
      <span>{label}</span>
    </TooltipContent>
  );
}
