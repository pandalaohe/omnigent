import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DEFAULT_TERMINAL_KEY_ITEMS, TerminalKeyRow } from "./TerminalKeyRow";
import type { TerminalSession } from "./TerminalSession";

function makeSessionMock(focused = false) {
  let isFocusedState = focused;
  const session = {
    sendSoftKey: vi.fn(),
    focus: vi.fn(() => {
      isFocusedState = true;
    }),
    blur: vi.fn(() => {
      isFocusedState = false;
    }),
    isFocused: vi.fn(() => isFocusedState),
  };
  return {
    session,
    sessionRef: { current: session as unknown as TerminalSession | null },
  };
}

function tapButton(button: HTMLElement, pointerId = 1, x = 20, y = 20) {
  fireEvent.pointerDown(button, { button: 0, pointerId, clientX: x, clientY: y });
  fireEvent.pointerUp(button, { button: 0, pointerId, clientX: x, clientY: y });
}

describe("TerminalKeyRow", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("renders the 16 default keys without Ctrl or Alt and shows page 1 by default", () => {
    expect(DEFAULT_TERMINAL_KEY_ITEMS).toHaveLength(16);
    expect(DEFAULT_TERMINAL_KEY_ITEMS.map((item) => item.id)).not.toContain("ctrl");
    expect(DEFAULT_TERMINAL_KEY_ITEMS.map((item) => item.id)).not.toContain("alt");

    const { sessionRef } = makeSessionMock();
    render(<TerminalKeyRow sessionRef={sessionRef} keyboardUp={false} />);

    const bar = screen.getByTestId("terminal-key-row");
    expect(bar).toHaveAttribute("data-page", "0");

    // Fixed first-row + Page 1 keys (12 buttons total)
    const buttons = screen.getAllByRole("button");
    expect(buttons).toHaveLength(12);
    expect(buttons.map((b) => b.getAttribute("data-key-id"))).toEqual([
      "esc",
      "up",
      "down",
      "enter",
      "ctrl-c",
      "keyboard",
      "tab",
      "shift-tab",
      "left",
      "right",
      "slash",
      "pgup",
    ]);
  });

  it("prevents focus shift on pointerDown and sends soft keys on pointerUp with { focus: false }", () => {
    const { session, sessionRef } = makeSessionMock(false);
    render(<TerminalKeyRow sessionRef={sessionRef} keyboardUp={false} />);

    const escBtn = screen.getByRole("button", { name: "Esc" });
    const preventSpy = vi.fn();
    const downEvent = new PointerEvent("pointerdown", {
      bubbles: true,
      cancelable: true,
      button: 0,
      pointerId: 1,
      clientX: 20,
      clientY: 20,
    });
    Object.defineProperty(downEvent, "preventDefault", { value: preventSpy });
    escBtn.dispatchEvent(downEvent);
    expect(preventSpy).toHaveBeenCalledOnce();
    expect(session.sendSoftKey).not.toHaveBeenCalled();

    fireEvent.pointerUp(escBtn, { button: 0, pointerId: 1, clientX: 20, clientY: 20 });
    expect(session.sendSoftKey).toHaveBeenCalledWith("escape", undefined, { focus: false });
    expect(session.focus).not.toHaveBeenCalled();

    tapButton(screen.getByRole("button", { name: "/" }), 2, 40, 60);
    expect(session.sendSoftKey).toHaveBeenCalledWith("enter", "/", { focus: false });

    tapButton(screen.getByRole("button", { name: "^C" }), 3, 60, 20);
    expect(session.sendSoftKey).toHaveBeenCalledWith("enter", "\x03", { focus: false });
  });

  it("handles the 3 Keyboard button focus/blur states on click", () => {
    // 1. Unfocused -> focus()
    const { session, sessionRef } = makeSessionMock(false);
    const { rerender } = render(<TerminalKeyRow sessionRef={sessionRef} keyboardUp={false} />);
    const kbBtn = screen.getByRole("button", { name: "Keyboard" });
    expect(kbBtn).toHaveAttribute("aria-pressed", "false");

    fireEvent.click(kbBtn);
    expect(session.blur).not.toHaveBeenCalled();
    expect(session.focus).toHaveBeenCalledOnce();

    // 2. Focused + keyboardUp === false (user dismissed keyboard natively) -> blur() then focus()
    session.focus.mockClear();
    session.blur.mockClear();
    fireEvent.click(kbBtn);
    expect(session.blur).toHaveBeenCalledOnce();
    expect(session.focus).toHaveBeenCalledOnce();

    // 3. Focused + keyboardUp === true -> blur() only
    session.focus.mockClear();
    session.blur.mockClear();
    rerender(<TerminalKeyRow sessionRef={sessionRef} keyboardUp={true} />);
    expect(kbBtn).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(kbBtn);
    expect(session.blur).toHaveBeenCalledOnce();
    expect(session.focus).not.toHaveBeenCalled();
  });

  it("repeats arrow keys at 400ms then 80ms and cancels on >10px pointer movement", () => {
    vi.useFakeTimers();
    const { session, sessionRef } = makeSessionMock(false);
    render(<TerminalKeyRow sessionRef={sessionRef} keyboardUp={false} />);

    const upBtn = screen.getByRole("button", { name: "↑" });
    fireEvent.pointerDown(upBtn, { button: 0, pointerId: 5, clientX: 50, clientY: 20 });
    expect(session.sendSoftKey).not.toHaveBeenCalled();

    vi.advanceTimersByTime(399);
    expect(session.sendSoftKey).not.toHaveBeenCalled();

    vi.advanceTimersByTime(1);
    expect(session.sendSoftKey).toHaveBeenCalledTimes(1);
    expect(session.sendSoftKey).toHaveBeenLastCalledWith("arrowUp", undefined, { focus: false });

    vi.advanceTimersByTime(160);
    expect(session.sendSoftKey).toHaveBeenCalledTimes(3);

    // Drag > 10px stops repeating and suppresses release fire
    fireEvent.pointerMove(upBtn, { pointerId: 5, clientX: 65, clientY: 20 });
    vi.advanceTimersByTime(240);
    expect(session.sendSoftKey).toHaveBeenCalledTimes(3);
    fireEvent.pointerUp(upBtn, { pointerId: 5, clientX: 65, clientY: 20 });
    expect(session.sendSoftKey).toHaveBeenCalledTimes(3);
  });

  it("swipes horizontally across the second row to flip between Page 1 and Page 2", () => {
    const { session, sessionRef } = makeSessionMock(false);
    render(<TerminalKeyRow sessionRef={sessionRef} keyboardUp={false} />);

    const bar = screen.getByTestId("terminal-key-row");
    const pager = bar.querySelector("[data-pager]") as HTMLElement;
    expect(bar).toHaveAttribute("data-page", "0");

    // Swipe left (>36px) to reach Page 2 (index 1) without firing a key
    fireEvent.pointerDown(pager, { button: 0, pointerId: 10, clientX: 200, clientY: 70 });
    fireEvent.pointerMove(pager, { pointerId: 10, clientX: 140, clientY: 72 });
    fireEvent.pointerUp(pager, { pointerId: 10, clientX: 140, clientY: 72 });

    expect(bar).toHaveAttribute("data-page", "1");
    expect(session.sendSoftKey).not.toHaveBeenCalled();
    expect(
      screen
        .getAllByRole("button")
        .slice(6)
        .map((b) => b.getAttribute("data-key-id")),
    ).toEqual(["pgdn", "home", "end", "ctrl-d"]);

    tapButton(screen.getByRole("button", { name: "Home" }), 11, 80, 70);
    expect(session.sendSoftKey).toHaveBeenCalledWith("home", undefined, { focus: false });

    tapButton(screen.getByRole("button", { name: "End" }), 12, 140, 70);
    expect(session.sendSoftKey).toHaveBeenCalledWith("end", undefined, { focus: false });

    // Swipe right (>36px) to return to Page 1 (index 0)
    fireEvent.pointerDown(pager, { button: 0, pointerId: 13, clientX: 100, clientY: 70 });
    fireEvent.pointerMove(pager, { pointerId: 13, clientX: 160, clientY: 71 });
    fireEvent.pointerUp(pager, { pointerId: 13, clientX: 160, clientY: 71 });
    expect(bar).toHaveAttribute("data-page", "0");
  });

  it("clamps the page at both ends and tells a page swipe from a tap and a diagonal drag", () => {
    const { session, sessionRef } = makeSessionMock(false);
    render(<TerminalKeyRow sessionRef={sessionRef} keyboardUp={false} />);
    const pager = screen.getByTestId("terminal-key-row-pager");

    // Swipe right on the first page: nothing to go back to, no key fired.
    fireEvent.pointerDown(pager, { button: 0, pointerId: 20, clientX: 100, clientY: 70 });
    fireEvent.pointerUp(pager, { pointerId: 20, clientX: 160, clientY: 70 });
    expect(screen.getByTestId("terminal-key-row")).toHaveAttribute("data-page", "0");
    expect(session.sendSoftKey).not.toHaveBeenCalled();

    // Swipe left twice: the second one is already on the last page.
    for (const pointerId of [21, 22]) {
      fireEvent.pointerDown(pager, { button: 0, pointerId, clientX: 200, clientY: 70 });
      fireEvent.pointerUp(pager, { pointerId, clientX: 140, clientY: 70 });
    }
    expect(screen.getByTestId("terminal-key-row")).toHaveAttribute("data-page", "1");
    expect(session.sendSoftKey).not.toHaveBeenCalled();

    // A 40 px diagonal drag with a larger vertical part is neither a page nor a key.
    const homeBtn = screen.getByRole("button", { name: "Home" });
    fireEvent.pointerDown(homeBtn, { button: 0, pointerId: 23, clientX: 100, clientY: 60 });
    fireEvent.pointerMove(homeBtn, { pointerId: 23, clientX: 140, clientY: 110 });
    fireEvent.pointerUp(homeBtn, { pointerId: 23, clientX: 140, clientY: 110 });
    expect(screen.getByTestId("terminal-key-row")).toHaveAttribute("data-page", "1");
    expect(session.sendSoftKey).not.toHaveBeenCalled();

    // A 5 px movement is still a tap: the key fires and the page does not move.
    fireEvent.pointerDown(homeBtn, { button: 0, pointerId: 24, clientX: 100, clientY: 60 });
    fireEvent.pointerMove(homeBtn, { pointerId: 24, clientX: 105, clientY: 63 });
    fireEvent.pointerUp(homeBtn, { pointerId: 24, clientX: 105, clientY: 63 });
    expect(screen.getByTestId("terminal-key-row")).toHaveAttribute("data-page", "1");
    expect(session.sendSoftKey).toHaveBeenCalledTimes(1);
    expect(session.sendSoftKey).toHaveBeenLastCalledWith("home", undefined, { focus: false });
  });

  it("disables all keys while reconnecting", () => {
    const { session, sessionRef } = makeSessionMock(false);
    render(<TerminalKeyRow sessionRef={sessionRef} keyboardUp={false} disabled={true} />);

    const bar = screen.getByTestId("terminal-key-row");
    expect(bar).toHaveAttribute("aria-disabled", "true");
    for (const button of screen.getAllByRole("button")) {
      expect(button).toBeDisabled();
    }

    tapButton(screen.getByRole("button", { name: "Esc" }), 1);
    fireEvent.click(screen.getByRole("button", { name: "Keyboard" }));
    expect(session.sendSoftKey).not.toHaveBeenCalled();
    expect(session.focus).not.toHaveBeenCalled();
  });

  it("enforces single-pointer ownership and preserves detail===0 accessible activation after cancelled drags", () => {
    const { session, sessionRef } = makeSessionMock(false);
    render(<TerminalKeyRow sessionRef={sessionRef} keyboardUp={false} />);

    const bar = screen.getByTestId("terminal-key-row");
    const pager = screen.getByTestId("terminal-key-row-pager");
    expect(bar.className).toContain("touch-none");
    expect(pager.className).toContain("touch-none");

    const upBtn = screen.getByRole("button", { name: "↑" });
    const ctrlCBtn = screen.getByRole("button", { name: "^C" });

    // Finger 1 presses ↑; finger 2 presses ^C while finger 1 is down.
    fireEvent.pointerDown(upBtn, { button: 0, pointerId: 1, clientX: 20, clientY: 20 });
    fireEvent.pointerDown(ctrlCBtn, { button: 0, pointerId: 2, clientX: 200, clientY: 20 });
    // Finger 2 move/up is ignored; releasing finger 1 dispatches ↑, never ^C.
    fireEvent.pointerMove(ctrlCBtn, { pointerId: 2, clientX: 240, clientY: 20 });
    fireEvent.pointerUp(ctrlCBtn, { pointerId: 2, clientX: 240, clientY: 20 });
    expect(session.sendSoftKey).not.toHaveBeenCalled();

    fireEvent.pointerUp(upBtn, { pointerId: 1, clientX: 20, clientY: 20 });
    expect(session.sendSoftKey).toHaveBeenCalledTimes(1);
    expect(session.sendSoftKey).toHaveBeenLastCalledWith("arrowUp", undefined, { focus: false });

    // Drag Esc >= 10px and cancel/release, then activate Tab with detail === 0 (keyboard/screen-reader).
    session.sendSoftKey.mockClear();
    const escBtn = screen.getByRole("button", { name: "Esc" });
    const tabBtn = screen.getByRole("button", { name: "Tab" });
    fireEvent.pointerDown(escBtn, { button: 0, pointerId: 3, clientX: 10, clientY: 10 });
    fireEvent.pointerMove(escBtn, { pointerId: 3, clientX: 30, clientY: 10 });
    fireEvent.pointerCancel(escBtn, { pointerId: 3 });
    expect(session.sendSoftKey).not.toHaveBeenCalled();

    fireEvent.click(tabBtn, { detail: 0 });
    expect(session.sendSoftKey).toHaveBeenCalledTimes(1);
    expect(session.sendSoftKey).toHaveBeenLastCalledWith("tab", undefined, { focus: false });

    // Pointer-originated click (detail === 1) on a soft key is ignored because pointerUp handles it.
    session.sendSoftKey.mockClear();
    fireEvent.click(tabBtn, { detail: 1 });
    expect(session.sendSoftKey).not.toHaveBeenCalled();
  });
});
