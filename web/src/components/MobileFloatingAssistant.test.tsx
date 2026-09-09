import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { POLL_SESSIONS_ACTION_EVENT } from "@/hooks/useSessionPollingHotkeys";
import {
  readMobileAssistantPreferences,
  writeMobileAssistantPreferences,
} from "@/lib/mobileAssistantPreferences";
import {
  MobileFloatingAssistant,
  layoutMobileAssistantActions,
  layoutMobileAssistantCircle,
} from "./MobileFloatingAssistant";

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

  it("keeps all nine controls visible and separated at each viewport corner", () => {
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
        expect(point.x).toBeGreaterThanOrEqual(21.5);
        expect(point.x).toBeLessThanOrEqual(368.5);
        expect(point.y).toBeGreaterThanOrEqual(21.5);
        expect(point.y).toBeLessThanOrEqual(822.5);
        for (const other of positions.slice(index + 1)) {
          expect(Math.hypot(point.x - other.x, point.y - other.y)).toBeGreaterThanOrEqual(43);
        }
      });
    }
  });

  it.each([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])(
    "keeps %i configured controls visible at every viewport corner",
    (count) => {
      const viewport = { width: 390, height: 844 };
      const centers = [
        { x: 26, y: 26 },
        { x: 364, y: 26 },
        { x: 26, y: 818 },
        { x: 364, y: 818 },
      ];

      for (const center of centers) {
        const layout = layoutMobileAssistantCircle(count, center, viewport);
        const inset = layout.actionSize / 2;
        layout.actions.forEach((point, index) => {
          expect(point.x).toBeGreaterThanOrEqual(inset);
          expect(point.x).toBeLessThanOrEqual(viewport.width - inset);
          expect(point.y).toBeGreaterThanOrEqual(inset);
          expect(point.y).toBeLessThanOrEqual(viewport.height - inset);
          for (const other of layout.actions.slice(index + 1)) {
            expect(Math.hypot(point.x - other.x, point.y - other.y)).toBeGreaterThanOrEqual(
              layout.actionSize,
            );
          }
        });
      }
    },
  );

  it.each([8, 9, 10])(
    "keeps %i controls visible and separated across edge transition bands",
    (count) => {
      const viewport = { width: 390, height: 844 };
      const centers = [
        { x: 26, y: 26 },
        { x: 40, y: 60 },
        { x: 104, y: 422 },
        { x: 110, y: 140 },
        { x: 195, y: 110 },
        { x: 280, y: 704 },
        { x: 286, y: 422 },
        { x: 350, y: 784 },
        { x: 364, y: 818 },
      ];

      for (const center of centers) {
        const layout = layoutMobileAssistantCircle(count, center, viewport);
        const inset = layout.actionSize / 2;
        layout.actions.forEach((point, index) => {
          expect(point.x).toBeGreaterThanOrEqual(inset);
          expect(point.x).toBeLessThanOrEqual(viewport.width - inset);
          expect(point.y).toBeGreaterThanOrEqual(inset);
          expect(point.y).toBeLessThanOrEqual(viewport.height - inset);
          for (const other of layout.actions.slice(index + 1)) {
            expect(Math.hypot(point.x - other.x, point.y - other.y)).toBeGreaterThanOrEqual(
              layout.actionSize,
            );
          }
        });
      }
    },
  );

  it.each([
    { width: 320, height: 568 },
    { width: 568, height: 320 },
  ])("keeps 1..10 controls visible on a $width×$height viewport", (viewport) => {
    const centers = [
      { x: 26, y: 26 },
      { x: viewport.width - 26, y: 26 },
      { x: 26, y: viewport.height - 26 },
      { x: viewport.width - 26, y: viewport.height - 26 },
      { x: viewport.width / 2 + 10, y: 116 },
      { x: viewport.width / 2 - 10, y: viewport.height - 116 },
      { x: 116, y: viewport.height / 2 + 10 },
      { x: viewport.width - 116, y: viewport.height / 2 - 10 },
    ];

    for (let count = 1; count <= 10; count += 1) {
      for (const center of centers) {
        const layout = layoutMobileAssistantCircle(count, center, viewport);
        const inset = layout.actionSize / 2;
        layout.actions.forEach((point, index) => {
          expect(point.x).toBeGreaterThanOrEqual(inset);
          expect(point.x).toBeLessThanOrEqual(viewport.width - inset);
          expect(point.y).toBeGreaterThanOrEqual(inset);
          expect(point.y).toBeLessThanOrEqual(viewport.height - inset);
          for (const other of layout.actions.slice(index + 1)) {
            expect(Math.hypot(point.x - other.x, point.y - other.y)).toBeGreaterThanOrEqual(
              layout.actionSize,
            );
          }
        });
      }
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

  it.each([
    [8, 4],
    [9, 5],
    [10, 5],
  ])("splits %i edge buttons into ordered inner and outer arcs", (count, innerCount) => {
    const viewport = { width: 390, height: 844 };
    const layout = layoutMobileAssistantCircle(count, { x: 26, y: 422 }, viewport);

    expect(layout.twoRings).toBe(true);
    expect(layout.innerCount).toBe(innerCount);
    expect(layout.actionSize).toBeLessThan(46);
    expect(layout.actions[0]?.y).toBeLessThan(layout.actions[innerCount - 1]?.y ?? 0);
    expect(layout.actions[innerCount]?.y).toBeLessThan(layout.actions[count - 1]?.y ?? 0);
  });

  it.each([
    [{ x: 104, y: 422 }, "vertical"],
    [{ x: 286, y: 422 }, "vertical"],
    [{ x: 195, y: 104 }, "horizontal"],
    [{ x: 195, y: 740 }, "horizontal"],
  ] as const)("keeps both transition-band rings ordered on the %s axis", (center, axis) => {
    const layout = layoutMobileAssistantCircle(10, center, { width: 390, height: 844 });
    const coordinate = axis === "vertical" ? "y" : "x";
    const inner = layout.actions.slice(0, layout.innerCount).map((point) => point[coordinate]);
    const outer = layout.actions.slice(layout.innerCount).map((point) => point[coordinate]);

    expect(layout.twoRings).toBe(true);
    expect(inner).toEqual([...inner].sort((a, b) => a - b));
    expect(outer).toEqual([...outer].sort((a, b) => a - b));
  });

  it("keeps the requested center instead of pushing the assistant inward", () => {
    const requested = { x: 40, y: 60 };
    const layout = layoutMobileAssistantCircle(10, requested, { width: 390, height: 844 });

    expect(layout.center).toEqual(requested);
  });

  it("keeps both right-edge rings ordered from top to bottom", () => {
    const layout = layoutMobileAssistantCircle(
      10,
      { x: 364, y: 422 },
      {
        width: 390,
        height: 844,
      },
    );

    expect(layout.actions[0]?.y).toBeLessThan(layout.actions[4]?.y ?? 0);
    expect(layout.actions[5]?.y).toBeLessThan(layout.actions[9]?.y ?? 0);
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
