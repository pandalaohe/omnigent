// A read-only "Keyboard shortcuts" overlay listing the shortcuts that already
// exist in the chat surface. It is intentionally a mirror of the live
// behavior — every row here corresponds to a handler that ships today
// (composer `handleKeyDown`, the global session-switch / message-nav hotkeys,
// and the approve hotkey). Nothing here binds new behavior except the dialog's
// own opener, which follows the showShortcuts binding.
//
// Self-contained: it owns its open state and listens for its opener directly
// (a window keydown for ⌘/Ctrl+/, plus a custom event so a menu entry can open
// it without prop-drilling). Mount it once near the app shell.

import { Fragment, useEffect, useState } from "react";

import {
  ALT_KEY,
  composerNewLineShortcutKeys,
  composerSendShortcutKeys,
  composerSteerAllShortcutKeys,
  ENTER_KEY,
  Kbd,
  shortcutKeys,
  useKeyboardShortcutsVersion,
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
import { hasCommandModifier } from "@/lib/hotkeys";
import {
  currentShortcutPlatform,
  eventMatchesShortcutAction,
  hasCustomShortcutBindings,
  isShortcutActionEnabled,
  isShortcutRecordingActive,
  resolveShortcutBindings,
  shortcutBindingLabels,
  type ShortcutActionId,
} from "@/lib/keyboardShortcutPreferences";
import { isElectronShell, isNativeShell, supportsBrowser } from "@/lib/nativeBridge";

// Custom event the dialog listens for, so non-adjacent surfaces (e.g. the
// account menu) can open it without threading state through the tree.
export const KEYBOARD_SHORTCUTS_EVENT = "omnigent:open-keyboard-shortcuts";

/** Dispatch the open event — used by menu entries that can't reach the state. */
export function openKeyboardShortcuts(): void {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new Event(KEYBOARD_SHORTCUTS_EVENT));
}

interface Shortcut {
  label: string;
  /** Keys rendered left→right as chips. A chord (held together) or, for the
   *  arrow-pairs, the two interchangeable keys for that action. */
  keys: string[];
  lastKeySeparator?: string;
  /** When set, keys come from the live shortcut layer instead of `keys`. */
  actionId?: ShortcutActionId;
  /** Another chord for the same action, shown after "or". */
  alternateKeys?: string[];
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
      { label: "Start a new session", keys: [], actionId: "newSession" },
      { label: "Open command palette", keys: [], actionId: "commandPalette" },
      { label: "Find a session by name", keys: [], actionId: "findSession" },
      { label: "Open Settings", keys: [], actionId: "openSettings" },
      { label: "Show keyboard shortcuts", keys: [], actionId: "showShortcuts" },
    ],
  },
  {
    title: "In chats",
    items: [
      { label: "Recall previous prompt", keys: [], actionId: "recallPreviousPrompt" },
      { label: "Recall next prompt", keys: [], actionId: "recallNextPrompt" },
      { label: "Accept approval prompt", keys: [], actionId: "approvePrompt" },
      { label: "Open model picker", keys: [], actionId: "openModelPicker" },
      { label: "Focus chat input", keys: [], actionId: "focusComposer" },
      { label: "Toggle voice dictation", keys: [], actionId: "voiceDictation" },
      { label: "Stop response", keys: [], actionId: "stopResponse" },
    ],
  },
  {
    title: "Navigation",
    items: [
      { label: "Previous session", keys: [], actionId: "previousSession" },
      { label: "Next session", keys: [], actionId: "nextSession" },
    ],
  },
  {
    title: "View",
    items: [
      { label: "Toggle Chat / Terminal view", keys: [], actionId: "toggleViewMode" },
      { label: "Toggle conversations sidebar", keys: [], actionId: "toggleConversationsSidebar" },
      { label: "Focus or close workspace sidebar", keys: [], actionId: "toggleWorkspaceSidebar" },
      {
        label: "Select a workspace tab",
        keys: [],
        lastKeySeparator: "+",
      },
      { label: "Open a new browser tab", keys: [], actionId: "newBrowserTab" },
      { label: "Open a new shell", keys: [], actionId: "newShell" },
    ],
  },
  {
    title: "Slash commands",
    note: "while the suggestions menu is open",
    items: [
      { label: "Navigate suggestions", keys: [] },
      { label: "Apply highlighted command", keys: [], actionId: "applySuggestion" },
      { label: "Dismiss menu", keys: [], actionId: "dismissSuggestions" },
    ],
  },
  {
    title: "Question cards",
    note: "while a question card has focus",
    items: [
      { label: "Focus question card", keys: [], actionId: "focusQuestionCard" },
      { label: "Move between options", keys: [] },
      { label: "Select option", keys: [], actionId: "questionCardSelectOption" },
      { label: "Next question / submit", keys: [], actionId: "questionCardNextOrSubmit" },
      { label: "Previous / next question", keys: [] },
      { label: "Leave card", keys: [], actionId: "questionCardLeave" },
      { label: "Cancel question", keys: [], actionId: "questionCardCancel" },
      { label: "Cancel & interrupt", keys: [], actionId: "questionCardCancelAndInterrupt" },
    ],
  },
];

