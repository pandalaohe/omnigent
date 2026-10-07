// Async-card behaviour of the AskUserQuestion form: the free markdown
// context renders through the chat markdown stack (links clickable), and
// a question with no options falls back to its free-text row instead of
// crashing on `options.map`.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useFocusQuestionCardHotkey } from "@/hooks/useQuestionCardHotkeys";
import { type ClaudeQuestion, castAskUserQuestionPayload } from "@/lib/askUserQuestion";
import { writeShortcutPreference } from "@/lib/keyboardShortcutPreferences";
import { FileViewerContext } from "@/shell/FileViewerContext";
import { AskUserQuestionForm } from "./AskUserQuestionForm";

afterEach(cleanup);

const openFile = vi.fn();
const FILE_VIEWER = {
  openFile,
  openGithubTab: () => {},
  isChangedPath: (p: string) => p === "report.html",
  conversationId: undefined,
  workspaceRoot: "/abs",
  workspaceHome: "/abs",
};

beforeEach(() => {
  openFile.mockClear();
});

function renderForm(questions: ClaudeQuestion[], context?: string) {
  const onSubmit = vi.fn();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  render(
    <QueryClientProvider client={client}>
      <FileViewerContext.Provider value={FILE_VIEWER}>
        <AskUserQuestionForm
          questions={questions}
          context={context}
          onSubmit={onSubmit}
          onReject={() => {}}
        />
      </FileViewerContext.Provider>
    </QueryClientProvider>,
  );
  return onSubmit;
}

describe("AskUserQuestionForm — async card", () => {
  it("renders the context markdown link as a clickable anchor above the questions", () => {
    // The context is agent-authored markdown (report paths, links); it must
    // go through the file-path-aware chat renderer, not sit as plain text.
    const payload = castAskUserQuestionPayload({
      questions: [{ question: "Ship it?", options: [{ label: "Yes" }] }],
      context: "[r](/abs/report.html)",
    });
    if (payload === null) throw new Error("expected a payload");
    renderForm(payload.questions, payload.context);

    // A rule under the context separates the recap from the question.
    expect(screen.getByTestId("ask-user-question-context")).toHaveClass(
      "border-b",
      "border-border-strong",
    );
    const link = screen.getByRole("button", { name: "r" });
    expect(link.tagName).toBe("A");
    expect(link).toHaveAttribute("data-streamdown", "link");
    fireEvent.click(link);
    expect(openFile).toHaveBeenCalledWith("report.html");
  });

  it("defaults missing options and submits the free-text answer", () => {
    // The server normalizes async questions; a question that arrives with
    // no options (or a payload that omits the key) must still render its
    // free-text row and be answerable — not crash on `options.map`.
    const payload = castAskUserQuestionPayload({
      questions: [{ question: "Name the report?" }],
    });
    if (payload === null) throw new Error("expected the defaulted payload");
    expect(payload.questions[0]!.options).toEqual([]);
    expect(payload.questions[0]!.header).toBe("");
    expect(payload.questions[0]!.multiSelect).toBe(false);

    const onSubmit = renderForm(payload.questions);

    // No option inputs; only the custom row's toggle.
    expect(screen.queryAllByRole("radio")).toHaveLength(1);
    expect(screen.getByTestId("ask-user-question-custom-input")).toBeDefined();

    fireEvent.change(screen.getByTestId("ask-user-question-custom-input"), {
      target: { value: "monthly-report" },
    });
    fireEvent.click(screen.getByTestId("ask-user-question-submit"));

    expect(onSubmit).toHaveBeenCalledWith({ "Name the report?": "monthly-report" });
  });
});

function HotkeyHost() {
  useFocusQuestionCardHotkey();
  return null;
}

interface QuestionSpec {
  id: string;
  question: string;
  options?: { label: string; preview?: string }[];
  multiSelect?: boolean;
}

function questionsOf(specs: QuestionSpec[]): ClaudeQuestion[] {
  const payload = castAskUserQuestionPayload({ questions: specs });
  if (payload === null) throw new Error("expected a payload");
  return payload.questions;
}

function renderKeyboardForm(
  questions: ClaudeQuestion[],
  {
    composer = true,
    before,
    onAbort,
  }: { composer?: boolean; before?: ReactNode; onAbort?: () => void } = {},
) {
  const onSubmit = vi.fn();
  const onReject = vi.fn();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  render(
    <QueryClientProvider client={client}>
      <FileViewerContext.Provider value={FILE_VIEWER}>
        <div>
          {composer && (
            <div data-composer-card>
              <textarea data-testid="composer" defaultValue="draft" />
            </div>
          )}
          {before}
          <AskUserQuestionForm
            questions={questions}
            onSubmit={onSubmit}
            onReject={onReject}
            onAbort={onAbort}
          />
        </div>
        <HotkeyHost />
      </FileViewerContext.Provider>
    </QueryClientProvider>,
  );
  return { onSubmit, onReject };
}

