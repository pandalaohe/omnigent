import { useSyncExternalStore } from "react";
import { dispatchArchiveSession, dispatchPollSessions } from "@/hooks/useSessionPollingHotkeys";
import { dispatchSelectWorkspaceTab } from "@/hooks/useWorkspaceTabHotkeys";
import {
  DEFAULT_SHORTCUT_DEFINITIONS,
  currentShortcutPlatform,
  resolveShortcutBindings,
  type ShortcutActionId,
  type ShortcutChord,
} from "@/lib/keyboardShortcutPreferences";
import {
  bindingSupportsRepeat as terminalKeyBindingSupportsRepeat,
  defaultTerminalButtons,
  normalizeChord,
  normalizeTerminalButtons,
  type TerminalBar,
  type TerminalBarKey,
  type TerminalKeyRowItem,
  type TerminalStickyModifiers,
} from "@/lib/terminalKeyBar";
import { queueUserPreferencePatch } from "@/lib/userPreferencesSync";

export {
  DEFAULT_TERMINAL_KEY_ITEMS,
  EMPTY_STICKY_MODIFIERS,
  barHeightPx,
  canonicalTerminalFields,
  defaultTerminalBar,
  defaultTerminalButtons,
  flattenBar,
  legacyFingerprint,
  migrateLegacy,
  normalizeChord,
  normalizeTerminalButtons,
  packRow,
  projectLegacy,
  reconcileLegacy,
  reduceSticky,
  resolveTerminalBar,
  validateBar,
  type TerminalBar,
  type TerminalBarKey,
  type TerminalBarRow,
  type TerminalKeyIcon,
  type TerminalKeyRowItem,
  type TerminalStickyModifiers,
  type TerminalStickyState,
} from "@/lib/terminalKeyBar";

export type MobileAssistantSoftKey =
  | "escape"
  | "tab"
  | "arrowUp"
  | "arrowDown"
  | "arrowLeft"
  | "arrowRight"
  | "enter"
  | "home"
  | "end";

/** Legacy fixed action ids retained for a lossless version-1 migration. */
export type MobileAssistantAction = MobileAssistantSoftKey | "pollSessions" | "archiveSession";

export interface MobileAssistantActionDefinition {
  id: MobileAssistantAction;
  label: string;
  shortLabel: string;
  description?: string;
  key?: string;
  code?: string;
}

export const MOBILE_ASSISTANT_ACTIONS: MobileAssistantActionDefinition[] = [
  { id: "escape", label: "Escape", shortLabel: "Esc", key: "Escape", code: "Escape" },
  { id: "tab", label: "Tab", shortLabel: "Tab", key: "Tab", code: "Tab" },
  { id: "arrowUp", label: "Arrow up", shortLabel: "↑", key: "ArrowUp", code: "ArrowUp" },
  { id: "arrowDown", label: "Arrow down", shortLabel: "↓", key: "ArrowDown", code: "ArrowDown" },
  { id: "arrowLeft", label: "Arrow left", shortLabel: "←", key: "ArrowLeft", code: "ArrowLeft" },
  {
    id: "arrowRight",
    label: "Arrow right",
    shortLabel: "→",
    key: "ArrowRight",
    code: "ArrowRight",
  },
  { id: "enter", label: "Enter", shortLabel: "↵", key: "Enter", code: "Enter" },
  {
    id: "pollSessions",
    label: "Poll sessions",
    shortLabel: "Poll",
    description: DEFAULT_SHORTCUT_DEFINITIONS.pollSessions.note,
  },
  { id: "archiveSession", label: "Archive session", shortLabel: "Archive" },
];

export const MOBILE_ASSISTANT_DIRECT_ACTIONS = {
  openTerminals: { label: "Terminals" },
  openFiles: { label: "Files" },
  openChanges: { label: "Changes" },
  openAgents: { label: "Agents" },
  openSideChats: { label: "Side chats" },
};
export type MobileAssistantDirectAction =
  keyof typeof MOBILE_ASSISTANT_DIRECT_ACTIONS | "stopResponse" | "renameSession" | "focusComposer";
export type MobileAssistantActionId =
  ShortcutActionId | keyof typeof MOBILE_ASSISTANT_DIRECT_ACTIONS;
export const MOBILE_ASSISTANT_ACTION_EVENT = "omnigent:mobile-assistant-action";
export const MOBILE_ASSISTANT_ACTION_DEFINITIONS: Record<
  MobileAssistantActionId,
  { label: string; note?: string }
> = { ...DEFAULT_SHORTCUT_DEFINITIONS, ...MOBILE_ASSISTANT_DIRECT_ACTIONS };

export type MobileAssistantButtonBinding =
  | { kind: "shortcut"; actionId: MobileAssistantActionId }
  | { kind: "key"; chord: ShortcutChord }
  | { kind: "text"; text: string; submit?: boolean };

export const MOBILE_ASSISTANT_ICONS = [
  { id: "escape", label: "Escape" },
  { id: "tab", label: "Tab" },
  { id: "arrow-up", label: "Arrow up" },
  { id: "arrow-down", label: "Arrow down" },
  { id: "arrow-left", label: "Arrow left" },
  { id: "arrow-right", label: "Arrow right" },
  { id: "enter", label: "Enter" },
  { id: "backspace", label: "Backspace" },
  { id: "delete", label: "Delete" },
  { id: "page-up", label: "Page up" },
  { id: "page-down", label: "Page down" },
  { id: "keyboard", label: "Keyboard" },
  { id: "refresh", label: "Refresh" },
  { id: "archive", label: "Archive" },
  { id: "terminal", label: "Terminal" },
  { id: "command", label: "Command" },
] as const;

export type MobileAssistantIcon = (typeof MOBILE_ASSISTANT_ICONS)[number]["id"];

