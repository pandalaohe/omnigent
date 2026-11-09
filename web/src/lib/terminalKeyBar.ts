import type { ShortcutChord, ShortcutModifier } from "@/lib/keyboardShortcutPreferences";

export const ROW_CELLS = 12;
export const MAX_ROW_PAGES = 5;
export const PAGE_STRIP_PX = 26;
export const MAX_KEY_LABEL_LENGTH = 12;
export const MAX_KEY_TEXT_LENGTH = 64;
export const MAX_KEY_ID_LENGTH = 32;
export const MAX_PREFERENCE_ENVELOPE_BYTES = 65_536;

export const TERMINAL_KEY_ICONS = [
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

export type TerminalKeyIcon = (typeof TERMINAL_KEY_ICONS)[number]["id"];

export type TerminalKeyWidth = 1 | 2;
export type TerminalKeyDisplay = "text" | "icon";
export type TerminalKeyModifier = "ctrl" | "alt" | "shift";

interface TerminalBarKeyBase {
  id: string;
  /** The text label, and the accessible name when an icon is shown. */
  label: string;
  display: TerminalKeyDisplay;
  icon?: TerminalKeyIcon;
  /** Width in 12-cell row units. */
  width: TerminalKeyWidth;
}

export type TerminalBarKey = TerminalBarKeyBase &
  (
    | { kind: "key"; chord: ShortcutChord; repeat?: boolean }
    | { kind: "text"; text: string; submit?: boolean }
    | { kind: "modifier"; modifier: TerminalKeyModifier; tapLocks?: boolean }
    | { kind: "action"; action: "keyboard" }
  );

export interface TerminalBarRow {
  keys: TerminalBarKey[];
  /** Number of leading keys repeated on every page of this row. */
  pinned: number;
  enabled: boolean;
}

export interface TerminalBar {
  top: TerminalBarRow;
  bottom: TerminalBarRow;
}

export type TerminalBarSource =
  "stored" | "reconciled" | "legacy" | "fallback-legacy" | "fallback-defaults";

export interface ResolvedTerminalBar {
  bar: TerminalBar;
  source: TerminalBarSource;
  preset: TerminalBar | "invalid" | null;
}

/** Legacy (pre-terminalBar) terminal key model. Older builds only understand this. */
export type TerminalKeyRowItem =
  | {
      id: string;
      label: string;
      kind: "modifier";
      modifier: "ctrl" | "alt";
      repeat?: false;
    }
  | {
      id: string;
      label: string;
      kind: "key";
      chord: ShortcutChord;
      repeat?: boolean;
    }
  | {
      id: string;
      label: string;
      kind: "action";
      action: "keyboard";
    };

/** First 6 keys populate Row 1; remaining keys fill pages of 6 in Row 2. */
export const DEFAULT_TERMINAL_KEY_ITEMS: readonly TerminalKeyRowItem[] = [
  { id: "esc", label: "Esc", kind: "key", chord: { code: "Escape", modifiers: [] } },
  {
    id: "up",
    label: "↑",
    kind: "key",
    chord: { code: "ArrowUp", modifiers: [] },
    repeat: true,
  },
  {
    id: "down",
    label: "↓",
    kind: "key",
    chord: { code: "ArrowDown", modifiers: [] },
    repeat: true,
  },
  { id: "enter", label: "↵", kind: "key", chord: { code: "Enter", modifiers: [] } },
  { id: "ctrl-c", label: "^C", kind: "key", chord: { code: "KeyC", modifiers: ["control"] } },
  { id: "keyboard", label: "Keyboard", kind: "action", action: "keyboard" },
  { id: "tab", label: "Tab", kind: "key", chord: { code: "Tab", modifiers: [] } },
  { id: "shift-tab", label: "⇧Tab", kind: "key", chord: { code: "Tab", modifiers: ["shift"] } },
  {
    id: "left",
    label: "←",
    kind: "key",
    chord: { code: "ArrowLeft", modifiers: [] },
    repeat: true,
  },
  {
    id: "right",
    label: "→",
    kind: "key",
    chord: { code: "ArrowRight", modifiers: [] },
    repeat: true,
  },
  { id: "slash", label: "/", kind: "key", chord: { code: "Slash", modifiers: [] } },
  { id: "ctrl", label: "Ctrl", kind: "modifier", modifier: "ctrl" },
  { id: "pgup", label: "PgUp", kind: "key", chord: { code: "PageUp", modifiers: [] } },
  { id: "pgdn", label: "PgDn", kind: "key", chord: { code: "PageDown", modifiers: [] } },
  { id: "home", label: "Home", kind: "key", chord: { code: "Home", modifiers: [] } },
  { id: "end", label: "End", kind: "key", chord: { code: "End", modifiers: [] } },
  { id: "alt", label: "Alt", kind: "modifier", modifier: "alt" },
  { id: "ctrl-d", label: "^D", kind: "key", chord: { code: "KeyD", modifiers: ["control"] } },
];

export function defaultTerminalButtons(): TerminalKeyRowItem[] {
  return structuredClone([...DEFAULT_TERMINAL_KEY_ITEMS]);
}

export type TerminalStickyState = "off" | "once" | "locked";
export interface TerminalStickyModifiers {
  ctrl: TerminalStickyState;
  alt: TerminalStickyState;
  shift: TerminalStickyState;
}

export const EMPTY_STICKY_MODIFIERS: TerminalStickyModifiers = {
  ctrl: "off",
  alt: "off",
  shift: "off",
};

export type StickyAction =
  | { type: "tap" | "longPress"; mod: TerminalKeyModifier; tapLocks: boolean }
  | { type: "consumed" }
  | { type: "reset" };

export function reduceSticky(
  state: TerminalStickyModifiers,
  action: StickyAction,
): TerminalStickyModifiers {
  if (action.type === "reset") return { ...EMPTY_STICKY_MODIFIERS };
  if (action.type === "consumed") {
    return {
      ctrl: state.ctrl === "once" ? "off" : state.ctrl,
      alt: state.alt === "once" ? "off" : state.alt,
      shift: state.shift === "once" ? "off" : state.shift,
    };
  }
  const current = state[action.mod];
  let next: TerminalStickyState;
  if (action.type === "longPress") next = "locked";
  else if (action.tapLocks) next = current === "locked" ? "off" : "locked";
  else next = current === "off" ? "once" : "off";
  return { ...state, [action.mod]: next };
}

const CHORD_MODIFIERS: readonly ShortcutModifier[] = ["primary", "control", "meta", "alt", "shift"];

export function normalizeChord(value: unknown): ShortcutChord | null {
  if (!value || typeof value !== "object") return null;
  const chord = value as Partial<ShortcutChord>;
  if (typeof chord.code !== "string" || chord.code.length === 0 || chord.code.length > 64)
    return null;
  if (
    !Array.isArray(chord.modifiers) ||
    !chord.modifiers.every((item) => CHORD_MODIFIERS.includes(item as ShortcutModifier))
  )
    return null;
  return { code: chord.code, modifiers: [...new Set(chord.modifiers)] } as ShortcutChord;
}

export function bindingSupportsRepeat(binding: { kind: "key"; chord: ShortcutChord }): boolean {
  return (
    binding.chord.modifiers.length === 0 &&
    ["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"].includes(binding.chord.code)
  );
}

/** Today's legacy normalizer, retained so projections are a fixed point. */
export function normalizeTerminalButtons(value: unknown): TerminalKeyRowItem[] {
  if (!Array.isArray(value)) return defaultTerminalButtons();
  const ids = new Set<string>();
  return value.flatMap((candidate): TerminalKeyRowItem[] => {
    if (!candidate || typeof candidate !== "object") return [];
    const item = candidate as Partial<TerminalKeyRowItem>;
    if (
      typeof item.id !== "string" ||
      !item.id ||
      ids.has(item.id) ||
      typeof item.label !== "string" ||
      !item.label.trim()
    )
      return [];
    let normalized: TerminalKeyRowItem;
    if (item.kind === "modifier" && (item.modifier === "ctrl" || item.modifier === "alt")) {
      normalized = { id: item.id, label: item.label, kind: "modifier", modifier: item.modifier };
    } else if (item.kind === "action" && item.action === "keyboard") {
      normalized = { id: item.id, label: item.label, kind: "action", action: "keyboard" };
    } else if (item.kind === "key") {
      const chord = normalizeChord(item.chord);
      if (!chord) return [];
      normalized = {
        id: item.id,
        label: item.label,
        kind: "key",
        chord,
        ...(item.repeat && bindingSupportsRepeat({ kind: "key", chord }) ? { repeat: true } : {}),
      };
    } else return [];
    ids.add(item.id);
    return [normalized];
  });
}

export function isTerminalKeyIcon(value: unknown): value is TerminalKeyIcon {
  return TERMINAL_KEY_ICONS.some((icon) => icon.id === value);
}

function legacyItemToBarKey(item: TerminalKeyRowItem): TerminalBarKey {
  const base = {
    id: item.id,
    label: item.label,
    width: 2 as const,
    display: item.kind === "action" ? ("icon" as const) : ("text" as const),
    ...(item.kind === "action" ? { icon: "keyboard" as const } : {}),
  };
  if (item.kind === "modifier") return { ...base, kind: "modifier", modifier: item.modifier };
  if (item.kind === "action") return { ...base, kind: "action", action: "keyboard" };
  return {
    ...base,
    kind: "key",
    chord: { code: item.chord.code, modifiers: [...item.chord.modifiers] },
    ...(item.repeat ? { repeat: true } : {}),
  };
}

export function migrateLegacy(items: unknown, secondRow: unknown): TerminalBar {
  const legacy = normalizeTerminalButtons(items);
  const keys = legacy.map(legacyItemToBarKey);
  return {
    top: { keys: keys.slice(0, 6), pinned: 0, enabled: true },
    bottom: { keys: keys.slice(6), pinned: 0, enabled: secondRow !== false },
  };
}

export function defaultTerminalBar(): TerminalBar {
  return migrateLegacy(defaultTerminalButtons(), true);
}

function isProjectable(key: TerminalBarKey): boolean {
  return (
    key.kind === "key" ||
    key.kind === "action" ||
    (key.kind === "modifier" && key.modifier !== "shift")
  );
}

function projectKey(key: TerminalBarKey): TerminalKeyRowItem | null {
  if (key.kind === "modifier" && key.modifier !== "shift")
    return { id: key.id, label: key.label.slice(0, 24), kind: "modifier", modifier: key.modifier };
  if (key.kind === "action")
    return { id: key.id, label: key.label.slice(0, 24), kind: "action", action: "keyboard" };
  if (key.kind === "key")
    return {
      id: key.id,
      label: key.label.slice(0, 24),
      kind: "key",
      chord: { code: key.chord.code, modifiers: [...key.chord.modifiers] },
      ...(key.repeat ? { repeat: true } : {}),
    };
  return null;
}

/** The lossy terminalButtons view older builds read. Nothing in this build reads it. */
export function projectLegacy(bar: TerminalBar): TerminalKeyRowItem[] {
  return normalizeTerminalButtons(
    [...bar.top.keys, ...bar.bottom.keys]
      .map(projectKey)
      .filter((key): key is TerminalKeyRowItem => key !== null),
  );
}

function stableJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value as Record<string, unknown>)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${stableJson((value as Record<string, unknown>)[key])}`)
      .join(",")}}`;
  }
  return JSON.stringify(value) ?? "null";
}

