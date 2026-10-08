// Target selection for the question-card focus chord: focus advances to the
// next card of the focused pane; without focus in a card, DOM proximity to
// the reference node (focused element, or the last pointerdown when focus is
// on <body>), then the last touched card, then the newest. A card's scroll
// container scopes the cycle to its pane. The reveal after entry scrolls the
// card into view and releases the conversation bottom-lock when it scrolls up.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useRef, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  ConversationScrollLockContext,
  type ConversationScrollLock,
} from "@/components/ai-elements/conversation";
import { writeShortcutPreference } from "@/lib/keyboardShortcutPreferences";
import {
  leaveQuestionCard,
  useFocusQuestionCardHotkey,
  useQuestionCardTarget,
} from "./useQuestionCardHotkeys";

afterEach(() => {
  cleanup();
  document.body.innerHTML = "";
  localStorage.clear();
});

beforeEach(() => {
  localStorage.clear();
});

function QuestionCard({ id, enter }: { id: string; enter: () => void }) {
  const ref = useRef<HTMLDivElement>(null);
  useQuestionCardTarget(ref, enter);
  return (
    <div ref={ref} data-question-card tabIndex={-1} data-testid={id}>
      <button type="button" data-testid={`${id}-button`}>
        inside {id}
      </button>
    </div>
  );
}

function Host() {
  useFocusQuestionCardHotkey();
  return null;
}

function Pane({
  composer,
  children,
  scroll = false,
}: {
  composer: string;
  children: ReactNode;
  scroll?: boolean;
}) {
  return (
    <div>
      <div data-composer-card>
        <textarea data-testid={composer} />
      </div>
      {scroll ? <div style={{ overflowY: "auto" }}>{children}</div> : children}
    </div>
  );
}

function pressFocusChord(init: KeyboardEventInit = {}): KeyboardEvent {
  const event = new KeyboardEvent("keydown", {
    code: "KeyF",
    key: "F",
    ctrlKey: true,
    shiftKey: true,
    bubbles: true,
    cancelable: true,
    ...init,
  });
  window.dispatchEvent(event);
  return event;
}

/** A card whose enter really moves focus, so the chord's cycle is observable. */
function FocusCard({ id, onEnter }: { id: string; onEnter: (id: string) => void }) {
  const ref = useRef<HTMLDivElement>(null);
  useQuestionCardTarget(ref, () => {
    ref.current?.focus();
    onEnter(id);
  });
  return (
    <div ref={ref} data-question-card tabIndex={-1} data-testid={id}>
      <button type="button" data-testid={`${id}-button`}>
        inside {id}
      </button>
    </div>
  );
}

function recordingEnter(): { entered: string[]; enter: (id: string) => void } {
  const entered: string[] = [];
  return { entered, enter: (id) => entered.push(id) };
}

function rect(top: number, bottom: number): DOMRect {
  return {
    top,
    bottom,
    height: bottom - top,
    left: 0,
    right: 0,
    width: 0,
    x: 0,
    y: top,
    toJSON: () => ({}),
  } as DOMRect;
}

function renderRevealCard(options: { scroller?: boolean; lock?: ConversationScrollLock } = {}) {
  const { entered, enter } = recordingEnter();
  const card = <FocusCard id="card-a" onEnter={enter} />;
  render(
    <>
      <Pane composer="composer">
        {options.scroller ? (
          <div data-testid="scroller" style={{ overflowY: "auto" }}>
            {options.lock ? (
              <ConversationScrollLockContext.Provider value={options.lock}>
                {card}
              </ConversationScrollLockContext.Provider>
            ) : (
              card
            )}
          </div>
        ) : (
          card
        )}
      </Pane>
      <Host />
    </>,
  );
  return { entered };
}

