// A read-only "Keyboard shortcuts" overlay listing the shortcuts that already
// exist in the chat surface. It is intentionally a mirror of the live
// behavior — every row here corresponds to a handler that ships today
// (composer `handleKeyDown`, the global session-switch / message-nav hotkeys,
// and the approve hotkey). Nothing here binds new behavior except the dialog's
// own opener (⌘/Ctrl + /), which this component registers.
//
// Self-contained: it owns its open state and listens for its opener directly
// (a window keydown for ⌘/Ctrl+/, plus a custom event so a menu entry can open
// it without prop-drilling). Mount it once near the app shell.

import { useEffect, useState } from "react";

import {
  ALT_KEY,
  composerNewLineShortcutKeys,
  composerSendShortcutKeys,
  ENTER_KEY,
  Kbd,
  MOD_KEY,
} from "@/components/KeyboardShortcut";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useIsCoarsePointer } from "@/hooks/useIsCoarsePointer";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { readSubmitWithModEnter } from "@/lib/composerSendShortcutPreferences";
import { isNativeShell } from "@/lib/nativeBridge";
import {
  eventMatchesShortcutAction,
  hasCustomShortcutBindings,
  resolveShortcutBindings,
  shortcutBindingLabels,
  type ShortcutActionId,
} from "@/lib/keyboardShortcutPreferences";

// Custom event the dialog listens for, so non-adjacent surfaces (e.g. the
// account menu) can open it without threading state through the tree.
export const KEYBOARD_SHORTCUTS_EVENT = "omnigent:open-keyboard-shortcuts";

/** Dispatch the open event — used by menu entries that can't reach the state. */
export function openKeyboardShortcuts(): void {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new Event(KEYBOARD_SHORTCUTS_EVENT));
}

// Glyphs match the in-app tooltips (e.g. UserMessageNav's "⌘⌥↑").
const UP = "↑";
const DOWN = "↓";

interface Shortcut {
  label: string;
  actionId?: ShortcutActionId;
  /** Keys rendered left→right as chips. A chord (held together) or, for the
   *  arrow-pairs, the two interchangeable keys for that action. */
  keys: string[];
}

interface ShortcutGroup {
  title: string;
  /** Optional qualifier shown next to the group title. */
  note?: string;
  items: Shortcut[];
}

// ONLY shortcuts that exist today (see file header). Keep in sync with the
// composer's `handleKeyDown` and the global hotkey hooks.
const SHORTCUT_GROUPS: ShortcutGroup[] = [
  {
    title: "General",
    items: [
      { label: "Start a new session", actionId: "newSession", keys: [MOD_KEY, "N"] },
      { label: "Open command palette", actionId: "commandPalette", keys: [MOD_KEY, "K"] },
      { label: "Show keyboard shortcuts", actionId: "showShortcuts", keys: [MOD_KEY, "/"] },
    ],
  },
  {
    title: "In chats",
    items: [
      { label: "Recall previous prompt", actionId: "recallPreviousPrompt", keys: [UP] },
      { label: "Recall next prompt", actionId: "recallNextPrompt", keys: [DOWN] },
      { label: "Accept approval prompt", actionId: "approvePrompt", keys: [MOD_KEY, ENTER_KEY] },
      {
        label: "Toggle voice dictation",
        actionId: "voiceDictation",
        keys: [MOD_KEY, ALT_KEY, "V"],
      },
      { label: "Stop response", actionId: "stopResponse", keys: ["Esc"] },
    ],
  },
  {
    title: "Navigation",
    items: [
      { label: "Previous session", actionId: "previousSession", keys: [MOD_KEY, UP] },
      { label: "Next session", actionId: "nextSession", keys: [MOD_KEY, DOWN] },
      { label: "Poll sessions", actionId: "pollSessions", keys: [ALT_KEY, "~"] },
      { label: "Archive current session", actionId: "archiveSession", keys: [ALT_KEY, "W"] },
    ],
  },
  {
    title: "View",
    items: [
      {
        label: "Toggle conversations sidebar",
        actionId: "toggleConversationsSidebar",
        keys: [MOD_KEY, ALT_KEY, "["],
      },
      {
        label: "Toggle workspace sidebar",
        actionId: "toggleWorkspaceSidebar",
        keys: [MOD_KEY, ALT_KEY, "]"],
      },
    ],
  },
  {
    title: "Slash commands",
    note: "while the suggestions menu is open",
    items: [
      { label: "Navigate suggestions", keys: [UP, DOWN] },
      { label: "Apply highlighted command", actionId: "applySuggestion", keys: ["Tab"] },
      { label: "Dismiss menu", actionId: "dismissSuggestions", keys: ["Esc"] },
    ],
  },
];

