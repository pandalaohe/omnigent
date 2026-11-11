// Shared catalog for the live configurable keyboard shortcut reference.

import {
  composerNewLineAlternateKeys,
  ADDRESS_BAR_KEYS,
  ARCHIVE_SESSION_KEYS,
  CLOSE_KEYS,
  CLOSE_WINDOW_KEYS,
  composerNewLineShortcutKeys,
  composerSendShortcutKeys,
  composerSteerAllShortcutKeys,
  ESC_KEY,
  MOD_KEY,
  ALT_KEY,
  FORK_SESSION_KEYS,
  shortcutKeys,
  newSessionShortcutKeys,
  NEXT_TAB_KEYS,
  PIN_SESSION_KEYS,
  PREVIOUS_TAB_KEYS,
  QUIT_KEYS,
  RELOAD_KEYS,
  REOPEN_TAB_KEYS,
  settingsShortcutKeys,
} from "@/components/KeyboardShortcut";
import {
  currentShortcutPlatform,
  isShortcutActionEnabled,
  resolveShortcutBindings,
  shortcutBindingLabels,
  type ShortcutActionId,
} from "@/lib/keyboardShortcutPreferences";
/** What the current runtime supports, which decides the rows that apply. */
export interface ShortcutCatalogContext {
  /** A native shell, where chords a browser reserves (⌘-digit, ⌘B) are free. */
  native: boolean;
  electron: boolean;
  /** The shell hosts browser tabs in the workspace rail. */
  browser: boolean;
  /** The composer sends on ⌘/Ctrl+Enter instead of Enter. */
  submitWithModEnter: boolean;
  /** Touch or narrow layout, where Enter inserts a newline instead of sending. */
  preventsKeyboardSubmit: boolean;
  // Server features that add rows.
  contentSearch: boolean;
  grids: boolean;
  splitTerminal: boolean;
  siblingSessions: boolean;
  /** The shell's menus hand desktop commands (Close, Pin, Quit) to the page. */
  desktopCommands: boolean;
}

// Same glyph constants the control tooltips use, so the dialog and tooltips match.

export interface Shortcut {
  label: string;
  /** Keys rendered left→right as chips. A chord (held together) or, for the
   *  arrow-pairs, the two interchangeable keys for that action. */
  keys: string[];
  lastKeySeparator?: string;
  /** When set, keys come from the live shortcut layer instead of `keys`. */
  actionId?: ShortcutActionId;
  /** Another chord for the same action, shown after "or". */
  alternateKeys?: string[];
  /** Hidden on touch and narrow layouts, where Enter inserts a newline instead of submitting. */
  desktopOnly?: boolean;
}

