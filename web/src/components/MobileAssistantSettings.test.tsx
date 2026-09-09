import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { readMobileAssistantPreferences } from "@/lib/mobileAssistantPreferences";
import { MobileAssistantSettings } from "./MobileAssistantSettings";

describe("MobileAssistantSettings", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.restoreAllMocks();
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

  it("records a custom in-app key combination", () => {
    render(<MobileAssistantSettings />);

    fireEvent.click(screen.getByRole("button", { name: "Add button" }));
    fireEvent.change(screen.getByLabelText("Button label"), { target: { value: "Open" } });
    fireEvent.change(screen.getByLabelText("Binding type"), { target: { value: "key" } });
    fireEvent.click(screen.getByRole("button", { name: "Record custom key combination" }));
    fireEvent.keyDown(window, { key: "t", code: "KeyT", ctrlKey: true });
    fireEvent.click(screen.getByRole("button", { name: "Save button" }));

    expect(readMobileAssistantPreferences().buttons.at(-1)).toMatchObject({
      label: "Open",
      binding: { kind: "key", chord: { code: "KeyT", modifiers: ["primary"] } },
    });
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
});
