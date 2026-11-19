import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { terminalKeyBarGate } from "@/hooks/useIsPhone";
import { escapeLiteral } from "@/lib/keySequence";
import {
  MOBILE_ASSISTANT_STORAGE_KEY,
  readMobileAssistantPreferences,
} from "@/lib/mobileAssistantPreferences";
import {
  defaultTerminalBar,
  canonicalizeBar,
  EMPTY_STICKY_MODIFIERS,
  MAX_PREFERENCE_ENVELOPE_BYTES,
  preferencesEnvelopeBytes,
  validateBar,
  type TerminalBar,
} from "@/lib/terminalKeyBar";
import {
  applyCmd,
  buildTerminalKeyPreferences,
  encodeSend,
  fromBar322,
  reconcileShell,
  resolveTerminalKeys,
  splitTerminalWrites,
  toBar322,
  writeTerminalKeyPreferences,
  type BarChord,
  type KeyAction,
  type SurfaceKeys,
} from "./terminalKeys";

const oldBar = defaultTerminalBar();
oldBar.top.keys.push(
  { id: "caret", label: "Caret", display: "text", width: 1, kind: "text", text: "^C" },
  {
    id: "escape",
    label: "Escape",
    display: "text",
    width: 1,
    kind: "text",
    text: String.raw`echo \n\e`,
    submit: true,
  },
  {
    id: "primary",
    label: "Primary+K",
    display: "text",
    width: 1,
    kind: "key",
    chord: { code: "KeyK", modifiers: ["primary"] },
  },
);
const readRaw = () => JSON.parse(localStorage.getItem(MOBILE_ASSISTANT_STORAGE_KEY) ?? "null");
const envelopeBytes = (mobile_assistant: Record<string, unknown>) =>
  preferencesEnvelopeBytes({ version: 1, settings: { mobile_assistant } });

beforeEach(() => localStorage.clear());
afterEach(() => vi.unstubAllGlobals());

it.each([
  [390, 844, true, false, true, true],
  [844, 390, true, false, true, true],
  [1024, 768, true, true, false, false],
  [600, 900, false, true, true, false],
])(
  "selects the configured key-bar gate at %ix%i",
  (width, height, coarse, fine, layout, device) => {
    vi.stubGlobal("innerWidth", width);
    vi.stubGlobal("innerHeight", height);
    vi.stubGlobal("screen", { width, height });
    vi.stubGlobal("matchMedia", (query: string) => ({
      matches: query.includes("max-width") ? width < 768 : query.includes("coarse") ? coarse : fine,
    }));
    expect(terminalKeyBarGate("layout")).toBe(layout);
    expect(terminalKeyBarGate("device")).toBe(device);
  },
);

it("migrates every legacy key and its preset without changing its projection or saving on read", () => {
  const { keys, preset } = resolveTerminalKeys({ terminalBar: oldBar, terminalBarPreset: oldBar });
  expect(keys.native).not.toBe(keys.shell);
  expect(keys.native).toEqual(keys.shell);
  expect(toBar322(keys.shell)).toEqual(canonicalizeBar(oldBar));
  expect(validateBar(toBar322(keys.shell)).structural).toEqual([]);
  expect(preset.shell).toEqual(keys.shell);
  expect(readRaw()).toBeNull();
});

it.each<[KeyAction, BarChord]>([
  [
    { type: "send", send: "\\x1e" },
    { code: "Unidentified", modifiers: ["meta"] },
  ],
  [
    { type: "send", send: "\\r", autoEnter: true },
    { code: "Enter", modifiers: [] },
  ],
  [
    { type: "send", send: "\\e[1;5D" },
    { code: "ArrowLeft", modifiers: ["control"] },
  ],
  [
    { type: "combo", chord: { code: "KeyK", modifiers: ["meta"] } },
    { code: "Unidentified", modifiers: ["meta"] },
  ],
  [
    { type: "combo", chord: { code: "ArrowLeft", modifiers: ["meta"] } },
    { code: "ArrowLeft", modifiers: ["meta"] },
  ],
])("projects %j to an encodable 322 chord or an inert key", (action, chord) => {
  const {
    keys: { shell },
  } = resolveTerminalKeys({});
  shell.top.keys[0].action = action;
  const projected = toBar322(shell);
  expect(projected.top.keys[0]).toMatchObject({ kind: "key", chord });
  expect(reconcileShell(shell, projected).top.keys[0].action).toEqual(action);
});