// Numeric pinned-session jump. The chord is platform-aware (see
// usePinnedSessionHotkeys): plain Cmd/Ctrl+digit in the Electron shell, but
// Cmd/Ctrl+Alt+digit in a browser tab, where plain Cmd+digit is reserved for
// native tab-switching. Shown in both, with the matching glyphs.
function pinnedSessionShortcut(native: boolean): Shortcut {
  return {
    label: "Jump to pinned session (1–10)",
    actionId: "pinnedSession",
    keys: native ? [MOD_KEY, "1…0"] : [MOD_KEY, ALT_KEY, "1…0"],
  };
}

/** Shortcut groups for the current runtime and composer preference. */
function shortcutGroupsFor(
  native: boolean,
  submitWithModEnter: boolean,
  preventsKeyboardSubmit: boolean,
): ShortcutGroup[] {
  const effectiveKeys = (item: Shortcut): string[] => {
    if (!item.actionId || !hasCustomShortcutBindings(item.actionId)) return item.keys;
    return resolveShortcutBindings(item.actionId).flatMap((binding) =>
      shortcutBindingLabels(binding),
    );
  };
  return SHORTCUT_GROUPS.map((group) => {
    if (group.title === "In chats" && !preventsKeyboardSubmit) {
      return {
        ...group,
        items: [
          {
            label: "Send message",
            actionId: "sendMessage",
            keys: hasCustomShortcutBindings("sendMessage")
              ? resolveShortcutBindings("sendMessage").flatMap((binding) =>
                  shortcutBindingLabels(binding),
                )
              : composerSendShortcutKeys(submitWithModEnter),
          },
          {
            label: "New line in message",
            actionId: "newLine",
            keys: hasCustomShortcutBindings("newLine")
              ? resolveShortcutBindings("newLine").flatMap((binding) =>
                  shortcutBindingLabels(binding),
                )
              : composerNewLineShortcutKeys(submitWithModEnter),
          },
          ...group.items.map((item) => ({ ...item, keys: effectiveKeys(item) })),
        ],
      };
    }
    if (group.title === "Navigation") {
      const pinned = pinnedSessionShortcut(native);
      return {
        ...group,
        items: [
          ...group.items.map((item) => ({ ...item, keys: effectiveKeys(item) })),
          hasCustomShortcutBindings("pinnedSession")
            ? { ...pinned, keys: effectiveKeys(pinned) }
            : pinned,
        ],
      };
    }
    return { ...group, items: group.items.map((item) => ({ ...item, keys: effectiveKeys(item) })) };
  });
}

/**
 * The shortcut reference, grouped, as plain inline content (no dialog
 * chrome). Shared by the {@link KeyboardShortcutsDialog} overlay and the
 * Settings page, which embeds it directly instead of behind a trigger.
 */
export function KeyboardShortcutsList() {
  // Feature-based, stable per session; computed at render so tests can vary it.
  const isMobileViewport = useIsMobileViewport();
  const isCoarsePointer = useIsCoarsePointer();
  const preventsKeyboardSubmit = isMobileViewport || isCoarsePointer;
  const groups = shortcutGroupsFor(
    isNativeShell(),
    readSubmitWithModEnter(),
    preventsKeyboardSubmit,
  );
  return (
    <>
      {groups.map((group) => (
        <section key={group.title} className="mb-4 last:mb-0">
          <h3 className="mb-1 text-sm font-medium text-muted-foreground">
            {group.title}
            {group.note ? (
              <span className="ml-1.5 font-normal text-muted-foreground/70">· {group.note}</span>
            ) : null}
          </h3>
          <ul>
            {group.items.map((item) => (
              <li
                key={item.label}
                className="flex items-center justify-between gap-4 border-b border-border/60 py-2.5 last:border-b-0"
              >
                <span className="text-ui text-foreground">{item.label}</span>
                <span className="flex shrink-0 items-center gap-1">
                  {item.keys.map((key) => (
                    <Kbd key={`${item.label}-${key}`}>{key}</Kbd>
                  ))}
                </span>
              </li>
            ))}
          </ul>
        </section>
      ))}
    </>
  );
}

export function KeyboardShortcutsDialog() {
  const [open, setOpen] = useState(false);

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.repeat) return;
      // ⌘/Ctrl + / toggles the panel. Plain `/` is the composer's slash-menu
      // trigger, so require the modifier and no Shift/Alt to avoid clashing.
      if (eventMatchesShortcutAction(e, "showShortcuts")) {
        e.preventDefault();
        setOpen((prev) => !prev);
      }
    };
    const onOpenEvent = () => setOpen(true);
    window.addEventListener("keydown", onKeyDown);
    window.addEventListener(KEYBOARD_SHORTCUTS_EVENT, onOpenEvent);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener(KEYBOARD_SHORTCUTS_EVENT, onOpenEvent);
    };
  }, []);

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Keyboard shortcuts</DialogTitle>
          <DialogDescription className="sr-only">
            The keyboard shortcuts available in the chat.
          </DialogDescription>
        </DialogHeader>
        <div className="max-h-[70vh] overflow-y-auto pr-1">
          <KeyboardShortcutsList />
        </div>
      </DialogContent>
    </Dialog>
  );
}
