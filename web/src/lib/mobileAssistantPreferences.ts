import { dispatchArchiveSession, dispatchPollSessions } from "@/hooks/useSessionPollingHotkeys";
import {
  DEFAULT_SHORTCUT_DEFINITIONS,
  currentShortcutPlatform,
  resolveShortcutBindings,
  type ShortcutActionId,
  type ShortcutChord,
} from "@/lib/keyboardShortcutPreferences";
import { queueUserPreferencePatch } from "@/lib/userPreferencesSync";

export type MobileAssistantSoftKey =
  "escape" | "tab" | "arrowUp" | "arrowDown" | "arrowLeft" | "arrowRight" | "enter";

/** Legacy fixed action ids retained for a lossless version-1 migration. */
export type MobileAssistantAction = MobileAssistantSoftKey | "pollSessions" | "archiveSession";

export interface MobileAssistantActionDefinition {
  id: MobileAssistantAction;
  label: string;
  shortLabel: string;
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
  { id: "pollSessions", label: "Poll sessions", shortLabel: "Poll" },
  { id: "archiveSession", label: "Archive session", shortLabel: "Archive" },
];

export type MobileAssistantButtonBinding =
  | { kind: "shortcut"; actionId: ShortcutActionId }
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

export interface MobileAssistantPreferences {
  version: 2;
  enabled: boolean;
  buttons: MobileAssistantButton[];
  position?: { x: number; y: number };
  dock?: MobileAssistantDock;
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

export interface TerminalSoftKeyEventDetail {
  action: MobileAssistantSoftKey;
  handled: boolean;
  preferredTarget?: HTMLElement | null;
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
};

const DEFAULT_BUTTONS: MobileAssistantButton[] = [
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

export function defaultMobileAssistantButtons(): MobileAssistantButton[] {
  return structuredClone(DEFAULT_BUTTONS);
}

function defaults(): MobileAssistantPreferences {
  return {
    version: 2,
    enabled: true,
    buttons: defaultMobileAssistantButtons(),
  };
}

function isAction(value: unknown): value is MobileAssistantAction {
  return MOBILE_ASSISTANT_ACTIONS.some((action) => action.id === value);
}

function isShortcutAction(value: unknown): value is ShortcutActionId {
  return typeof value === "string" && value in DEFAULT_SHORTCUT_DEFINITIONS;
}

function isIcon(value: unknown): value is MobileAssistantIcon {
  return MOBILE_ASSISTANT_ICONS.some((icon) => icon.id === value);
}

export function mobileAssistantBindingSupportsRepeat(
  binding: MobileAssistantButtonBinding,
): boolean {
  return (
    binding.kind === "key" &&
    binding.chord.modifiers.length === 0 &&
    ["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"].includes(binding.chord.code)
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

function normalizeChord(value: unknown): ShortcutChord | null {
  if (!value || typeof value !== "object") return null;
  const chord = value as Partial<ShortcutChord>;
  if (typeof chord.code !== "string" || chord.code.length === 0 || chord.code.length > 64)
    return null;
  const validModifiers = ["primary", "control", "meta", "alt", "shift"];
  if (
    !Array.isArray(chord.modifiers) ||
    !chord.modifiers.every((item) => validModifiers.includes(item))
  )
    return null;
  return { code: chord.code, modifiers: [...new Set(chord.modifiers)] } as ShortcutChord;
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

export function readMobileAssistantPreferences(): MobileAssistantPreferences {
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
        version: 2,
        enabled: parsed.enabled,
        buttons: actions.map(legacyActionToButton),
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
      })
      .slice(0, MOBILE_ASSISTANT_MAX_BUTTONS);
    return {
      version: 2,
      enabled: parsed.enabled,
      buttons,
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

function softKeyForChord(chord: ShortcutChord): MobileAssistantSoftKey | null {
  if (chord.modifiers.length > 0) return null;
  return (Object.entries(SOFT_KEY_DEFAULTS).find(
    ([, candidate]) => candidate.code === chord.code,
  )?.[0] ?? null) as MobileAssistantSoftKey | null;
}

function preferredTarget(preferredDomTarget?: HTMLElement | null): HTMLElement {
  const activeTarget =
    document.activeElement instanceof HTMLElement ? document.activeElement : document.body;
  return preferredDomTarget?.isConnected ? preferredDomTarget : activeTarget;
}

function dispatchChord(chord: ShortcutChord, preferredDomTarget?: HTMLElement | null): void {
  const target = preferredTarget(preferredDomTarget);
  const softKey = softKeyForChord(chord);
  if (softKey) {
    const detail: TerminalSoftKeyEventDetail = {
      action: softKey,
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
    const targetIsEditable =
      target instanceof HTMLInputElement ||
      target instanceof HTMLTextAreaElement ||
      target.isContentEditable;
    const onlyTerminal =
      !targetIsEditable && detail.candidates?.length === 1 ? detail.candidates[0] : undefined;
    const terminal = ownedTerminal ?? focusedTerminal ?? onlyTerminal;
    if (!detail.handled && terminal) {
      terminal.send();
      detail.handled = true;
    }
    if (detail.handled) return;
    if (target === document.body) {
      const fallbackTerminal = detail.candidates?.[0];
      if (fallbackTerminal) {
        fallbackTerminal.send();
        return;
      }
    }
  }
  target.dispatchEvent(new KeyboardEvent("keydown", keyboardEventInit(chord)));
}

function insertText(text: string, target: HTMLElement): boolean {
  if (!(target instanceof HTMLTextAreaElement || target instanceof HTMLInputElement)) return false;
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
  if (binding.kind === "shortcut") return DEFAULT_SHORTCUT_DEFINITIONS[binding.actionId].label;
  if (binding.kind === "text") return binding.submit ? `${binding.text} + send` : binding.text;
  const parts = binding.chord.modifiers.map((modifier) =>
    modifier === "primary" ? "Primary" : modifier[0].toUpperCase() + modifier.slice(1),
  );
  parts.push(codeToKey(binding.chord.code));
  return parts.join("+");
}

export function dispatchMobileAssistantButton(
  button: MobileAssistantButton,
  preferredDomTarget?: HTMLElement | null,
): void {
  const binding = button.binding;
  if (binding.kind === "shortcut") {
    if (binding.actionId === "pollSessions") {
      dispatchPollSessions();
      return;
    }
    if (binding.actionId === "archiveSession") {
      dispatchArchiveSession();
      return;
    }
    const chord = resolveShortcutBindings(binding.actionId)[0];
    if (chord) dispatchChord(chord, preferredDomTarget);
    return;
  }
  if (binding.kind === "key") {
    dispatchChord(binding.chord, preferredDomTarget);
    return;
  }
  const target = preferredTarget(preferredDomTarget);
  if (!insertText(binding.text, target)) return;
  if (binding.submit)
    window.setTimeout(() => dispatchChord({ code: "Enter", modifiers: [] }, target), 0);
}

/** Backward-compatible helper used by older callers and tests. */
export function dispatchMobileAssistantAction(
  action: MobileAssistantAction,
  preferredDomTarget?: HTMLElement | null,
): void {
  dispatchMobileAssistantButton(legacyActionToButton(action, 0), preferredDomTarget);
}
