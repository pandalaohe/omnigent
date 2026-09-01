import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { POLL_SESSIONS_ACTION_EVENT } from "@/hooks/useSessionPollingHotkeys";
import {
  readMobileAssistantPreferences,
  writeMobileAssistantPreferences,
} from "@/lib/mobileAssistantPreferences";
import { MobileFloatingAssistant, layoutMobileAssistantActions } from "./MobileFloatingAssistant";

vi.mock("@/hooks/useIsMobileViewport", () => ({
  useIsMobileViewport: () => true,
}));
vi.mock("@/lib/nativeBridge", () => ({
  isAndroidShell: () => false,
  isIOSShell: () => false,
}));

describe("MobileFloatingAssistant", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.stubGlobal("innerWidth", 390);
    vi.stubGlobal("innerHeight", 844);
  });

  afterEach(() => vi.useRealTimers());

  it("expands configured controls and dispatches the shared poll action", () => {
    const listener = vi.fn();
    window.addEventListener(POLL_SESSIONS_ACTION_EVENT, listener);
    render(<MobileFloatingAssistant />);

    fireEvent.click(screen.getByRole("button", { name: "Open floating assistant" }));
    fireEvent.click(screen.getByRole("button", { name: "Poll" }));

    expect(screen.getByRole("button", { name: "Esc" })).toBeTruthy();
    expect(listener).toHaveBeenCalledOnce();
    window.removeEventListener(POLL_SESSIONS_ACTION_EVENT, listener);
  });

  it("persists a dragged position", () => {
    render(<MobileFloatingAssistant />);
    const button = screen.getByRole("button", { name: "Open floating assistant" });

    fireEvent.pointerDown(button, { pointerId: 1, clientX: 330, clientY: 660 });
    fireEvent.pointerMove(button, { pointerId: 1, clientX: 150, clientY: 250 });
    fireEvent.pointerUp(button, { pointerId: 1, clientX: 150, clientY: 250 });

    expect(readMobileAssistantPreferences().position).toEqual({
      x: 150 / 390,
      y: 250 / 844,
    });
  });

  it("keeps all nine controls separated at each viewport corner", () => {
    const viewport = { width: 390, height: 844 };
    const centers = [
      { x: 26, y: 26 },
      { x: 364, y: 26 },
      { x: 26, y: 818 },
      { x: 364, y: 818 },
    ];

    for (const center of centers) {
      const positions = layoutMobileAssistantActions(9, center, viewport);
      expect(positions).toHaveLength(9);
      positions.forEach((point, index) => {
        expect(point.x).toBeGreaterThanOrEqual(29);
        expect(point.x).toBeLessThanOrEqual(361);
        expect(point.y).toBeGreaterThanOrEqual(29);
        expect(point.y).toBeLessThanOrEqual(815);
        for (const other of positions.slice(index + 1)) {
          expect(Math.hypot(point.x - other.x, point.y - other.y)).toBeGreaterThanOrEqual(54);
        }
      });
    }
  });

  it("lays buttons clockwise around a full circle from the fixed top slot", () => {
    const center = { x: 195, y: 300 };
    const viewport = { width: 390, height: 844 };
    const positions = layoutMobileAssistantActions(4, center, viewport);

    expect(positions[0]?.x).toBeCloseTo(center.x);
    expect(positions[0]?.y).toBeLessThan(center.y);
    expect(positions[1]?.x).toBeGreaterThan(center.x);
    expect(
      new Set(
        positions.map((point) => Math.round(Math.hypot(point.x - center.x, point.y - center.y))),
      ),
    ).toHaveLength(1);
  });

  it("renders a configured icon without action-specific branching", () => {
    writeMobileAssistantPreferences({
      version: 2,
      enabled: true,
      buttons: [
        {
          id: "custom-terminal",
          label: "Open terminal",
          display: "icon",
          icon: "terminal",
          binding: { kind: "text", text: "terminal" },
        },
      ],
    });
    render(<MobileFloatingAssistant />);

    fireEvent.click(screen.getByRole("button", { name: "Open floating assistant" }));

    expect(screen.getByRole("button", { name: "Open terminal" })).toHaveAttribute(
      "data-display",
      "icon",
    );
  });

  it("repeats an enabled arrow while held and stops on release", () => {
    vi.useFakeTimers();
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
      ],
    });
    const listener = vi.fn();
    window.addEventListener("omnigent:terminal-soft-key", listener);
    render(<MobileFloatingAssistant />);
    fireEvent.click(screen.getByRole("button", { name: "Open floating assistant" }));
    const up = screen.getByRole("button", { name: "Up" });

    fireEvent.pointerDown(up, { pointerId: 3, button: 0 });
    expect(listener).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(560);
    expect(listener.mock.calls.length).toBeGreaterThanOrEqual(3);
    fireEvent.pointerUp(up, { pointerId: 3, button: 0 });
    const stoppedAt = listener.mock.calls.length;
    vi.advanceTimersByTime(320);
    expect(listener).toHaveBeenCalledTimes(stoppedAt);
    window.removeEventListener("omnigent:terminal-soft-key", listener);
  });

  it("dispatches a soft key to the control focused before the palette click", () => {
    writeMobileAssistantPreferences({
      version: 2,
      enabled: true,
      buttons: [
        {
          id: "tab",
          label: "Tab",
          binding: { kind: "key", chord: { code: "Tab", modifiers: [] } },
        },
      ],
    });
    const textarea = document.createElement("textarea");
    document.body.appendChild(textarea);
    const listener = vi.fn();
    textarea.addEventListener("keydown", listener);
    textarea.focus();
    render(<MobileFloatingAssistant />);

    const opener = screen.getByRole("button", { name: "Open floating assistant" });
    fireEvent.click(opener);
    opener.focus();
    fireEvent.click(screen.getByRole("button", { name: "Tab" }));

    expect(listener).toHaveBeenCalledOnce();
    expect(listener.mock.calls[0]?.[0]).toMatchObject({ key: "Tab", code: "Tab" });
    textarea.remove();
  });

  it("inserts a phrase into a controlled composer input", () => {
    writeMobileAssistantPreferences({
      version: 2,
      enabled: true,
      buttons: [
        {
          id: "compact",
          label: "Compact",
          binding: { kind: "text", text: "/compact" },
        },
      ],
    });
    function ControlledComposer() {
      const [value, setValue] = useState("hello ");
      return (
        <textarea
          aria-label="Composer"
          value={value}
          onChange={(event) => setValue(event.target.value)}
        />
      );
    }
    render(
      <>
        <ControlledComposer />
        <MobileFloatingAssistant />
      </>,
    );
    const composer = screen.getByRole("textbox", { name: "Composer" });
    composer.focus();
    (composer as HTMLTextAreaElement).setSelectionRange(6, 6);
    fireEvent.click(screen.getByRole("button", { name: "Open floating assistant" }));
    fireEvent.click(screen.getByRole("button", { name: "Compact" }));

    expect(composer).toHaveValue("hello /compact");
  });

  it("collapses into an edge handle and restores when pulled out", () => {
    render(<MobileFloatingAssistant />);
    const button = screen.getByRole("button", { name: "Open floating assistant" });

    fireEvent.pointerDown(button, { pointerId: 7, clientX: 330, clientY: 660 });
    fireEvent.pointerMove(button, { pointerId: 7, clientX: 2, clientY: 300 });
    fireEvent.pointerUp(button, { pointerId: 7, clientX: 2, clientY: 300 });

    expect(readMobileAssistantPreferences().dock).toMatchObject({ edge: "left" });
    const handle = screen.getByRole("button", { name: "Expand floating assistant" });
    fireEvent.pointerDown(handle, { pointerId: 8, clientX: 9, clientY: 300 });
    fireEvent.pointerMove(handle, { pointerId: 8, clientX: 120, clientY: 300 });
    fireEvent.pointerUp(handle, { pointerId: 8, clientX: 120, clientY: 300 });

    expect(readMobileAssistantPreferences().dock).toBeUndefined();
    expect(screen.getByRole("button", { name: "Open floating assistant" })).toBeTruthy();
  });
});