function fnv1a(text: string): string {
  let hash = 0x811c9dc5;
  for (let index = 0; index < text.length; index += 1) {
    hash ^= text.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return hash.toString(16).padStart(8, "0");
}

export function legacyFingerprint(items: unknown, secondRow: unknown): string {
  return `fnv1a:${fnv1a(
    stableJson({
      terminalButtons: normalizeTerminalButtons(items),
      terminalSecondRow: secondRow !== false,
    }),
  )}`;
}

function cloneChord(chord: ShortcutChord): ShortcutChord {
  return { code: chord.code, modifiers: [...chord.modifiers] };
}

export function canonicalizeBarKey(key: TerminalBarKey): TerminalBarKey {
  const base = {
    id: key.id,
    label: key.label,
    display: key.display,
    ...(key.icon ? { icon: key.icon } : {}),
    width: key.width,
  };
  if (key.kind === "key")
    return {
      ...base,
      kind: "key",
      chord: cloneChord(key.chord),
      ...(key.repeat ? { repeat: true } : {}),
    };
  if (key.kind === "text")
    return { ...base, kind: "text", text: key.text, ...(key.submit ? { submit: true } : {}) };
  if (key.kind === "modifier")
    return {
      ...base,
      kind: "modifier",
      modifier: key.modifier,
      ...(key.tapLocks ? { tapLocks: true } : {}),
    };
  return { ...base, kind: "action", action: "keyboard" };
}

export function canonicalizeBar(bar: TerminalBar): TerminalBar {
  return {
    top: {
      keys: bar.top.keys.map(canonicalizeBarKey),
      pinned: bar.top.pinned,
      enabled: true,
    },
    bottom: {
      keys: bar.bottom.keys.map(canonicalizeBarKey),
      pinned: bar.bottom.pinned,
      enabled: bar.bottom.enabled,
    },
  };
}

function canonicalJsonValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonicalJsonValue);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.keys(value as Record<string, unknown>)
        .sort()
        .map((key) => [key, canonicalJsonValue((value as Record<string, unknown>)[key])]),
    );
  }
  return value;
}