export interface MobileAssistantButton {
  id: string;
  label: string;
  binding: MobileAssistantButtonBinding;
  display?: "text" | "icon";
  icon?: MobileAssistantIcon;
  repeat?: boolean;
}

export type MobileAssistantDockEdge = "left" | "right" | "top" | "bottom";

export interface MobileAssistantDock {
  edge: MobileAssistantDockEdge;
  /** Normalized offset along the selected edge. */
  offset: number;
}

export type TerminalBarModifier = "ctrl" | "alt" | "shift";
export interface MobileAssistantPreferences {
  version: 2;
  enabled: boolean;
  buttons: MobileAssistantButton[];
  terminalButtons?: TerminalKeyRowItem[];
  terminalKeysEnabled?: boolean;
  terminalSecondRow?: boolean;
  terminalBar?: TerminalBar;
  terminalBarPreset?: TerminalBar;
  terminalLegacyBasis?: string;
  chatActionsRevision?: 1;
  position?: { x: number; y: number };
  dock?: MobileAssistantDock;
}

export type ResolvedMobileAssistantPreferences = MobileAssistantPreferences & {
  terminalKeysEnabled: boolean;
  terminalSecondRow: boolean;
};

export const MOBILE_ASSISTANT_CHAT_ACTION_IDS: readonly MobileAssistantActionId[] = [
  "openTerminals",
  "stopResponse",
  "openFiles",
  "openChanges",
  "openAgents",
  "openSideChats",
  "approvePrompt",
  "pollSessions",
  "previousSession",
  "nextSession",
  "archiveSession",
  "newSession",
  "findSession",
  "commandPalette",
  "openSettings",
  "renameSession",
  "openModelPicker",
  "cycleModel",
  "cycleEffort",
  "cyclePermissionMode",
  "focusComposer",
  "voiceDictation",
  "toggleViewMode",
  "toggleConversationsSidebar",
  "newShell",
];

export function visibleChatButtons(
  preferences: MobileAssistantPreferences,
): MobileAssistantButton[] {
  return preferences.buttons.filter(
    (button) =>
      button.binding.kind !== "shortcut" ||
      MOBILE_ASSISTANT_CHAT_ACTION_IDS.includes(button.binding.actionId),
  );
}

interface LegacyMobileAssistantPreferences {
  version: 1;
  enabled: boolean;
  actions: MobileAssistantAction[];
  position?: { x: number; y: number };
}

export const MOBILE_ASSISTANT_MAX_BUTTONS = 10;
export const MOBILE_ASSISTANT_STORAGE_KEY = "omnigent:mobile-assistant-preferences";
export const MOBILE_ASSISTANT_DEVICE_STORAGE_KEY = "omnigent:mobile-assistant-device-state";
export const MOBILE_ASSISTANT_CHANGED_EVENT = "omnigent:mobile-assistant-changed";
export const TERMINAL_SOFT_KEY_EVENT = "omnigent:terminal-soft-key";

export interface TerminalBarTarget {
  isCurrent: () => boolean;
  press: (key: TerminalBarKey, gesture: "tap" | "longPress" | "repeat") => void;
}

export interface TerminalSoftKeyEventDetail {
  action: MobileAssistantSoftKey;
  /** Raw terminal payload for modified key chords or phrase buttons. */
  data?: string;
  handled: boolean;
  preferredTarget?: HTMLElement | null;
  terminalBarKey?: TerminalBarKey;
  terminalGesture?: "tap" | "longPress" | "repeat";
  onTerminalTarget?: (target: TerminalBarTarget) => void;
  onStickyChanged?: (next: TerminalStickyModifiers) => void;
  onStickySubscribed?: (unsubscribe: () => void) => void;
  candidates?: { focused: boolean; ownsPreferredTarget: boolean; send: () => void }[];
}

const SOFT_KEY_DEFAULTS: Record<MobileAssistantSoftKey, ShortcutChord> = {
  escape: { code: "Escape", modifiers: [] },
  tab: { code: "Tab", modifiers: [] },
  arrowUp: { code: "ArrowUp", modifiers: [] },
  arrowDown: { code: "ArrowDown", modifiers: [] },
  arrowLeft: { code: "ArrowLeft", modifiers: [] },
  arrowRight: { code: "ArrowRight", modifiers: [] },
  enter: { code: "Enter", modifiers: [] },
  home: { code: "Home", modifiers: [] },
  end: { code: "End", modifiers: [] },
};

export interface TypableTerminalsSnapshot {
  count: number;
  withBar: number;
}

let nextTypableTerminalId = 1;
const typableTerminals = new Map<number, boolean>();
const typableTerminalListeners = new Set<() => void>();
let typableTerminalsSnapshot: TypableTerminalsSnapshot = { count: 0, withBar: 0 };

function updateTypableTerminalsSnapshot(): void {
  let withBar = 0;
  for (const hasBar of typableTerminals.values()) {
    if (hasBar) withBar += 1;
  }
  const count = typableTerminals.size;
  if (typableTerminalsSnapshot.count === count && typableTerminalsSnapshot.withBar === withBar) {
    return;
  }
  typableTerminalsSnapshot = { count, withBar };
  for (const listener of typableTerminalListeners) {
    listener();
  }
}

export function registerTypableTerminal(hasBar: boolean): () => void {
  const id = nextTypableTerminalId++;
  typableTerminals.set(id, hasBar);
  updateTypableTerminalsSnapshot();
  return () => {
    if (!typableTerminals.delete(id)) return;
    updateTypableTerminalsSnapshot();
  };
}

function subscribeTypableTerminals(listener: () => void): () => void {
  typableTerminalListeners.add(listener);
  return () => {
    typableTerminalListeners.delete(listener);
  };
}

function getTypableTerminalsSnapshot(): TypableTerminalsSnapshot {
  return typableTerminalsSnapshot;
}