function card(): HTMLElement {
  return screen.getByTestId("ask-user-question-form");
}

function focusCard(): void {
  act(() => card().focus());
}

/** Which label row carries the keyboard highlight (options then custom row). */
function highlightedIndex(): number {
  return Array.from(card().querySelectorAll("label")).findIndex(
    (label) => label.getAttribute("data-highlighted") === "true",
  );
}

function pressCard(init: KeyboardEventInit): boolean {
  return fireEvent.keyDown(card(), init);
}

function progress(): string {
  return screen.getByTestId("ask-user-question-progress").textContent ?? "";
}

const TWO_QUESTIONS: QuestionSpec[] = [
  { id: "q1", question: "First?", options: [{ label: "A" }, { label: "B" }] },
  { id: "q2", question: "Second?", options: [{ label: "C" }, { label: "D" }] },
];

describe("AskUserQuestionForm — keyboard", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  afterEach(() => {
    cleanup();
    localStorage.clear();
  });

  it("enters from the composer on the focus chord and leaves the draft alone", () => {
    renderKeyboardForm(questionsOf(TWO_QUESTIONS));
    screen.getByTestId("composer").focus();

    fireEvent.keyDown(window, { key: "F", code: "KeyF", ctrlKey: true, shiftKey: true });

    expect(document.activeElement).toBe(card());
    expect(highlightedIndex()).toBe(0);
    expect(screen.getByTestId("composer")).toHaveValue("draft");
  });

  it("moves the highlight without selecting", () => {
    renderKeyboardForm(questionsOf(TWO_QUESTIONS));
    focusCard();

    pressCard({ key: "ArrowDown", code: "ArrowDown" });
    pressCard({ key: "ArrowDown", code: "ArrowDown" });

    expect(highlightedIndex()).toBe(2);
    for (const radio of screen.getAllByRole("radio")) expect(radio).not.toBeChecked();
  });

  it("selects the highlighted option and shows its preview", () => {
    renderKeyboardForm(
      questionsOf([
        {
          id: "q1",
          question: "Which patch?",
          options: [{ label: "A" }, { label: "B", preview: "diff --git a/f b/f" }],
        },
        { id: "q2", question: "Then?", options: [{ label: "C" }] },
      ]),
    );
    focusCard();

    pressCard({ key: "ArrowDown", code: "ArrowDown" });
    pressCard({ key: " ", code: "Space" });

    expect(screen.getAllByRole("radio")[1]).toBeChecked();
    expect(screen.getByTestId("ask-user-question-previews")).toHaveTextContent(
      "diff --git a/f b/f",
    );
    expect(progress()).toContain("Question 1 of 2");
  });

  it("advances with primary+Enter, submits on the last question and returns focus", () => {
    const { onSubmit } = renderKeyboardForm(questionsOf(TWO_QUESTIONS));
    screen.getByTestId("composer").focus();
    fireEvent.keyDown(window, { key: "F", code: "KeyF", ctrlKey: true, shiftKey: true });

    pressCard({ key: " ", code: "Space" });
    pressCard({ key: "Enter", code: "Enter", ctrlKey: true });
    expect(progress()).toContain("Question 2 of 2");

    pressCard({ key: " ", code: "Space" });
    pressCard({ key: "Enter", code: "Enter", ctrlKey: true });

    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect(onSubmit).toHaveBeenCalledWith({ q1: "A", q2: "C" });
    expect(document.activeElement).toBe(screen.getByTestId("composer"));
  });

  it("jumps to the first unanswered question instead of submitting", () => {
    const { onSubmit } = renderKeyboardForm(questionsOf(TWO_QUESTIONS));
    focusCard();

    pressCard({ key: "ArrowRight", code: "ArrowRight" });
    expect(progress()).toContain("Question 2 of 2");

    pressCard({ key: "Enter", code: "Enter", ctrlKey: true });

    expect(progress()).toContain("Question 1 of 2");
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("keeps the question at the ends when paging", () => {
    renderKeyboardForm(questionsOf(TWO_QUESTIONS));
    focusCard();

    pressCard({ key: "ArrowLeft", code: "ArrowLeft" });
    expect(progress()).toContain("Question 1 of 2");

    pressCard({ key: "ArrowRight", code: "ArrowRight" });
    pressCard({ key: "ArrowRight", code: "ArrowRight" });
    expect(progress()).toContain("Question 2 of 2");
  });

  it("selects the custom row, keeps typed text, and Esc returns to the options", () => {
    renderKeyboardForm(
      questionsOf([{ id: "q1", question: "Name it?", options: [{ label: "A" }, { label: "B" }] }]),
    );
    focusCard();

    pressCard({ key: "ArrowDown", code: "ArrowDown" });
    pressCard({ key: "ArrowDown", code: "ArrowDown" });
    pressCard({ key: " ", code: "Space" });

    const toggle = screen.getByTestId("ask-user-question-custom-toggle");
    const textarea = screen.getByTestId("ask-user-question-custom-input");
    expect(document.activeElement).toBe(textarea);
    expect(toggle).toBeChecked();

    fireEvent.change(textarea, { target: { value: "a" } });
    // Plain Enter stays a newline in the box: the event is left alone.
    expect(fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" })).toBe(true);
    fireEvent.change(textarea, { target: { value: "a\n" } });

    fireEvent.keyDown(textarea, { key: "Escape", code: "Escape" });

    expect(document.activeElement).toBe(card());
    expect(highlightedIndex()).toBe(2);
    expect(textarea).toHaveValue("a\n");
    expect(toggle).toBeChecked();
  });

  it("toggles a multi option once per Space press", () => {
    renderKeyboardForm(
      questionsOf([
        {
          id: "q1",
          question: "Pick any?",
          options: [{ label: "A" }, { label: "B" }],
          multiSelect: true,
        },
      ]),
    );
    focusCard();

    pressCard({ key: " ", code: "Space" });
    fireEvent.keyUp(card(), { key: " ", code: "Space" });
    expect(screen.getAllByRole("checkbox")[0]).toBeChecked();

    pressCard({ key: " ", code: "Space" });
    fireEvent.keyUp(card(), { key: " ", code: "Space" });
    expect(screen.getAllByRole("checkbox")[0]).not.toBeChecked();
  });

  it("selects and unselects the multi custom row", () => {
    renderKeyboardForm(
      questionsOf([
        { id: "q1", question: "Pick any?", options: [{ label: "A" }], multiSelect: true },
      ]),
    );
    focusCard();

    pressCard({ key: "ArrowDown", code: "ArrowDown" });
    pressCard({ key: " ", code: "Space" });
    const textarea = screen.getByTestId("ask-user-question-custom-input");
    const toggle = screen.getByTestId("ask-user-question-custom-toggle");
    expect(toggle).toBeChecked();
    expect(document.activeElement).toBe(textarea);

    focusCard();
    pressCard({ key: " ", code: "Space" });
    expect(toggle).not.toBeChecked();
  });

  it("moves the highlight when an option input takes mouse focus", () => {
    renderKeyboardForm(questionsOf(TWO_QUESTIONS));
    focusCard();

    act(() => screen.getAllByRole("radio")[1]?.focus());
    expect(highlightedIndex()).toBe(1);
  });

  it("leaves to the card's own composer on Esc, without rejecting", () => {
    const { onReject } = renderKeyboardForm(questionsOf(TWO_QUESTIONS), {
      before: (
        <button type="button" data-testid="toolbar">
          open the card
        </button>
      ),
    });
    const toolbar = screen.getByTestId("toolbar");
    toolbar.focus();
    fireEvent.keyDown(window, { key: "F", code: "KeyF", ctrlKey: true, shiftKey: true });

    pressCard({ key: "Escape", code: "Escape" });

    expect(document.activeElement).toBe(screen.getByTestId("composer"));
    expect(onReject).not.toHaveBeenCalled();
    expect(screen.getByTestId("ask-user-question-form")).toBeTruthy();
  });

  it("ignores card keys while the composer has focus", () => {
    renderKeyboardForm(questionsOf(TWO_QUESTIONS));
    const composer = screen.getByTestId("composer");
    composer.focus();

    fireEvent.keyDown(composer, { key: "ArrowDown", code: "ArrowDown" });
    fireEvent.keyDown(composer, { key: " ", code: "Space" });
    fireEvent.keyDown(composer, { key: "Escape", code: "Escape" });

    for (const radio of screen.getAllByRole("radio")) expect(radio).not.toBeChecked();
    expect(screen.queryByTestId("ask-user-question-hint")).toBeNull();
    expect(progress()).toContain("Question 1 of 2");
  });

  it("keeps the keys working after primary+Enter from the custom box", () => {
    renderKeyboardForm(questionsOf(TWO_QUESTIONS));
    focusCard();

    pressCard({ key: "ArrowDown", code: "ArrowDown" });
    pressCard({ key: "ArrowDown", code: "ArrowDown" });
    pressCard({ key: " ", code: "Space" });
    fireEvent.change(screen.getByTestId("ask-user-question-custom-input"), {
      target: { value: "typed" },
    });

    pressCard({ key: "Enter", code: "Enter", ctrlKey: true });
    expect(progress()).toContain("Question 2 of 2");
    expect(document.activeElement).toBe(card());

    pressCard({ key: "ArrowDown", code: "ArrowDown" });
    pressCard({ key: " ", code: "Space" });
    expect(screen.getAllByRole("radio")[1]).toBeChecked();
  });

  it("leaves plain Enter on the navigation buttons inert but lets Space reach them", () => {
    const { onSubmit } = renderKeyboardForm(
      questionsOf([{ id: "q1", question: "Only?", options: [{ label: "A" }] }]),
    );
    focusCard();
    pressCard({ key: " ", code: "Space" });

    const submit = screen.getByTestId("ask-user-question-submit");
    submit.focus();

    expect(fireEvent.keyDown(submit, { key: "Enter", code: "Enter" })).toBe(false);
    expect(onSubmit).not.toHaveBeenCalled();
    // Not prevented: native Space activation still reaches the button.
    expect(fireEvent.keyDown(submit, { key: " ", code: "Space" })).toBe(true);
  });

  it("owns native Space and arrows on option inputs even when select is rebound", () => {
    act(() => {
      writeShortcutPreference("questionCardSelectOption", {
        common: [{ code: "KeyX", modifiers: [] }],
      });
    });
    renderKeyboardForm(questionsOf(TWO_QUESTIONS));
    focusCard();
    pressCard({ key: "ArrowDown", code: "ArrowDown" });

    const radio = screen.getAllByRole("radio")[1] as HTMLInputElement;
    act(() => radio.focus());

    expect(fireEvent.keyDown(radio, { key: " ", code: "Space" })).toBe(false);
    expect(fireEvent.keyUp(radio, { key: " ", code: "Space" })).toBe(false);
    expect(fireEvent.keyDown(radio, { key: "ArrowDown", code: "ArrowDown" })).toBe(false);
    expect(radio).not.toBeChecked();
  });

  it("follows a rebound select key and drops the old one", () => {
    renderKeyboardForm(questionsOf(TWO_QUESTIONS));
    act(() => {
      writeShortcutPreference("questionCardSelectOption", {
        common: [{ code: "KeyX", modifiers: [] }],
      });
    });
    focusCard();

    pressCard({ key: " ", code: "Space" });
    expect(screen.getAllByRole("radio")[0]).not.toBeChecked();

    pressCard({ key: "x", code: "KeyX" });
    expect(screen.getAllByRole("radio")[0]).toBeChecked();
    expect(screen.getByTestId("ask-user-question-hint")).toHaveTextContent("X select");
  });

  it("drops a disabled action from the hint and stops acting on it", () => {
    renderKeyboardForm(questionsOf(TWO_QUESTIONS));
    act(() => {
      writeShortcutPreference("questionCardLeave", { enabled: false });
    });
    focusCard();

    expect(screen.getByTestId("ask-user-question-hint")).not.toHaveTextContent("leave");
    expect(pressCard({ key: "Escape", code: "Escape" })).toBe(true);
    expect(document.activeElement).toBe(card());
  });

  it("lists the default bindings in the hint line", () => {
    renderKeyboardForm(questionsOf(TWO_QUESTIONS));
    focusCard();

    expect(screen.getByTestId("ask-user-question-hint")).toHaveTextContent(
      "↑ ↓ move · Space select · Ctrl ↵ next / submit · ← → question · Esc leave",
    );
  });

  it("cancels and interrupts through their bindings", () => {
    const onAbort = vi.fn();
    const { onReject } = renderKeyboardForm(questionsOf(TWO_QUESTIONS), { onAbort });
    act(() => {
      writeShortcutPreference("questionCardCancel", { common: [{ code: "KeyC", modifiers: [] }] });
      writeShortcutPreference("questionCardCancelAndInterrupt", {
        common: [{ code: "KeyI", modifiers: [] }],
      });
    });
    focusCard();

    pressCard({ key: "c", code: "KeyC" });
    expect(onReject).toHaveBeenCalledTimes(1);
    expect(onAbort).not.toHaveBeenCalled();

    pressCard({ key: "i", code: "KeyI" });
    expect(onAbort).toHaveBeenCalledTimes(1);
  });
});