describe("useFocusQuestionCardHotkey", () => {
  it("enters the card containing focus and takes the event", () => {
    const enter = vi.fn();
    render(
      <>
        <QuestionCard id="card-a" enter={enter} />
        <Host />
      </>,
    );
    screen.getByTestId("card-a-button").focus();

    const event = pressFocusChord();

    expect(enter).toHaveBeenCalledTimes(1);
    expect(event.defaultPrevented).toBe(true);
  });

  it("leaves the event untouched when nothing is pending", () => {
    render(<Host />);

    const event = pressFocusChord();

    expect(event.defaultPrevented).toBe(false);
  });

  it("ignores auto-repeat and AltGraph", () => {
    const enter = vi.fn();
    render(
      <>
        <QuestionCard id="card-a" enter={enter} />
        <Host />
      </>,
    );

    pressFocusChord({ repeat: true });
    const altGraph = new KeyboardEvent("keydown", {
      code: "KeyF",
      ctrlKey: true,
      shiftKey: true,
      cancelable: true,
    });
    altGraph.getModifierState = () => true;
    window.dispatchEvent(altGraph);

    expect(enter).not.toHaveBeenCalled();
  });

  it("bails when focus sits in a surface that owns its keys", () => {
    const enter = vi.fn();
    render(
      <>
        <QuestionCard id="card-a" enter={enter} />
        <div className="xterm">
          <input data-testid="terminal-input" />
        </div>
        <Host />
      </>,
    );
    screen.getByTestId("terminal-input").focus();

    const event = pressFocusChord();

    expect(enter).not.toHaveBeenCalled();
    expect(event.defaultPrevented).toBe(false);
  });

  it("follows a rebound focus chord and ignores the old one", () => {
    const enter = vi.fn();
    render(
      <>
        <QuestionCard id="card-a" enter={enter} />
        <Host />
      </>,
    );
    writeShortcutPreference("focusQuestionCard", {
      common: [{ code: "KeyP", modifiers: ["control", "shift"] }],
    });

    const old = pressFocusChord();
    expect(enter).not.toHaveBeenCalled();
    expect(old.defaultPrevented).toBe(false);

    const rebound = pressFocusChord({ code: "KeyP", key: "P" });
    expect(enter).toHaveBeenCalledTimes(1);
    expect(rebound.defaultPrevented).toBe(true);
  });
});