const MOBILE_SYNC_FIELD_ORDER = [
  "version",
  "enabled",
  "buttons",
  "terminalButtons",
  "terminalKeysEnabled",
  "terminalSecondRow",
  "terminalBar",
  "terminalBarPreset",
  "terminalLegacyBasis",
  "chatActionsRevision",
] as const;

/**
 * Canonical pass-through for the sync layer. It never repairs or drops a
 * stored value, so an invalid terminal bar remains recoverable. Idempotent:
 * a second pass serializes byte-identically.
 */
export function canonicalTerminalFields(raw: Record<string, unknown>): Record<string, unknown> {
  const output: Record<string, unknown> = {};
  for (const key of MOBILE_SYNC_FIELD_ORDER) {
    if (!Object.hasOwn(raw, key) || raw[key] === undefined) continue;
    output[key] =
      key === "terminalBar" || key === "terminalBarPreset"
        ? canonicalJsonValue(raw[key])
        : raw[key];
  }
  for (const key of Object.keys(raw).sort()) {
    if (Object.hasOwn(output, key) || raw[key] === undefined) continue;
    output[key] = raw[key];
  }
  return output;
}

function sameValue(left: unknown, right: unknown): boolean {
  return stableJson(left) === stableJson(right);
}

export interface BarLimitIssue {
  row: "top" | "bottom";
  keyId?: string;
  kind: "pages" | "label" | "text" | "id";
  worse: boolean;
}

