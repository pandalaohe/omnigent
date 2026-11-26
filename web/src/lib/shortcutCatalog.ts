// Apply configurable Omnigent bindings to the shared shortcut page.
import {
  composerNewLineAlternateKeys,
  composerNewLineShortcutKeys,
  composerSendShortcutKeys,
  composerSteerAllShortcutKeys,
  newSessionShortcutKeys,
  settingsShortcutKeys,
  shortcutKeys,
} from "@/components/KeyboardShortcut";
import {
  hasCustomShortcutBindings,
  isShortcutActionEnabled,
  resolveShortcutBindings,
  shortcutBindingLabels,
  type ShortcutActionId,
} from "@/lib/keyboardShortcutPreferences";
import {
  shortcutPageGroups,
  type ShortcutPageContext,
  type ShortcutPageGroup,
  type ShortcutPageRow,
} from "@/lib/shortcuts/shortcutPage";

export type ShortcutCatalogContext = ShortcutPageContext;
type ShortcutRow = ShortcutPageRow & { lastKeySeparator?: string };
type ShortcutGroup = Omit<ShortcutPageGroup, "items"> & { items: ShortcutRow[] };

const configurableActions: Partial<Record<string, ShortcutActionId>> = {
  "Open command palette": "commandPalette",
  "Find a session by name": "findSession",
  "Show keyboard shortcuts": "showShortcuts",
  "Rename current session": "renameSession",
  "Recall previous prompt": "recallPreviousPrompt",
  "Recall next prompt": "recallNextPrompt",
  "Accept approval prompt": "approvePrompt",
  "Open model picker": "openModelPicker",
  "Focus chat input": "focusComposer",
  "Toggle voice dictation": "voiceDictation",
  "Stop response": "stopResponse",
  "Cycle model": "cycleModel",
  "Cycle reasoning effort": "cycleEffort",
  "Cycle permission mode (in session)": "cyclePermissionMode",
  "Switch recent sessions": "recentSessions",
  "Toggle Chat / Terminal view": "toggleViewMode",
  "Toggle conversations sidebar": "toggleConversationsSidebar",
  "Toggle workspace sidebar": "toggleWorkspaceSidebar",
  "Search workspace contents": "searchWorkspaceContents",
  "Open a new browser tab": "newBrowserTab",
  "Open a new shell": "newShell",
  "Focus previous pane": "previousPane",
  "Focus next pane": "nextPane",
  "Split terminal right": "splitTerminalColumns",
  "Split terminal down": "splitTerminalRows",
  "Apply highlighted command": "applySuggestion",
  "Dismiss menu": "dismissSuggestions",
};

function configurableRow(row: ShortcutPageRow, context: ShortcutCatalogContext): ShortcutRow[] {
  const { label } = row;
  switch (label) {
    case "Start a new session":
      return [
        {
          ...row,
          keys:
            !isShortcutActionEnabled("newSession") || hasCustomShortcutBindings("newSession")
              ? shortcutKeys("newSession")
              : newSessionShortcutKeys(context.native),
        },
      ];
    case "Open Settings":
      return [
        {
          ...row,
          keys:
            !isShortcutActionEnabled("openSettings") || hasCustomShortcutBindings("openSettings")
              ? shortcutKeys("openSettings")
              : settingsShortcutKeys(context.desktopCommands),
        },
      ];
    case "Send message":
      return [{ ...row, keys: composerSendShortcutKeys(context.submitWithModEnter) }];
    case "Send now, with all queued messages":
      return [{ ...row, keys: composerSteerAllShortcutKeys(context.submitWithModEnter) }];
    case "New line in message":
      return [
        {
          ...row,
          keys: composerNewLineShortcutKeys(context.submitWithModEnter),
          alternateKeys: composerNewLineAlternateKeys(context.submitWithModEnter),
        },
      ];
    // These keys are listed in the Question cards group with their real scope.
    case "Next question, or submit all answers":
    case "Pick a question option by number":
    case "Cancel pending question":
      return [];
    case "Jump to a sidebar session (1–10)":
      return [
        {
          label: "Jump to pinned session (1–10)",
          keys: isShortcutActionEnabled("pinnedSession")
            ? resolveShortcutBindings("pinnedSession", undefined, {
                nativeShell: context.native,
              }).flatMap((binding) => shortcutBindingLabels(binding))
            : [],
        },
      ];
    case "Go back":
    case "Go forward": {
      // Session stepping claims these chords when it uses the same binding.
      const sessionAction = label === "Go back" ? "previousSession" : "nextSession";
      return row.keys.join("+") === shortcutKeys(sessionAction).join("+") ? [] : [row];
    }
    case "Select a workspace tab":
      return [
        {
          ...row,
          keys: [...shortcutKeys("toggleWorkspaceSidebar"), context.contentSearch ? "1…5" : "1…4"],
          lastKeySeparator: "+",
        },
      ];
    case "Navigate suggestions":
      return [
        {
          ...row,
          keys: [...shortcutKeys("previousSuggestion"), ...shortcutKeys("nextSuggestion")],
        },
      ];
    default: {
      const action = configurableActions[label];
      return [
        {
          ...row,
          label: label === "Toggle workspace sidebar" ? "Focus or close workspace sidebar" : label,
          keys: action ? shortcutKeys(action) : row.keys,
        },
      ];
    }
  }
}

export function shortcutGroupsFor(context: ShortcutCatalogContext): ShortcutGroup[] {
  const groups: ShortcutGroup[] = shortcutPageGroups(context).map((group) => {
    const items = group.items.flatMap((row) => configurableRow(row, context));
    if (group.title === "Navigation") {
      items.splice(
        items.findIndex((row) => row.label === "Jump to pinned session (1–10)"),
        0,
        { label: "Previous session", keys: shortcutKeys("previousSession") },
        { label: "Next session", keys: shortcutKeys("nextSession") },
      );
    }
    if (group.title === "View") {
      items.push({ label: "Toggle annotation mode", keys: shortcutKeys("toggleAnnotationMode") });
    }
    return { ...group, items };
  });
  groups.push({
    title: "Question cards",
    note: "while a question card has focus",
    items: [
      { label: "Focus question card", keys: shortcutKeys("focusQuestionCard") },
      {
        label: "Move between options",
        keys: [
          ...shortcutKeys("questionCardPreviousOption"),
          ...shortcutKeys("questionCardNextOption"),
        ],
      },
      { label: "Select option", keys: shortcutKeys("questionCardSelectOption") },
      { label: "Next question / submit", keys: shortcutKeys("questionCardNextOrSubmit") },
      {
        label: "Previous / next question",
        keys: [
          ...shortcutKeys("questionCardPreviousQuestion"),
          ...shortcutKeys("questionCardNextQuestion"),
        ],
      },
      { label: "Leave card", keys: shortcutKeys("questionCardLeave") },
      { label: "Cancel question", keys: shortcutKeys("questionCardCancel") },
      { label: "Cancel & interrupt", keys: shortcutKeys("questionCardCancelAndInterrupt") },
    ],
  });
  return groups
    .map((group) => ({ ...group, items: group.items.filter((row) => row.keys.length > 0) }))
    .filter((group) => group.items.length > 0);
}
