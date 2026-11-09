import { act, fireEvent, render, screen } from "@testing-library/react";
import { toast } from "sonner";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  defaultTerminalButtons,
  readMobileAssistantPreferences,
  writeMobileAssistantPreferences,
} from "@/lib/mobileAssistantPreferences";
import { MobileAssistantSettings } from "./MobileAssistantSettings";
import {
  defaultTerminalBar,
  canonicalTerminalFields,
  canonicalizeBar,
  preferencesEnvelopeBytes,
  legacyFingerprint,
  projectLegacy,
} from "@/lib/terminalKeyBar";
import { collectLocalUserPreferences } from "@/lib/userPreferencesSync";

vi.mock("sonner", () => ({ toast: vi.fn() }));

describe("MobileAssistantSettings", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.restoreAllMocks();
    // Exercise the existing wheel editor with an explicitly customized current-schema set.
    writeMobileAssistantPreferences({
      version: 2,
      enabled: true,
      terminalButtons: defaultTerminalButtons(),
      buttons: [
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
          repeat: true,
          binding: { kind: "key", chord: { code: "ArrowUp", modifiers: [] } },
        },
        {
          id: "default-down",
          label: "↓",
          repeat: true,
          binding: { kind: "key", chord: { code: "ArrowDown", modifiers: [] } },
        },
        {
          id: "default-enter",
          label: "↵",
          binding: { kind: "key", chord: { code: "Enter", modifiers: [] } },
        },
        {
          id: "default-poll",
          label: "Poll",
          binding: { kind: "shortcut", actionId: "pollSessions" },
        },
        {
          id: "default-archive",
          label: "Archive",
          binding: { kind: "shortcut", actionId: "archiveSession" },
        },
      ],
    });
  });

  function mockCircleRect() {
    const circle = screen.getByTestId("mobile-assistant-circle-preview");
    Object.defineProperty(circle, "getBoundingClientRect", {
      configurable: true,
      value: () => ({
        left: 0,
        top: 0,
        right: 240,
        bottom: 240,
        width: 240,
        height: 240,
        x: 0,
        y: 0,
        toJSON: () => ({}),
      }),
    });
    return circle;
  }

  it("adds a phrase button and inserts it into a new slot by dragging on the circle", () => {
    render(<MobileAssistantSettings />);

    fireEvent.click(screen.getByRole("button", { name: "Add button" }));
    fireEvent.change(screen.getByLabelText("Button label"), { target: { value: "Compact" } });
    fireEvent.click(screen.getByRole("radio", { name: "Text or phrase" }));
    fireEvent.change(screen.getByLabelText("Text or phrase"), {
      target: { value: "/compact" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save button" }));

    const added = readMobileAssistantPreferences().buttons.at(-1);
    expect(added).toMatchObject({
      label: "Compact",
      binding: { kind: "text", text: "/compact" },
    });
    if (!added) throw new Error("Compact button was not saved");

    mockCircleRect();
    const compact = screen.getByTestId(`mobile-assistant-preview-button-${added.id}`);
    const setPointerCapture = vi.fn();
    compact.setPointerCapture = setPointerCapture;
    fireEvent.pointerDown(compact, { pointerId: 4, button: 0, clientX: 120, clientY: 38 });
    expect(setPointerCapture).toHaveBeenCalledWith(4);
    fireEvent.pointerMove(compact, { pointerId: 4, button: 0, clientX: 178, clientY: 62 });

    expect(screen.getByTestId("mobile-assistant-drag-preview")).toHaveStyle({
      left: "178px",
      top: "62px",
    });
    expect(readMobileAssistantPreferences().buttons.at(-1)?.label).toBe("Compact");

    fireEvent.pointerUp(compact, { pointerId: 4, button: 0, clientX: 178, clientY: 62 });

    expect(readMobileAssistantPreferences().buttons[1]?.label).toBe("Compact");
    expect(screen.queryByLabelText("Circle order direction")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Move Compact/ })).not.toBeInTheDocument();
  });

  it("tracks freely across the circle and inserts from lower-left to upper-left", () => {
    render(<MobileAssistantSettings />);
    mockCircleRect();
    const moving = screen.getByTestId("mobile-assistant-preview-button-default-enter");

    fireEvent.pointerDown(moving, { pointerId: 7, button: 0, clientX: 84, clientY: 194 });
    fireEvent.pointerMove(moving, { pointerId: 7, button: 0, clientX: 120, clientY: 120 });
    expect(screen.getByTestId("mobile-assistant-drag-preview")).toHaveStyle({
      left: "120px",
      top: "120px",
    });

    fireEvent.pointerMove(moving, { pointerId: 7, button: 0, clientX: 56, clientY: 69 });
    expect(screen.getByTestId("mobile-assistant-drag-preview")).toHaveStyle({
      left: "56px",
      top: "69px",
    });
    fireEvent.pointerUp(moving, { pointerId: 7, button: 0, clientX: 56, clientY: 69 });

    expect(readMobileAssistantPreferences().buttons[6]?.id).toBe("default-enter");
  });

  it("can move outside the circle but cancels when released there", () => {
    render(<MobileAssistantSettings />);
    const circle = mockCircleRect();
    const before = readMobileAssistantPreferences().buttons.map((button) => button.id);
    const moving = screen.getByTestId("mobile-assistant-preview-button-default-down");

    fireEvent.pointerDown(moving, { pointerId: 8, button: 0, clientX: 156, clientY: 194 });
    fireEvent.pointerMove(moving, { pointerId: 8, button: 0, clientX: 292, clientY: 80 });

    expect(screen.getByTestId("mobile-assistant-drag-preview")).toHaveStyle({
      left: "292px",
      top: "80px",
    });
    expect(circle).toHaveAttribute("data-drop-valid", "false");

    fireEvent.pointerUp(moving, { pointerId: 8, button: 0, clientX: 292, clientY: 80 });
    expect(readMobileAssistantPreferences().buttons.map((button) => button.id)).toEqual(before);
    expect(screen.queryByTestId("mobile-assistant-drag-preview")).not.toBeInTheDocument();
  });

  it("rolls back a pointer-cancelled drag", () => {
    render(<MobileAssistantSettings />);
    mockCircleRect();
    const before = readMobileAssistantPreferences().buttons.map((button) => button.id);
    const moving = screen.getByTestId("mobile-assistant-preview-button-default-enter");

    fireEvent.pointerDown(moving, { pointerId: 9, button: 0, clientX: 84, clientY: 194 });
    fireEvent.pointerMove(moving, { pointerId: 9, button: 0, clientX: 56, clientY: 69 });
    fireEvent.pointerCancel(moving, { pointerId: 9, clientX: 56, clientY: 69 });

    expect(readMobileAssistantPreferences().buttons.map((button) => button.id)).toEqual(before);
    expect(screen.queryByTestId("mobile-assistant-drag-preview")).not.toBeInTheDocument();
  });

  it("keeps keyboard reordering and wraps the first button through the last slot", () => {
    render(<MobileAssistantSettings />);
    const escape = screen.getByTestId("mobile-assistant-preview-button-default-escape");
    escape.focus();

    fireEvent.keyDown(escape, { key: "ArrowLeft" });
    expect(readMobileAssistantPreferences().buttons.at(-1)?.id).toBe("default-escape");
    expect(escape).toHaveFocus();

    fireEvent.keyDown(escape, { key: "ArrowRight" });
    expect(readMobileAssistantPreferences().buttons[0]?.id).toBe("default-escape");
    expect(escape).toHaveFocus();
  });

  it("chooses a custom in-app key combination by tapping instead of recording", () => {
    render(<MobileAssistantSettings />);

    fireEvent.click(screen.getByRole("button", { name: "Add button" }));
    fireEvent.change(screen.getByLabelText("Button label"), { target: { value: "Open" } });
    fireEvent.click(screen.getByRole("radio", { name: "Key combination" }));
    fireEvent.change(screen.getByLabelText("Key"), { target: { value: "KeyT" } });
    fireEvent.click(screen.getByRole("button", { name: "Ctrl" }));
    fireEvent.click(screen.getByRole("button", { name: "Save button" }));

    expect(readMobileAssistantPreferences().buttons.at(-1)).toMatchObject({
      label: "Open",
      binding: { kind: "key", chord: { code: "KeyT", modifiers: ["control"] } },
    });
  });

  it("omits the held-key recent-sessions switcher and explains why", () => {
    render(<MobileAssistantSettings />);

    fireEvent.click(screen.getByRole("button", { name: "Add button" }));
    const select = screen.getByLabelText("Action from keyboard shortcuts");
    const values = Array.from(select.querySelectorAll("option"), (option) => option.value);

    expect(values).not.toContain("recentSessions");
    expect(values).toContain("newSession");
    expect(
      screen.getByText("Switch recent sessions is keyboard-only: it needs a held key."),
    ).toBeTruthy();
  });

  it("does not offer question-card actions in the picker", () => {
    render(<MobileAssistantSettings />);

    fireEvent.click(screen.getByRole("button", { name: "Add button" }));
    const select = screen.getByLabelText("Action from keyboard shortcuts");
    const values = Array.from(select.querySelectorAll("option"), (option) => option.value);

    expect(values).not.toContain("focusQuestionCard");
    expect(values).not.toContain("questionCardNextOrSubmit");
    expect(values).toContain("newSession");
  });

  it("shows the real circular order preview and persists icon and repeat", () => {
    render(<MobileAssistantSettings />);

    expect(screen.getByTestId("mobile-assistant-circle-preview")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Edit ↑" }));
    fireEvent.click(screen.getByRole("radio", { name: "Icon" }));
    fireEvent.change(screen.getByLabelText("Button icon"), { target: { value: "arrow-up" } });
    expect(screen.getByLabelText("Repeat while held")).toHaveAttribute("aria-checked", "true");
    fireEvent.click(screen.getByRole("button", { name: "Save button" }));

    const preferences = readMobileAssistantPreferences();
    expect(preferences.buttons.find((button) => button.id === "default-up")).toMatchObject({
      display: "icon",
      icon: "arrow-up",
      repeat: true,
    });
  });

  it("shows the Poll sessions hint on the Poll row and in the button editor", () => {
    render(<MobileAssistantSettings />);

    expect(screen.getByText("Jumps to the next session waiting for your answer.")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Edit Poll" }));
    expect(
      screen.getAllByText("Jumps to the next session waiting for your answer.").length,
    ).toBeGreaterThanOrEqual(2);
  });

  it("edits the terminal bar, adds a chord by tap, moves it across rows, and persists both switches", () => {
    localStorage.clear();
    render(<MobileAssistantSettings />);
    const chat = readMobileAssistantPreferences().buttons;
    fireEvent.click(screen.getByRole("radio", { name: "Terminal" }));
    expect(screen.getByText(/Top row · 1 page/)).toBeTruthy();
    expect(screen.getByText(/Bottom row · 2 pages/)).toBeTruthy();
    fireEvent.click(screen.getAllByRole("button", { name: "Add key" }).at(-1)!);
    fireEvent.change(screen.getByLabelText("More keys"), { target: { value: "KeyR" } });
    fireEvent.click(screen.getByRole("button", { name: "Ctrl" }));
    fireEvent.click(screen.getByRole("button", { name: "Shift" }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    const added = readMobileAssistantPreferences().terminalBar!.bottom.keys.at(-1)!;
    expect(added).toMatchObject({
      kind: "key",
      width: 2,
      chord: { code: "KeyR", modifiers: ["control", "shift"] },
      label: "Ctrl+Shift+R",
    });
    fireEvent.click(screen.getByRole("button", { name: /^Edit Ctrl\+Shift\+R, 2 cells/ }));
    fireEvent.click(screen.getByRole("button", { name: "Move to other row" }));
    expect(
      readMobileAssistantPreferences().terminalBar!.top.keys.some((key) => key.id === added.id),
    ).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    fireEvent.click(screen.getByRole("switch", { name: "Enable terminal key rows" }));
    fireEvent.click(screen.getByRole("switch", { name: "Show bottom terminal key row" }));
    expect(readMobileAssistantPreferences()).toMatchObject({
      buttons: chat,
      terminalKeysEnabled: false,
      terminalSecondRow: false,
    });
    fireEvent.click(screen.getByRole("radio", { name: "Chat" }));
    expect(readMobileAssistantPreferences().buttons).toEqual(chat);
    fireEvent.click(screen.getByRole("radio", { name: "Terminal" }));
    fireEvent.click(screen.getByRole("button", { name: /^Edit Ctrl\+Shift\+R, 2 cells/ }));
    fireEvent.click(screen.getByRole("button", { name: "Delete key" }));
    expect(
      readMobileAssistantPreferences().terminalBar!.top.keys.some((key) => key.id === added.id),
    ).toBe(false);
  });

  it("pins leading keys with the stepper and refuses an unsupported terminal chord", () => {
    localStorage.clear();
    render(<MobileAssistantSettings />);
    fireEvent.click(screen.getByRole("radio", { name: "Terminal" }));
    fireEvent.click(screen.getByRole("button", { name: "Pin one more bottom row key" }));
    expect(readMobileAssistantPreferences().terminalBar!.bottom.pinned).toBe(1);
    const tab = readMobileAssistantPreferences().terminalBar!.bottom.keys[0];
    expect(tab.id).toBe("tab");
    expect(screen.getByRole("button", { name: /^Edit Tab, 2 cells, pinned/ })).toBeInTheDocument();

    fireEvent.click(screen.getAllByRole("button", { name: "Add key" }).at(-1)!);
    fireEvent.click(screen.getByRole("button", { name: "Tab" }));
    fireEvent.click(screen.getByRole("button", { name: "Ctrl" }));
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    expect(screen.getByText(/Not supported in terminals/)).toBeVisible();
    fireEvent.change(screen.getByLabelText("More keys"), { target: { value: "KeyR" } });
    fireEvent.click(screen.getByRole("button", { name: "Shift" }));
    expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();
    expect(screen.queryByText(/Not supported in terminals/)).toBeNull();
  });

  it("saves modified arrows in the terminal bar", () => {
    localStorage.clear();
    render(<MobileAssistantSettings />);
    fireEvent.click(screen.getByRole("radio", { name: "Terminal" }));
    fireEvent.click(screen.getAllByRole("button", { name: "Add key" }).at(-1)!);
    fireEvent.click(screen.getByRole("button", { name: "↑" }));
    fireEvent.click(screen.getByRole("button", { name: "Ctrl" }));
    expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(readMobileAssistantPreferences().terminalBar!.bottom.keys.at(-1)).toMatchObject({
      kind: "key",
      chord: { code: "ArrowUp", modifiers: ["control"] },
    });
  });

  it("uses the shared help control for terminal rows and closes it on a second tap", () => {
    render(<MobileAssistantSettings />);
    fireEvent.click(screen.getByRole("radio", { name: "Terminal" }));
    const help = screen.getByRole("button", { name: "About Terminal key rows" });
    fireEvent.click(help);
    expect(
      screen.getByText(/the floating assistant uses the first ten terminal keys/),
    ).toBeVisible();
    fireEvent.click(help);
    expect(
      screen.queryByText(/the floating assistant uses the first ten terminal keys/),
    ).not.toBeInTheDocument();
  });

  it("round-trips a preset and undoes defaults without reverting an unrelated switch", () => {
    localStorage.clear();
    render(<MobileAssistantSettings />);
    fireEvent.click(screen.getByRole("radio", { name: "Terminal" }));
    fireEvent.click(screen.getByRole("button", { name: /^Edit Esc,/ }));
    fireEvent.click(screen.getByRole("radio", { name: "1 cell" }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    fireEvent.click(screen.getByRole("button", { name: "Save as my preset" }));
    fireEvent.click(screen.getByRole("button", { name: "Restore defaults" }));
    expect(readMobileAssistantPreferences().terminalBar!.top.keys[0].width).toBe(2);
    fireEvent.click(screen.getByRole("button", { name: "Load my preset" }));
    expect(readMobileAssistantPreferences().terminalBar!.top.keys[0].width).toBe(1);
    fireEvent.click(screen.getByRole("button", { name: "Restore defaults" }));
    const undo = vi.mocked(toast).mock.calls.at(-1)?.[1]?.action;
    expect(undo && typeof undo === "object" && "onClick" in undo).toBe(true);
    fireEvent.click(screen.getByRole("switch", { name: "Enable terminal key rows" }));
    act(() => {
      if (undo && typeof undo === "object" && "onClick" in undo) undo.onClick({} as never);
    });
    expect(readMobileAssistantPreferences().terminalBar!.top.keys[0].width).toBe(1);
    expect(readMobileAssistantPreferences().terminalKeysEnabled).toBe(false);
  });

  it("refuses an oversized saved preset before touching local storage", () => {
    const bar = defaultTerminalBar();
    for (const row of ["top", "bottom"] as const) {
      bar[row] = {
        enabled: true,
        pinned: 0,
        keys: Array.from({ length: 60 }, (_, index) => ({
          id: `${row}-${index}`,
          label: "快捷".repeat(6),
          kind: "text",
          display: "text",
          width: 1,
          text: "输入".repeat(32),
        })),
      };
    }
    const legacy = projectLegacy(bar);
    writeMobileAssistantPreferences({
      version: 2,
      enabled: true,
      buttons: [],
      terminalBar: bar,
      terminalButtons: legacy,
      terminalLegacyBasis: legacyFingerprint(legacy, true),
      chatActionsRevision: 1,
    });
    render(<MobileAssistantSettings />);
    fireEvent.click(screen.getByRole("radio", { name: "Terminal" }));
    const before = localStorage.getItem("omnigent:mobile-assistant-preferences");
    fireEvent.click(screen.getByRole("button", { name: "Save as my preset" }));
    expect(screen.getByText("Too large to sync: remove keys or shorten texts")).toBeVisible();
    expect(localStorage.getItem("omnigent:mobile-assistant-preferences")).toBe(before);
  });

  it("names the default fallback when an invalid stored bar had no older key list", () => {
    const raw = {
      version: 2,
      enabled: true,
      buttons: [],
      chatActionsRevision: 1,
      terminalBar: { invalid: true },
    };
    localStorage.setItem("omnigent:mobile-assistant-preferences", JSON.stringify(raw));
    expect(readMobileAssistantPreferences().terminalButtons).toBeUndefined();
    render(<MobileAssistantSettings />);
    fireEvent.click(screen.getByRole("radio", { name: "Terminal" }));
    expect(screen.getByText(/showing the default keys/)).toBeVisible();
    expect(JSON.parse(localStorage.getItem("omnigent:mobile-assistant-preferences")!)).toEqual(raw);
    fireEvent.click(screen.getByRole("switch", { name: "Enable mobile floating assistant" }));
    expect(readMobileAssistantPreferences().terminalButtons).toBeUndefined();
    expect(screen.getByText(/showing the default keys/)).toBeVisible();
  });

  it.each([65_536, 65_537])("enforces the exact normalized editor budget at %i bytes", (bytes) => {
    localStorage.clear();
    const bar = defaultTerminalBar();
    const terminalButtons = projectLegacy(bar);
    writeMobileAssistantPreferences({
      version: 2,
      enabled: true,
      buttons: [],
      chatActionsRevision: 1,
      terminalBar: bar,
      terminalButtons,
      terminalLegacyBasis: legacyFingerprint(terminalButtons, true),
    });
    render(<MobileAssistantSettings />);
    fireEvent.click(screen.getByRole("radio", { name: "Terminal" }));
    const envelope = collectLocalUserPreferences();
    const mobile = canonicalTerminalFields({
      ...(envelope.settings.mobile_assistant as Record<string, unknown>),
      terminalBarPreset: canonicalizeBar(bar),
    });
    envelope.settings.mobile_assistant = mobile;
    envelope.settings.usage_context = "";
    const padding = "x".repeat(bytes - preferencesEnvelopeBytes(envelope));
    localStorage.setItem("omnigent:usage-context-preferences", JSON.stringify(padding));
    const actual = collectLocalUserPreferences();
    actual.settings.mobile_assistant = mobile;
    expect(preferencesEnvelopeBytes(actual)).toBe(bytes);
    fireEvent.click(screen.getByRole("button", { name: "Save as my preset" }));
    if (bytes === 65_536) {
      expect(readMobileAssistantPreferences().terminalBarPreset).toEqual(bar);
    } else {
      expect(readMobileAssistantPreferences().terminalBarPreset).toBeUndefined();
      expect(screen.getByText("Too large to sync: remove keys or shorten texts")).toBeVisible();
    }
  });
});
