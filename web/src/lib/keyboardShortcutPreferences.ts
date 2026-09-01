import { queueUserPreferencePatch } from "./userPreferencesSync";

export type ShortcutPlatform = "macos" | "windows" | "linux";
export type ShortcutModifier = "primary" | "control" | "meta" | "alt" | "shift";

export interface ShortcutChord {
  code: string;
  modifiers: ShortcutModifier[];
}

export type ShortcutGroupId = "general" | "chats" | "navigation" | "view" | "slash";
export type ShortcutScope = "global" | "composer" | "suggestions";

export type ShortcutActionId =
  | "newSession"
  | "commandPalette"
  | "showShortcuts"
  | "sendMessage"
  | "newLine"
  | "recallPreviousPrompt"
  | "recallNextPrompt"
  | "approvePrompt"
  | "voiceDictation"
  | "stopResponse"
  | "previousSession"
  | "nextSession"
  | "pollSessions"
  | "archiveSession"
  | "pinnedSession"
  | "toggleConversationsSidebar"
  | "toggleWorkspaceSidebar"
  | "previousSuggestion"
  | "nextSuggestion"
  | "applySuggestion"
  | "dismissSuggestions";

export interface ShortcutDefinition {
  id: ShortcutActionId;
  label: string;
  group: ShortcutGroupId;
  scope: ShortcutScope;
  defaultBindings: ShortcutChord[];
  enabledByDefault?: boolean;
  note?: string;
}

const chord = (code: string, modifiers: ShortcutModifier[] = []): ShortcutChord => ({
  code,
  modifiers,
});

export const DEFAULT_SHORTCUT_DEFINITIONS: Record<ShortcutActionId, ShortcutDefinition> = {
  newSession: {
    id: "newSession",
    label: "Start a new session",
    group: "general",
    scope: "global",
    defaultBindings: [chord("KeyN", ["primary"])],
  },
  commandPalette: {
    id: "commandPalette",
    label: "Open command palette",
    group: "general",
    scope: "global",
    defaultBindings: [chord("KeyK", ["primary"])],
  },
  showShortcuts: {
    id: "showShortcuts",
    label: "Show keyboard shortcuts",
    group: "general",
    scope: "global",
    defaultBindings: [chord("Slash", ["primary"])],
  },
  sendMessage: {
    id: "sendMessage",
    label: "Send message",
    group: "chats",
    scope: "composer",
    defaultBindings: [chord("Enter")],
  },
  newLine: {
    id: "newLine",
    label: "New line in message",
    group: "chats",
    scope: "composer",
    defaultBindings: [chord("Enter", ["shift"])],
  },
  recallPreviousPrompt: {
    id: "recallPreviousPrompt",
    label: "Recall previous prompt",
    group: "chats",
    scope: "composer",
    defaultBindings: [chord("ArrowUp")],
  },
  recallNextPrompt: {
    id: "recallNextPrompt",
    label: "Recall next prompt",
    group: "chats",
    scope: "composer",
    defaultBindings: [chord("ArrowDown")],
  },
  approvePrompt: {
    id: "approvePrompt",
    label: "Accept approval prompt",
    group: "chats",
    scope: "global",
    defaultBindings: [chord("Enter", ["primary"])],
  },
  voiceDictation: {
    id: "voiceDictation",
    label: "Toggle voice dictation",
    group: "chats",
    scope: "global",
    defaultBindings: [chord("KeyV", ["primary", "alt"])],
  },
  stopResponse: {
    id: "stopResponse",
    label: "Stop response",
    group: "chats",
    scope: "composer",
    defaultBindings: [chord("Escape")],
  },
  previousSession: {
    id: "previousSession",
    label: "Previous session",
    group: "navigation",
    scope: "global",
    defaultBindings: [chord("ArrowUp", ["primary"])],
  },
  nextSession: {
    id: "nextSession",
    label: "Next session",
    group: "navigation",
    scope: "global",
    defaultBindings: [chord("ArrowDown", ["primary"])],
  },
  pollSessions: {
    id: "pollSessions",
    label: "Poll sessions",
    group: "navigation",
    scope: "global",
    defaultBindings: [chord("Backquote", ["alt"])],
  },
  archiveSession: {
    id: "archiveSession",
    label: "Archive current session",
    group: "navigation",
    scope: "global",
    defaultBindings: [chord("KeyW", ["alt"])],
  },
  pinnedSession: {
    id: "pinnedSession",
    label: "Jump to pinned session (1–10)",
    group: "navigation",
    scope: "global",
    defaultBindings: [chord("Digit*", ["primary", "alt"])],
  },
  toggleConversationsSidebar: {
    id: "toggleConversationsSidebar",
    label: "Toggle conversations sidebar",
    group: "view",
    scope: "global",
    defaultBindings: [chord("BracketLeft", ["primary", "alt"])],
  },
  toggleWorkspaceSidebar: {
    id: "toggleWorkspaceSidebar",
    label: "Toggle workspace sidebar",
    group: "view",
    scope: "global",
    defaultBindings: [chord("BracketRight", ["primary", "alt"])],
  },
  previousSuggestion: {
    id: "previousSuggestion",
    label: "Previous suggestion",
    group: "slash",
    scope: "suggestions",
    defaultBindings: [chord("ArrowUp")],
  },
  nextSuggestion: {
    id: "nextSuggestion",
    label: "Next suggestion",
    group: "slash",
    scope: "suggestions",
    defaultBindings: [chord("ArrowDown")],
  },
  applySuggestion: {
    id: "applySuggestion",
    label: "Apply highlighted command",
    group: "slash",
    scope: "suggestions",
    defaultBindings: [chord("Tab"), chord("Enter")],
  },
  dismissSuggestions: {
    id: "dismissSuggestions",
    label: "Dismiss menu",
    group: "slash",
    scope: "suggestions",
    defaultBindings: [chord("Escape")],
  },
};