export interface BarValidation {
  structural: string[];
  limits: BarLimitIssue[];
}

interface ValidationOptions {
  before?: TerminalBar;
}

function validateRow(
  raw: unknown,
  rowName: "top" | "bottom",
  seenIds: Set<string>,
  structural: string[],
): TerminalBarRow | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    structural.push(`${rowName} is not an object`);
    return null;
  }
  const row = raw as Partial<TerminalBarRow>;
  if (!Array.isArray(row.keys)) {
    structural.push(`${rowName}.keys is not an array`);
    return null;
  }
  if (
    typeof row.pinned !== "number" ||
    !Number.isInteger(row.pinned) ||
    row.pinned < 0 ||
    row.pinned > row.keys.length
  ) {
    structural.push(`${rowName}.pinned is out of range`);
  }
  if (typeof row.enabled !== "boolean") structural.push(`${rowName}.enabled is not a boolean`);
  let pinnedCells = 0;
  const keys: TerminalBarKey[] = [];
  row.keys.forEach((candidate, index) => {
    if (!candidate || typeof candidate !== "object") {
      structural.push(`${rowName}.keys[${index}] is not an object`);
      return;
    }
    const key = candidate as Partial<TerminalBarKey> & Record<string, unknown>;
    if (typeof key.id !== "string" || !key.id) {
      structural.push(`${rowName}.keys[${index}].id is invalid`);
    } else if (seenIds.has(key.id)) {
      structural.push(`duplicate key id ${key.id}`);
    } else {
      seenIds.add(key.id);
    }
    if (typeof key.label !== "string" || !key.label.trim()) {
      structural.push(`${rowName}.keys[${index}].label is invalid`);
    }
    if (key.width !== 1 && key.width !== 2) {
      structural.push(`${rowName}.keys[${index}].width is invalid`);
    }
    if (key.display !== "text" && key.display !== "icon") {
      structural.push(`${rowName}.keys[${index}].display is invalid`);
    } else if (key.display === "icon" && !isTerminalKeyIcon(key.icon)) {
      structural.push(`${rowName}.keys[${index}].icon is invalid`);
    }
    if (key.kind === "key") {
      if (!normalizeChord(key.chord)) {
        structural.push(`${rowName}.keys[${index}].chord is invalid`);
      }
      if (key.repeat !== undefined && typeof key.repeat !== "boolean") {
        structural.push(`${rowName}.keys[${index}].repeat is invalid`);
      }
    } else if (key.kind === "text") {
      if (typeof key.text !== "string" || !key.text) {
        structural.push(`${rowName}.keys[${index}].text is invalid`);
      }
      if (key.submit !== undefined && typeof key.submit !== "boolean") {
        structural.push(`${rowName}.keys[${index}].submit is invalid`);
      }
    } else if (key.kind === "modifier") {
      if (!["ctrl", "alt", "shift"].includes(key.modifier as string)) {
        structural.push(`${rowName}.keys[${index}].modifier is invalid`);
      }
      if (key.tapLocks !== undefined && typeof key.tapLocks !== "boolean") {
        structural.push(`${rowName}.keys[${index}].tapLocks is invalid`);
      }
    } else if (key.kind === "action") {
      if (key.action !== "keyboard") {
        structural.push(`${rowName}.keys[${index}].action is invalid`);
      }
    } else {
      structural.push(`${rowName}.keys[${index}].kind is invalid`);
    }
    const normalized = key as unknown as TerminalBarKey;
    if (index < (row.pinned ?? 0) && (key.width === 1 || key.width === 2)) {
      pinnedCells += key.width;
    }
    keys.push(normalized);
  });
  if (pinnedCells > ROW_CELLS) structural.push(`${rowName} pinned keys exceed ${ROW_CELLS} cells`);
  const pageable = keys.slice(row.pinned ?? 0);
  if (pageable.length > 0 && (row.pinned ?? 0) <= keys.length) {
    const widest = Math.max(...pageable.map((key) => (key.width === 1 ? 1 : 2)));
    if (ROW_CELLS - pinnedCells < widest)
      structural.push(`${rowName} pinned keys leave no room for paged keys`);
  }
  return {
    keys,
    pinned: row.pinned ?? 0,
    enabled: row.enabled !== false,
  };
}

