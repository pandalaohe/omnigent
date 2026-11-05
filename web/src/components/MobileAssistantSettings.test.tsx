import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  defaultTerminalButtons,
  readMobileAssistantPreferences,
  writeMobileAssistantPreferences,
} from "@/lib/mobileAssistantPreferences";
import { MobileAssistantSettings } from "./MobileAssistantSettings";

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
    fireEvent.change(screen.getByLabelText("Binding type"), { target: { value: "text" } });
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
    fireEvent.change(screen.getByLabelText("Binding type"), { target: { value: "key" } });
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
    fireEvent.change(screen.getByLabelText("Button display"), { target: { value: "icon" } });
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

  it("edits the terminal set separately, adds a chord by tap, moves it, and persists both switches", () => {
    localStorage.clear();
    render(<MobileAssistantSettings />);
    const chat = readMobileAssistantPreferences().buttons;
    fireEvent.click(screen.getByRole("radio", { name: "Terminal" }));
    expect(screen.getByText("First row")).toBeTruthy();
    expect(screen.getByText("Page 1")).toBeTruthy();
    expect(screen.getByText("Page 2")).toBeTruthy();
    fireEvent.click(screen.getByRole("switch", { name: "Enable terminal key rows" }));
    fireEvent.click(screen.getByRole("switch", { name: "Show second terminal key row" }));
    fireEvent.click(screen.getByRole("button", { name: "Add a key" }));
    fireEvent.change(screen.getByLabelText("Key"), { target: { value: "KeyR" } });
    fireEvent.click(screen.getByRole("button", { name: "Ctrl" }));
    fireEvent.click(screen.getByRole("button", { name: "Shift" }));
    fireEvent.click(screen.getByRole("button", { name: "Save key" }));
    const added = readMobileAssistantPreferences().terminalButtons.at(-1)!;
    expect(added).toMatchObject({
      kind: "key",
      chord: { code: "KeyR", modifiers: ["control", "shift"] },
    });
    fireEvent.click(screen.getByRole("button", { name: `Move ${added.label} up` }));
    expect(readMobileAssistantPreferences().terminalButtons[17].id).toBe(added.id);
    expect(readMobileAssistantPreferences()).toMatchObject({
      buttons: chat,
      terminalKeysEnabled: false,
      terminalSecondRow: false,
    });
    fireEvent.click(screen.getByRole("radio", { name: "Chat" }));
    expect(readMobileAssistantPreferences().buttons).toEqual(chat);
    fireEvent.click(screen.getByRole("radio", { name: "Terminal" }));
    fireEvent.click(screen.getByRole("button", { name: `Delete ${added.label}` }));
    expect(readMobileAssistantPreferences().terminalButtons).toHaveLength(18);
  });

  it("keeps the same focused move control when a key crosses a page boundary", () => {
    localStorage.clear();
    render(<MobileAssistantSettings />);
    fireEvent.click(screen.getByRole("radio", { name: "Terminal" }));
    const move = screen.getByRole("button", { name: "Move Tab up" });
    move.focus();
    fireEvent.click(move);
    expect(readMobileAssistantPreferences().terminalButtons[5].id).toBe("tab");
    expect(screen.getByRole("button", { name: "Move Tab up" })).toBe(move);
    expect(move).toHaveFocus();
  });

  it("explains an unsupported terminal chord and prevents saving an inert key", () => {
    render(<MobileAssistantSettings />);
    fireEvent.click(screen.getByRole("radio", { name: "Terminal" }));
    fireEvent.click(screen.getByRole("button", { name: "Add a key" }));
    fireEvent.change(screen.getByLabelText("Key"), { target: { value: "ArrowUp" } });
    fireEvent.click(screen.getByRole("button", { name: "Ctrl" }));
    expect(screen.getByRole("button", { name: "Save key" })).toBeDisabled();
    expect(screen.getByRole("status")).toHaveTextContent("not supported in terminals");
    fireEvent.change(screen.getByLabelText("Key"), { target: { value: "KeyR" } });
    fireEvent.click(screen.getByRole("button", { name: "Shift" }));
    expect(screen.getByRole("button", { name: "Save key" })).toBeEnabled();
    expect(screen.queryByRole("status")).toBeNull();
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
});