export const SHORTCUT_ACTION_IDS = Object.keys(DEFAULT_SHORTCUT_DEFINITIONS) as ShortcutActionId[];

export interface ShortcutActionPreference {
  common?: ShortcutChord[];
  platformOverrides?: Partial<Record<ShortcutPlatform, ShortcutChord[]>>;
  enabled?: boolean;
}

export interface KeyboardShortcutPreferences {
  version: 1;
  actions: Partial<Record<ShortcutActionId, ShortcutActionPreference>>;
}

export interface ShortcutDefaultContext {
  submitWithModEnter?: boolean;
  nativeShell?: boolean;
}

export const KEYBOARD_SHORTCUTS_STORAGE_KEY = "omnigent:keyboard-shortcut-preferences";
export const KEYBOARD_SHORTCUTS_CHANGED_EVENT = "omnigent:keyboard-shortcuts-changed";
let shortcutRecordingActive = false;

export function setShortcutRecordingActive(active: boolean): void {
  shortcutRecordingActive = active;
}

export function isShortcutRecordingActive(): boolean {
  return shortcutRecordingActive;
}

function emptyPreferences(): KeyboardShortcutPreferences {
  return { version: 1, actions: {} };
}
const MODIFIER_ORDER: ShortcutModifier[] = ["primary", "control", "meta", "alt", "shift"];

export function currentShortcutPlatform(): ShortcutPlatform {
  if (typeof navigator === "undefined") return "windows";
  const uaData = (navigator as Navigator & { userAgentData?: { platform?: string } }).userAgentData;
  const value = uaData?.platform ?? navigator.platform ?? navigator.userAgent ?? "";
  if (/Mac|iPhone|iPad|iPod/i.test(value)) return "macos";
  if (/Linux|X11/i.test(value)) return "linux";
  return "windows";
}

function isModifier(value: unknown): value is ShortcutModifier {
  return MODIFIER_ORDER.includes(value as ShortcutModifier);
}

function normalizeChord(value: unknown): ShortcutChord | null {
  if (!value || typeof value !== "object") return null;
  const raw = value as { code?: unknown; modifiers?: unknown };
  if (typeof raw.code !== "string" || raw.code.length === 0 || raw.code.length > 64) return null;
  if (!Array.isArray(raw.modifiers) || !raw.modifiers.every(isModifier)) return null;
  const rawModifiers = raw.modifiers as ShortcutModifier[];
  const modifiers = MODIFIER_ORDER.filter((modifier) => rawModifiers.includes(modifier));
  return { code: raw.code, modifiers };
}

