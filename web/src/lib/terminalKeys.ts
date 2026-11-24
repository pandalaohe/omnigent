import type { ShortcutChord, ShortcutModifier } from "@/lib/keyboardShortcutPreferences";
import { unescapeFromDisplay } from "@/lib/keySequence";
import {
  defaultMobileAssistantButtons,
  MOBILE_ASSISTANT_CHANGED_EVENT,
  MOBILE_ASSISTANT_STORAGE_KEY,
  readMobileAssistantPreferences,
  softKeyForChord,
  terminalDataForChord,
  type MobileAssistantPreferences,
} from "@/lib/mobileAssistantPreferences";
import {
  MAX_PREFERENCE_ENVELOPE_BYTES,
  barFingerprint,
  canonicalTerminalFields,
  canonicalizeBar,
  canonicalizeBarKey,
  EMPTY_STICKY_MODIFIERS,
  isTerminalKeyIcon,
  legacyFingerprint,
  normalizeChord,
  preferencesEnvelopeBytes,
  projectLegacy,
  resolveTerminalBar,
  validateBar,
  type BarValidation,
  type TerminalBar,
  type TerminalBarKey,
  type TerminalKeyDisplay,
  type TerminalKeyWidth,
  type TerminalStickyModifiers,
} from "@/lib/terminalKeyBar";
import { collectLocalUserPreferences, queueUserPreferencePatch } from "@/lib/userPreferencesSync";
import {
  applyTerminalKeyboardChanges,
  type TerminalKeyboardChange,
} from "@/lib/terminalKeyboardChanges";

export type KeySurface = "shell" | "native";
export const TERMINAL_KEY_SURFACE = {
  hostTerminals: "shell",
  terminalsPanel: "shell",
  mainTerminal: "native",
} as const satisfies Record<string, KeySurface>;
export const KEY_BAR_GATE: "layout" | "device" = "layout";
export const SPLIT_LONE_ESC_MS = 0;
export type CmdKeyMode = "mac-meta" | "meta" | "mac" | "none";
export const CMD_KEY_MODE: CmdKeyMode = "mac-meta";
export type ClipboardOp = "copy" | "paste" | "pasteHost";
export const TERMINAL_CLIPBOARD_KEYS: ReadonlySet<ClipboardOp> = new Set([
  "copy",
  "paste",
  "pasteHost",
]);
export const MAX_SEND_BYTES = 256;

const SPECIAL_MODIFIERS = ["ctrl", "shift", "alt", "opt", "cmd", "win"] as const;
export type SpecialModifier = (typeof SPECIAL_MODIFIERS)[number];
export type SpecialId = SpecialModifier | "tab" | "esc";
/** A terminal chord can use a logical character as its code. */
export type BarChord = ShortcutChord;
export type ArmedModifiers = TerminalStickyModifiers;

type LegacyBarKey =
  | Pick<Extract<TerminalBarKey, { kind: "key" }>, "kind" | "chord" | "repeat">
  | Pick<Extract<TerminalBarKey, { kind: "text" }>, "kind" | "text" | "submit">;

export type KeyAction =
  | { type: "send"; send: string; autoEnter?: true }
  | { type: "special"; special: SpecialId; keepHeld?: true }
  | { type: "combo"; chord: BarChord }
  | { type: "legacy"; key: LegacyBarKey }
  | { type: "keyboard" }
  | { type: "clipboard"; op: ClipboardOp; autoEnter?: boolean };

interface UnknownAction {
  type: string;
  [field: string]: unknown;
}

export interface TerminalKey<Action extends UnknownAction | KeyAction = KeyAction> {
  id: string;
  label: string;
  width: TerminalKeyWidth;
  display: TerminalKeyDisplay;
  icon?: string;
  style?: "danger";
  repeat?: true;
  action: Action;
}