export function validateBar(bar: unknown, options: ValidationOptions = {}): BarValidation {
  const structural: string[] = [];
  const limits: BarLimitIssue[] = [];
  if (!bar || typeof bar !== "object" || Array.isArray(bar)) {
    return { structural: ["terminal bar is not an object"], limits };
  }
  const raw = bar as Partial<TerminalBar>;
  const seenIds = new Set<string>();
  const top = validateRow(raw.top, "top", seenIds, structural);
  const bottom = validateRow(raw.bottom, "bottom", seenIds, structural);
  if (!top || !bottom) return { structural, limits };
  const beforeKeys = new Map(
    (options.before ? flattenBar(options.before) : []).map((key) => [key.id, key]),
  );

  const checkRow = (row: TerminalBarRow, rowName: "top" | "bottom") => {
    const beforeRow = options.before?.[rowName];
    const pageCount = packRow(row).pages.length;
    const beforePages =
      beforeRow && Array.isArray(beforeRow.keys) ? packRow(beforeRow).pages.length : undefined;
    if (pageCount > MAX_ROW_PAGES) {
      limits.push({
        row: rowName,
        kind: "pages",
        worse: beforePages === undefined || pageCount > beforePages,
      });
    }
    row.keys.forEach((key) => {
      const beforeKey = beforeKeys.get(key.id);
      if (typeof key.id === "string" && key.id.length > MAX_KEY_ID_LENGTH) {
        limits.push({
          row: rowName,
          keyId: key.id,
          kind: "id",
          worse: !beforeKey || beforeKey.id !== key.id,
        });
      }
      if (typeof key.label === "string" && key.label.length > MAX_KEY_LABEL_LENGTH) {
        limits.push({
          row: rowName,
          keyId: key.id,
          kind: "label",
          worse: !beforeKey || beforeKey.label !== key.label,
        });
      }
      if (
        key.kind === "text" &&
        typeof key.text === "string" &&
        key.text.length > MAX_KEY_TEXT_LENGTH
      ) {
        limits.push({
          row: rowName,
          keyId: key.id,
          kind: "text",
          worse: !beforeKey || beforeKey.kind !== "text" || beforeKey.text !== key.text,
        });
      }
    });
  };
  checkRow(top, "top");
  checkRow(bottom, "bottom");
  return { structural, limits };
}