const SERVER_TYPABLE_TERMINALS_SNAPSHOT: TypableTerminalsSnapshot = {
  count: 0,
  withBar: 0,
};

export function useTypableTerminals(): TypableTerminalsSnapshot {
  return useSyncExternalStore(
    subscribeTypableTerminals,
    getTypableTerminalsSnapshot,
    () => SERVER_TYPABLE_TERMINALS_SNAPSHOT,
  );
}

const LEGACY_DEFAULT_BUTTONS: MobileAssistantButton[] = [
  {
    id: "default-escape",
    label: "Esc",
    icon: "escape",
    binding: { kind: "key", chord: SOFT_KEY_DEFAULTS.escape },
  },
  {
    id: "default-tab",
    label: "Tab",
    icon: "tab",
    binding: { kind: "key", chord: SOFT_KEY_DEFAULTS.tab },
  },
  {
    id: "default-up",
    label: "↑",
    display: "icon",
    icon: "arrow-up",
    repeat: true,
    binding: { kind: "key", chord: SOFT_KEY_DEFAULTS.arrowUp },
  },
  {
    id: "default-down",
    label: "↓",
    display: "icon",
    icon: "arrow-down",
    repeat: true,
    binding: { kind: "key", chord: SOFT_KEY_DEFAULTS.arrowDown },
  },
  {
    id: "default-enter",
    label: "↵",
    display: "icon",
    icon: "enter",
    binding: { kind: "key", chord: SOFT_KEY_DEFAULTS.enter },
  },
  {
    id: "default-poll",
    label: "Poll",
    display: "icon",
    icon: "refresh",
    binding: { kind: "shortcut", actionId: "pollSessions" },
  },
  {
    id: "default-archive",
    label: "Archive",
    display: "icon",
    icon: "archive",
    binding: { kind: "shortcut", actionId: "archiveSession" },
  },
];

function previousChatDefaults(): MobileAssistantButton[] {
  return [
    {
      id: "default-approve",
      label: "Approve",
      binding: { kind: "shortcut", actionId: "approvePrompt" },
    },
    {
      id: "default-poll",
      label: "Poll",
      display: "icon",
      icon: "refresh",
      binding: { kind: "shortcut", actionId: "pollSessions" },
    },
    {
      id: "default-previous",
      label: "Previous",
      binding: { kind: "shortcut", actionId: "previousSession" },
    },
    { id: "default-next", label: "Next", binding: { kind: "shortcut", actionId: "nextSession" } },
    {
      id: "default-archive",
      label: "Archive",
      display: "icon",
      icon: "archive",
      binding: { kind: "shortcut", actionId: "archiveSession" },
    },
  ];
}

export function defaultMobileAssistantButtons(): MobileAssistantButton[] {
  return [
    {
      id: "default-terminals",
      label: "Terminals",
      icon: "terminal",
      display: "icon",
      binding: { kind: "shortcut", actionId: "openTerminals" },
    },
    {
      id: "default-stop",
      label: "Stop",
      icon: "escape",
      display: "icon",
      binding: { kind: "shortcut", actionId: "stopResponse" },
    },
    ...previousChatDefaults(),
  ];
}

function upgradeChatActions(
  sets: Pick<ResolvedMobileAssistantPreferences, "buttons" | "terminalButtons">,
  revision?: number,
) {
  if (
    revision === 1 ||
    sets.buttons.some(
      (button) => button.binding.kind === "shortcut" && button.binding.actionId === "openTerminals",
    )
  )
    return sets;
  const stock =
    JSON.stringify(sets.buttons) === JSON.stringify(previousChatDefaults().map(normalizeButton));
  return {
    ...sets,
    buttons: stock
      ? defaultMobileAssistantButtons()
      : [defaultMobileAssistantButtons()[0], ...sets.buttons],
  };
}

function defaults(): ResolvedMobileAssistantPreferences {
  return {
    version: 2,
    enabled: true,
    buttons: defaultMobileAssistantButtons(),
    terminalButtons: defaultTerminalButtons(),
    terminalKeysEnabled: true,
    terminalSecondRow: true,
    chatActionsRevision: 1,
  };
}

function isAction(value: unknown): value is MobileAssistantAction {
  return MOBILE_ASSISTANT_ACTIONS.some((action) => action.id === value);
}

function isShortcutAction(value: unknown): value is MobileAssistantActionId {
  return typeof value === "string" && value in MOBILE_ASSISTANT_ACTION_DEFINITIONS;
}

function isIcon(value: unknown): value is MobileAssistantIcon {
  return MOBILE_ASSISTANT_ICONS.some((icon) => icon.id === value);
}

export function mobileAssistantBindingSupportsRepeat(
  binding: MobileAssistantButtonBinding,
): boolean {
  return (
    binding.kind === "key" &&
    terminalKeyBindingSupportsRepeat({ kind: "key", chord: binding.chord })
  );
}

function normalizePosition(value: unknown): { x: number; y: number } | undefined {
  if (!value || typeof value !== "object") return undefined;
  const point = value as { x?: unknown; y?: unknown };
  return Number.isFinite(point.x) &&
    Number.isFinite(point.y) &&
    (point.x as number) >= 0 &&
    (point.x as number) <= 1 &&
    (point.y as number) >= 0 &&
    (point.y as number) <= 1
    ? { x: point.x as number, y: point.y as number }
    : undefined;
}