type StoredKey = TerminalKey<KeyAction | UnknownAction>;
export interface TerminalKeyRowV2 {
  keys: StoredKey[];
  pinned: number;
  enabled: boolean;
}
export interface SurfaceKeys {
  enabled: boolean;
  showRows?: "keyboard";
  top: TerminalKeyRowV2;
  bottom: TerminalKeyRowV2;
}
export interface TerminalKeys {
  v: 2;
  shell: SurfaceKeys;
  native: SurfaceKeys;
  barBasis: string;
  enabledBasis: boolean;
}
export type TerminalKeysPreset = Partial<Record<KeySurface, SurfaceKeys>>;

type TerminalKeyPreferencesFields = Partial<
  Pick<MobileAssistantPreferences, "terminalKeys" | "terminalKeysPreset" | "terminalKeyboard">
>;

function record(value: unknown): Record<string, unknown> | undefined {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : undefined;
}

function isSpecialId(value: unknown): value is SpecialId {
  return (
    SPECIAL_MODIFIERS.some((special) => special === value) || value === "tab" || value === "esc"
  );
}

export function specialModifier(key: StoredKey): SpecialModifier | undefined {
  const action = key.action;
  if (action.type !== "special") return;
  return SPECIAL_MODIFIERS.find((special) => special === action.special);
}

export function keySupportsRepeat(key: StoredKey): boolean {
  const action = availableAction(key);
  if (!action) return false;
  return (
    action.type === "send" ||
    (action.type === "combo" && "bytes" in applyCmd({ chord: action.chord }, CMD_KEY_MODE)) ||
    (action.type === "legacy" && action.key.kind === "key") ||
    (action.type === "special" && (action.special === "tab" || action.special === "esc"))
  );
}

/** Unknown actions remain in storage and render as unavailable. */
export function availableAction(key: StoredKey): KeyAction | undefined {
  const action = key.action;
  switch (action.type) {
    case "special":
      if (CMD_KEY_MODE === "none" && (action.special === "cmd" || action.special === "win"))
        return undefined;
      return isSpecialId(action.special) ? (action as KeyAction) : undefined;
    case "clipboard":
      return TERMINAL_CLIPBOARD_KEYS.has(action.op as ClipboardOp)
        ? (action as KeyAction)
        : undefined;
    case "send":
    case "combo":
    case "legacy":
    case "keyboard":
      return action as KeyAction;
    default:
      return undefined;
  }
}

function migrateKey(input: TerminalBarKey): TerminalKey {
  const key = canonicalizeBarKey(input);
  const { id, label, display, width, icon } = key;
  const base = { id, label, display, width, ...(icon ? { icon } : {}) };
  switch (key.kind) {
    case "key":
      return {
        ...base,
        ...(key.repeat ? { repeat: true } : {}),
        action: {
          type: "legacy",
          key: { kind: "key", chord: key.chord, ...(key.repeat ? { repeat: true } : {}) },
        },
      };
    case "text":
      return {
        ...base,
        action: {
          type: "legacy",
          key: { kind: "text", text: key.text, ...(key.submit ? { submit: true } : {}) },
        },
      };
    case "modifier":
      return {
        ...base,
        action: {
          type: "special",
          special: key.modifier,
          ...(key.tapLocks ? { keepHeld: true } : {}),
        },
      };
    case "action":
      return { ...base, action: { type: "keyboard" } };
  }
}

export function fromBar322(bar: TerminalBar, enabled = true): SurfaceKeys {
  return {
    enabled,
    top: { ...bar.top, keys: bar.top.keys.map(migrateKey) },
    bottom: { ...bar.bottom, keys: bar.bottom.keys.map(migrateKey) },
  };
}

