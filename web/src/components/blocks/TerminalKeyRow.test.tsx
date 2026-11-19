import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  DEFAULT_TERMINAL_KEY_ITEMS,
  EMPTY_STICKY_MODIFIERS,
  migrateLegacy,
  type TerminalBar,
  type TerminalStickyModifiers,
} from "@/lib/terminalKeyBar";
import { DEFAULT_TERMINAL_KEY_ITEMS as REEXPORTED, TerminalKeyRow } from "./TerminalKeyRow";
import { fromBar322 } from "@/lib/terminalKeys";
import type { StickyModifiers, TerminalSession } from "./TerminalSession";

function makeSessionMock(focused = false) {
  let isFocusedState = focused;
  let sticky: TerminalStickyModifiers = { ...EMPTY_STICKY_MODIFIERS };
  const listeners = new Set<(next: TerminalStickyModifiers) => void>();
  const session = {
    sendSoftKey: vi.fn(),
    sendBarSequence: vi.fn(),
    sendBarChord: vi.fn(),
    runClipboardAction: vi.fn(),
    cancelBarWrites: vi.fn(),
    runBarKey: vi.fn((send: () => void) => send()),
    setStickyModifiers: vi.fn((next: TerminalStickyModifiers) => {
      sticky = next;
      for (const listener of listeners) listener(next);
    }),
    getStickyModifiers: vi.fn(() => sticky),
    onStickyConsumed: vi.fn((listener: (next: TerminalStickyModifiers) => void) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    }),
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

function swipePager(element: HTMLElement, pointerId: number, fromX: number, toX: number) {
  fireEvent.pointerDown(element, { button: 0, pointerId, clientX: fromX, clientY: 70 });
  fireEvent.pointerMove(element, { pointerId, clientX: toX, clientY: 72 });
  fireEvent.pointerUp(element, { pointerId, clientX: toX, clientY: 72 });
}

/** Width-2 keys: six fill a 12-cell page, so 13 keys pack into three pages. */
function wideRowKeys(prefix: string, count: number): TerminalBar["top"]["keys"] {
  return Array.from({ length: count }, (_, index) => ({
    id: `${prefix}${index + 1}`,
    label: `${prefix} ${index + 1}`,
    kind: "key" as const,
    chord: { code: "KeyA", modifiers: [] },
    display: "text" as const,
    width: 2 as const,
  }));
}

function barWith(keys: TerminalBar["top"]["keys"], bottomEnabled = false): TerminalBar {
  return {
    top: { keys, pinned: 0, enabled: true },
    bottom: { keys: [], pinned: 0, enabled: bottomEnabled },
  };
}

describe("TerminalKeyRow", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("renders the migrated 18 default keys as 6 fixed top keys and two bottom pages", () => {
    expect(DEFAULT_TERMINAL_KEY_ITEMS).toHaveLength(18);
    expect(REEXPORTED).toBe(DEFAULT_TERMINAL_KEY_ITEMS);

    const { sessionRef } = makeSessionMock();
    render(<TerminalKeyRow sessionRef={sessionRef} keyboardUp={false} />);

    const bar = screen.getByTestId("terminal-key-row");
    expect(bar).toHaveAttribute("data-page", "0");
    expect(bar).toHaveAttribute("data-bottom-page", "0");
    expect(screen.getByTestId("terminal-key-row-primary").querySelectorAll("button")).toHaveLength(
      6,
    );
    expect(
      Array.from(
        screen.getByTestId("terminal-key-row-primary").querySelectorAll("[data-key-id]"),
        (node) => node.getAttribute("data-key-id"),
      ),
    ).toEqual(["esc", "up", "down", "enter", "ctrl-c", "keyboard"]);
    expect(
      Array.from(
        screen.getByTestId("terminal-key-row-pager").querySelectorAll("[data-key-id]"),
        (node) => node.getAttribute("data-key-id"),
      ),
    ).toEqual(["tab", "shift-tab", "left", "right", "slash", "ctrl"]);
    expect(
      screen.getByRole("button", { name: "Bottom row: next page (1 of 2)" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Top row: next page/ })).toBeNull();
  });

  it("toggles a modifier once when a held pointer is released", () => {
    vi.useFakeTimers();
    const { session, sessionRef } = makeSessionMock(true);
    render(<TerminalKeyRow sessionRef={sessionRef} keyboardUp={true} />);
    const ctrl = screen.getByRole("button", { name: /^Ctrl,/ });
    expect(ctrl).toHaveAttribute("aria-pressed", "false");
    tapButton(ctrl);
    expect(ctrl).toHaveAttribute("data-sticky-state", "once");
    expect(ctrl).toHaveAttribute("aria-label", "Ctrl, next key");
    tapButton(ctrl, 2);
    expect(ctrl).toHaveAttribute("data-sticky-state", "off");

    fireEvent.pointerDown(ctrl, { button: 0, pointerId: 3, clientX: 20, clientY: 20 });
    act(() => vi.advanceTimersByTime(499));
    expect(ctrl).toHaveAttribute("data-sticky-state", "off");
    act(() => vi.advanceTimersByTime(1));
    expect(ctrl).toHaveAttribute("data-sticky-state", "off");
    fireEvent.pointerUp(ctrl, { pointerId: 3, clientX: 20, clientY: 20 });
    expect(ctrl).toHaveAttribute("data-sticky-state", "once");
    tapButton(ctrl, 4);
    expect(ctrl).toHaveAttribute("data-sticky-state", "off");
    expect(session.sendSoftKey).not.toHaveBeenCalled();
    expect(session.focus).not.toHaveBeenCalled();
  });

  it("dispatches the new action kinds and retains a label for future actions", () => {
    const { session, sessionRef } = makeSessionMock();
    const surface = fromBar322(migrateLegacy([], false));
    const base = { width: 1 as const, display: "text" as const };
    surface.top.keys = [
      { ...base, id: "special", label: "Escape", action: { type: "special", special: "esc" } },
      {
        ...base,
        id: "send",
        label: "Command",
        action: { type: "send", send: "echo ok", autoEnter: true },
      },
      {
        ...base,
        id: "combo",
        label: "Line start",
        action: { type: "combo", chord: { code: "ArrowLeft", modifiers: ["meta"] } },
      },
      {
        ...base,
        id: "future",
        label: "Future",
        display: "icon",
        icon: "future-icon",
        action: { type: "future" },
      },
    ];
    render(<TerminalKeyRow sessionRef={sessionRef} keyboardUp={false} surface={surface} />);
    fireEvent.click(screen.getByRole("button", { name: "Escape" }));
    fireEvent.click(screen.getByRole("button", { name: /^Command,/ }));
    fireEvent.click(screen.getByRole("button", { name: /^Line start,/ }));
    expect(session.sendBarSequence.mock.calls).toEqual([
      ["\\e", false],
      ["echo ok", true],
    ]);
    expect(session.sendBarChord).toHaveBeenCalledExactlyOnceWith({
      code: "ArrowLeft",
      modifiers: ["meta"],
    });
    expect(screen.getByRole("button", { name: "Future, Needs a newer version" })).toBeDisabled();
  });

  it("renders an agent icon and runs clipboard keys only for whole taps", () => {
    vi.useFakeTimers();
    const { session, sessionRef } = makeSessionMock();
    const surface = fromBar322(barWith([]));
    surface.top.keys = [
      {
        id: "copy",
        label: "Copy tool",
        width: 2,
        display: "icon",
        icon: "agent-pi",
        repeat: true,
        action: { type: "clipboard", op: "copy" },
      },
      {
        id: "paste",
        label: "Paste tool",
        width: 2,
        display: "text",
        action: { type: "clipboard", op: "paste", autoEnter: true },
      },
      {
        id: "paste-host",
        label: "Host tool",
        width: 2,
        display: "text",
        action: { type: "clipboard", op: "pasteHost", autoEnter: false },
      },
    ];
    render(<TerminalKeyRow sessionRef={sessionRef} keyboardUp={false} surface={surface} />);
    const copy = screen.getByRole("button", { name: "Copy tool" });
    expect(copy.querySelector("svg")).not.toBeNull();
    fireEvent.pointerDown(copy, { button: 0, pointerId: 7, clientX: 20, clientY: 20 });
    act(() => vi.advanceTimersByTime(2000));
    expect(session.runClipboardAction).not.toHaveBeenCalled();
    fireEvent.pointerUp(copy, { pointerId: 7, clientX: 20, clientY: 20 });
    expect(session.runClipboardAction).not.toHaveBeenCalled();
    tapButton(copy);
    tapButton(screen.getByRole("button", { name: "Paste tool" }));
    tapButton(screen.getByRole("button", { name: "Host tool" }));
    expect(session.runClipboardAction.mock.calls).toEqual([
      ["copy", { keyId: "copy", enter: false }],
      ["paste", { keyId: "paste", enter: true }],
      ["pasteHost", { keyId: "paste-host", enter: false }],
    ]);
  });

  it("supports a Tap locks modifier and Shift in the sticky state", () => {
    const shift = {
      id: "shift",
      label: "Shift",
      kind: "modifier" as const,
      modifier: "shift" as const,
      tapLocks: true,
      display: "text" as const,
      width: 2 as const,
    };
    const { session, sessionRef } = makeSessionMock();
    render(
      <TerminalKeyRow
        sessionRef={sessionRef}
        keyboardUp={false}
        bar={barWith([
          shift,
          {
            ...shift,
            id: "left",
            label: "Left",
            kind: "key",
            chord: { code: "ArrowLeft", modifiers: [] },
          },
        ])}
      />,
    );
    const key = screen.getByRole("button", { name: /^Shift,/ });
    tapButton(key);
    expect(key).toHaveAttribute("data-sticky-state", "locked");
    tapButton(key, 2);
    expect(key).toHaveAttribute("data-sticky-state", "off");
    expect(session.setStickyModifiers).toHaveBeenLastCalledWith({
      ctrl: "off",
      alt: "off",
      shift: "off",
    });
  });

  it("reflects keyboard consumption and resets ref, state and session on interruptions", () => {
    const { session, sessionRef } = makeSessionMock();
    const { unmount, rerender } = render(
      <TerminalKeyRow sessionRef={sessionRef} keyboardUp={false} identityKey="a" />,
    );
    tapButton(screen.getByRole("button", { name: /^Ctrl,/ }));
    tapButton(screen.getByRole("button", { name: "←" }));
    expect(session.sendSoftKey).toHaveBeenLastCalledWith("arrowLeft", undefined, {
      focus: false,
      chord: { code: "ArrowLeft", modifiers: [] },
    });
    const listener = session.onStickyConsumed.mock.calls[0][0];
    act(() => listener({ ctrl: "off", alt: "locked", shift: "off" }));
    expect(screen.getByRole("button", { name: /^Ctrl,/ })).toHaveAttribute("aria-pressed", "false");

    tapButton(screen.getByRole("button", { name: /^Ctrl,/ }), 3);
    act(() => {
      Object.defineProperty(document, "visibilityState", {
        configurable: true,
        value: "hidden",
      });
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(session.setStickyModifiers).toHaveBeenLastCalledWith({
      ctrl: "off",
      alt: "off",
      shift: "off",
    });
    expect(screen.getByRole("button", { name: /^Ctrl,/ })).toHaveAttribute(
      "data-sticky-state",
      "off",
    );
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
    tapButton(screen.getByRole("button", { name: /^Ctrl,/ }), 4);
    rerender(<TerminalKeyRow sessionRef={sessionRef} keyboardUp={false} identityKey="b" />);
    expect(screen.getByRole("button", { name: /^Ctrl,/ })).toHaveAttribute(
      "data-sticky-state",
      "off",
    );
    unmount();
    expect(session.setStickyModifiers).toHaveBeenLastCalledWith({
      ctrl: "off",
      alt: "off",
      shift: "off",
    });
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

  it("handles the Keyboard button focus/blur states on click", () => {
    const { session, sessionRef } = makeSessionMock(false);
    const { rerender } = render(<TerminalKeyRow sessionRef={sessionRef} keyboardUp={false} />);
    expect(screen.getByRole("button", { name: "Keyboard" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );

    fireEvent.click(screen.getByRole("button", { name: "Keyboard" }));
    expect(session.blur).not.toHaveBeenCalled();
    expect(session.focus).toHaveBeenCalledOnce();

    session.focus.mockClear();
    session.blur.mockClear();
    fireEvent.click(screen.getByRole("button", { name: "Keyboard" }));
    expect(session.blur).toHaveBeenCalledOnce();
    expect(session.focus).toHaveBeenCalledOnce();

    session.focus.mockClear();
    session.blur.mockClear();
    rerender(<TerminalKeyRow sessionRef={sessionRef} keyboardUp={true} />);
    const kbBtn = screen.getByRole("button", { name: "Keyboard" });
    expect(kbBtn).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(kbBtn);
    expect(session.blur).toHaveBeenCalledOnce();
    expect(session.focus).not.toHaveBeenCalled();
  });

  it("swallows a dragged Keyboard click after capture loss and still permits keyboard activation", () => {
    const { session, sessionRef } = makeSessionMock();
    render(<TerminalKeyRow sessionRef={sessionRef} keyboardUp={false} />);
    const keyboard = screen.getByRole("button", { name: "Keyboard" });
    fireEvent.pointerDown(keyboard, { pointerId: 7, button: 0, clientX: 20, clientY: 20 });
    fireEvent.pointerMove(keyboard, { pointerId: 7, clientX: 60, clientY: 20 });
    fireEvent.pointerUp(keyboard, { pointerId: 7, clientX: 60, clientY: 20 });
    fireEvent.lostPointerCapture(keyboard, { pointerId: 7 });
    fireEvent.click(keyboard, { detail: 1 });
    expect(session.focus).not.toHaveBeenCalled();
    expect(session.blur).not.toHaveBeenCalled();
    fireEvent.click(keyboard, { detail: 0 });
    expect(session.focus).toHaveBeenCalledOnce();
  });

  it("repeats any repeat key at 400ms then 80ms and cancels on >10px pointer movement", () => {
    vi.useFakeTimers();
    const { session, sessionRef } = makeSessionMock(false);
    const backspace = {
      id: "backspace",
      label: "Backspace",
      kind: "key" as const,
      chord: { code: "Backspace", modifiers: [] },
      repeat: true,
      display: "text" as const,
      width: 2 as const,
    };
    render(
      <TerminalKeyRow sessionRef={sessionRef} keyboardUp={false} bar={barWith([backspace])} />,
    );

    const key = screen.getByRole("button", { name: "Backspace" });
    fireEvent.pointerDown(key, { button: 0, pointerId: 5, clientX: 50, clientY: 20 });
    expect(session.sendSoftKey).not.toHaveBeenCalled();
    act(() => vi.advanceTimersByTime(399));
    expect(session.sendSoftKey).not.toHaveBeenCalled();
    act(() => vi.advanceTimersByTime(1));
    expect(session.sendSoftKey).toHaveBeenCalledTimes(1);
    expect(session.sendSoftKey).toHaveBeenLastCalledWith("enter", "\x7f", { focus: false });
    act(() => vi.advanceTimersByTime(160));
    expect(session.sendSoftKey).toHaveBeenCalledTimes(3);

    fireEvent.pointerMove(key, { pointerId: 5, clientX: 65, clientY: 20 });
    act(() => vi.advanceTimersByTime(240));
    expect(session.sendSoftKey).toHaveBeenCalledTimes(3);
    fireEvent.pointerUp(key, { pointerId: 5, clientX: 65, clientY: 20 });
    expect(session.sendSoftKey).toHaveBeenCalledTimes(3);
  });

  it("swipes only the paged area, and the page button advances and wraps that row", () => {
    const { session, sessionRef } = makeSessionMock(false);
    render(<TerminalKeyRow sessionRef={sessionRef} keyboardUp={false} />);

    const bar = screen.getByTestId("terminal-key-row");
    const pager = screen
      .getByTestId("terminal-key-row-pager")
      .querySelector("[data-pager]") as HTMLElement;
    expect(bar).toHaveAttribute("data-bottom-page", "0");

    fireEvent.pointerDown(pager, { button: 0, pointerId: 10, clientX: 200, clientY: 70 });
    fireEvent.pointerMove(pager, { pointerId: 10, clientX: 140, clientY: 72 });
    fireEvent.pointerUp(pager, { pointerId: 10, clientX: 140, clientY: 72 });

    expect(bar).toHaveAttribute("data-bottom-page", "1");
    expect(session.sendSoftKey).not.toHaveBeenCalled();
    expect(
      Array.from(pager.querySelectorAll("[data-key-id]"), (node) =>
        node.getAttribute("data-key-id"),
      ),
    ).toEqual(["pgup", "pgdn", "home", "end", "alt", "ctrl-d"]);

    tapButton(screen.getByRole("button", { name: "Home" }), 11, 80, 70);
    expect(session.sendSoftKey).toHaveBeenCalledWith("home", undefined, { focus: false });

    const pageButton = screen.getByRole("button", { name: "Bottom row: next page (2 of 2)" });
    pageButton.focus();
    fireEvent.click(pageButton);
    expect(bar).toHaveAttribute("data-bottom-page", "0");
    expect(screen.getByRole("button", { name: "Bottom row: next page (1 of 2)" })).toHaveFocus();
  });

  it("wraps either end when swiping a multi-page row and keeps rows independent", () => {
    const { session, sessionRef } = makeSessionMock();
    const bar: TerminalBar = {
      top: { keys: wideRowKeys("top", 13), pinned: 0, enabled: true },
      bottom: { keys: wideRowKeys("bottom", 13), pinned: 0, enabled: true },
    };
    render(<TerminalKeyRow sessionRef={sessionRef} keyboardUp={false} bar={bar} />);

    const row = screen.getByTestId("terminal-key-row");
    const topPager = screen
      .getByTestId("terminal-key-row-primary")
      .querySelector("[data-pager]") as HTMLElement;
    const bottomPager = screen
      .getByTestId("terminal-key-row-pager")
      .querySelector("[data-pager]") as HTMLElement;
    expect(row).toHaveAttribute("data-page", "0");
    expect(row).toHaveAttribute("data-page-count", "3");
    expect(row).toHaveAttribute("data-bottom-page", "0");
    expect(row).toHaveAttribute("data-bottom-page-count", "3");

    swipePager(topPager, 40, 200, 140);
    swipePager(topPager, 41, 200, 140);
    expect(row).toHaveAttribute("data-page", "2");

    // Forward past the last page wraps to the first.
    swipePager(topPager, 42, 200, 140);
    expect(row).toHaveAttribute("data-page", "0");

    // Back past the first page wraps to the last.
    swipePager(topPager, 43, 140, 200);
    expect(row).toHaveAttribute("data-page", "2");

    // The bottom row keeps its own page while the top row moves.
    expect(row).toHaveAttribute("data-bottom-page", "0");
    swipePager(bottomPager, 44, 200, 140);
    expect(row).toHaveAttribute("data-bottom-page", "1");
    expect(row).toHaveAttribute("data-page", "2");
    expect(session.sendSoftKey).not.toHaveBeenCalled();
  });

  it("keeps a single-page row on its only page when swiped either way", () => {
    const { session, sessionRef } = makeSessionMock();
    render(
      <TerminalKeyRow
        sessionRef={sessionRef}
        keyboardUp={false}
        bar={barWith(wideRowKeys("solo", 3))}
      />,
    );

    const row = screen.getByTestId("terminal-key-row");
    const topPager = screen
      .getByTestId("terminal-key-row-primary")
      .querySelector("[data-pager]") as HTMLElement;
    expect(row).toHaveAttribute("data-page-count", "1");

    swipePager(topPager, 50, 200, 140);
    expect(row).toHaveAttribute("data-page", "0");
    swipePager(topPager, 51, 140, 200);
    expect(row).toHaveAttribute("data-page", "0");
    expect(session.sendSoftKey).not.toHaveBeenCalled();
  });

  it("does not page when a swipe starts on a pinned key", () => {
    const { session, sessionRef } = makeSessionMock();
    const bar: TerminalBar = {
      top: {
        keys: [
          {
            id: "pin",
            label: "Pin",
            kind: "key",
            chord: { code: "Escape", modifiers: [] },
            display: "text",
            width: 2,
          },
          {
            id: "a",
            label: "A",
            kind: "key",
            chord: { code: "KeyA", modifiers: [] },
            display: "text",
            width: 2,
          },
          {
            id: "b",
            label: "B",
            kind: "key",
            chord: { code: "KeyB", modifiers: [] },
            display: "text",
            width: 2,
          },
          {
            id: "c",
            label: "C",
            kind: "key",
            chord: { code: "KeyC", modifiers: [] },
            display: "text",
            width: 2,
          },
          {
            id: "d",
            label: "D",
            kind: "key",
            chord: { code: "KeyD", modifiers: [] },
            display: "text",
            width: 2,
          },
          {
            id: "e",
            label: "E",
            kind: "key",
            chord: { code: "KeyE", modifiers: [] },
            display: "text",
            width: 2,
          },
          {
            id: "f",
            label: "F",
            kind: "key",
            chord: { code: "KeyF", modifiers: [] },
            display: "text",
            width: 2,
          },
        ],
        pinned: 1,
        enabled: true,
      },
      bottom: { keys: [], pinned: 0, enabled: false },
    };
    render(<TerminalKeyRow sessionRef={sessionRef} keyboardUp={false} bar={bar} />);
    const pinned = screen.getByRole("button", { name: "Pin" });
    fireEvent.pointerDown(pinned, { button: 0, pointerId: 30, clientX: 200, clientY: 20 });
    fireEvent.pointerMove(pinned, { pointerId: 30, clientX: 140, clientY: 22 });
    fireEvent.pointerUp(pinned, { pointerId: 30, clientX: 140, clientY: 22 });
    expect(screen.getByTestId("terminal-key-row")).toHaveAttribute("data-page", "0");
    expect(session.sendSoftKey).not.toHaveBeenCalled();
    // The pinned key remains visible while the rest pages through 10 cells.
    expect(screen.getByRole("button", { name: "Pin" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Top row: next page (1 of 2)" })).toBeInTheDocument();
  });

  it("sends a text key's explicit payload and leaves an armed modifier alone", () => {
    const { session, sessionRef } = makeSessionMock();
    const text = {
      id: "ok",
      label: "OK",
      kind: "text" as const,
      text: "ok",
      submit: true,
      display: "text" as const,
      width: 2 as const,
    };
    const ctrl = {
      id: "ctrl",
      label: "Ctrl",
      kind: "modifier" as const,
      modifier: "ctrl" as const,
      display: "text" as const,
      width: 2 as const,
    };
    render(
      <TerminalKeyRow sessionRef={sessionRef} keyboardUp={false} bar={barWith([ctrl, text])} />,
    );
    tapButton(screen.getByRole("button", { name: /^Ctrl,/ }));
    session.setStickyModifiers.mockClear();
    tapButton(screen.getByRole("button", { name: "OK" }));
    expect(session.sendSoftKey).toHaveBeenCalledWith("enter", "ok\r", { focus: false });
    expect(session.setStickyModifiers).not.toHaveBeenCalled();
    expect(session.getStickyModifiers().ctrl).toBe("once");
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

    const upBtn = screen.getByRole("button", { name: "↑" });
    const ctrlCBtn = screen.getByRole("button", { name: "^C" });

    fireEvent.pointerDown(upBtn, { button: 0, pointerId: 1, clientX: 20, clientY: 20 });
    fireEvent.pointerDown(ctrlCBtn, { button: 0, pointerId: 2, clientX: 200, clientY: 20 });
    fireEvent.pointerMove(ctrlCBtn, { pointerId: 2, clientX: 240, clientY: 20 });
    fireEvent.pointerUp(ctrlCBtn, { pointerId: 2, clientX: 240, clientY: 20 });
    expect(session.sendSoftKey).not.toHaveBeenCalled();

    fireEvent.pointerUp(upBtn, { pointerId: 1, clientX: 20, clientY: 20 });
    expect(session.sendSoftKey).toHaveBeenCalledTimes(1);
    expect(session.sendSoftKey).toHaveBeenLastCalledWith("arrowUp", undefined, { focus: false });

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

    session.sendSoftKey.mockClear();
    fireEvent.click(tabBtn, { detail: 1 });
    expect(session.sendSoftKey).not.toHaveBeenCalled();
  });

  it("keeps legacy items and container width scoping for header compatibility", () => {
    const { sessionRef } = makeSessionMock();
    render(<TerminalKeyRow sessionRef={sessionRef} keyboardUp={false} />);
    const content = screen.getByTestId("terminal-key-row").firstElementChild;
    expect(content).toHaveClass("@container");
    expect(content?.firstElementChild).toHaveClass("@min-[35rem]:grid-cols-12");
  });

  it("migrates legacy items cell-for-cell", () => {
    const bar = migrateLegacy(DEFAULT_TERMINAL_KEY_ITEMS, true);
    expect(bar.top.keys).toHaveLength(6);
    expect(bar.bottom.keys).toHaveLength(12);
    expect(bar.top.keys.every((key) => key.width === 2)).toBe(true);
    expect(bar.bottom.enabled).toBe(true);
  });

  it("keeps the original session typing surface for StickyModifiers", () => {
    const sticky: StickyModifiers = { ctrl: "off", alt: "off", shift: "off" };
    expect(sticky.shift).toBe("off");
  });
});