function normalizeButton(value: unknown, index: number): MobileAssistantButton | null {
  if (!value || typeof value !== "object") return null;
  const raw = value as {
    id?: unknown;
    label?: unknown;
    binding?: unknown;
    display?: unknown;
    icon?: unknown;
    repeat?: unknown;
  };
  if (!raw.binding || typeof raw.binding !== "object") return null;
  const binding = raw.binding as {
    kind?: unknown;
    actionId?: unknown;
    chord?: unknown;
    text?: unknown;
    submit?: unknown;
  };
  let normalizedBinding: MobileAssistantButtonBinding | null = null;
  if (binding.kind === "shortcut" && isShortcutAction(binding.actionId)) {
    normalizedBinding = { kind: "shortcut", actionId: binding.actionId };
  } else if (binding.kind === "key") {
    const chord = normalizeChord(binding.chord);
    if (chord) normalizedBinding = { kind: "key", chord };
  } else if (
    binding.kind === "text" &&
    typeof binding.text === "string" &&
    binding.text.length > 0 &&
    binding.text.length <= 2000
  ) {
    normalizedBinding = {
      kind: "text",
      text: binding.text,
      submit: binding.submit === true || undefined,
    };
  }
  if (!normalizedBinding) return null;
  const rawId = typeof raw.id === "string" ? raw.id.trim().slice(0, 80) : "";
  const rawLabel = typeof raw.label === "string" ? raw.label.trim().slice(0, 24) : "";
  const icon = isIcon(raw.icon) ? raw.icon : undefined;
  const display =
    raw.display === "icon" && icon ? "icon" : raw.display === "text" ? "text" : undefined;
  return {
    id: rawId || `button-${index + 1}`,
    label: rawLabel || mobileAssistantBindingLabel(normalizedBinding),
    binding: normalizedBinding,
    ...(display ? { display } : {}),
    ...(icon ? { icon } : {}),
    ...(raw.repeat === true && mobileAssistantBindingSupportsRepeat(normalizedBinding)
      ? { repeat: true }
      : {}),
  };
}

function normalizeDock(value: unknown): MobileAssistantDock | undefined {
  if (!value || typeof value !== "object") return undefined;
  const dock = value as { edge?: unknown; offset?: unknown };
  if (
    !["left", "right", "top", "bottom"].includes(dock.edge as string) ||
    !Number.isFinite(dock.offset)
  )
    return undefined;
  return {
    edge: dock.edge as MobileAssistantDockEdge,
    offset: Math.min(1, Math.max(0, dock.offset as number)),
  };
}

function legacyActionToButton(action: MobileAssistantAction, index: number): MobileAssistantButton {
  const definition = MOBILE_ASSISTANT_ACTIONS.find((candidate) => candidate.id === action);
  if (action === "pollSessions" || action === "archiveSession") {
    return {
      id: `legacy-${action}-${index}`,
      label: definition?.shortLabel ?? action,
      binding: { kind: "shortcut", actionId: action },
    };
  }
  return {
    id: `legacy-${action}-${index}`,
    label: definition?.shortLabel ?? action,
    binding: { kind: "key", chord: SOFT_KEY_DEFAULTS[action] },
  };
}

function splitLegacyButtons(
  buttons: MobileAssistantButton[],
  stock: boolean,
  fromVersionOne = false,
): Pick<ResolvedMobileAssistantPreferences, "buttons" | "terminalButtons"> {
  if (stock)
    return { buttons: defaultMobileAssistantButtons(), terminalButtons: defaultTerminalButtons() };
  const terminalButtons = defaultTerminalButtons();
  for (const button of buttons) {
    if (button.binding.kind !== "key") continue;
    const stockKey = LEGACY_DEFAULT_BUTTONS.find(
      (candidate) =>
        candidate.binding.kind === "key" &&
        (JSON.stringify(normalizeButton(candidate, 0)) === JSON.stringify(button) ||
          (fromVersionOne && JSON.stringify(candidate.binding) === JSON.stringify(button.binding))),
    );
    if (stockKey) continue;
    terminalButtons.push({
      id: `moved-${button.id}`,
      label: button.label,
      kind: "key",
      chord: button.binding.chord,
      ...(button.repeat ? { repeat: true } : {}),
    });
  }
  return { buttons: buttons.filter((button) => button.binding.kind !== "key"), terminalButtons };
}