function projectKey(key: StoredKey): TerminalBarKey {
  const { id, label, width } = key;
  const icon = isTerminalKeyIcon(key.icon) ? key.icon : undefined;
  const base = {
    id,
    label,
    width,
    display: icon ? key.display : ("text" as const),
    ...(icon ? { icon } : {}),
  };
  const repeat = key.repeat ? { repeat: true } : {};
  const action = availableAction(key);
  switch (action?.type) {
    case "legacy":
      return action.key.kind === "key"
        ? { ...base, kind: "key", chord: action.key.chord, ...repeat }
        : { ...base, ...action.key };
    case "keyboard":
      return { ...base, kind: "action", action: "keyboard" };
    case "special":
      if (
        action.special === "ctrl" ||
        action.special === "shift" ||
        action.special === "alt" ||
        action.special === "opt"
      ) {
        return {
          ...base,
          kind: "modifier",
          modifier: action.special === "opt" ? "alt" : action.special,
          ...(action.keepHeld ? { tapLocks: true } : {}),
        };
      }
      break;
    case "combo": {
      const output = encodeChord(action.chord, EMPTY_STICKY_MODIFIERS, false);
      if ("bytes" in output && output.bytes === terminalDataForChord(action.chord)) {
        return { ...base, kind: "key", chord: action.chord, ...repeat };
      }
      break;
    }
    case "send": {
      const data = unescapeFromDisplay(action.send);
      const chord = chordForSequence(data);
      if (
        chord &&
        (!action.autoEnter || isSingleControl(data)) &&
        (softKeyForChord(chord) !== null || terminalDataForChord(chord) !== null)
      ) {
        return { ...base, kind: "key", chord, ...repeat };
      }
      if (data && !Array.from(data).some((char) => isSingleControl(char))) {
        return { ...base, kind: "text", text: data, ...(action.autoEnter ? { submit: true } : {}) };
      }
    }
  }
  return { ...base, kind: "key", chord: { code: "Unidentified", modifiers: ["meta"] } };
}

export function toBar322(surface: SurfaceKeys): TerminalBar {
  return canonicalizeBar({
    top: { ...surface.top, keys: surface.top.keys.map(projectKey) },
    bottom: { ...surface.bottom, keys: surface.bottom.keys.map(projectKey) },
  });
}

function sameFunction(left: TerminalBarKey, right: TerminalBarKey): boolean {
  const {
    id: _id,
    label: _label,
    width: _width,
    display: _display,
    icon: _icon,
    ...leftBinding
  } = canonicalizeBarKey(left);
  const {
    id: _rightId,
    label: _rightLabel,
    width: _rightWidth,
    display: _rightDisplay,
    icon: _rightIcon,
    ...rightBinding
  } = canonicalizeBarKey(right);
  return JSON.stringify(leftBinding) === JSON.stringify(rightBinding);
}

export function reconcileShell(surface: SurfaceKeys, bar: TerminalBar): SurfaceKeys {
  const stored = new Map([...surface.top.keys, ...surface.bottom.keys].map((key) => [key.id, key]));
  const reconcile = (row: TerminalBar["top"]): TerminalKeyRowV2 => ({
    ...row,
    keys: row.keys.map((key) => {
      const previous = stored.get(key.id);
      const migrated = migrateKey(key);
      if (!previous) return migrated;
      const projection = canonicalizeBarKey(projectKey(previous));
      if (JSON.stringify(projection) === JSON.stringify(canonicalizeBarKey(key))) return previous;
      const sameAppearance = projection.icon === key.icon && projection.display === key.display;
      const { icon, repeat, ...base } = migrated;
      const nextIcon = sameAppearance ? previous.icon : icon;
      const sameAction = sameFunction(projection, key);
      const nextRepeat = sameAction ? previous.repeat : repeat;
      return {
        ...base,
        display: sameAppearance ? previous.display : migrated.display,
        ...(nextIcon ? { icon: nextIcon } : {}),
        ...(previous.style ? { style: previous.style } : {}),
        ...(nextRepeat ? { repeat: nextRepeat } : {}),
        action: sameAction ? previous.action : migrated.action,
      };
    }),
  });
  return { ...surface, top: reconcile(bar.top), bottom: reconcile(bar.bottom) };
}