describe("targetQuestionCard order", () => {
  it("prefers the last-touched card over a newer arrival in the same pane", () => {
    const enterA = vi.fn();
    const enterB = vi.fn();
    render(
      <>
        <Pane composer="composer">
          <QuestionCard id="card-a" enter={enterA} />
          <QuestionCard id="card-b" enter={enterB} />
        </Pane>
        <Host />
      </>,
    );
    fireEvent.pointerDown(screen.getByTestId("card-a"));

    // With the composer focused both cards tie on proximity (same pane), so
    // the touched one wins even though B is newer.
    screen.getByTestId("composer").focus();
    pressFocusChord();

    expect(enterA).toHaveBeenCalledTimes(1);
    expect(enterB).not.toHaveBeenCalled();
  });

  it("falls back to the newest card when nothing was touched", () => {
    const enterA = vi.fn();
    const enterB = vi.fn();
    render(
      <>
        <Pane composer="composer">
          <QuestionCard id="card-a" enter={enterA} />
          <QuestionCard id="card-b" enter={enterB} />
        </Pane>
        <Host />
      </>,
    );
    screen.getByTestId("composer").focus();
    pressFocusChord();

    expect(enterB).toHaveBeenCalledTimes(1);
    expect(enterA).not.toHaveBeenCalled();
  });

  it("uses the last pointerdown target when focus never left <body>", () => {
    // Safari does not focus a clicked button, so body stays the active
    // element; the click itself still says which card the user is on.
    const enterA = vi.fn();
    const enterB = vi.fn();
    render(
      <>
        <Pane composer="composer">
          <QuestionCard id="card-a" enter={enterA} />
          <QuestionCard id="card-b" enter={enterB} />
        </Pane>
        <Host />
      </>,
    );
    fireEvent.pointerDown(screen.getByTestId("card-a-button"));
    expect(document.activeElement).toBe(document.body);

    pressFocusChord();

    expect(enterA).toHaveBeenCalledTimes(1);
  });

  it("ignores a pointerdown target that was removed before the chord", () => {
    // The clicked control can vanish (its row collapsed) while focus sits on
    // <body>; the stale target must not hide the cards that are still mounted.
    const enter = vi.fn();
    const layout = (showControl: boolean) => (
      <>
        <Pane composer="composer">
          <QuestionCard id="card-a" enter={enter} />
        </Pane>
        {showControl && (
          <button type="button" data-testid="ephemeral">
            open
          </button>
        )}
        <Host />
      </>
    );
    const view = render(layout(true));
    fireEvent.pointerDown(screen.getByTestId("ephemeral"));

    view.rerender(layout(false));
    expect(document.activeElement).toBe(document.body);

    pressFocusChord();

    expect(enter).toHaveBeenCalledTimes(1);
  });

  it("scopes to the pane whose composer has focus", () => {
    const enterMain = vi.fn();
    const enterSide = vi.fn();
    render(
      <>
        <div data-testid="main-pane">
          <div data-composer-card>
            <textarea data-testid="main-composer" />
          </div>
          <QuestionCard id="main-card" enter={enterMain} />
        </div>
        <div data-testid="side-pane">
          <div data-composer-card>
            <textarea data-testid="side-composer" />
          </div>
          <QuestionCard id="side-card" enter={enterSide} />
        </div>
        <Host />
      </>,
    );
    fireEvent.pointerDown(screen.getByTestId("main-card"));

    screen.getByTestId("main-composer").focus();
    pressFocusChord();
    expect(enterMain).toHaveBeenCalledTimes(1);
    expect(enterSide).not.toHaveBeenCalled();

    screen.getByTestId("side-composer").focus();
    pressFocusChord();
    expect(enterSide).toHaveBeenCalledTimes(1);
  });

  it("picks the card in the row whose expand control has focus", () => {
    const enterA = vi.fn();
    const enterB = vi.fn();
    render(
      <>
        <div data-testid="inbox">
          <div>
            <button type="button" data-testid="toggle-a">
              expand a
            </button>
            <QuestionCard id="card-a" enter={enterA} />
          </div>
          <div>
            <button type="button" data-testid="toggle-b">
              expand b
            </button>
            <QuestionCard id="card-b" enter={enterB} />
          </div>
        </div>
        <Host />
      </>,
    );
    fireEvent.pointerDown(screen.getByTestId("card-a"));

    // Expanding B moves focus to B's own toggle, which is nearer B than A.
    screen.getByTestId("toggle-b").focus();
    pressFocusChord();

    expect(enterB).toHaveBeenCalledTimes(1);
    expect(enterA).not.toHaveBeenCalled();
  });

  it("picks the newest card of the focused pane over a touched card in another pane", () => {
    const enterOld = vi.fn();
    const enterNew = vi.fn();
    const enterSide = vi.fn();
    render(
      <>
        <Pane composer="main-composer">
          <QuestionCard id="main-old" enter={enterOld} />
          <QuestionCard id="main-new" enter={enterNew} />
        </Pane>
        <Pane composer="side-composer">
          <QuestionCard id="side-touched" enter={enterSide} />
        </Pane>
        <Host />
      </>,
    );
    fireEvent.pointerDown(screen.getByTestId("side-touched"));

    screen.getByTestId("main-composer").focus();
    pressFocusChord();

    expect(enterNew).toHaveBeenCalledTimes(1);
    expect(enterOld).not.toHaveBeenCalled();
    expect(enterSide).not.toHaveBeenCalled();
  });

  it("keeps the touched card when a newer one mounts in the same pane mid-answer", () => {
    const enterA = vi.fn();
    const enterB = vi.fn();
    const layout = (showB: boolean) => (
      <Pane composer="composer">
        <QuestionCard id="card-a" enter={enterA} />
        {showB && <QuestionCard id="card-b" enter={enterB} />}
      </Pane>
    );
    const view = render(
      <>
        {layout(false)}
        <Host />
      </>,
    );
    fireEvent.pointerDown(screen.getByTestId("card-a"));

    view.rerender(
      <>
        {layout(true)}
        <Host />
      </>,
    );
    screen.getByTestId("composer").focus();
    pressFocusChord();

    expect(enterA).toHaveBeenCalledTimes(1);
    expect(enterB).not.toHaveBeenCalled();
  });
});