it("preserves a legacy key during appearance edits and can explicitly convert literal text to Send", () => {
  const surface = fromBar322(oldBar);
  const edited = structuredClone(oldBar);
  const editedKey = edited.top.keys.find((key) => key.id === "caret")!;
  editedKey.label = "Renamed";
  editedKey.width = 2;
  const updated = reconcileShell(surface, edited);
  expect(updated.top.keys.find((key) => key.id === "caret")).toMatchObject({
    label: "Renamed",
    width: 2,
    action: { type: "legacy", key: { kind: "text", text: "^C" } },
  });
  expect(escapeLiteral("^C")).toBe("\\x5eC");
  expect(encodeSend(escapeLiteral("^C"), false, EMPTY_STICKY_MODIFIERS, false)).toEqual(["^C"]);
});

it("reconciles an older client's bar, preset and switch only into Shell", () => {
  const current = { terminalBar: oldBar, terminalKeysEnabled: true, terminalBarPreset: oldBar };
  const { keys } = resolveTerminalKeys(current);
  const native = { ...keys.native, showRows: "keyboard" as const };
  const stored = buildTerminalKeyPreferences(
    { terminalKeys: { ...keys, shell: { ...keys.shell, enabled: false }, native } },
    current,
  );
  const changedBar = structuredClone(oldBar);
  changedBar.top.keys[0].label = "Edited";
  const next = resolveTerminalKeys({
    ...stored,
    terminalBar: changedBar,
    terminalBarPreset: changedBar,
    terminalKeysEnabled: true,
  });
  expect(next.keys.shell.enabled).toBe(true);
  expect(next.keys.shell.top.keys[0].label).toBe("Edited");
  expect(next.preset.shell).toMatchObject({
    top: { keys: changedBar.top.keys.map((key) => expect.objectContaining({ label: key.label })) },
  });
  expect(next.keys.native).toEqual(native);
  const canonical = buildTerminalKeyPreferences({ terminalKeys: next.keys }, stored, stored);
  const toggledAgain = resolveTerminalKeys({ ...canonical, terminalKeysEnabled: false }).keys;
  expect(toggledAgain.shell.enabled).toBe(false);
  expect(toggledAgain.native).toEqual(native);
});

it("preserves unknown actions and icons through saves and older appearance edits", () => {
  const { keys } = resolveTerminalKeys({ terminalBar: oldBar });
  keys.shell.top.keys[0] = {
    ...keys.shell.top.keys[0],
    icon: "future-icon",
    display: "icon",
    action: { type: "future", opaque: [1, 2, 3] },
  };
  const stored = buildTerminalKeyPreferences({ terminalKeys: keys }, {});
  const oldEdit = structuredClone(stored.terminalBar as TerminalBar);
  oldEdit.top.keys[0].label = "Edited";
  const reconciled = resolveTerminalKeys({ ...stored, terminalBar: oldEdit });
  expect(reconciled.keys.shell.top.keys[0]).toMatchObject({
    label: "Edited",
    ...{ display: "icon", icon: "future-icon", action: { type: "future", opaque: [1, 2, 3] } },
  });
});

it("preserves raw circle fields byte-for-byte, falls back for an invalid envelope and leaves device state in place", () => {
  const raw = {
    version: 2,
    enabled: false,
    buttons: [{ label: "Old", id: "old", binding: { kind: "text", text: "^C" } }],
    terminalBar: oldBar,
  };
  const deviceKey = "omnigent:mobile-assistant-device-state";
  localStorage.setItem(deviceKey, '{"position":{"x":0.1,"y":0.6}}');
  localStorage.setItem(MOBILE_ASSISTANT_STORAGE_KEY, JSON.stringify(raw));
  expect(writeTerminalKeyPreferences({ terminalKeyboard: { v: 1 } })).toBe("saved");
  expect(JSON.stringify(readRaw().buttons)).toBe(JSON.stringify(raw.buttons));
  expect(readRaw().enabled).toBe(false);
  expect(readRaw()).not.toHaveProperty("chatActionsRevision");
  expect(readMobileAssistantPreferences().terminalKeyboard).toEqual({ v: 1 });
  expect(localStorage.getItem(deviceKey)).toBe('{"position":{"x":0.1,"y":0.6}}');
  localStorage.setItem(
    MOBILE_ASSISTANT_STORAGE_KEY,
    JSON.stringify({ enabled: false, buttons: raw.buttons }),
  );
  expect(writeTerminalKeyPreferences({})).toBe("saved");
  expect(readRaw()).toMatchObject({ version: 2, enabled: true, chatActionsRevision: 1 });
});