// Numeric pinned-session jump. The chord is platform-aware (see
// usePinnedSessionHotkeys): plain Cmd/Ctrl+digit in the Electron shell, but
// Cmd/Ctrl+Alt+digit in a browser tab, where plain Cmd+digit is reserved for
// native tab-switching. Shown in both, with the matching glyphs.
function pinnedSessionShortcut(native: boolean): Shortcut {
  if (!isShortcutActionEnabled("pinnedSession")) {
    return { label: "Jump to pinned session (1–10)", keys: [] };
  }
  const bindings = resolveShortcutBindings("pinnedSession", currentShortcutPlatform(), {
    nativeShell: native,
  });
  return {
    label: "Jump to pinned session (1–10)",
    keys: bindings.flatMap((binding) => shortcutBindingLabels(binding)),
  };
}

/** Shortcut groups for the current runtime and composer preference. */
function shortcutGroupsFor(
  native: boolean,
  electron: boolean,
  browser: boolean,
  submitWithModEnter: boolean,
  preventsKeyboardSubmit: boolean,
): ShortcutGroup[] {
  return SHORTCUT_GROUPS.map((group) => {
    if (group.title === "In chats" && !preventsKeyboardSubmit) {
      return {
        ...group,
        items: [
          { label: "Send message", keys: composerSendShortcutKeys(submitWithModEnter) },
          {
            label: "Send now, with all queued messages",
            keys: composerSteerAllShortcutKeys(submitWithModEnter),
          },
          {
            label: "New line in message",
            keys: composerNewLineShortcutKeys(submitWithModEnter),
            // Alt+Enter is a newline in both modes; plain Enter already is in alternate mode.
            alternateKeys: submitWithModEnter ? undefined : [ALT_KEY, ENTER_KEY],
          },
          ...group.items,
        ],
      };
    }
    if (group.title === "Navigation") {
      return {
        ...group,
        items: [
          ...(electron
            ? [{ label: "Switch recent sessions", keys: [], actionId: "recentSessions" as const }]
            : []),
          ...group.items,
          pinnedSessionShortcut(native),
        ],
      };
    }
    if (group.title === "Slash commands") {
      return {
        ...group,
        items: group.items.map((item) =>
          item.label === "Navigate suggestions"
            ? {
                ...item,
                keys: [...shortcutKeys("previousSuggestion"), ...shortcutKeys("nextSuggestion")],
              }
            : item,
        ),
      };
    }
    if (group.title === "Question cards") {
      return {
        ...group,
        items: group.items.map((item) =>
          item.label === "Move between options"
            ? {
                ...item,
                keys: [
                  ...shortcutKeys("questionCardPreviousOption"),
                  ...shortcutKeys("questionCardNextOption"),
                ],
              }
            : item.label === "Previous / next question"
              ? {
                  ...item,
                  keys: [
                    ...shortcutKeys("questionCardPreviousQuestion"),
                    ...shortcutKeys("questionCardNextQuestion"),
                  ],
                }
              : item,
        ),
      };
    }
    if (group.title === "View") {
      return {
        ...group,
        items: group.items
          .filter((item) => browser || item.label !== "Open a new browser tab")
          .map((item) =>
            item.label === "Select a workspace tab"
              ? { ...item, keys: [...shortcutKeys("toggleWorkspaceSidebar"), "1…4"] }
              : item,
          ),
      };
    }
    return group;
  });
}