describe("targetQuestionCard cycle", () => {
  it("cycles newest-first through the focused pane and wraps", () => {
    const { entered, enter } = recordingEnter();
    render(
      <>
        <Pane composer="composer" scroll>
          <FocusCard id="card-a" onEnter={enter} />
          <FocusCard id="card-b" onEnter={enter} />
          <FocusCard id="card-c" onEnter={enter} />
        </Pane>
        <Host />
      </>,
    );

    screen.getByTestId("composer").focus();
    pressFocusChord();
    pressFocusChord();
    pressFocusChord();
    pressFocusChord();

    expect(entered).toEqual(["card-c", "card-b", "card-a", "card-c"]);
  });

  it("cycles between two cards", () => {
    const { entered, enter } = recordingEnter();
    render(
      <>
        <Pane composer="composer" scroll>
          <FocusCard id="card-a" onEnter={enter} />
          <FocusCard id="card-b" onEnter={enter} />
        </Pane>
        <Host />
      </>,
    );

    screen.getByTestId("composer").focus();
    pressFocusChord();
    pressFocusChord();
    pressFocusChord();

    expect(entered).toEqual(["card-b", "card-a", "card-b"]);
  });

  it("re-enters the only card", () => {
    const { entered, enter } = recordingEnter();
    render(
      <>
        <Pane composer="composer" scroll>
          <FocusCard id="card-a" onEnter={enter} />
        </Pane>
        <Host />
      </>,
    );

    screen.getByTestId("composer").focus();
    pressFocusChord();
    pressFocusChord();

    expect(entered).toEqual(["card-a", "card-a"]);
  });

  it("never cycles into a card of another pane", () => {
    const { entered, enter } = recordingEnter();
    render(
      <>
        <Pane composer="main-composer" scroll>
          <FocusCard id="main-a" onEnter={enter} />
          <FocusCard id="main-b" onEnter={enter} />
          <FocusCard id="main-c" onEnter={enter} />
        </Pane>
        <Pane composer="side-composer" scroll>
          <FocusCard id="side-card" onEnter={enter} />
        </Pane>
        <Host />
      </>,
    );

    screen.getByTestId("main-composer").focus();
    pressFocusChord();
    pressFocusChord();
    pressFocusChord();

    expect(entered).toEqual(["main-c", "main-b", "main-a"]);
  });

  it("enters a touched card first, then cycles from it", () => {
    const { entered, enter } = recordingEnter();
    render(
      <>
        <Pane composer="composer" scroll>
          <FocusCard id="card-a" onEnter={enter} />
          <FocusCard id="card-b" onEnter={enter} />
          <FocusCard id="card-c" onEnter={enter} />
        </Pane>
        <Host />
      </>,
    );
    fireEvent.pointerDown(screen.getByTestId("card-a"));

    screen.getByTestId("composer").focus();
    pressFocusChord();
    pressFocusChord();
    pressFocusChord();

    expect(entered).toEqual(["card-a", "card-c", "card-b"]);
  });

  it("keeps the cycle in a one-card pane beside another pane's card", () => {
    const { entered, enter } = recordingEnter();
    render(
      <>
        <Pane composer="pane-1-composer" scroll>
          <FocusCard id="pane-1-card" onEnter={enter} />
        </Pane>
        <Pane composer="pane-2-composer" scroll>
          <FocusCard id="pane-2-card" onEnter={enter} />
        </Pane>
        <Host />
      </>,
    );

    screen.getByTestId("pane-1-card-button").focus();
    pressFocusChord();
    pressFocusChord();
    pressFocusChord();

    expect(entered).toEqual(["pane-1-card", "pane-1-card", "pane-1-card"]);
  });

  it("cycles a card nested deeper than its siblings in the same pane", () => {
    const { entered, enter } = recordingEnter();
    render(
      <>
        <Pane composer="composer" scroll>
          <FocusCard id="card-a" onEnter={enter} />
          <div>
            <div>
              <FocusCard id="card-b" onEnter={enter} />
            </div>
          </div>
          <FocusCard id="card-c" onEnter={enter} />
        </Pane>
        <Host />
      </>,
    );

    screen.getByTestId("composer").focus();
    pressFocusChord();
    pressFocusChord();
    pressFocusChord();
    pressFocusChord();

    expect(entered).toEqual(["card-c", "card-b", "card-a", "card-c"]);
  });

  it("re-enters a card whose pane has no scroll container", () => {
    const { entered, enter } = recordingEnter();
    render(
      <>
        <Pane composer="plain-composer">
          <FocusCard id="plain-card" onEnter={enter} />
        </Pane>
        <Pane composer="scrolled-composer" scroll>
          <FocusCard id="scrolled-card" onEnter={enter} />
        </Pane>
        <Host />
      </>,
    );

    screen.getByTestId("plain-card-button").focus();
    pressFocusChord();
    pressFocusChord();

    expect(entered).toEqual(["plain-card", "plain-card"]);
  });
});