function normalizeBindings(value: unknown): ShortcutChord[] | null {
  if (!Array.isArray(value) || value.length === 0 || value.length > 4) return null;
  const bindings = value.map(normalizeChord);
  return bindings.every((binding): binding is ShortcutChord => binding !== null) ? bindings : null;
}

function normalizeActionPreference(value: unknown): ShortcutActionPreference | null {
  if (!value || typeof value !== "object") return null;
  const raw = value as {
    common?: unknown;
    platformOverrides?: unknown;
    enabled?: unknown;
  };
  const preference: ShortcutActionPreference = {};
  if (raw.common !== undefined) {
    const common = normalizeBindings(raw.common);
    if (!common) return null;
    preference.common = common;
  }
  if (raw.enabled !== undefined) {
    if (typeof raw.enabled !== "boolean") return null;
    preference.enabled = raw.enabled;
  }
  if (raw.platformOverrides !== undefined) {
    if (!raw.platformOverrides || typeof raw.platformOverrides !== "object") return null;
    const overrides: Partial<Record<ShortcutPlatform, ShortcutChord[]>> = {};
    for (const platform of ["macos", "windows", "linux"] as const) {
      const candidate = (raw.platformOverrides as Record<string, unknown>)[platform];
      if (candidate === undefined) continue;
      const bindings = normalizeBindings(candidate);
      if (!bindings) return null;
      overrides[platform] = bindings;
    }
    preference.platformOverrides = overrides;
  }
  return preference;
}

export function readKeyboardShortcutPreferences(): KeyboardShortcutPreferences {
  if (typeof window === "undefined") return emptyPreferences();
  try {
    const raw = window.localStorage.getItem(KEYBOARD_SHORTCUTS_STORAGE_KEY);
    if (!raw) return emptyPreferences();
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return emptyPreferences();
    const record = parsed as { version?: unknown; actions?: unknown };
    if (record.version !== 1 || !record.actions || typeof record.actions !== "object") {
      return emptyPreferences();
    }
    const actions: KeyboardShortcutPreferences["actions"] = {};
    for (const actionId of SHORTCUT_ACTION_IDS) {
      const candidate = (record.actions as Record<string, unknown>)[actionId];
      if (candidate === undefined) continue;
      const normalized = normalizeActionPreference(candidate);
      if (normalized) actions[actionId] = normalized;
    }
    return { version: 1, actions };
  } catch {
    return emptyPreferences();
  }
}

function persistKeyboardShortcutPreferences(preferences: KeyboardShortcutPreferences): void {
  if (typeof window === "undefined") return;
  try {
    if (Object.keys(preferences.actions).length === 0) {
      window.localStorage.removeItem(KEYBOARD_SHORTCUTS_STORAGE_KEY);
    } else {
      window.localStorage.setItem(KEYBOARD_SHORTCUTS_STORAGE_KEY, JSON.stringify(preferences));
    }
    window.dispatchEvent(new Event(KEYBOARD_SHORTCUTS_CHANGED_EVENT));
    queueUserPreferencePatch(
      "keyboard_shortcuts",
      Object.keys(preferences.actions).length === 0 ? null : preferences,
    );
  } catch {
    // Keyboard shortcuts must retain their defaults when storage is unavailable.
  }
}

function withoutAction(
  actions: KeyboardShortcutPreferences["actions"],
  actionId: ShortcutActionId,
): KeyboardShortcutPreferences["actions"] {
  return Object.fromEntries(
    Object.entries(actions).filter(([candidate]) => candidate !== actionId),
  ) as KeyboardShortcutPreferences["actions"];
}

function withoutPlatform(
  overrides: Partial<Record<ShortcutPlatform, ShortcutChord[]>>,
  platform: ShortcutPlatform,
): Partial<Record<ShortcutPlatform, ShortcutChord[]>> {
  return Object.fromEntries(
    Object.entries(overrides).filter(([candidate]) => candidate !== platform),
  ) as Partial<Record<ShortcutPlatform, ShortcutChord[]>>;
}

export function writeShortcutPreference(
  actionId: ShortcutActionId,
  preference: ShortcutActionPreference,
): void {
  const preferences = readKeyboardShortcutPreferences();
  const hasOverrides = Object.keys(preference.platformOverrides ?? {}).length > 0;
  if (preference.common || preference.enabled !== undefined || hasOverrides) {
    preferences.actions[actionId] = preference;
  } else {
    preferences.actions = withoutAction(preferences.actions, actionId);
  }
  persistKeyboardShortcutPreferences(preferences);
}