export function readMobileAssistantPreferences(): ResolvedMobileAssistantPreferences {
  if (typeof window === "undefined") return defaults();
  try {
    const raw = window.localStorage.getItem(MOBILE_ASSISTANT_STORAGE_KEY);
    const rawDeviceState = window.localStorage.getItem(MOBILE_ASSISTANT_DEVICE_STORAGE_KEY);
    const parsedDeviceState = rawDeviceState
      ? (JSON.parse(rawDeviceState) as { position?: unknown; dock?: unknown })
      : null;
    const devicePosition = normalizePosition(parsedDeviceState?.position);
    const deviceDock = normalizeDock(parsedDeviceState?.dock);
    if (!raw) {
      return {
        ...defaults(),
        ...(devicePosition ? { position: devicePosition } : {}),
        ...(deviceDock ? { dock: deviceDock } : {}),
      };
    }
    const parsed = JSON.parse(raw) as Partial<MobileAssistantPreferences> &
      Partial<LegacyMobileAssistantPreferences>;
    if (
      parsed.version === 1 &&
      typeof parsed.enabled === "boolean" &&
      Array.isArray(parsed.actions)
    ) {
      const actions = [...new Set(parsed.actions.filter(isAction))].slice(
        0,
        MOBILE_ASSISTANT_MAX_BUTTONS,
      );
      return {
        ...defaults(),
        version: 2,
        enabled: parsed.enabled,
        ...upgradeChatActions(
          splitLegacyButtons(
            actions.map(legacyActionToButton),
            JSON.stringify(actions) ===
              JSON.stringify(MOBILE_ASSISTANT_ACTIONS.map((action) => action.id)) ||
              JSON.stringify(actions) ===
                JSON.stringify([
                  "escape",
                  "tab",
                  "arrowUp",
                  "arrowDown",
                  "enter",
                  "pollSessions",
                  "archiveSession",
                ]),
            true,
          ),
        ),
        ...((devicePosition ?? normalizePosition(parsed.position))
          ? { position: devicePosition ?? normalizePosition(parsed.position) }
          : {}),
      };
    }
    if (
      parsed.version !== 2 ||
      typeof parsed.enabled !== "boolean" ||
      !Array.isArray(parsed.buttons)
    )
      return defaults();
    const ids = new Set<string>();
    const buttons = parsed.buttons
      .map(normalizeButton)
      .filter((button): button is MobileAssistantButton => {
        if (!button || ids.has(button.id)) return false;
        ids.add(button.id);
        return true;
      });
    return {
      ...defaults(),
      version: 2,
      enabled: parsed.enabled,
      ...upgradeChatActions(
        Array.isArray(parsed.terminalButtons)
          ? { buttons, terminalButtons: normalizeTerminalButtons(parsed.terminalButtons) }
          : splitLegacyButtons(
              buttons,
              JSON.stringify(buttons) ===
                JSON.stringify(LEGACY_DEFAULT_BUTTONS.map(normalizeButton)),
            ),
        parsed.chatActionsRevision,
      ),
      terminalKeysEnabled: parsed.terminalKeysEnabled !== false,
      terminalSecondRow: parsed.terminalSecondRow !== false,
      ...(parsed.terminalBar !== undefined && !Array.isArray(parsed.terminalButtons)
        ? { terminalButtons: undefined }
        : {}),
      ...(parsed.terminalBar !== undefined ? { terminalBar: parsed.terminalBar } : {}),
      ...(parsed.terminalBarPreset !== undefined
        ? { terminalBarPreset: parsed.terminalBarPreset }
        : {}),
      ...(typeof parsed.terminalLegacyBasis === "string"
        ? { terminalLegacyBasis: parsed.terminalLegacyBasis }
        : {}),
      ...((devicePosition ?? normalizePosition(parsed.position))
        ? { position: devicePosition ?? normalizePosition(parsed.position) }
        : {}),
      ...((deviceDock ?? normalizeDock(parsed.dock))
        ? { dock: deviceDock ?? normalizeDock(parsed.dock) }
        : {}),
    };
  } catch {
    return defaults();
  }
}

export function writeMobileAssistantPreferences(preferences: MobileAssistantPreferences): void {
  if (typeof window === "undefined") return;
  try {
    // Persist only the current schema. Older builds stored a circle direction;
    // direct wheel ordering now owns the visual sequence, so that legacy field
    // must not leak back out when an old object is passed through at runtime.
    const canonical: MobileAssistantPreferences = {
      version: 2,
      enabled: preferences.enabled,
      buttons: preferences.buttons,
      terminalButtons:
        preferences.terminalButtons ??
        (preferences.terminalBar !== undefined ? undefined : defaultTerminalButtons()),
      terminalKeysEnabled: preferences.terminalKeysEnabled !== false,
      terminalSecondRow: preferences.terminalSecondRow !== false,
      ...(preferences.terminalBar !== undefined ? { terminalBar: preferences.terminalBar } : {}),
      ...(preferences.terminalBarPreset !== undefined
        ? { terminalBarPreset: preferences.terminalBarPreset }
        : {}),
      ...(typeof preferences.terminalLegacyBasis === "string"
        ? { terminalLegacyBasis: preferences.terminalLegacyBasis }
        : {}),
      chatActionsRevision: 1,
      ...(preferences.position ? { position: preferences.position } : {}),
      ...(preferences.dock ? { dock: preferences.dock } : {}),
    };
    window.localStorage.setItem(MOBILE_ASSISTANT_STORAGE_KEY, JSON.stringify(canonical));
    const deviceState = {
      ...(canonical.position ? { position: canonical.position } : {}),
      ...(canonical.dock ? { dock: canonical.dock } : {}),
    };
    if (Object.keys(deviceState).length > 0) {
      window.localStorage.setItem(MOBILE_ASSISTANT_DEVICE_STORAGE_KEY, JSON.stringify(deviceState));
    } else {
      window.localStorage.removeItem(MOBILE_ASSISTANT_DEVICE_STORAGE_KEY);
    }
    queueUserPreferencePatch("mobile_assistant", canonical);
    window.dispatchEvent(new Event(MOBILE_ASSISTANT_CHANGED_EVENT));
  } catch {
    // Mobile controls retain their safe defaults when storage is unavailable.
  }
}

function subscribeMobileAssistantPreferences(listener: () => void): () => void {
  window.addEventListener(MOBILE_ASSISTANT_CHANGED_EVENT, listener);
  window.addEventListener("storage", listener);
  return () => {
    window.removeEventListener(MOBILE_ASSISTANT_CHANGED_EVENT, listener);
    window.removeEventListener("storage", listener);
  };
}

export function useMobileAssistantPreferences(): ResolvedMobileAssistantPreferences {
  useSyncExternalStore(
    subscribeMobileAssistantPreferences,
    () => {
      try {
        return window.localStorage.getItem(MOBILE_ASSISTANT_STORAGE_KEY);
      } catch {
        return null;
      }
    },
    () => null,
  );
  return readMobileAssistantPreferences();
}

/** Persist only this device's on-screen placement without syncing button data. */
export function writeMobileAssistantDeviceState(
  placement: Pick<MobileAssistantPreferences, "position" | "dock">,
): void {
  if (typeof window === "undefined") return;
  try {
    const current = readMobileAssistantPreferences();
    const canonical: MobileAssistantPreferences = {
      ...current,
      ...(placement.position ? { position: placement.position } : { position: undefined }),
      ...(placement.dock ? { dock: placement.dock } : { dock: undefined }),
    };
    const deviceState = {
      ...(canonical.position ? { position: canonical.position } : {}),
      ...(canonical.dock ? { dock: canonical.dock } : {}),
    };
    window.localStorage.setItem(MOBILE_ASSISTANT_STORAGE_KEY, JSON.stringify(canonical));
    if (Object.keys(deviceState).length > 0) {
      window.localStorage.setItem(MOBILE_ASSISTANT_DEVICE_STORAGE_KEY, JSON.stringify(deviceState));
    } else {
      window.localStorage.removeItem(MOBILE_ASSISTANT_DEVICE_STORAGE_KEY);
    }
    window.dispatchEvent(new Event(MOBILE_ASSISTANT_CHANGED_EVENT));
  } catch {
    // Placement is optional; keep the assistant usable at its default point.
  }
}