export function hasStructuralErrors(validation: BarValidation): boolean {
  return validation.structural.length > 0;
}

function rowFromUnknown(raw: unknown): TerminalBarRow {
  return raw && typeof raw === "object" && !Array.isArray(raw)
    ? (raw as TerminalBarRow)
    : { keys: [], pinned: 0, enabled: false };
}

/** Reconcile legacy edits made by an older build after the last new-build write. */
export function reconcileLegacy(bar: TerminalBar, items: unknown, secondRow: unknown): TerminalBar {
  const legacy = normalizeTerminalButtons(items);
  const projected = projectLegacy(bar);
  const projectedById = new Map(projected.map((item) => [item.id, item]));
  const legacyIds = new Set(legacy.map((item) => item.id));
  const topSource = rowFromUnknown(bar.top);
  const bottomSource = rowFromUnknown(bar.bottom);
  const allKeys = [...topSource.keys, ...bottomSource.keys];
  const keyById = new Map(allKeys.map((key) => [key.id, key]));

  const orderProjectable: TerminalBarKey[] = [];
  const appended: TerminalBarKey[] = [];
  const keptPinned = new Map<string, boolean>();

  for (const entry of legacy) {
    const before = keyById.get(entry.id);
    const projectedEntry = projectedById.get(entry.id);
    if (!before || !projectedEntry) {
      appended.push({
        ...legacyItemToBarKey(entry),
        label: entry.label.slice(0, 24),
        width: 2,
        display: "text",
      });
      continue;
    }
    let next: TerminalBarKey = canonicalizeBarKey(before);
    if (sameValue(entry, projectedEntry)) {
      next = canonicalizeBarKey(before);
    } else {
      const beforeProjected = projectedEntry;
      const labelChanged = entry.label !== beforeProjected.label;
      const functionChanged =
        entry.kind !== beforeProjected.kind ||
        (entry.kind === "key" &&
          beforeProjected.kind === "key" &&
          !sameValue(entry.chord, beforeProjected.chord)) ||
        (entry.kind === "modifier" &&
          beforeProjected.kind === "modifier" &&
          entry.modifier !== beforeProjected.modifier);
      if (labelChanged) next = { ...next, label: entry.label };
      if (functionChanged) {
        next = legacyItemToBarKey(entry);
        next = {
          ...next,
          id: before.id,
          label: labelChanged ? entry.label : before.label,
          width: before.width,
          display: before.display,
          ...(before.icon ? { icon: before.icon } : {}),
          ...(next.kind === "modifier" && before.kind === "modifier" && before.tapLocks
            ? { tapLocks: true }
            : {}),
        };
      }
      if (next.kind === "key" && next.repeat) {
        // An unchanged legacy entry keeps the old build's repeat value only when
        // its function changed; otherwise the richer new-build key wins.
        if (!functionChanged) next = { ...next, repeat: before.kind === "key" && before.repeat };
      }
    }
    orderProjectable.push(next);
    const wasPinned =
      (topSource.keys.indexOf(before) >= 0 && topSource.keys.indexOf(before) < topSource.pinned) ||
      (bottomSource.keys.indexOf(before) >= 0 &&
        bottomSource.keys.indexOf(before) < bottomSource.pinned);
    keptPinned.set(before.id, wasPinned);
  }

  const notProjectable = allKeys.filter(
    (key) => !isProjectable(key) && !legacyIds.has(key.id) && keyById.has(key.id),
  );
  for (const source of [topSource, bottomSource]) {
    for (const key of source.keys.slice(0, source.pinned)) {
      if (!isProjectable(key)) keptPinned.set(key.id, true);
    }
  }
  const projectableInRows = {
    top: orderProjectable.filter((key) => topSource.keys.some((item) => item.id === key.id)),
    bottom: orderProjectable.filter((key) => bottomSource.keys.some((item) => item.id === key.id)),
  };
  const rowKeys = (rowName: "top" | "bottom", source: TerminalBarRow): TerminalBarKey[] => {
    const projectable = projectableInRows[rowName];
    const extras = notProjectable.filter((key) => source.keys.some((item) => item.id === key.id));
    const extrasById = new Map(extras.map((key) => [key.id, key]));
    const output = [...projectable];
    source.keys.forEach((key, index) => {
      if (!extrasById.has(key.id)) return;
      const at = Math.min(index, output.length);
      output.splice(at, 0, extrasById.get(key.id)!);
    });
    if (rowName === "bottom") {
      output.push(...appended);
    }
    // Preserve pinned membership, moving surviving pinned keys to the front.
    const pinnedKeys = output.filter((key) => keptPinned.get(key.id));
    const regular = output.filter((key) => !keptPinned.get(key.id));
    return [...pinnedKeys, ...regular];
  };
  const topKeys = rowKeys("top", topSource);
  const bottomKeys = rowKeys("bottom", bottomSource);
  return canonicalizeBar({
    top: {
      keys: topKeys,
      pinned: topKeys.filter((key) => keptPinned.get(key.id)).length,
      enabled: true,
    },
    bottom: {
      keys: bottomKeys,
      pinned: bottomKeys.filter((key) => keptPinned.get(key.id)).length,
      enabled: secondRow !== false,
    },
  });
}