export interface ShortcutGroup {
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
      { label: "Open command palette", keys: [], actionId: "commandPalette" },
      { label: "Find a session by name", keys: [], actionId: "findSession" },
      { label: "Open Settings", keys: [], actionId: "openSettings" },
      { label: "Show keyboard shortcuts", keys: [], actionId: "showShortcuts" },
      { label: "Rename current session", keys: [], actionId: "renameSession" },
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
      { label: "Cycle model", keys: [], actionId: "cycleModel" },
      { label: "Cycle reasoning effort", keys: [], actionId: "cycleEffort" },
      { label: "Cycle permission mode (in session)", keys: [], actionId: "cyclePermissionMode" },
      { label: "Decline approval prompt", keys: [ESC_KEY] },
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
      { label: "Toggle annotation mode", keys: [], actionId: "toggleAnnotationMode" },
      { label: "Search workspace contents", keys: [], actionId: "searchWorkspaceContents" },
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
    title: "Panes",
    note: "in grids and split terminals",
    items: [
      { label: "Focus previous pane", keys: [], actionId: "previousPane" },
      { label: "Focus next pane", keys: [], actionId: "nextPane" },
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

// Desktop menu commands (File → Close, Session → Pin/Archive, Quit), present
// only when the shell hands them to the page.
const DESKTOP_COMMAND_SHORTCUTS: Shortcut[] = [
  { label: "Close tab, or leave the session", keys: [...CLOSE_KEYS] },
  { label: "Close window", keys: [...CLOSE_WINDOW_KEYS] },
  { label: "Pin or unpin current session", keys: [...PIN_SESSION_KEYS] },
  { label: "Archive current session", keys: [...ARCHIVE_SESSION_KEYS] },
  { label: "Fork current session", keys: [...FORK_SESSION_KEYS] },
  { label: "Quit Omnigent", keys: [...QUIT_KEYS] },
];

// Desktop menu chords for the workspace rail's tabs.
const DESKTOP_TAB_SHORTCUTS: Shortcut[] = [
  { label: "Show the previous workspace tab", keys: [...PREVIOUS_TAB_KEYS] },
  { label: "Show the next workspace tab", keys: [...NEXT_TAB_KEYS] },
  { label: "Reopen the last closed tab", keys: [...REOPEN_TAB_KEYS] },
];

// Desktop menu chords that act on the browser tab in the workspace rail.
const DESKTOP_BROWSER_SHORTCUTS: Shortcut[] = [
  { label: "Reload the browser tab", keys: [...RELOAD_KEYS] },
  { label: "Focus the browser address bar", keys: [...ADDRESS_BAR_KEYS] },
];

// Numbered sidebar-session jump. The chord is platform-aware (see
// useSessionNumberHotkeys): plain Cmd/Ctrl+digit in the Electron shell, but
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
export function shortcutGroupsFor({
  native,
  electron,
  browser,
  submitWithModEnter,
  preventsKeyboardSubmit,
  contentSearch,
  grids,
  splitTerminal,
  desktopCommands,
  siblingSessions,
}: ShortcutCatalogContext): ShortcutGroup[] {
  const groups = SHORTCUT_GROUPS.map((group) => {
    if (group.title === "General") {
      return {
        ...group,
        items: [
          { label: "Start a new session", keys: newSessionShortcutKeys(native) },
          ...(siblingSessions
            ? [{ label: "Add nested agent", keys: [MOD_KEY, ALT_KEY, "A"] }]
            : []),
          // The desktop menu's Settings item owns ⌘, there.
          ...group.items.map((item) =>
            item.label === "Open Settings"
              ? { ...item, keys: settingsShortcutKeys(desktopCommands) }
              : item,
          ),
          ...(desktopCommands ? DESKTOP_COMMAND_SHORTCUTS : []),
        ],
      };
    }
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
            alternateKeys: composerNewLineAlternateKeys(submitWithModEnter),
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
          .filter(
            (item) =>
              // ⌘T is the desktop menu's New Browser Tab, so older shells have no chord.
              ((browser && desktopCommands) || item.label !== "Open a new browser tab") &&
              (contentSearch || item.label !== "Search workspace contents"),
          )
          .map((item) =>
            item.label === "Select a workspace tab"
              ? {
                  ...item,
                  keys: [...shortcutKeys("toggleWorkspaceSidebar"), contentSearch ? "1…5" : "1…4"],
                }
              : item,
          )
          .concat(
            desktopCommands ? DESKTOP_TAB_SHORTCUTS : [],
            desktopCommands && browser ? DESKTOP_BROWSER_SHORTCUTS : [],
          ),
      };
    }
    if (group.title === "Panes") {
      return {
        ...group,
        items: [
          ...(grids || splitTerminal ? group.items : []),
          ...(splitTerminal
            ? [
                {
                  label: "Split terminal right",
                  keys: [],
                  actionId: "splitTerminalColumns" as const,
                },
                { label: "Split terminal down", keys: [], actionId: "splitTerminalRows" as const },
              ]
            : []),
        ],
      };
    }
    return group;
  });
  return groups.filter((group) => group.items.length > 0);
}
