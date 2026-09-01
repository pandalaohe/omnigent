import { beforeEach, describe, expect, it, vi } from "vitest";

import { POLL_SESSIONS_ACTION_EVENT } from "@/hooks/useSessionPollingHotkeys";
import {
  MOBILE_ASSISTANT_STORAGE_KEY,
  TERMINAL_SOFT_KEY_EVENT,
  dispatchMobileAssistantAction,
  dispatchMobileAssistantButton,
  readMobileAssistantPreferences,
  writeMobileAssistantDeviceState,
  writeMobileAssistantPreferences,
  type TerminalSoftKeyEventDetail,
} from "./mobileAssistantPreferences";
import { queueUserPreferencePatch } from "./userPreferencesSync";

vi.mock("./userPreferencesSync", () => ({ queueUserPreferencePatch: vi.fn() }));

describe("mobileAssistantPreferences", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.mocked(queueUserPreferencePatch).mockClear();
  });

  it("uses repeatable arrow defaults and persists version-2 customization", () => {
    expect(readMobileAssistantPreferences().buttons).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          binding: { kind: "shortcut", actionId: "pollSessions" },
        }),
        expect.objectContaining({ id: "default-up", repeat: true }),
        expect.objectContaining({ id: "default-down", repeat: true }),
      ]),
    );

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

    expect(readMobileAssistantPreferences()).toEqual({
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
    expect(preferences.buttons.map((button) => button.label)).toEqual(["Tab", "Poll"]);
  });

  it("falls back safely when stored data is malformed", () => {
    localStorage.setItem(MOBILE_ASSISTANT_STORAGE_KEY, "{broken");
    expect(readMobileAssistantPreferences().enabled).toBe(true);
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
});