function codeToKey(code: string): string {
  if (/^Key[A-Z]$/.test(code)) return code.slice(3).toLowerCase();
  if (/^Digit[0-9]$/.test(code)) return code.slice(5);
  const labels: Record<string, string> = {
    Escape: "Escape",
    Tab: "Tab",
    Enter: "Enter",
    Space: " ",
    ArrowUp: "ArrowUp",
    ArrowDown: "ArrowDown",
    ArrowLeft: "ArrowLeft",
    ArrowRight: "ArrowRight",
    Backspace: "Backspace",
    Delete: "Delete",
    Slash: "/",
    Backquote: "`",
    BracketLeft: "[",
    BracketRight: "]",
  };
  return labels[code] ?? code;
}

function keyboardEventInit(chord: ShortcutChord): KeyboardEventInit {
  const platform = currentShortcutPlatform();
  const modifiers = new Set(chord.modifiers);
  return {
    key: codeToKey(chord.code),
    code: chord.code,
    ctrlKey: modifiers.has("control") || (modifiers.has("primary") && platform !== "macos"),
    metaKey: modifiers.has("meta") || (modifiers.has("primary") && platform === "macos"),
    altKey: modifiers.has("alt"),
    shiftKey: modifiers.has("shift"),
    bubbles: true,
    cancelable: true,
  };
}

export function softKeyForChord(chord: ShortcutChord): MobileAssistantSoftKey | null {
  if (chord.modifiers.length > 0) return null;
  return (Object.entries(SOFT_KEY_DEFAULTS).find(
    ([, candidate]) => candidate.code === chord.code,
  )?.[0] ?? null) as MobileAssistantSoftKey | null;
}

export function isDomTargetVisible(
  element: HTMLElement | null | undefined,
): element is HTMLElement {
  if (!element?.isConnected) return false;
  if (element.closest('[inert], [aria-hidden="true"], [hidden], .invisible')) {
    return false;
  }
  for (let current: HTMLElement | null = element; current; current = current.parentElement) {
    const style = getComputedStyle(current);
    if (
      style.display === "none" ||
      style.visibility === "hidden" ||
      style.visibility === "collapse"
    ) {
      return false;
    }
  }
  if (typeof element.checkVisibility === "function") {
    return element.checkVisibility({
      checkVisibilityCSS: true,
      visibilityProperty: true,
      contentVisibilityAuto: true,
    } as CheckVisibilityOptions);
  }
  return true;
}

function isXtermElement(element: HTMLElement): boolean {
  return element.classList.contains("xterm-helper-textarea") || Boolean(element.closest(".xterm"));
}

function isEditableDomInput(element: HTMLElement): boolean {
  if (isXtermElement(element)) return false;
  return (
    element instanceof HTMLInputElement ||
    element instanceof HTMLTextAreaElement ||
    element.isContentEditable
  );
}

function preferredTarget(preferredDomTarget?: HTMLElement | null): HTMLElement {
  const activeTarget =
    document.activeElement instanceof HTMLElement && isDomTargetVisible(document.activeElement)
      ? document.activeElement
      : document.body;
  if (!isDomTargetVisible(preferredDomTarget)) return activeTarget;
  return preferredDomTarget;
}