describe("bytes sent by the key bar", () => {
  it("uses terminal cursor mode, merges armed and recorded modifiers, and appends Enter only when applicable", () => {
    const ctrl = { ...EMPTY_STICKY_MODIFIERS, ctrl: "once" as const };
    expect(encodeSend("\\e[A", false, EMPTY_STICKY_MODIFIERS, true)).toEqual(["\x1bOA"]);
    expect(encodeSend("\\e[1;2A", false, ctrl, true)).toEqual(["\x1b[1;6A"]);
    expect(encodeSend("x", true, ctrl, false)).toEqual(["\x18\r"]);
    expect(encodeSend("^C", true, EMPTY_STICKY_MODIFIERS, false)).toEqual(["\x03"]);
    expect(encodeSend("constructor", false, EMPTY_STICKY_MODIFIERS, false)).toEqual([
      "constructor",
    ]);
    expect(encodeSend("\\e:wq\\r", false, EMPTY_STICKY_MODIFIERS, false)).toEqual(["\x1b:wq\r"]);
    expect(splitTerminalWrites("\x1b:wq\r\x1bb\x1b[A", 50)).toEqual(["\x1b", ":wq\r\x1bb\x1b[A"]);
  });

  it.each([
    ["ArrowLeft", "\x01"],
    ["ArrowRight", "\x05"],
    ["Backspace", "\x15"],
    ["Enter", "\r"],
    ["KeyK", "\x1bk"],
  ])("maps bar Cmd+%s consistently", (code, bytes) => {
    expect(applyCmd({ chord: { code, modifiers: ["meta"] } }, "mac-meta")).toEqual({ bytes });
  });

  it("returns clipboard actions without emitting terminal bytes", () => {
    expect(applyCmd({ char: "c" }, "mac-meta")).toEqual({ clipboard: "copy" });
    expect(applyCmd({ chord: { code: "KeyV", modifiers: ["meta", "shift"] } }, "mac-meta")).toEqual(
      { clipboard: "paste" },
    );
    expect(encodeSend("v", true, { ...EMPTY_STICKY_MODIFIERS, cmd: "once" }, false)).toEqual([]);
  });
});

it("measures the default migrated envelope within the account limit", () => {
  const { keys } = resolveTerminalKeys({});
  const defaultBytes = envelopeBytes(buildTerminalKeyPreferences({ terminalKeys: keys }, {}));
  expect(defaultBytes).toBeLessThan(MAX_PREFERENCE_ENVELOPE_BYTES);
  if (process.env.OMNIGENT_MEASURE_TERMINAL_KEY_BYTES !== "1") return;

  const full = (legacy: boolean): SurfaceKeys => {
    const s = structuredClone(keys.shell);
    for (const row of ["top", "bottom"] as const) {
      s[row] = {
        pinned: 0,
        enabled: true,
        keys: Array.from({ length: 60 }, (_, index) => ({
          id: `${row}-${index}`.padEnd(32, "x"),
          label: "快捷".repeat(6),
          width: 1,
          display: "text",
          action: legacy
            ? { type: "legacy", key: { kind: "text", text: "输入".repeat(32) } }
            : { type: "send", send: "x".repeat(256) },
        })),
      };
    }
    return s;
  };
  const legacy = full(true);
  const modern = full(false);
  const measurements = {
    default: defaultBytes,
    legacy322: envelopeBytes({
      version: 2,
      enabled: true,
      buttons: [],
      terminalBar: toBar322(legacy),
    }),
    migratedLegacy: envelopeBytes(
      buildTerminalKeyPreferences({ terminalKeys: { ...keys, shell: legacy, native: legacy } }, {}),
    ),
    fourMaximal: envelopeBytes(
      buildTerminalKeyPreferences(
        {
          terminalKeys: { ...keys, shell: modern, native: modern },
          terminalKeysPreset: { shell: modern, native: modern },
        },
        {},
      ),
    ),
  };
  process.stdout.write(`Terminal key preference bytes ${JSON.stringify(measurements)}\n`);
});
