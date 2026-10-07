// Target selection for the question-card focus chord: focus wins, then DOM
// proximity to the reference node (focused element, or the last pointerdown
// when focus is on <body>), then the last touched card, then the newest.
// Pane scoping comes from proximity alone, so no session id is needed.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useRef, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

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

function Pane({ composer, children }: { composer: string; children: ReactNode }) {
  return (
    <div>
      <div data-composer-card>
        <textarea data-testid={composer} />
      </div>
      {children}
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

    pressFocusChord();
    expect(enter).not.toHaveBeenCalled();

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
