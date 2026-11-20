import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  MOBILE_ASSISTANT_ACTIONS,
  MOBILE_ASSISTANT_STORAGE_KEY,
  defaultTerminalButtons,
  defaultMobileAssistantButtons,
  visibleChatButtons,
  readMobileAssistantPreferences,
  softKeyForChord,
  terminalDataForChord,
  writeMobileAssistantPreferences,
} from "./mobileAssistantPreferences";
import { resolveMobileMenuPins, DEFAULT_MOBILE_MENU_PINS } from "./mobileMenuPins";
import { queueUserPreferencePatch } from "./userPreferencesSync";

vi.mock("./userPreferencesSync", () => ({ queueUserPreferencePatch: vi.fn() }));

describe("mobileAssistantPreferences", () => {
  it("adds Terminals to a legacy custom chat set without losing the tenth button, and respects removal after writing", () => {
    const buttons = Array.from({ length: 10 }, (_, index) => ({
      id: `phrase-${index}`,
      label: `Phrase ${index}`,
      binding: { kind: "text", text: `text ${index}` },
    }));
    const raw = JSON.stringify({
      version: 2,
      enabled: true,
      buttons,
      terminalButtons: defaultTerminalButtons(),
    });
    localStorage.setItem(MOBILE_ASSISTANT_STORAGE_KEY, raw);
    const read = readMobileAssistantPreferences();
    expect(read.buttons).toHaveLength(11);
    expect(read.buttons[0].binding).toEqual({ kind: "shortcut", actionId: "openTerminals" });
    expect(read.buttons.at(-1)?.id).toBe("phrase-9");
    expect(localStorage.getItem(MOBILE_ASSISTANT_STORAGE_KEY)).toBe(raw);
    writeMobileAssistantPreferences({ ...read, buttons: read.buttons.slice(1) });
    expect(readMobileAssistantPreferences().buttons).toEqual(buttons);
    expect(
      JSON.parse(localStorage.getItem(MOBILE_ASSISTANT_STORAGE_KEY)!).chatActionsRevision,
    ).toBe(1);
  });
  beforeEach(() => {
    localStorage.clear();
    vi.mocked(queueUserPreferencePatch).mockClear();
  });

  it("uses separate chat and terminal defaults and persists version-2 customization", () => {
    expect(readMobileAssistantPreferences().buttons).toEqual(defaultMobileAssistantButtons());
    expect(readMobileAssistantPreferences().terminalButtons).toEqual(defaultTerminalButtons());

    writeMobileAssistantPreferences({
      version: 2,
      enabled: false,
      buttons: [
        {
          id: "compact",
          label: "Compact",
          display: "icon",
          icon: "terminal",
          binding: { kind: "text", text: "/compact" },
        },
      ],
    });

    expect(readMobileAssistantPreferences()).toMatchObject({
      version: 2,
      enabled: false,
      buttons: [
        {
          id: "compact",
          label: "Compact",
          display: "icon",
          icon: "terminal",
          binding: { kind: "text", text: "/compact", submit: undefined },
        },
      ],
    });
  });

  it("keeps the chosen menu pins when another mobile setting is saved, and keeps an empty choice empty", () => {
    localStorage.setItem(
      MOBILE_ASSISTANT_STORAGE_KEY,
      JSON.stringify({
        version: 2,
        enabled: true,
        buttons: defaultMobileAssistantButtons(),
        pinnedMenuItems: ["files", "shells", "retired-row"],
      }),
    );
    const preferences = readMobileAssistantPreferences();
    expect(preferences.pinnedMenuItems).toEqual(["files", "shells"]);

    writeMobileAssistantPreferences({ ...preferences, enabled: false });
    expect(readMobileAssistantPreferences().pinnedMenuItems).toEqual(["files", "shells"]);

    // A user who unpinned everything does not fall back to the stock pins.
    writeMobileAssistantPreferences({ ...preferences, pinnedMenuItems: [] });
    expect(readMobileAssistantPreferences().pinnedMenuItems).toEqual([]);
  });

  it("defaults the menu pins to Shells until the user edits them", () => {
    expect(readMobileAssistantPreferences()).not.toHaveProperty("pinnedMenuItems");
    expect(resolveMobileMenuPins(readMobileAssistantPreferences().pinnedMenuItems)).toEqual([
      "shells",
    ]);
  });

  it("ignores the legacy direction field and never writes it back", () => {
    localStorage.setItem(
      MOBILE_ASSISTANT_STORAGE_KEY,
      JSON.stringify({
        version: 2,
        enabled: true,
        direction: "counterclockwise",
        buttons: [],
      }),
    );

    const preferences = readMobileAssistantPreferences();
    expect(preferences).not.toHaveProperty("direction");
    writeMobileAssistantPreferences(preferences);
    expect(
      JSON.parse(localStorage.getItem(MOBILE_ASSISTANT_STORAGE_KEY) ?? "null"),
    ).not.toHaveProperty("direction");
  });

  it("migrates the fixed version-1 action list without losing order or position", () => {
    localStorage.setItem(
      MOBILE_ASSISTANT_STORAGE_KEY,
      JSON.stringify({
        version: 1,
        enabled: true,
        actions: ["tab", "pollSessions"],
        position: { x: 0.2, y: 0.3 },
      }),
    );

    const preferences = readMobileAssistantPreferences();
    expect(preferences.version).toBe(2);
    expect(preferences.position).toEqual({ x: 0.2, y: 0.3 });
    expect(preferences.buttons.map((button) => button.label)).toEqual(["Terminals", "Poll"]);
    expect(preferences.terminalButtons!.find((item) => item.id === "tab")).toMatchObject({
      label: "Tab",
    });
  });

  it("falls back safely when stored data is malformed", () => {
    localStorage.setItem(MOBILE_ASSISTANT_STORAGE_KEY, "{broken");
    expect(readMobileAssistantPreferences().enabled).toBe(true);
  });

  it("keeps unavailable actions stored while hiding them from the chat wheel", () => {
    // Card actions need a focused card, which a wheel tap cannot give them.
    localStorage.setItem(
      MOBILE_ASSISTANT_STORAGE_KEY,
      JSON.stringify({
        version: 2,
        enabled: true,
        buttons: [
          {
            id: "card",
            label: "Card",
            binding: { kind: "shortcut", actionId: "questionCardNextOrSubmit" },
          },
          {
            id: "poll",
            label: "Poll",
            binding: { kind: "shortcut", actionId: "pollSessions" },
          },
        ],
      }),
    );

    const preferences = readMobileAssistantPreferences();
    expect(preferences.buttons.map((button) => button.id)).toEqual([
      "default-terminals",
      "card",
      "poll",
    ]);
    expect(visibleChatButtons(preferences).map((button) => button.id)).toEqual([
      "default-terminals",
      "poll",
    ]);
  });

  it("derives custom old keys into the terminal tail without writing or losing chat phrases and actions", () => {
    const raw = JSON.stringify({
      version: 2,
      enabled: true,
      buttons: [
        {
          id: "default-escape",
          label: "Esc",
          icon: "escape",
          binding: { kind: "key", chord: { code: "Escape", modifiers: [] } },
        },
        {
          id: "custom-save",
          label: "Save",
          binding: { kind: "key", chord: { code: "KeyS", modifiers: ["control"] } },
        },
        { id: "phrase", label: "Phrase", binding: { kind: "text", text: "/compact" } },
        { id: "poll", label: "Poll", binding: { kind: "shortcut", actionId: "pollSessions" } },
      ],
      position: { x: 0.2, y: 0.3 },
    });
    localStorage.setItem(MOBILE_ASSISTANT_STORAGE_KEY, raw);
    const preferences = readMobileAssistantPreferences();
    expect(preferences.buttons.map((button) => button.id)).toEqual([
      "default-terminals",
      "phrase",
      "poll",
    ]);
    expect(preferences.terminalButtons).toHaveLength(19);
    expect(preferences.terminalButtons!.at(-1)).toMatchObject({
      label: "Save",
      kind: "key",
      chord: { code: "KeyS", modifiers: ["control"] },
    });
    expect(localStorage.getItem(MOBILE_ASSISTANT_STORAGE_KEY)).toBe(raw);
    expect(preferences.position).toEqual({ x: 0.2, y: 0.3 });
    writeMobileAssistantPreferences(preferences);
    // The write materialises the stock pin choice: the namespace syncs whole,
    // so an older device's save must not drop this device's pins back to it.
    expect(readMobileAssistantPreferences()).toEqual({
      ...preferences,
      pinnedMenuItems: [...DEFAULT_MOBILE_MENU_PINS],
    });
  });

  it("uses current defaults for the complete old v1 stock list and preserves explicit empty new sets", () => {
    localStorage.setItem(
      MOBILE_ASSISTANT_STORAGE_KEY,
      JSON.stringify({
        version: 1,
        enabled: true,
        actions: MOBILE_ASSISTANT_ACTIONS.map((action) => action.id),
      }),
    );
    expect(readMobileAssistantPreferences().buttons).toEqual(defaultMobileAssistantButtons());
    writeMobileAssistantPreferences({
      version: 2,
      enabled: true,
      buttons: [],
      terminalButtons: [],
      terminalKeysEnabled: false,
      terminalSecondRow: false,
    });
    expect(readMobileAssistantPreferences()).toMatchObject({
      buttons: [],
      terminalButtons: [],
      terminalKeysEnabled: false,
      terminalSecondRow: false,
    });
  });

  it("derives current defaults from a complete unmodified version-2 stock list without rewriting storage", () => {
    const buttons = [
      {
        id: "default-escape",
        label: "Esc",
        icon: "escape",
        binding: { kind: "key", chord: { code: "Escape", modifiers: [] } },
      },
      {
        id: "default-tab",
        label: "Tab",
        icon: "tab",
        binding: { kind: "key", chord: { code: "Tab", modifiers: [] } },
      },
      {
        id: "default-up",
        label: "↑",
        display: "icon",
        icon: "arrow-up",
        repeat: true,
        binding: { kind: "key", chord: { code: "ArrowUp", modifiers: [] } },
      },
      {
        id: "default-down",
        label: "↓",
        display: "icon",
        icon: "arrow-down",
        repeat: true,
        binding: { kind: "key", chord: { code: "ArrowDown", modifiers: [] } },
      },
      {
        id: "default-enter",
        label: "↵",
        display: "icon",
        icon: "enter",
        binding: { kind: "key", chord: { code: "Enter", modifiers: [] } },
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
    const raw = JSON.stringify({ version: 2, enabled: true, buttons });
    localStorage.setItem(MOBILE_ASSISTANT_STORAGE_KEY, raw);
    expect(readMobileAssistantPreferences().buttons).toEqual(defaultMobileAssistantButtons());
    expect(readMobileAssistantPreferences().terminalButtons).toEqual(defaultTerminalButtons());
    expect(localStorage.getItem(MOBILE_ASSISTANT_STORAGE_KEY)).toBe(raw);
  });

  it("keeps repeat only for repeatable terminal keys", () => {
    writeMobileAssistantPreferences({
      version: 2,
      enabled: true,
      buttons: [
        {
          id: "up",
          label: "Up",
          repeat: true,
          binding: { kind: "key", chord: { code: "ArrowUp", modifiers: [] } },
        },
        {
          id: "escape",
          label: "Esc",
          repeat: true,
          binding: { kind: "key", chord: { code: "Escape", modifiers: [] } },
        },
      ],
    });

    expect(readMobileAssistantPreferences().buttons).toEqual([
      expect.objectContaining({ id: "up", repeat: true }),
      expect.not.objectContaining({ repeat: true }),
    ]);
  });

  it.each([
    [{ code: "BracketLeft", modifiers: ["control"] as const }, "\x1b"],
    [{ code: "Slash", modifiers: ["shift"] as const }, "?"],
    [{ code: "Digit1", modifiers: ["shift"] as const }, "!"],
    [{ code: "F2", modifiers: [] as const }, "\x1bOQ"],
    [{ code: "ArrowLeft", modifiers: ["control"] as const }, "\x1b[1;5D"],
    [{ code: "ArrowRight", modifiers: ["control"] as const }, "\x1b[1;5C"],
    [{ code: "ArrowLeft", modifiers: ["meta"] as const }, "\x01"],
    [{ code: "ArrowRight", modifiers: ["meta"] as const }, "\x05"],
    [{ code: "KeyR", modifiers: ["control", "shift"] as const }, "\x12"],
    [{ code: "KeyR", modifiers: ["control", "alt", "shift"] as const }, "\x1b\x12"],
  ])("translates recordable terminal chord %o to %j", (chord, expected) => {
    expect(terminalDataForChord({ code: chord.code, modifiers: [...chord.modifiers] })).toBe(
      expected,
    );
  });

  const terminalNavigationCases = [
    ["ArrowUp", "A"],
    ["ArrowDown", "B"],
    ["ArrowRight", "C"],
    ["ArrowLeft", "D"],
    ["Home", "H"],
    ["End", "F"],
    ["Insert", "2~"],
    ["Delete", "3~"],
    ["PageUp", "5~"],
    ["PageDown", "6~"],
    ["F1", "P"],
    ["F2", "Q"],
    ["F3", "R"],
    ["F4", "S"],
    ["F5", "15~"],
    ["F6", "17~"],
    ["F7", "18~"],
    ["F8", "19~"],
    ["F9", "20~"],
    ["F10", "21~"],
    ["F11", "23~"],
    ["F12", "24~"],
  ];
  it.each([
    [[], 1],
    [["shift"], 2],
    [["alt"], 3],
    [["shift", "alt"], 4],
    [["control"], 5],
    [["shift", "control"], 6],
    [["alt", "control"], 7],
    [["shift", "alt", "control"], 8],
  ] as const)("encodes navigation and function keys with modifiers %j", (modifiers, parameter) => {
    for (const applicationCursorKeys of [false, true]) {
      for (const [code, suffix] of terminalNavigationCases) {
        const isTilde = suffix.endsWith("~");
        const plainPrefix =
          code.match(/^F[1-4]$/) || (applicationCursorKeys && !isTilde) ? "O" : "[";
        const expected =
          parameter === 1
            ? `\x1b${plainPrefix}${suffix}`
            : isTilde
              ? `\x1b[${suffix.slice(0, -1)};${parameter}~`
              : `\x1b[1;${parameter}${suffix}`;
        expect(
          terminalDataForChord({ code, modifiers: [...modifiers] }, applicationCursorKeys),
          `${code}, applicationCursorKeys=${applicationCursorKeys}`,
        ).toBe(expected);
      }
    }
  });

  it("rejects unsupported modified keys", () => {
    expect(terminalDataForChord({ code: "Tab", modifiers: ["alt"] })).toBeNull();
    expect(terminalDataForChord({ code: "ArrowUp", modifiers: ["meta"] })).toBeNull();
  });

  it.each(["toString", "constructor", "__proto__"])("rejects inherited key name %s", (code) => {
    expect(terminalDataForChord({ code, modifiers: [] })).toBeNull();
    expect(terminalDataForChord({ code, modifiers: ["control"] })).toBeNull();
    expect(terminalDataForChord({ code, modifiers: ["alt"] })).toBeNull();
  });

  it("maps unmodified Home and End chords to softKeyForChord actions", () => {
    expect(softKeyForChord({ code: "Home", modifiers: [] })).toBe("home");
    expect(softKeyForChord({ code: "End", modifiers: [] })).toBe("end");
    expect(softKeyForChord({ code: "Home", modifiers: ["shift"] })).toBeNull();
  });
});