export function terminalDataForChord(
  chord: ShortcutChord,
  applicationCursorKeys = false,
): string | null {
  const platform = currentShortcutPlatform();
  const modifiers = new Set(chord.modifiers);
  const hasPrimary = modifiers.has("primary");
  const hasControl = modifiers.has("control") || (hasPrimary && platform !== "macos");
  const hasMeta = modifiers.has("meta") || (hasPrimary && platform === "macos");
  const hasCtrlOrPrimary = modifiers.has("control") || hasPrimary;
  const hasAlt = modifiers.has("alt");
  const hasShift = modifiers.has("shift");

  if (chord.code === "Tab" && hasShift && !hasCtrlOrPrimary && !hasMeta && !hasAlt) {
    return "\x1b[Z";
  }
  if (chord.code === "Enter") {
    if (hasShift && !hasCtrlOrPrimary && !hasMeta && !hasAlt) return "\x1b[13;2u";
    if (hasAlt && !hasCtrlOrPrimary && !hasMeta && !hasShift) return "\x1b\r";
    if ((hasCtrlOrPrimary || hasMeta) && !hasAlt && !hasShift) return "\r";
  }
  if (chord.code === "Backspace") {
    if (!hasCtrlOrPrimary && !hasMeta && !hasAlt && !hasShift) return "\x7f";
    if (hasAlt && !hasCtrlOrPrimary && !hasMeta && !hasShift) return "\x1b\x7f";
    if (hasMeta && !hasControl && !hasAlt && !hasShift) return "\x15";
    if (hasCtrlOrPrimary && !hasAlt && !hasShift) return "\x17";
  }
  if (
    (chord.code === "ArrowLeft" || chord.code === "ArrowRight") &&
    hasMeta &&
    !hasControl &&
    !hasAlt &&
    !hasShift
  ) {
    return chord.code === "ArrowLeft" ? "\x01" : "\x05";
  }
  if (!hasMeta) {
    const modifier = 1 + Number(hasShift) + 2 * Number(hasAlt) + 4 * Number(hasControl);
    const cursorKeys: Record<string, string> = {
      ArrowUp: "A",
      ArrowDown: "B",
      ArrowRight: "C",
      ArrowLeft: "D",
      Home: "H",
      End: "F",
    };
    const cursor = cursorKeys[chord.code];
    if (Object.hasOwn(cursorKeys, chord.code)) {
      return modifier === 1
        ? `\x1b${applicationCursorKeys ? "O" : "["}${cursor}`
        : `\x1b[1;${modifier}${cursor}`;
    }
    const functionKeys: Record<string, string> = { F1: "P", F2: "Q", F3: "R", F4: "S" };
    const functionKey = functionKeys[chord.code];
    if (Object.hasOwn(functionKeys, chord.code)) {
      return modifier === 1 ? `\x1bO${functionKey}` : `\x1b[1;${modifier}${functionKey}`;
    }
    const tildeKeys: Record<string, number> = {
      Insert: 2,
      Delete: 3,
      PageUp: 5,
      PageDown: 6,
      F5: 15,
      F6: 17,
      F7: 18,
      F8: 19,
      F9: 20,
      F10: 21,
      F11: 23,
      F12: 24,
    };
    const number = tildeKeys[chord.code];
    if (Object.hasOwn(tildeKeys, chord.code)) {
      return `\x1b[${number}${modifier === 1 ? "" : `;${modifier}`}~`;
    }
    if (chord.code === "Space" && modifier === 1) return " ";
  }
  if (hasCtrlOrPrimary && !modifiers.has("meta")) {
    if (/^Key[A-Z]$/.test(chord.code)) {
      const ctrlChar = String.fromCharCode(chord.code.charCodeAt(3) - 64);
      return hasAlt ? `\x1b${ctrlChar}` : ctrlChar;
    }
    const controlPunctuation: Record<string, string> = {
      Space: "\x00",
      BracketLeft: "\x1b",
      Backslash: "\x1c",
      BracketRight: "\x1d",
      Minus: "\x1f",
      Slash: "\x1f",
    };
    if (!hasShift && Object.hasOwn(controlPunctuation, chord.code)) {
      const ctrlChar = controlPunctuation[chord.code];
      return hasAlt ? `\x1b${ctrlChar}` : ctrlChar;
    }
  }
  if (!hasCtrlOrPrimary && !hasMeta) {
    let char: string | null = null;
    if (/^Key[A-Z]$/.test(chord.code)) {
      const letter = chord.code.slice(3);
      char = hasShift ? letter : letter.toLowerCase();
    } else if (/^Digit[0-9]$/.test(chord.code)) {
      const digit = chord.code.slice(5);
      const shiftedDigits: Record<string, string> = {
        "0": ")",
        "1": "!",
        "2": "@",
        "3": "#",
        "4": "$",
        "5": "%",
        "6": "^",
        "7": "&",
        "8": "*",
        "9": "(",
      };
      char = hasShift ? (shiftedDigits[digit] ?? null) : digit;
    } else {
      const punctuation: Record<string, [string, string]> = {
        Slash: ["/", "?"],
        Period: [".", ">"],
        Comma: [",", "<"],
        Minus: ["-", "_"],
        Equal: ["=", "+"],
        Backquote: ["`", "~"],
        BracketLeft: ["[", "{"],
        BracketRight: ["]", "}"],
        Backslash: ["\\", "|"],
        Semicolon: [";", ":"],
        Quote: ["'", '"'],
      };
      const pair = punctuation[chord.code];
      if (Object.hasOwn(punctuation, chord.code)) char = hasShift ? pair[1] : pair[0];
    }
    if (char !== null) return hasAlt ? `\x1b${char}` : char;
  }
  return null;
}

function dispatchToTerminal(
  payload: Pick<
    TerminalSoftKeyEventDetail,
    | "action"
    | "data"
    | "terminalBarKey"
    | "terminalGesture"
    | "onStickyChanged"
    | "onStickySubscribed"
    | "onTerminalTarget"
  >,
  target: HTMLElement,
): boolean {
  const detail: TerminalSoftKeyEventDetail = {
    ...payload,
    handled: false,
    preferredTarget: target,
    candidates: [],
  };
  window.dispatchEvent(
    new CustomEvent<TerminalSoftKeyEventDetail>(TERMINAL_SOFT_KEY_EVENT, { detail }),
  );
  const ownedTerminal = detail.candidates?.find((candidate) => candidate.ownsPreferredTarget);
  const focusedTerminal = detail.candidates?.find((candidate) => candidate.focused);
  // A mobile palette click can move DOM focus to app chrome after the TUI
  // was active. When exactly one writable terminal is mounted, it remains
  // the unambiguous target even if neither focus heuristic survived.
  const targetIsEditable = isEditableDomInput(target);
  const onlyTerminal =
    !targetIsEditable && detail.candidates?.length === 1 ? detail.candidates[0] : undefined;
  const terminal = ownedTerminal ?? focusedTerminal ?? onlyTerminal;
  if (!detail.handled && terminal) {
    terminal.send();
    detail.handled = true;
  }
  if (detail.handled) return true;
  if (!targetIsEditable && (target === document.body || isXtermElement(target))) {
    const fallbackTerminal = detail.candidates?.[0];
    if (fallbackTerminal) {
      fallbackTerminal.send();
      return true;
    }
  }
  return false;
}

/** Bind held gestures once, so repeats cannot move to another terminal. */
export function bindTerminalBarTarget(
  preferredDomTarget?: HTMLElement | null,
  callbacks?: Pick<TerminalSoftKeyEventDetail, "onStickyChanged" | "onStickySubscribed">,
): TerminalBarTarget | null {
  let target: TerminalBarTarget | null = null;
  dispatchToTerminal(
    {
      action: "enter",
      ...callbacks,
      onTerminalTarget: (next) => {
        target = next;
      },
    },
    preferredTarget(preferredDomTarget),
  );
  return target;
}