function validAction(value: unknown): boolean {
  const action = record(value);
  if (!action || typeof action.type !== "string" || !action.type) return false;
  if (action.type === "send")
    return (
      typeof action.send === "string" &&
      !!action.send &&
      (action.autoEnter === undefined || action.autoEnter === true)
    );
  if (action.type === "combo")
    return normalizeChord(action.chord)?.modifiers.includes("meta") === true;
  if (action.type === "special")
    return (
      typeof action.special === "string" &&
      (!isSpecialId(action.special) ||
        action.keepHeld === undefined ||
        (action.keepHeld === true && action.special !== "tab" && action.special !== "esc"))
    );
  if (action.type === "clipboard")
    return (
      typeof action.op === "string" &&
      (!TERMINAL_CLIPBOARD_KEYS.has(action.op as ClipboardOp) ||
        (action.op === "copy"
          ? action.autoEnter === undefined
          : typeof action.autoEnter === "boolean"))
    );
  if (action.type !== "legacy") return true;
  const key = record(action.key);
  if (key?.kind === "key")
    return (
      !!normalizeChord(key.chord) && (key.repeat === undefined || typeof key.repeat === "boolean")
    );
  return (
    key?.kind === "text" &&
    typeof key.text === "string" &&
    !!key.text &&
    (key.submit === undefined || typeof key.submit === "boolean")
  );
}

export function validateTerminalKeySurface(value: unknown, before?: SurfaceKeys): BarValidation {
  const surface = record(value);
  if (
    !surface ||
    typeof surface.enabled !== "boolean" ||
    (surface.showRows !== undefined && surface.showRows !== "keyboard")
  ) {
    return { structural: ["invalid surface switches"], limits: [] };
  }
  const structural: string[] = [];
  const sendLimits: BarValidation["limits"] = [];
  const beforeKeys = new Map(
    before ? [...before.top.keys, ...before.bottom.keys].map((key) => [key.id, key.action]) : [],
  );
  const projectForValidation = (raw: unknown, rowName: "top" | "bottom") => {
    const row = record(raw);
    if (!row || !Array.isArray(row.keys)) return raw;
    return {
      ...row,
      keys: row.keys.map((candidate: unknown, index: number) => {
        const key = record(candidate);
        if (!key) return candidate;
        if (
          !validAction(key.action) ||
          (key.repeat !== undefined && key.repeat !== true) ||
          (key.style !== undefined && key.style !== "danger") ||
          ((key.icon !== undefined || key.display === "icon") &&
            (typeof key.icon !== "string" || !/^[a-z0-9-]{1,32}$/.test(key.icon)))
        ) {
          structural.push(`${rowName}.keys[${index}] has invalid action or presentation`);
        }
        const action = record(key.action);
        if (action?.type === "send" && typeof action.send === "string") {
          const length = new TextEncoder().encode(unescapeFromDisplay(action.send)).length;
          if (length > MAX_SEND_BYTES) {
            const previous = typeof key.id === "string" ? beforeKeys.get(key.id) : undefined;
            const previousLength =
              previous?.type === "send" && typeof previous.send === "string"
                ? new TextEncoder().encode(unescapeFromDisplay(previous.send)).length
                : 0;
            sendLimits.push({
              row: rowName,
              keyId: typeof key.id === "string" ? key.id : undefined,
              kind: "text",
              worse: length > previousLength,
            });
          }
        }
        return {
          ...key,
          display: key.display === "icon" ? "text" : key.display,
          ...(action?.type === "legacy" && record(action.key)?.kind === "text"
            ? { kind: "text", text: record(action.key)?.text }
            : { kind: "action", action: "keyboard" }),
        };
      }),
    };
  };
  const validation = validateBar(
    {
      top: projectForValidation(surface.top, "top"),
      bottom: projectForValidation(surface.bottom, "bottom"),
    },
    { before: before && toBar322(before) },
  );
  return {
    structural: [...structural, ...validation.structural],
    limits: [...sendLimits, ...validation.limits],
  };
}

export function validateKeys(
  value: unknown,
  options: { before?: TerminalKeys } = {},
): BarValidation {
  const keys = record(value);
  if (
    !keys ||
    keys.v !== 2 ||
    typeof keys.enabledBasis !== "boolean" ||
    typeof keys.barBasis !== "string"
  ) {
    return { structural: ["invalid terminal key layout basis"], limits: [] };
  }
  const shell = validateTerminalKeySurface(keys.shell, options.before?.shell);
  const native = validateTerminalKeySurface(keys.native, options.before?.native);
  return {
    structural: [
      ...shell.structural.map((error) => `shell: ${error}`),
      ...native.structural.map((error) => `native: ${error}`),
    ],
    limits: [...shell.limits, ...native.limits],
  };
}