describe("question card reveal", () => {
  it("scrolls a card above the view up and releases the bottom lock", () => {
    const lock: ConversationScrollLock = {
      stopScroll: vi.fn(),
      state: { isAtBottom: true, escapedFromLock: false },
    };
    renderRevealCard({ scroller: true, lock });
    const scroller = screen.getByTestId("scroller");
    const card = screen.getByTestId("card-a");
    vi.spyOn(scroller, "getBoundingClientRect").mockReturnValue(rect(100, 300));
    vi.spyOn(card, "getBoundingClientRect").mockReturnValue(rect(40, 90));
    scroller.scrollTop = 500;

    screen.getByTestId("composer").focus();
    pressFocusChord();

    expect(scroller.scrollTop).toBe(500 - (100 - 40 + 12));
    expect(lock.stopScroll).toHaveBeenCalledTimes(1);
    expect(lock.state.isAtBottom).toBe(false);
    expect(lock.state.escapedFromLock).toBe(true);
  });

  it("scrolls a card below the view down and leaves the lock alone", () => {
    const lock: ConversationScrollLock = {
      stopScroll: vi.fn(),
      state: { isAtBottom: true, escapedFromLock: false },
    };
    renderRevealCard({ scroller: true, lock });
    const scroller = screen.getByTestId("scroller");
    const card = screen.getByTestId("card-a");
    vi.spyOn(scroller, "getBoundingClientRect").mockReturnValue(rect(100, 300));
    vi.spyOn(card, "getBoundingClientRect").mockReturnValue(rect(310, 400));
    scroller.scrollTop = 500;

    screen.getByTestId("composer").focus();
    pressFocusChord();

    expect(scroller.scrollTop).toBe(500 + (400 - 300 + 12));
    expect(lock.stopScroll).not.toHaveBeenCalled();
    expect(lock.state.isAtBottom).toBe(true);
    expect(lock.state.escapedFromLock).toBe(false);
  });

  it("fits a card no taller than the view fully inside it", () => {
    const lock: ConversationScrollLock = {
      stopScroll: vi.fn(),
      state: { isAtBottom: true, escapedFromLock: false },
    };
    renderRevealCard({ scroller: true, lock });
    const scroller = screen.getByTestId("scroller");
    const card = screen.getByTestId("card-a");
    vi.spyOn(scroller, "getBoundingClientRect").mockReturnValue(rect(100, 300));
    vi.spyOn(card, "getBoundingClientRect").mockReturnValue(rect(310, 505));
    scroller.scrollTop = 500;

    screen.getByTestId("composer").focus();
    pressFocusChord();

    expect(scroller.scrollTop).toBe(500 + (505 - 300 + 2.5));
  });

  it("leaves the scroll alone when the card is already fully visible", () => {
    const lock: ConversationScrollLock = {
      stopScroll: vi.fn(),
      state: { isAtBottom: true, escapedFromLock: false },
    };
    renderRevealCard({ scroller: true, lock });
    const scroller = screen.getByTestId("scroller");
    const card = screen.getByTestId("card-a");
    vi.spyOn(scroller, "getBoundingClientRect").mockReturnValue(rect(100, 300));
    vi.spyOn(card, "getBoundingClientRect").mockReturnValue(rect(150, 250));
    scroller.scrollTop = 500;

    screen.getByTestId("composer").focus();
    pressFocusChord();

    expect(scroller.scrollTop).toBe(500);
    expect(lock.stopScroll).not.toHaveBeenCalled();
    expect(lock.state.isAtBottom).toBe(true);
    expect(lock.state.escapedFromLock).toBe(false);
  });

  it("top-aligns a card taller than the view", () => {
    const lock: ConversationScrollLock = {
      stopScroll: vi.fn(),
      state: { isAtBottom: true, escapedFromLock: false },
    };
    renderRevealCard({ scroller: true, lock });
    const scroller = screen.getByTestId("scroller");
    const card = screen.getByTestId("card-a");
    vi.spyOn(scroller, "getBoundingClientRect").mockReturnValue(rect(100, 300));
    vi.spyOn(card, "getBoundingClientRect").mockReturnValue(rect(120, 500));
    scroller.scrollTop = 500;

    screen.getByTestId("composer").focus();
    pressFocusChord();

    expect(scroller.scrollTop).toBe(500 + (120 - 100 - 12));
  });

  it("falls back to scrollIntoView outside a scrolling container", () => {
    renderRevealCard();
    const scrollIntoView = vi.fn();
    Object.defineProperty(screen.getByTestId("card-a"), "scrollIntoView", {
      configurable: true,
      value: scrollIntoView,
    });

    screen.getByTestId("composer").focus();
    pressFocusChord();

    expect(scrollIntoView).toHaveBeenCalledWith({ block: "nearest" });
  });
});