export function resetShortcutPreference(actionId: ShortcutActionId): void {
  const preferences = readKeyboardShortcutPreferences();
  preferences.actions = withoutAction(preferences.actions, actionId);
  persistKeyboardShortcutPreferences(preferences);
}

export function deleteShortcutPlatformOverride(
  actionId: ShortcutActionId,
  platform: ShortcutPlatform,
): void {
  const preferences = readKeyboardShortcutPreferences();
  const preference = preferences.actions[actionId];
  if (!preference?.platformOverrides?.[platform]) return;
  const platformOverrides = withoutPlatform(preference.platformOverrides, platform);
  const next = { ...preference, platformOverrides };
  if (next.common || next.enabled !== undefined || Object.keys(platformOverrides).length > 0) {
    preferences.actions[actionId] = next;
  } else {
    preferences.actions = withoutAction(preferences.actions, actionId);
  }
  persistKeyboardShortcutPreferences(preferences);
}

export function defaultShortcutBindings(
  actionId: ShortcutActionId,
  context: ShortcutDefaultContext = {},
): ShortcutChord[] {
  if (actionId === "sendMessage" && context.submitWithModEnter) {
    return [chord("Enter", ["primary"])];
  }
  if (actionId === "newLine" && context.submitWithModEnter) {
    return [chord("Enter")];
  }
  if (actionId === "pinnedSession" && context.nativeShell) {
    return [chord("Digit*", ["primary"])];
  }
  return DEFAULT_SHORTCUT_DEFINITIONS[actionId].defaultBindings;
}

export function resolveShortcutBindings(
  actionId: ShortcutActionId,
  platform = currentShortcutPlatform(),
  context: ShortcutDefaultContext = {},
): ShortcutChord[] {
  const preference = readKeyboardShortcutPreferences().actions[actionId];
  return (
    preference?.platformOverrides?.[platform] ??
    preference?.common ??
    defaultShortcutBindings(actionId, context)
  );
}

export function isShortcutActionEnabled(actionId: ShortcutActionId): boolean {
  const preference = readKeyboardShortcutPreferences().actions[actionId];
  return preference?.enabled ?? DEFAULT_SHORTCUT_DEFINITIONS[actionId].enabledByDefault ?? true;
}

export function hasCustomShortcutBindings(actionId: ShortcutActionId): boolean {
  const preference = readKeyboardShortcutPreferences().actions[actionId];
  return Boolean(preference?.common || Object.keys(preference?.platformOverrides ?? {}).length > 0);
}

function eventCode(event: Pick<KeyboardEvent, "code" | "key">): string {
  if (event.code) return event.code;
  if (/^[a-z]$/i.test(event.key)) return `Key${event.key.toUpperCase()}`;
  if (/^[0-9]$/.test(event.key)) return `Digit${event.key}`;
  const byKey: Record<string, string> = {
    Enter: "Enter",
    Escape: "Escape",
    Esc: "Escape",
    Tab: "Tab",
    ArrowUp: "ArrowUp",
    ArrowDown: "ArrowDown",
    ArrowLeft: "ArrowLeft",
    ArrowRight: "ArrowRight",
    "/": "Slash",
    "`": "Backquote",
    "~": "Backquote",
    "[": "BracketLeft",
    "]": "BracketRight",
  };
  return byKey[event.key] ?? event.key;
}

export function shortcutChordFromEvent(event: KeyboardEvent): ShortcutChord | null {
  const code = eventCode(event);
  if (
    !code ||
    [
      "ControlLeft",
      "ControlRight",
      "MetaLeft",
      "MetaRight",
      "AltLeft",
      "AltRight",
      "ShiftLeft",
      "ShiftRight",
    ].includes(code)
  ) {
    return null;
  }
  const modifiers: ShortcutModifier[] = [];
  if (event.ctrlKey) modifiers.push("control");
  if (event.metaKey) modifiers.push("meta");
  if (event.altKey) modifiers.push("alt");
  if (event.shiftKey) modifiers.push("shift");
  return { code, modifiers };
}