export function resolveTerminalKeys(stored: Record<string, unknown>): {
  keys: TerminalKeys;
  source: "stored" | "migrated" | "reconciled" | "fallback";
  preset: Partial<Record<KeySurface, SurfaceKeys | "invalid">>;
} {
  const effective = resolveTerminalBar(stored);
  const enabled = stored.terminalKeysEnabled !== false;
  const basis = barFingerprint(effective.bar);
  const fallback = effective.source.startsWith("fallback-");
  const raw = stored.terminalKeys;
  const valid = !!raw && validateKeys(raw).structural.length === 0;
  let keys: TerminalKeys;
  let source: ReturnType<typeof resolveTerminalKeys>["source"];
  if (!valid) {
    const shell = fromBar322(effective.bar, enabled);
    keys = { v: 2, shell, native: structuredClone(shell), barBasis: basis, enabledBasis: enabled };
    source = raw !== undefined || fallback ? "fallback" : "migrated";
  } else {
    keys = raw as TerminalKeys;
    source = "stored";
    if (!fallback && basis !== keys.barBasis) {
      keys = { ...keys, shell: reconcileShell(keys.shell, effective.bar), barBasis: basis };
      source = "reconciled";
    }
    if (enabled !== keys.enabledBasis) {
      keys = { ...keys, shell: { ...keys.shell, enabled }, enabledBasis: enabled };
      source = "reconciled";
    }
  }
  const preset: ReturnType<typeof resolveTerminalKeys>["preset"] = {};
  const rawPresets = record(stored.terminalKeysPreset);
  for (const surface of ["shell", "native"] as const) {
    const rawPreset = rawPresets?.[surface];
    if (rawPreset !== undefined) {
      preset[surface] =
        validateTerminalKeySurface(rawPreset).structural.length === 0
          ? (rawPreset as SurfaceKeys)
          : "invalid";
    }
  }
  if (effective.preset && effective.preset !== "invalid") {
    const shellPreset = preset.shell;
    preset.shell =
      shellPreset && shellPreset !== "invalid"
        ? reconcileShell(shellPreset, effective.preset)
        : fromBar322(effective.preset);
  } else if (!preset.shell && effective.preset === "invalid") preset.shell = "invalid";
  return { keys, source, preset };
}

const NAMED_KEYS: Record<string, string> = {
  A: "ArrowUp",
  B: "ArrowDown",
  C: "ArrowRight",
  D: "ArrowLeft",
  H: "Home",
  F: "End",
  P: "F1",
  Q: "F2",
  R: "F3",
  S: "F4",
  "2": "Insert",
  "3": "Delete",
  "5": "PageUp",
  "6": "PageDown",
  "15": "F5",
  "17": "F6",
  "18": "F7",
  "19": "F8",
  "20": "F9",
  "21": "F10",
  "23": "F11",
  "24": "F12",
};
const CONTROL_KEYS: Record<string, string> = {
  "\x00": "Space",
  "\x1c": "Backslash",
  "\x1d": "BracketRight",
  "\x1e": "Digit6",
  "\x1f": "Slash",
};
const PLAIN_KEY_SEQUENCES = new Map([
  ["\x1b", "Escape"],
  ["\t", "Tab"],
  ["\r", "Enter"],
  ["\x7f", "Backspace"],
]);

function isSingleControl(data: string): boolean {
  return data.length === 1 && (data.charCodeAt(0) < 32 || data === "\x7f");
}