describe("registry cleanup", () => {
  it("never returns a card that has unmounted", () => {
    const enterA = vi.fn();
    const enterB = vi.fn();
    const layout = (showB: boolean) => (
      <Pane composer="composer">
        <QuestionCard id="card-a" enter={enterA} />
        {showB && <QuestionCard id="card-b" enter={enterB} />}
      </Pane>
    );
    const view = render(
      <>
        {layout(true)}
        <Host />
      </>,
    );

    view.rerender(
      <>
        {layout(false)}
        <Host />
      </>,
    );
    screen.getByTestId("composer").focus();
    pressFocusChord();

    expect(enterA).toHaveBeenCalledTimes(1);
    expect(enterB).not.toHaveBeenCalled();
  });

  it("removes the document pointerdown listener with the last card", () => {
    const addListener = vi.spyOn(document, "addEventListener");
    const removeListener = vi.spyOn(document, "removeEventListener");
    const view = render(<QuestionCard id="card-a" enter={vi.fn()} />);

    expect(addListener).toHaveBeenCalledWith("pointerdown", expect.any(Function), true);

    view.unmount();

    expect(removeListener).toHaveBeenCalledWith("pointerdown", expect.any(Function), true);
    addListener.mockRestore();
    removeListener.mockRestore();
  });
});

describe("leaveQuestionCard", () => {
  it("returns focus to the card's own pane composer", () => {
    const enter = vi.fn();
    render(
      <>
        <div data-testid="main-pane">
          <div data-composer-card>
            <textarea data-testid="main-composer" />
          </div>
        </div>
        <div data-testid="side-pane">
          <div data-composer-card>
            <textarea data-testid="side-composer" />
          </div>
          <QuestionCard id="side-card" enter={enter} />
        </div>
      </>,
    );

    leaveQuestionCard(screen.getByTestId("side-card"));

    expect(document.activeElement).toBe(screen.getByTestId("side-composer"));
  });

  it("restores the pre-entry element when the card has no composer", () => {
    const enter = vi.fn();
    render(
      <>
        <button type="button" data-testid="toolbar">
          open card
        </button>
        <QuestionCard id="card-a" enter={enter} />
        <Host />
      </>,
    );
    const toolbar = screen.getByTestId("toolbar");
    toolbar.focus();
    pressFocusChord();

    leaveQuestionCard(screen.getByTestId("card-a"));

    expect(document.activeElement).toBe(toolbar);
  });
});