/** Dispatch a full terminal-bar key (including text and Shift) to the active terminal. */
export function dispatchTerminalBarKey(
  key: TerminalBarKey,
  preferredDomTarget?: HTMLElement | null,
  callbacks?: Pick<TerminalSoftKeyEventDetail, "onStickyChanged" | "onStickySubscribed">,
  gesture: "tap" | "longPress" | "repeat" = "tap",
): void {
  dispatchToTerminal(
    { action: "enter", terminalBarKey: key, terminalGesture: gesture, ...callbacks },
    preferredTarget(preferredDomTarget),
  );
}

function dispatchGlobalShortcutChord(
  chord: ShortcutChord,
  preferredDomTarget?: HTMLElement | null,
): void {
  const activeElement =
    document.activeElement instanceof HTMLElement ? document.activeElement : null;
  if (activeElement && isXtermElement(activeElement)) {
    activeElement.blur();
  }
  const target = preferredTarget(preferredDomTarget);
  const eventTarget = isXtermElement(target) ? document.body : target;
  eventTarget.dispatchEvent(new KeyboardEvent("keydown", keyboardEventInit(chord)));
}

function dispatchChord(chord: ShortcutChord, preferredDomTarget?: HTMLElement | null): void {
  const target = preferredTarget(preferredDomTarget);
  const softKey = softKeyForChord(chord);
  if (softKey) {
    if (dispatchToTerminal({ action: softKey }, target)) return;
  } else {
    const data = terminalDataForChord(chord);
    if (data !== null && dispatchToTerminal({ action: "enter", data }, target)) return;
  }
  target.dispatchEvent(new KeyboardEvent("keydown", keyboardEventInit(chord)));
}

function insertText(text: string, target: HTMLElement): boolean {
  if (
    !isEditableDomInput(target) ||
    !(target instanceof HTMLTextAreaElement || target instanceof HTMLInputElement)
  ) {
    return false;
  }
  const start = target.selectionStart ?? target.value.length;
  const end = target.selectionEnd ?? start;
  target.setRangeText(text, start, end, "end");
  target.dispatchEvent(
    new InputEvent("input", { bubbles: true, inputType: "insertText", data: text }),
  );
  target.focus();
  return true;
}

export function mobileAssistantBindingLabel(binding: MobileAssistantButtonBinding): string {
  if (binding.kind === "shortcut")
    return MOBILE_ASSISTANT_ACTION_DEFINITIONS[binding.actionId].label;
  if (binding.kind === "text") return binding.submit ? `${binding.text} + send` : binding.text;
  const parts = binding.chord.modifiers.map((modifier) =>
    modifier === "primary" ? "Primary" : modifier[0].toUpperCase() + modifier.slice(1),
  );
  parts.push(codeToKey(binding.chord.code));
  return parts.join("+");
}

const WORKSPACE_TAB_SHORTCUTS: Partial<Record<ShortcutActionId, 1 | 2 | 3 | 4>> = {
  selectWorkspaceTab1: 1,
  selectWorkspaceTab2: 2,
  selectWorkspaceTab3: 3,
  selectWorkspaceTab4: 4,
};

export function dispatchMobileAssistantButton(
  button: MobileAssistantButton,
  preferredDomTarget?: HTMLElement | null,
): void {
  const binding = button.binding;
  if (binding.kind === "shortcut") {
    if (
      binding.actionId in MOBILE_ASSISTANT_DIRECT_ACTIONS ||
      binding.actionId === "stopResponse" ||
      binding.actionId === "renameSession" ||
      binding.actionId === "focusComposer"
    ) {
      window.dispatchEvent(
        new CustomEvent(MOBILE_ASSISTANT_ACTION_EVENT, { detail: { actionId: binding.actionId } }),
      );
      return;
    }
    if (binding.actionId === "pollSessions") {
      dispatchPollSessions();
      return;
    }
    if (binding.actionId === "archiveSession") {
      dispatchArchiveSession();
      return;
    }
    // The switcher needs a held modifier to cycle and release to commit, which
    // a tap can't express.
    if (binding.actionId === "recentSessions") return;
    const actionId = binding.actionId as ShortcutActionId;
    const tabNumber = WORKSPACE_TAB_SHORTCUTS[actionId];
    if (tabNumber !== undefined) {
      dispatchSelectWorkspaceTab(tabNumber);
      return;
    }
    const chord = resolveShortcutBindings(actionId)[0];
    if (!chord) return;
    if (DEFAULT_SHORTCUT_DEFINITIONS[actionId].scope === "global") {
      dispatchGlobalShortcutChord(chord, preferredDomTarget);
      return;
    }
    const target = preferredTarget(preferredDomTarget);
    if (binding.actionId === "sendMessage" && !isEditableDomInput(target)) {
      dispatchChord({ code: "Enter", modifiers: [] }, preferredDomTarget);
      return;
    }
    dispatchChord(chord, preferredDomTarget);
    return;
  }
  if (binding.kind === "key") {
    dispatchChord(binding.chord, preferredDomTarget);
    return;
  }
  const target = preferredTarget(preferredDomTarget);
  if (insertText(binding.text, target)) {
    if (binding.submit)
      window.setTimeout(() => dispatchChord({ code: "Enter", modifiers: [] }, target), 0);
    return;
  }
  if (!dispatchToTerminal({ action: "enter", data: binding.text }, target)) return;
  if (binding.submit) {
    dispatchToTerminal({ action: "enter" }, target);
  }
}

/** Backward-compatible helper used by older callers and tests. */
export function dispatchMobileAssistantAction(
  action: MobileAssistantAction,
  preferredDomTarget?: HTMLElement | null,
): void {
  dispatchMobileAssistantButton(legacyActionToButton(action, 0), preferredDomTarget);
}