export function packRow(row: TerminalBarRow): {
  pinned: TerminalBarKey[];
  pages: TerminalBarKey[][];
  pagerCells: number;
} {
  const keys = Array.isArray(row.keys) ? row.keys : [];
  const pinnedCount = Math.max(0, Math.min(row.pinned, keys.length));
  const pinned = keys.slice(0, pinnedCount);
  const pagerCells = ROW_CELLS - pinned.reduce((total, key) => total + key.width, 0);
  const pages: TerminalBarKey[][] = [];
  let page: TerminalBarKey[] = [];
  let used = 0;
  for (const key of keys.slice(pinnedCount)) {
    const width = key.width === 1 ? 1 : 2;
    if (page.length > 0 && used + width > pagerCells) {
      pages.push(page);
      page = [];
      used = 0;
    }
    page.push(key);
    used += width;
  }
  pages.push(page);
  return { pinned, pages, pagerCells };
}

/** Insert at a boundary in the original row, adjusting for source removal. */
export function moveBarKey(
  bar: TerminalBar,
  id: string,
  toRow: "top" | "bottom",
  boundary: number,
  pinned: boolean,
): TerminalBar {
  const fromRow = bar.top.keys.some((key) => key.id === id) ? "top" : "bottom";
  const candidate = structuredClone(bar);
  const source = candidate[fromRow];
  const from = source.keys.findIndex((key) => key.id === id);
  if (from < 0) return bar;
  const [key] = source.keys.splice(from, 1);
  if (from < source.pinned) source.pinned -= 1;
  const target = candidate[toRow];
  let index = boundary - (fromRow === toRow && from < boundary ? 1 : 0);
  index = pinned ? Math.min(index, target.pinned) : Math.max(index, target.pinned);
  target.keys.splice(Math.max(0, Math.min(index, target.keys.length)), 0, key);
  if (pinned) target.pinned += 1;
  return candidate;
}