function resolvedModifierFlags(
  binding: ShortcutChord,
  platform: ShortcutPlatform,
): { ctrl: boolean; meta: boolean; alt: boolean; shift: boolean } {
  const modifiers = new Set(binding.modifiers);
  return {
    ctrl: modifiers.has("control") || (modifiers.has("primary") && platform !== "macos"),
    meta: modifiers.has("meta") || (modifiers.has("primary") && platform === "macos"),
    alt: modifiers.has("alt"),
    shift: modifiers.has("shift"),
  };
}

export function eventMatchesShortcut(
  event: Pick<KeyboardEvent, "code" | "key" | "ctrlKey" | "metaKey" | "altKey" | "shiftKey">,
  binding: ShortcutChord,
  platform = currentShortcutPlatform(),
  acceptEitherPrimary = false,
): boolean {
  const flags = resolvedModifierFlags(binding, platform);
  const usesPrimary = binding.modifiers.includes("primary");
  const primaryMatches =
    !usesPrimary ||
    (acceptEitherPrimary
      ? (event.ctrlKey || event.metaKey) && !(event.ctrlKey && event.metaKey)
      : event.ctrlKey === flags.ctrl && event.metaKey === flags.meta);
  if (
    !primaryMatches ||
    (!usesPrimary && event.ctrlKey !== flags.ctrl) ||
    (!usesPrimary && event.metaKey !== flags.meta) ||
    event.altKey !== flags.alt ||
    event.shiftKey !== flags.shift
  ) {
    return false;
  }
  const code = eventCode(event);
  return binding.code === "Digit*" ? /^Digit[0-9]$/.test(code) : code === binding.code;
}

export function eventMatchesShortcutAction(
  event: Pick<KeyboardEvent, "code" | "key" | "ctrlKey" | "metaKey" | "altKey" | "shiftKey">,
  actionId: ShortcutActionId,
  platform = currentShortcutPlatform(),
): boolean {
  if (shortcutRecordingActive) return false;
  const acceptEitherPrimary = !hasCustomShortcutBindings(actionId);
  return (
    isShortcutActionEnabled(actionId) &&
    resolveShortcutBindings(actionId, platform).some((binding) =>
      eventMatchesShortcut(event, binding, platform, acceptEitherPrimary),
    )
  );
}

function chordIdentity(binding: ShortcutChord, platform: ShortcutPlatform): string {
  const flags = resolvedModifierFlags(binding, platform);
  return `${flags.ctrl ? "C" : ""}${flags.meta ? "M" : ""}${flags.alt ? "A" : ""}${flags.shift ? "S" : ""}:${binding.code}`;
}

export function findShortcutConflicts(
  actionId: ShortcutActionId,
  bindings: ShortcutChord[],
  platform = currentShortcutPlatform(),
  context: ShortcutDefaultContext = {},
): ShortcutActionId[] {
  const identities = new Set(bindings.map((binding) => chordIdentity(binding, platform)));
  return SHORTCUT_ACTION_IDS.filter((candidate) => {
    if (candidate === actionId) {
      return false;
    }
    return resolveShortcutBindings(candidate, platform, context).some((binding) =>
      identities.has(chordIdentity(binding, platform)),
    );
  });
}

export function shortcutBindingLabels(
  binding: ShortcutChord,
  platform = currentShortcutPlatform(),
): string[] {
  const flags = resolvedModifierFlags(binding, platform);
  const labels: string[] = [];
  if (flags.ctrl) labels.push("Ctrl");
  if (flags.meta) labels.push(platform === "macos" ? "⌘" : "Meta");
  if (flags.alt) labels.push(platform === "macos" ? "⌥" : "Alt");
  if (flags.shift) labels.push("⇧");
  const keyLabels: Record<string, string> = {
    Enter: "↵",
    Escape: "Esc",
    Tab: "Tab",
    ArrowUp: "↑",
    ArrowDown: "↓",
    ArrowLeft: "←",
    ArrowRight: "→",
    Slash: "/",
    Backquote: "~",
    BracketLeft: "[",
    BracketRight: "]",
    "Digit*": "1…0",
  };
  labels.push(keyLabels[binding.code] ?? binding.code.replace(/^Key/, "").replace(/^Digit/, ""));
  return labels;
}