export function chordForSequence(data: string): BarChord | undefined {
  const plain = PLAIN_KEY_SEQUENCES.get(data);
  if (plain) return { code: plain, modifiers: [] };
  if (data === "\x1b[Z") return { code: "Tab", modifiers: ["shift"] };
  if (data === "\x1b[13;2u") return { code: "Enter", modifiers: ["shift"] };
  if (data === "\x1b\r") return { code: "Enter", modifiers: ["alt"] };
  if (data === "\x1b\x7f") return { code: "Backspace", modifiers: ["alt"] };
  if (isSingleControl(data)) {
    const single = data.charCodeAt(0);
    return {
      code: CONTROL_KEYS[data] ?? `Key${String.fromCharCode(single + 64)}`,
      modifiers: ["control"],
    };
  }
  if (!data.startsWith("\x1b")) return;
  const sequence = data.slice(1);
  const letter = /^(?:O([A-DFHP-S])|\[(?:1;([2-8]))?([A-DFHP-S]))$/.exec(sequence);
  const tilde = /^\[(\d{1,2})(?:;([2-8]))?~$/.exec(sequence);
  const code = letter ? NAMED_KEYS[letter[1] ?? letter[3]] : tilde && NAMED_KEYS[tilde[1]];
  if (!code) return;
  const mask = Number(letter?.[2] ?? tilde?.[2] ?? 1) - 1;
  const modifiers: ShortcutModifier[] = [];
  if (mask & 1) modifiers.push("shift");
  if (mask & 2) modifiers.push("alt");
  if (mask & 4) modifiers.push("control");
  return { code, modifiers };
}

type KeyOutput = { bytes: string } | { clipboard: "copy" | "paste" } | { refused: string };

function chordBytes(chord: BarChord, appCursor: boolean): string | null {
  const bytes = terminalDataForChord(chord, appCursor);
  if (bytes !== null) return bytes;
  if (chord.code === "Enter" && chord.modifiers.length === 0) return "\r";
  if (chord.code === "Digit6" && chord.modifiers.includes("control")) {
    return `${chord.modifiers.includes("alt") ? "\x1b" : ""}\x1e`;
  }
  if (chord.code === "Space" && !chord.modifiers.includes("control")) {
    return chord.modifiers.includes("alt") ? "\x1b " : " ";
  }
  if (chord.code === "Tab" || chord.code === "Escape") {
    const data =
      chord.code === "Escape" ? "\x1b" : chord.modifiers.includes("shift") ? "\x1b[Z" : "\t";
    return `${chord.modifiers.includes("alt") ? "\x1b" : ""}${data}`;
  }
  return null;
}

export function applyCmd(
  input: { chord: BarChord } | { char: string },
  mode: CmdKeyMode,
  appCursor = false,
): KeyOutput {
  const chord = "chord" in input ? input.chord : undefined;
  const character = chord && Array.from(chord.code).length === 1 ? chord.code : undefined;
  const key =
    "char" in input ? input.char.toLowerCase() : (character?.toLowerCase() ?? chord?.code);
  if (mode === "mac-meta" || mode === "mac") {
    const hasOtherModifiers = chord?.modifiers.some((modifier) => modifier !== "meta") ?? false;
    // Cmd+V always goes through the clipboard path, even with other modifiers armed.
    if (key === "v" || key === "KeyV") return { clipboard: "paste" };
    if (!hasOtherModifiers) {
      if (key === "c" || key === "KeyC") return { clipboard: "copy" };
      const lineKeys: Record<string, string> = {
        ArrowLeft: "\x01",
        ArrowRight: "\x05",
        Backspace: "\x15",
        Enter: "\r",
      };
      if (key && Object.hasOwn(lineKeys, key)) return { bytes: lineKeys[key] };
    }
  }
  if (mode === "meta" || mode === "mac-meta") {
    if ("char" in input) return { bytes: `\x1b${input.char}` };
    if (character !== undefined) {
      return {
        bytes: applyTerminalTextModifiers(character, {
          ...EMPTY_STICKY_MODIFIERS,
          ctrl: input.chord.modifiers.includes("control") ? "once" : "off",
          alt: "once",
        }),
      };
    }
    const modifiers = new Set(
      input.chord.modifiers.filter((modifier) => modifier !== "meta" && modifier !== "primary"),
    );
    modifiers.add("alt");
    const bytes = chordBytes({ code: input.chord.code, modifiers: [...modifiers] }, appCursor);
    if (bytes !== null) return { bytes };
  }
  return { refused: "This Command shortcut has no terminal meaning" };
}

export function isArmedModifier(armed: ArmedModifiers, modifier: SpecialModifier): boolean {
  const state = armed[modifier];
  return state !== undefined && state !== "off";
}

export function encodeChord(chord: BarChord, armed: ArmedModifiers, appCursor: boolean): KeyOutput {
  const modifiers = new Set(chord.modifiers);
  if (isArmedModifier(armed, "ctrl")) modifiers.add("control");
  if (isArmedModifier(armed, "shift")) modifiers.add("shift");
  if (isArmedModifier(armed, "alt") || isArmedModifier(armed, "opt")) modifiers.add("alt");
  const code =
    isArmedModifier(armed, "shift") && /^[a-z]$/.test(chord.code)
      ? chord.code.toUpperCase()
      : chord.code;
  const withArmed = { code, modifiers: [...modifiers] };
  if (modifiers.has("meta") || isArmedModifier(armed, "cmd") || isArmedModifier(armed, "win")) {
    return applyCmd(withChord(withArmed), CMD_KEY_MODE, appCursor);
  }
  const bytes = chordBytes(withArmed, appCursor);
  return bytes === null ? { refused: "This shortcut has no terminal sequence" } : { bytes };
}

function withChord(chord: BarChord): { chord: BarChord } {
  return {
    chord: { ...chord, modifiers: [...new Set<ShortcutModifier>([...chord.modifiers, "meta"])] },
  };
}

export function applyTerminalTextModifiers(data: string, armed: ArmedModifiers): string {
  let bytes = data;
  if (Array.from(data).length === 1) {
    if (isArmedModifier(armed, "shift") && /^[a-z]$/.test(bytes)) bytes = bytes.toUpperCase();
    if (isArmedModifier(armed, "ctrl")) {
      const code = bytes.toUpperCase().charCodeAt(0);
      if (code >= 64 && code <= 95) bytes = String.fromCharCode(code & 31);
      else if (bytes === " ") bytes = "\x00";
      else if (bytes === "?") bytes = "\x7f";
      else if (bytes === "/") bytes = "\x1f";
    }
  }
  if (isArmedModifier(armed, "alt") || isArmedModifier(armed, "opt")) bytes = `\x1b${bytes}`;
  return bytes;
}

export function encodeTerminalData(
  data: string,
  armed: ArmedModifiers,
  appCursor: boolean,
): KeyOutput {
  const chord = chordForSequence(data);
  if (chord) return encodeChord(chord, armed, appCursor);
  if (isArmedModifier(armed, "cmd") || isArmedModifier(armed, "win")) {
    if (/^[a-z]$/i.test(data)) {
      return encodeChord({ code: `Key${data.toUpperCase()}`, modifiers: [] }, armed, appCursor);
    }
    return applyCmd({ char: data }, CMD_KEY_MODE, appCursor);
  }
  return { bytes: applyTerminalTextModifiers(data, armed) };
}

export function resolveSend(
  send: string,
  autoEnter: boolean,
  armed: ArmedModifiers,
  appCursor: boolean,
): KeyOutput {
  const data = unescapeFromDisplay(send);
  const result = encodeTerminalData(data, armed, appCursor);
  if (!("bytes" in result) || !autoEnter || isSingleControl(data)) return result;
  return { bytes: result.bytes + "\r" };
}

export function splitTerminalWrites(bytes: string, delay = SPLIT_LONE_ESC_MS): string[] {
  if (!delay) return [bytes];
  // CSI, SS3 and Alt+one character stay in the same write.
  // eslint-disable-next-line no-control-regex -- Separate lone Escape from the next two printable characters.
  return bytes.split(/(?<=\x1b)(?=(?![[O])[^\x00-\x1f\x7f]{2})/u);
}

export function encodeSend(
  send: string,
  autoEnter: boolean,
  armed: ArmedModifiers,
  appCursor: boolean,
): string[] {
  const result = resolveSend(send, autoEnter, armed, appCursor);
  return "bytes" in result ? splitTerminalWrites(result.bytes) : [];
}

export function buildTerminalKeyPreferences(
  fields: TerminalKeyPreferencesFields,
  current: Record<string, unknown>,
  raw?: Record<string, unknown>,
): Record<string, unknown> {
  const keys = fields.terminalKeys ?? resolveTerminalKeys(current).keys;
  const terminalBar = toBar322(keys.shell);
  const terminalButtons = projectLegacy(terminalBar);
  const terminalSecondRow = terminalBar.bottom.enabled;
  const terminalKeysEnabled = keys.shell.enabled;
  const presets = fields.terminalKeysPreset ?? current.terminalKeysPreset;
  const shellPreset = record(presets)?.shell;
  const terminalBarPreset =
    fields.terminalKeysPreset !== undefined
      ? shellPreset !== undefined && !validateTerminalKeySurface(shellPreset).structural.length
        ? toBar322(shellPreset as SurfaceKeys)
        : undefined
      : current.terminalBarPreset;
  const circle =
    raw?.version === 2 && typeof raw.enabled === "boolean" && Array.isArray(raw.buttons)
      ? { enabled: raw.enabled, buttons: raw.buttons, chatActionsRevision: raw.chatActionsRevision }
      : { enabled: true, buttons: defaultMobileAssistantButtons(), chatActionsRevision: 1 };
  return canonicalTerminalFields({
    version: 2,
    ...circle,
    terminalKeys: {
      ...keys,
      barBasis: barFingerprint(terminalBar),
      enabledBasis: terminalKeysEnabled,
    },
    terminalKeysPreset: presets,
    terminalKeyboard: fields.terminalKeyboard ?? current.terminalKeyboard,
    terminalKeysEnabled,
    terminalBar,
    terminalBarPreset,
    terminalButtons,
    terminalSecondRow,
    terminalLegacyBasis: legacyFingerprint(terminalButtons, terminalSecondRow),
  });
}

export function writeTerminalKeyPreferences(
  fields: TerminalKeyPreferencesFields,
  keyboardChange?: TerminalKeyboardChange,
): "saved" | "too-large" | "unavailable" {
  if (typeof window === "undefined") return "unavailable";
  try {
    let raw: Record<string, unknown> | undefined;
    try {
      raw = record(JSON.parse(window.localStorage.getItem(MOBILE_ASSISTANT_STORAGE_KEY) ?? "null"));
    } catch {
      // Malformed stored data is replaced by the default envelope on a save.
    }
    const current = { ...readMobileAssistantPreferences() };
    const canonical = buildTerminalKeyPreferences(
      keyboardChange
        ? {
            terminalKeyboard: applyTerminalKeyboardChanges(current.terminalKeyboard, [
              keyboardChange,
            ]),
          }
        : fields,
      current,
      raw,
    );
    const envelope = collectLocalUserPreferences();
    if (
      preferencesEnvelopeBytes({
        version: 1,
        settings: { ...envelope.settings, mobile_assistant: canonical },
      }) > MAX_PREFERENCE_ENVELOPE_BYTES
    ) {
      return "too-large";
    }
    window.localStorage.setItem(MOBILE_ASSISTANT_STORAGE_KEY, JSON.stringify(canonical));
    queueUserPreferencePatch(
      "mobile_assistant",
      keyboardChange
        ? {
            terminal_keyboard: {
              defaults: {
                version: 2,
                enabled: true,
                buttons: defaultMobileAssistantButtons(),
                chatActionsRevision: 1,
              },
              changes: [keyboardChange],
            },
          }
        : { ...canonical, terminalKeyboard: fields.terminalKeyboard },
    );
    window.dispatchEvent(new Event(MOBILE_ASSISTANT_CHANGED_EVENT));
    return "saved";
  } catch {
    return "unavailable";
  }
}