export function flattenBar(bar: TerminalBar): TerminalBarKey[] {
  return [...bar.top.keys, ...bar.bottom.keys];
}

export function barHeightPx(bar: TerminalBar, oneLine: boolean): number {
  const lines = oneLine ? 1 : bar.bottom.enabled ? 2 : 1;
  const topPages = packRow(rowFromUnknown(bar.top)).pages.length;
  const bottomPages = packRow(rowFromUnknown(bar.bottom)).pages.length;
  const pages = topPages > 1 || (bar.bottom.enabled && bottomPages > 1);
  return lines * 44 + (lines - 1) * 2 + 3 + (pages ? PAGE_STRIP_PX : 0);
}

function parseBar(value: unknown): TerminalBar | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  return value as TerminalBar;
}

export function resolveTerminalBar(stored: Record<string, unknown>): ResolvedTerminalBar {
  const rawBar = stored.terminalBar;
  const rawPreset = stored.terminalBarPreset;
  const legacy = migrateLegacy(stored.terminalButtons, stored.terminalSecondRow);
  const legacyValidation = validateBar(legacy);
  const fallback: ResolvedTerminalBar =
    !Array.isArray(stored.terminalButtons) || hasStructuralErrors(legacyValidation)
      ? { bar: defaultTerminalBar(), source: "fallback-defaults", preset: null }
      : { bar: legacy, source: "fallback-legacy", preset: null };

  let resolved: ResolvedTerminalBar;
  if (rawBar === undefined || rawBar === null) {
    resolved = { bar: legacy, source: "legacy", preset: null };
  } else {
    const storedBar = parseBar(rawBar);
    const storedValidation = storedBar ? validateBar(storedBar) : null;
    if (!storedBar || (storedValidation && hasStructuralErrors(storedValidation))) {
      resolved = fallback;
    } else {
      const basis =
        typeof stored.terminalLegacyBasis === "string" ? stored.terminalLegacyBasis : null;
      const fingerprint = legacyFingerprint(stored.terminalButtons, stored.terminalSecondRow);
      if (basis !== null && basis !== fingerprint) {
        const reconciled = reconcileLegacy(
          storedBar,
          stored.terminalButtons,
          stored.terminalSecondRow,
        );
        const validation = validateBar(reconciled);
        resolved = hasStructuralErrors(validation)
          ? fallback
          : { bar: canonicalizeBar(reconciled), source: "reconciled", preset: null };
      } else {
        resolved = { bar: canonicalizeBar(storedBar), source: "stored", preset: null };
      }
    }
  }

  if (rawPreset !== undefined && rawPreset !== null) {
    const preset = parseBar(rawPreset);
    resolved.preset = preset && !hasStructuralErrors(validateBar(preset)) ? preset : "invalid";
  } else {
    resolved.preset = null;
  }
  return resolved;
}

function sortedForServer(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sortedForServer);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.keys(value as Record<string, unknown>)
        .sort()
        .map((key) => [key, sortedForServer((value as Record<string, unknown>)[key])]),
    );
  }
  return value;
}

/** Exact server rule: UTF-8 JSON, sorted keys, no spaces, prompt namespace excluded. */
export function preferencesEnvelopeBytes(envelope: {
  version: 1;
  settings: Record<string, unknown>;
}): number {
  const { builtin_agent_prompts: _excluded, ...settings } = envelope.settings;
  void _excluded;
  const serialized = JSON.stringify(sortedForServer({ version: envelope.version, settings }));
  return new TextEncoder().encode(serialized).length;
}
