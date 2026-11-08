import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { POLL_SESSIONS_ACTION_EVENT } from "@/hooks/useSessionPollingHotkeys";
import { SELECT_WORKSPACE_TAB_ACTION_EVENT } from "@/hooks/useWorkspaceTabHotkeys";
import {
  MOBILE_ASSISTANT_ACTIONS,
  MOBILE_ASSISTANT_STORAGE_KEY,
  TERMINAL_SOFT_KEY_EVENT,
  dispatchMobileAssistantAction,
  dispatchMobileAssistantButton,
  defaultTerminalButtons,
  defaultMobileAssistantButtons,
  visibleChatButtons,
  readMobileAssistantPreferences,
  registerTypableTerminal,
  softKeyForChord,
  terminalDataForChord,
  useTypableTerminals,
  writeMobileAssistantDeviceState,
  writeMobileAssistantPreferences,
  type TerminalSoftKeyEventDetail,
} from "./mobileAssistantPreferences";
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

  it("keeps placement device-local and never resends stale button configuration", () => {
    writeMobileAssistantPreferences({
      version: 2,
      enabled: true,
      buttons: [
        {
          id: "fresh",
          label: "Fresh",
          binding: { kind: "text", text: "/fresh" },
        },
      ],
    });
    vi.mocked(queueUserPreferencePatch).mockClear();

    writeMobileAssistantDeviceState({
      position: { x: 0.1, y: 0.4 },
      dock: { edge: "left", offset: 0.4 },
    });

    expect(queueUserPreferencePatch).not.toHaveBeenCalled();
    expect(readMobileAssistantPreferences()).toMatchObject({
      buttons: [expect.objectContaining({ id: "fresh" })],
      position: { x: 0.1, y: 0.4 },
      dock: { edge: "left", offset: 0.4 },
    });
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
    expect(preferences.terminalButtons.find((item) => item.id === "tab")).toMatchObject({
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
    expect(preferences.terminalButtons.at(-1)).toMatchObject({
      label: "Save",
      kind: "key",
      chord: { code: "KeyS", modifiers: ["control"] },
    });
    expect(localStorage.getItem(MOBILE_ASSISTANT_STORAGE_KEY)).toBe(raw);
    expect(preferences.position).toEqual({ x: 0.2, y: 0.3 });
    writeMobileAssistantPreferences(preferences);
    expect(readMobileAssistantPreferences()).toEqual(preferences);
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

  it("routes app actions through the shared session action event", () => {
    const listener = vi.fn();
    window.addEventListener(POLL_SESSIONS_ACTION_EVENT, listener);

    dispatchMobileAssistantAction("pollSessions");

    expect(listener).toHaveBeenCalledOnce();
    window.removeEventListener(POLL_SESSIONS_ACTION_EVENT, listener);
  });

  it("lets a terminal consume a soft key before DOM fallback", () => {
    const input = document.createElement("textarea");
    document.body.appendChild(input);
    input.focus();
    const keyListener = vi.fn();
    input.addEventListener("keydown", keyListener);
    const terminalListener = (event: Event) => {
      (event as CustomEvent<TerminalSoftKeyEventDetail>).detail.handled = true;
    };
    window.addEventListener(TERMINAL_SOFT_KEY_EVENT, terminalListener);

    dispatchMobileAssistantAction("escape");

    expect(keyListener).not.toHaveBeenCalled();
    window.removeEventListener(TERMINAL_SOFT_KEY_EVENT, terminalListener);
    input.remove();
  });

  it("prefers the focused terminal when multiple writable terminals are mounted", () => {
    const first = vi.fn();
    const focused = vi.fn();
    const collect = (event: Event) => {
      const detail = (event as CustomEvent<TerminalSoftKeyEventDetail>).detail;
      (detail.candidates ??= []).push(
        { focused: false, ownsPreferredTarget: false, send: first },
        { focused: true, ownsPreferredTarget: false, send: focused },
      );
    };
    window.addEventListener(TERMINAL_SOFT_KEY_EVENT, collect);

    dispatchMobileAssistantAction("tab");

    expect(focused).toHaveBeenCalledOnce();
    expect(first).not.toHaveBeenCalled();
    window.removeEventListener(TERMINAL_SOFT_KEY_EVENT, collect);
  });

  it("prefers the saved DOM control over an unfocused terminal", () => {
    const terminal = vi.fn();
    const collect = (event: Event) => {
      const detail = (event as CustomEvent<TerminalSoftKeyEventDetail>).detail;
      (detail.candidates ??= []).push({
        focused: false,
        ownsPreferredTarget: false,
        send: terminal,
      });
    };
    const textarea = document.createElement("textarea");
    document.body.appendChild(textarea);
    const domListener = vi.fn();
    textarea.addEventListener("keydown", domListener);
    window.addEventListener(TERMINAL_SOFT_KEY_EVENT, collect);

    dispatchMobileAssistantAction("enter", textarea);

    expect(domListener).toHaveBeenCalledOnce();
    expect(terminal).not.toHaveBeenCalled();
    window.removeEventListener(TERMINAL_SOFT_KEY_EVENT, collect);
    textarea.remove();
  });

  it("uses the only writable TUI after app chrome steals DOM focus", () => {
    const terminal = vi.fn();
    const collect = (event: Event) => {
      const detail = (event as CustomEvent<TerminalSoftKeyEventDetail>).detail;
      (detail.candidates ??= []).push({
        focused: false,
        ownsPreferredTarget: false,
        send: terminal,
      });
    };
    const chromeButton = document.createElement("button");
    document.body.appendChild(chromeButton);
    const domListener = vi.fn();
    chromeButton.addEventListener("keydown", domListener);
    window.addEventListener(TERMINAL_SOFT_KEY_EVENT, collect);

    dispatchMobileAssistantAction("escape", chromeButton);

    expect(terminal).toHaveBeenCalledOnce();
    expect(domListener).not.toHaveBeenCalled();
    window.removeEventListener(TERMINAL_SOFT_KEY_EVENT, collect);
    chromeButton.remove();
  });

  it("inserts a custom phrase at the saved input selection", () => {
    const textarea = document.createElement("textarea");
    textarea.value = "hello ";
    document.body.appendChild(textarea);
    textarea.setSelectionRange(6, 6);
    const input = vi.fn();
    textarea.addEventListener("input", input);

    dispatchMobileAssistantButton(
      { id: "compact", label: "Compact", binding: { kind: "text", text: "/compact" } },
      textarea,
    );

    expect(textarea.value).toBe("hello /compact");
    expect(input).toHaveBeenCalledOnce();
    textarea.remove();
  });

  it("ignores a connected saved input after its surface becomes hidden", () => {
    const hidden = document.createElement("textarea");
    hidden.style.visibility = "hidden";
    const current = document.createElement("textarea");
    document.body.append(hidden, current);
    current.focus();

    dispatchMobileAssistantButton(
      { id: "phrase", label: "Phrase", binding: { kind: "text", text: "hello" } },
      hidden,
    );

    expect(hidden.value).toBe("");
    expect(current.value).toBe("hello");
    expect(document.activeElement).toBe(current);
    hidden.remove();
    current.remove();
  });

  it("dispatches a workspace-tab action event for select-tab buttons", () => {
    const listener = vi.fn();
    window.addEventListener(SELECT_WORKSPACE_TAB_ACTION_EVENT, listener);

    dispatchMobileAssistantButton({
      id: "tab-2",
      label: "Tab 2",
      binding: { kind: "shortcut", actionId: "selectWorkspaceTab2" },
    });

    expect(listener).toHaveBeenCalledOnce();
    const dispatched = listener.mock.calls[0]?.[0] as
      CustomEvent<{ tabNumber: number }> | undefined;
    expect(dispatched?.detail).toEqual({ tabNumber: 2 });
    window.removeEventListener(SELECT_WORKSPACE_TAB_ACTION_EVENT, listener);
  });

  it("does nothing for the recent-sessions switcher (needs a held key)", () => {
    const listener = vi.fn();
    window.addEventListener("keydown", listener);

    dispatchMobileAssistantButton({
      id: "recents",
      label: "Recents",
      binding: { kind: "shortcut", actionId: "recentSessions" },
    });

    expect(listener).not.toHaveBeenCalled();
    window.removeEventListener("keydown", listener);
  });

  it("dispatches a recorded in-app chord with resolved primary modifier", () => {
    const textarea = document.createElement("textarea");
    document.body.appendChild(textarea);
    const listener = vi.fn();
    textarea.addEventListener("keydown", listener);

    dispatchMobileAssistantButton(
      {
        id: "custom-key",
        label: "Ctrl T",
        binding: { kind: "key", chord: { code: "KeyT", modifiers: ["primary"] } },
      },
      textarea,
    );

    expect(listener).toHaveBeenCalledOnce();
    expect(listener.mock.calls[0]?.[0]).toMatchObject({ code: "KeyT", ctrlKey: true });
    textarea.remove();
  });

  it("carries the Poll sessions hint on the mobile assistant action definition", () => {
    expect(
      MOBILE_ASSISTANT_ACTIONS.find((action) => action.id === "pollSessions")?.description,
    ).toBe("Jumps to the next session waiting for your answer.");
  });

  it("routes phrases and modified key chords to the active terminal when no DOM input is targeted", () => {
    const received: { action: string; data?: string }[] = [];
    const collect = (event: Event) => {
      const detail = (event as CustomEvent<TerminalSoftKeyEventDetail>).detail;
      (detail.candidates ??= []).push({
        focused: false,
        ownsPreferredTarget: false,
        send: () => received.push({ action: detail.action, data: detail.data }),
      });
    };
    const staleWarmContainer = document.createElement("div");
    staleWarmContainer.className = "invisible";
    staleWarmContainer.setAttribute("aria-hidden", "true");
    const staleXtermTextarea = document.createElement("textarea");
    staleXtermTextarea.className = "xterm-helper-textarea";
    staleWarmContainer.appendChild(staleXtermTextarea);
    document.body.appendChild(staleWarmContainer);
    staleXtermTextarea.focus();

    window.addEventListener(TERMINAL_SOFT_KEY_EVENT, collect);

    dispatchMobileAssistantButton(
      {
        id: "cp",
        label: "Cp",
        binding: { kind: "text", text: "/compact", submit: true },
      },
      staleXtermTextarea,
    );
    dispatchMobileAssistantButton(
      {
        id: "stop",
        label: "Stop",
        binding: { kind: "key", chord: { code: "KeyC", modifiers: ["primary"] } },
      },
      staleXtermTextarea,
    );
    dispatchMobileAssistantButton(
      {
        id: "newline",
        label: "Newline",
        binding: { kind: "shortcut", actionId: "newLine" },
      },
      staleXtermTextarea,
    );

    expect(received).toEqual([
      { action: "enter", data: "/compact" },
      { action: "enter", data: undefined },
      { action: "enter", data: "\x03" },
      { action: "enter", data: "\x1b[13;2u" },
    ]);

    window.removeEventListener(TERMINAL_SOFT_KEY_EVENT, collect);
    staleWarmContainer.remove();
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

  it("tracks active typable terminals and those showing a key row via registerTypableTerminal", () => {
    const { result } = renderHook(() => useTypableTerminals());
    expect(result.current).toEqual({ count: 0, withBar: 0 });

    let releaseBar = () => {};
    let releaseNoBar = () => {};
    act(() => {
      releaseBar = registerTypableTerminal(true);
    });
    expect(result.current).toEqual({ count: 1, withBar: 1 });

    act(() => {
      releaseNoBar = registerTypableTerminal(false);
    });
    expect(result.current).toEqual({ count: 2, withBar: 1 });

    act(() => {
      releaseBar();
    });
    expect(result.current).toEqual({ count: 1, withBar: 0 });

    act(() => {
      releaseNoBar();
    });
    expect(result.current).toEqual({ count: 0, withBar: 0 });
  });
});
