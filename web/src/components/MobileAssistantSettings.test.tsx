import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { readMobileAssistantPreferences } from "@/lib/mobileAssistantPreferences";
import { MobileAssistantSettings } from "./MobileAssistantSettings";

describe("MobileAssistantSettings", () => {
  beforeEach(() => localStorage.clear());

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
    const compact = screen.getByTestId(`mobile-assistant-preview-button-${added.id}`);
    fireEvent.pointerDown(compact, { pointerId: 4, button: 0, clientX: 120, clientY: 38 });
    fireEvent.pointerMove(compact, { pointerId: 4, button: 0, clientX: 178, clientY: 62 });
    fireEvent.pointerUp(compact, { pointerId: 4, button: 0, clientX: 178, clientY: 62 });

    expect(readMobileAssistantPreferences().buttons[1]?.label).toBe("Compact");
    expect(screen.queryByLabelText("Circle order direction")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Move Compact/ })).not.toBeInTheDocument();
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