/**
 * The shortcut reference shared by the dialog and Settings page. The dialog
 * keeps the compact inline list; Settings uses section headings with bordered
 * list cards to match the rest of its content.
 */
export function KeyboardShortcutsList({
  variant = "compact",
}: {
  variant?: "compact" | "settings";
}) {
  useKeyboardShortcutsVersion();
  // Feature-based, stable per session; computed at render so tests can vary it.
  const isMobileViewport = useIsMobileViewport();
  const isCoarsePointer = useIsCoarsePointer();
  const preventsKeyboardSubmit = isMobileViewport || isCoarsePointer;
  const groups = shortcutGroupsFor(
    isNativeShell(),
    isElectronShell(),
    supportsBrowser(),
    readSubmitWithModEnter(),
    preventsKeyboardSubmit,
  );
  const settings = variant === "settings";
  return (
    <div className={settings ? "flex flex-col gap-6" : undefined}>
      {groups.map((group) => (
        <section key={group.title} className={settings ? "" : "mb-4 last:mb-0"}>
          <h3
            className={
              settings
                ? "mb-3 text-ui font-medium text-foreground"
                : "mb-1 text-sm font-medium text-muted-foreground"
            }
          >
            {group.title}
            {group.note ? (
              <span className="ml-1.5 font-normal text-muted-foreground/70">· {group.note}</span>
            ) : null}
          </h3>
          <ul className={settings ? "rounded-xl border border-border bg-card px-4" : undefined}>
            {group.items.map((item) => {
              const keys = item.actionId ? shortcutKeys(item.actionId) : item.keys;
              if (keys.length === 0) return null;
              return (
                <li
                  key={item.label}
                  className={
                    settings
                      ? "flex items-center justify-between gap-4 border-b border-border py-4 last:border-b-0"
                      : "flex items-center justify-between gap-4 border-b border-border/60 py-2.5 last:border-b-0"
                  }
                >
                  <span className="text-ui text-foreground">{item.label}</span>
                  <span className="flex shrink-0 items-center gap-1">
                    {keys.map((key, index) => (
                      <Fragment key={`${item.label}-${key}`}>
                        {index === keys.length - 1 && item.lastKeySeparator ? (
                          <span aria-hidden="true" className="text-muted-foreground/70">
                            {item.lastKeySeparator}
                          </span>
                        ) : null}
                        <Kbd>{key}</Kbd>
                      </Fragment>
                    ))}
                    {item.alternateKeys ? (
                      <>
                        <span className="px-0.5 text-sm text-muted-foreground/70">or</span>
                        {item.alternateKeys.map((key) => (
                          <Kbd key={`${item.label}-alternate-${key}`}>{key}</Kbd>
                        ))}
                      </>
                    ) : null}
                  </span>
                </li>
              );
            })}
          </ul>
        </section>
      ))}
    </div>
  );
}

/** The dialog's own opener follows the showShortcuts binding. */
function isShowShortcutsHotkey(event: KeyboardEvent): boolean {
  if (event.getModifierState?.("AltGraph")) return false;
  if (isShortcutRecordingActive() || !isShortcutActionEnabled("showShortcuts")) return false;
  if (!hasCustomShortcutBindings("showShortcuts")) {
    // ⌘/Ctrl + / toggles the panel. Plain `/` is the composer's slash-menu
    // trigger, so require the platform command modifier and no Shift/Alt to
    // avoid clashing (only ⌘/ on macOS, only Ctrl+/ on Win/Linux).
    return hasCommandModifier(event) && !event.altKey && !event.shiftKey && event.key === "/";
  }
  return eventMatchesShortcutAction(event, "showShortcuts");
}

export function KeyboardShortcutsDialog() {
  const [open, setOpen] = useState(false);

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (isShowShortcutsHotkey(e)) {
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
