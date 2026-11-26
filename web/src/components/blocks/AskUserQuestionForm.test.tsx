// Async-card behaviour of the AskUserQuestion form: the free markdown
// context renders through the chat markdown stack (links clickable), and
// a question with no options falls back to its free-text row instead of
// crashing on `options.map`.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useFocusQuestionCardHotkey } from "@/hooks/useQuestionCardHotkeys";
import { type ClaudeQuestion, castAskUserQuestionPayload } from "@/lib/askUserQuestion";
import { type ShortcutActionId, writeShortcutPreference } from "@/lib/keyboardShortcutPreferences";
import { FileViewerContext } from "@/shell/FileViewerContext";
import type * as UseConversationsModule from "@/hooks/useConversations";
import type * as UseSidebarDataModule from "@/hooks/useSidebarData";
import * as CodeMirrorModule from "@/components/composer/engine/CodeMirrorComposerInput";
import { resetCodeMirrorComposerLoadForTests } from "@/components/composer/engine/codeMirrorComposerLoader";
import { ConversationScopeContext } from "@/components/chat/conversationScope";
import { composerTargetOf } from "@/components/composer/engine/composerInput";
import { undo } from "@codemirror/commands";
import { EditorView } from "@codemirror/view";
import { askUserQuestionSignature, getAskUserQuestionDraft } from "@/lib/askUserQuestionDrafts";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { SESSION_MENTION_HINT } from "@/lib/composerMentions";
import { insertComposerText } from "@/lib/composerText";
import { ApprovalCard } from "./ApprovalCard";
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
  return Array.from(
    card().querySelectorAll('input[type="radio"], input[type="checkbox"]'),
  ).findIndex((input) => {
    const label = input.closest("label");
    const row =
      input.getAttribute("data-testid") === "ask-user-question-custom-toggle"
        ? label?.parentElement
        : label;
    return row?.getAttribute("data-highlighted") === "true";
  });
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

interface RebindHarness {
  press: (init: KeyboardEventInit) => boolean;
  progress: () => string;
  highlighted: () => number;
  composer: HTMLElement;
  onReject: ReturnType<typeof vi.fn>;
  onAbort: ReturnType<typeof vi.fn>;
}

const REBIND_CASES: {
  action: ShortcutActionId;
  code: string;
  /** Bound first, then overwritten with `code` while the card is mounted. */
  priorCode?: string;
  run: (h: RebindHarness) => void;
}[] = [
  {
    action: "questionCardPreviousOption",
    code: "KeyA",
    run: (h) => {
      h.press({ key: "ArrowDown", code: "ArrowDown" });
      expect(h.highlighted()).toBe(1);
      h.press({ key: "ArrowUp", code: "ArrowUp" });
      expect(h.highlighted()).toBe(1);
      h.press({ key: "a", code: "KeyA" });
      expect(h.highlighted()).toBe(0);
    },
  },
  {
    action: "questionCardNextOption",
    code: "KeyB",
    run: (h) => {
      h.press({ key: "ArrowDown", code: "ArrowDown" });
      expect(h.highlighted()).toBe(0);
      h.press({ key: "b", code: "KeyB" });
      expect(h.highlighted()).toBe(1);
    },
  },
  {
    action: "questionCardSelectOption",
    code: "KeyC",
    run: (h) => {
      // Row 0 already starts selected; move the highlight to row 1
      // without selecting it so the rebound key has a row to confirm.
      const option = screen.getAllByRole("radio")[1] as HTMLInputElement;
      act(() => option.focus());
      expect(h.highlighted()).toBe(1);
      h.press({ key: " ", code: "Space" });
      expect(option).not.toBeChecked();
      h.press({ key: "c", code: "KeyC" });
      expect(option).toBeChecked();
    },
  },
  {
    action: "questionCardNextOrSubmit",
    code: "KeyD",
    run: (h) => {
      h.press({ key: "Enter", code: "Enter", ctrlKey: true });
      expect(h.progress()).toContain("Question 1 of 2");
      h.press({ key: "d", code: "KeyD" });
      expect(h.progress()).toContain("Question 2 of 2");
    },
  },
  {
    action: "questionCardPreviousQuestion",
    code: "KeyE",
    run: (h) => {
      h.press({ key: "ArrowRight", code: "ArrowRight" });
      expect(h.progress()).toContain("Question 2 of 2");
      h.press({ key: "ArrowLeft", code: "ArrowLeft" });
      expect(h.progress()).toContain("Question 2 of 2");
      h.press({ key: "e", code: "KeyE" });
      expect(h.progress()).toContain("Question 1 of 2");
    },
  },
  {
    action: "questionCardNextQuestion",
    code: "KeyF",
    run: (h) => {
      h.press({ key: "ArrowRight", code: "ArrowRight" });
      expect(h.progress()).toContain("Question 1 of 2");
      h.press({ key: "f", code: "KeyF" });
      expect(h.progress()).toContain("Question 2 of 2");
    },
  },
  {
    action: "questionCardLeave",
    code: "KeyG",
    run: (h) => {
      expect(h.press({ key: "Escape", code: "Escape" })).toBe(true);
      expect(document.activeElement).toBe(card());
      h.press({ key: "g", code: "KeyG" });
      expect(document.activeElement).toBe(h.composer);
    },
  },
  {
    action: "questionCardCancel",
    code: "KeyJ",
    priorCode: "KeyH",
    run: (h) => {
      h.press({ key: "h", code: "KeyH" });
      expect(h.onReject).not.toHaveBeenCalled();
      h.press({ key: "j", code: "KeyJ" });
      expect(h.onReject).toHaveBeenCalledTimes(1);
      expect(document.activeElement).toBe(h.composer);
    },
  },
  {
    action: "questionCardCancelAndInterrupt",
    code: "KeyJ",
    priorCode: "KeyH",
    run: (h) => {
      h.press({ key: "h", code: "KeyH" });
      expect(h.onAbort).not.toHaveBeenCalled();
      h.press({ key: "j", code: "KeyJ" });
      expect(h.onAbort).toHaveBeenCalledTimes(1);
      expect(document.activeElement).toBe(h.composer);
    },
  },
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

    fireEvent.keyDown(window, { key: "G", code: "KeyG", altKey: true, shiftKey: true });

    expect(document.activeElement).toBe(card());
    expect(highlightedIndex()).toBe(0);
    expect(screen.getByTestId("composer")).toHaveValue("draft");
  });

  it("preselects option one on single-select questions and leaves the rest empty", () => {
    // Every single-select question opens with its first option checked,
    // so the user only adjusts what they disagree with; multi-select and
    // free-text questions stay unanswered until the user acts.
    const { onSubmit } = renderKeyboardForm(
      questionsOf([
        { id: "s1", question: "First pick?", options: [{ label: "A" }, { label: "B" }] },
        { id: "s2", question: "Second pick?", options: [{ label: "C" }, { label: "D" }] },
        {
          id: "m1",
          question: "Pick any?",
          options: [{ label: "E" }, { label: "F" }],
          multiSelect: true,
        },
        { id: "free", question: "Anything else?" },
      ]),
    );

    const firstRadios = screen.getAllByRole("radio");
    expect(firstRadios[0]).toBeChecked();
    expect(firstRadios[1]).not.toBeChecked();
    expect(screen.getByTestId("ask-user-question-custom-toggle")).not.toBeChecked();

    fireEvent.click(screen.getByTestId("ask-user-question-next"));
    const secondRadios = screen.getAllByRole("radio");
    expect(secondRadios[0]).toBeChecked();
    expect(secondRadios[1]).not.toBeChecked();

    fireEvent.click(screen.getByTestId("ask-user-question-next"));
    const checkboxes = screen.getAllByRole("checkbox");
    expect(checkboxes).toHaveLength(3);
    for (const checkbox of checkboxes) expect(checkbox).not.toBeChecked();
    fireEvent.click(screen.getByLabelText("E"));

    fireEvent.click(screen.getByTestId("ask-user-question-next"));
    expect(screen.getByTestId("ask-user-question-custom-input")).toHaveValue("");
    expect(screen.getByTestId("ask-user-question-submit").hasAttribute("disabled")).toBe(true);

    fireEvent.change(screen.getByTestId("ask-user-question-custom-input"), {
      target: { value: "custom note" },
    });
    fireEvent.click(screen.getByTestId("ask-user-question-submit"));

    expect(onSubmit).toHaveBeenCalledWith({
      s1: "A",
      s2: "C",
      m1: ["E"],
      free: "custom note",
    });
  });

  it("re-enters on the custom row when a multi-select answer is custom-only", () => {
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

    pressCard({ key: "ArrowDown", code: "ArrowDown" });
    pressCard({ key: "ArrowDown", code: "ArrowDown" });
    pressCard({ key: " ", code: "Space" });
    const textarea = screen.getByTestId("ask-user-question-custom-input");
    const customLabel = screen
      .getByTestId("ask-user-question-custom-toggle")
      .closest("label")?.parentElement;
    fireEvent.change(textarea, { target: { value: "custom answer" } });

    // Esc from the box returns to the options, the next Esc leaves to the composer.
    fireEvent.keyDown(textarea, { key: "Escape", code: "Escape" });
    pressCard({ key: "Escape", code: "Escape" });
    expect(document.activeElement).toBe(screen.getByTestId("composer"));

    fireEvent.keyDown(window, { key: "G", code: "KeyG", altKey: true, shiftKey: true });

    expect(document.activeElement).toBe(card());
    expect(customLabel?.getAttribute("data-highlighted")).toBe("true");
    expect(highlightedIndex()).toBe(2);
  });

  it("selects each row the arrows move to on a single-select question", () => {
    renderKeyboardForm(
      questionsOf([
        {
          id: "q1",
          question: "Which patch?",
          options: [{ label: "A" }, { label: "B", preview: "diff --git a/f b/f" }],
        },
      ]),
    );
    focusCard();

    pressCard({ key: "ArrowDown", code: "ArrowDown" });
    expect(highlightedIndex()).toBe(1);
    expect(screen.getAllByRole("radio")[1]).toBeChecked();
    expect(screen.getByTestId("ask-user-question-previews")).toHaveTextContent(
      "diff --git a/f b/f",
    );

    pressCard({ key: "ArrowDown", code: "ArrowDown" });
    expect(highlightedIndex()).toBe(2);
    const toggle = screen.getByTestId("ask-user-question-custom-toggle");
    expect(toggle).toBeChecked();
    expect(screen.getAllByRole("radio")[0]).not.toBeChecked();
    expect(screen.getAllByRole("radio")[1]).not.toBeChecked();
    // Focus stays on the card so the arrows keep working from the custom row.
    expect(document.activeElement).toBe(card());
    expect(document.activeElement).not.toBe(screen.getByTestId("ask-user-question-custom-input"));

    pressCard({ key: "ArrowUp", code: "ArrowUp" });
    expect(highlightedIndex()).toBe(1);
    expect(screen.getAllByRole("radio")[1]).toBeChecked();
    expect(toggle).not.toBeChecked();
  });

  it("keeps the single-select highlight clamped at both ends and selects the row", () => {
    renderKeyboardForm(
      questionsOf([{ id: "q1", question: "Pick?", options: [{ label: "A" }, { label: "B" }] }]),
    );
    focusCard();

    pressCard({ key: "ArrowUp", code: "ArrowUp" });
    expect(highlightedIndex()).toBe(0);
    expect(screen.getAllByRole("radio")[0]).toBeChecked();

    pressCard({ key: "ArrowDown", code: "ArrowDown" });
    pressCard({ key: "ArrowDown", code: "ArrowDown" });
    pressCard({ key: "ArrowDown", code: "ArrowDown" });
    expect(highlightedIndex()).toBe(2);
    expect(screen.getByTestId("ask-user-question-custom-toggle")).toBeChecked();
  });

  it("moves the multi-select highlight without selecting", () => {
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

    pressCard({ key: "ArrowDown", code: "ArrowDown" });
    pressCard({ key: "ArrowDown", code: "ArrowDown" });
    expect(highlightedIndex()).toBe(2);
    for (const checkbox of screen.getAllByRole("checkbox")) expect(checkbox).not.toBeChecked();

    pressCard({ key: " ", code: "Space" });
    fireEvent.keyUp(card(), { key: " ", code: "Space" });
    expect(screen.getByTestId("ask-user-question-custom-toggle")).toBeChecked();

    pressCard({ key: " ", code: "Space" });
    fireEvent.keyUp(card(), { key: " ", code: "Space" });
    expect(screen.getByTestId("ask-user-question-custom-toggle")).not.toBeChecked();
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

    // Focusing the option input highlights its row without selecting it, so
    // the Space below is what must select.
    const option = screen.getAllByRole("radio")[1] as HTMLInputElement;
    act(() => option.focus());
    expect(highlightedIndex()).toBe(1);
    expect(option).not.toBeChecked();

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
    fireEvent.keyDown(window, { key: "G", code: "KeyG", altKey: true, shiftKey: true });

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
    // The single-select questions are answered by their default first
    // option, so the multi-select one is the unanswered question to jump
    // back to.
    const { onSubmit } = renderKeyboardForm(
      questionsOf([
        {
          id: "q1",
          question: "First?",
          options: [{ label: "A" }, { label: "B" }],
          multiSelect: true,
        },
        { id: "q2", question: "Second?", options: [{ label: "C" }] },
      ]),
    );
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

    // Focusing the custom toggle highlights its row without selecting it, so
    // the Space below is what must select.
    const toggle = screen.getByTestId("ask-user-question-custom-toggle") as HTMLInputElement;
    act(() => toggle.focus());
    expect(highlightedIndex()).toBe(2);
    expect(toggle).not.toBeChecked();

    pressCard({ key: " ", code: "Space" });

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

  it("keeps typed custom text when the arrows leave and re-select the custom row", () => {
    renderKeyboardForm(
      questionsOf([{ id: "q1", question: "Name it?", options: [{ label: "A" }, { label: "B" }] }]),
    );
    focusCard();

    const toggle = screen.getByTestId("ask-user-question-custom-toggle") as HTMLInputElement;
    act(() => toggle.focus());
    expect(highlightedIndex()).toBe(2);
    expect(toggle).not.toBeChecked();
    pressCard({ key: " ", code: "Space" });

    const textarea = screen.getByTestId("ask-user-question-custom-input");
    expect(document.activeElement).toBe(textarea);
    expect(toggle).toBeChecked();
    fireEvent.change(textarea, { target: { value: "abc" } });

    fireEvent.keyDown(textarea, { key: "Escape", code: "Escape" });
    expect(document.activeElement).toBe(card());

    // Up selects the option above and drops the custom row.
    pressCard({ key: "ArrowUp", code: "ArrowUp" });
    expect(highlightedIndex()).toBe(1);
    expect(screen.getAllByRole("radio")[1]).toBeChecked();
    expect(toggle).not.toBeChecked();

    // Back down re-selects the custom row with the text intact.
    pressCard({ key: "ArrowDown", code: "ArrowDown" });
    expect(highlightedIndex()).toBe(2);
    expect(toggle).toBeChecked();
    expect(textarea).toHaveValue("abc");
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
    fireEvent.keyDown(window, { key: "G", code: "KeyG", altKey: true, shiftKey: true });

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

    // The card keeps its seeded first-option selection; ArrowDown would
    // have moved it to B had the card acted on the composer's keys.
    const radios = screen.getAllByRole("radio");
    expect(radios[0]).toBeChecked();
    expect(radios[1]).not.toBeChecked();
    expect(screen.getByTestId("ask-user-question-custom-toggle")).not.toBeChecked();
    expect(progress()).toContain("Question 1 of 2");
  });

  it("keeps the keys working after primary+Enter from the focused custom box", () => {
    renderKeyboardForm(questionsOf(TWO_QUESTIONS));
    focusCard();

    pressCard({ key: "ArrowDown", code: "ArrowDown" });
    pressCard({ key: "ArrowDown", code: "ArrowDown" });
    pressCard({ key: " ", code: "Space" });
    const textarea = screen.getByTestId("ask-user-question-custom-input");
    expect(document.activeElement).toBe(textarea);
    fireEvent.change(textarea, { target: { value: "typed" } });

    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter", ctrlKey: true });
    expect(progress()).toContain("Question 2 of 2");
    expect(document.activeElement).toBe(card());

    // Space still confirms the next question's highlighted row: focus it
    // without selecting first.
    const option = screen.getAllByRole("radio")[1] as HTMLInputElement;
    act(() => option.focus());
    expect(highlightedIndex()).toBe(1);
    expect(option).not.toBeChecked();
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

    const radio = screen.getAllByRole("radio")[1] as HTMLInputElement;
    act(() => radio.focus());
    expect(highlightedIndex()).toBe(1);

    // Space is no longer the select binding, so the card only swallows it.
    expect(fireEvent.keyDown(radio, { key: " ", code: "Space" })).toBe(false);
    expect(fireEvent.keyUp(radio, { key: " ", code: "Space" })).toBe(false);
    expect(radio).not.toBeChecked();

    // The card also resolves the arrow itself: it selects the custom row.
    expect(fireEvent.keyDown(radio, { key: "ArrowDown", code: "ArrowDown" })).toBe(false);
    expect(highlightedIndex()).toBe(2);
    expect(screen.getByTestId("ask-user-question-custom-toggle")).toBeChecked();
  });

  it("follows a rebound select key and drops the old one", () => {
    renderKeyboardForm(questionsOf(TWO_QUESTIONS));
    act(() => {
      writeShortcutPreference("questionCardSelectOption", {
        common: [{ code: "KeyX", modifiers: [] }],
      });
    });
    focusCard();

    // Row 0 already starts selected; highlight row 1 without selecting
    // it so the old key would have something to select and the new key
    // has a row to confirm.
    const option = screen.getAllByRole("radio")[1] as HTMLInputElement;
    act(() => option.focus());

    pressCard({ key: " ", code: "Space" });
    expect(option).not.toBeChecked();

    pressCard({ key: "x", code: "KeyX" });
    expect(option).toBeChecked();
  });

  it("selects through a rebound next-option key and drops the old arrow", () => {
    renderKeyboardForm(questionsOf(TWO_QUESTIONS));
    act(() => {
      writeShortcutPreference("questionCardNextOption", {
        common: [{ code: "KeyB", modifiers: [] }],
      });
    });
    focusCard();

    pressCard({ key: "ArrowDown", code: "ArrowDown" });
    expect(highlightedIndex()).toBe(0);
    expect(screen.getAllByRole("radio")[0]).toBeChecked();
    expect(screen.getAllByRole("radio")[1]).not.toBeChecked();

    pressCard({ key: "b", code: "KeyB" });
    expect(highlightedIndex()).toBe(1);
    expect(screen.getAllByRole("radio")[1]).toBeChecked();
  });

  it("stops acting on a disabled action", () => {
    renderKeyboardForm(questionsOf(TWO_QUESTIONS));
    act(() => {
      writeShortcutPreference("questionCardLeave", { enabled: false });
    });
    focusCard();

    expect(pressCard({ key: "Escape", code: "Escape" })).toBe(true);
    expect(document.activeElement).toBe(card());
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

  it.each(REBIND_CASES)(
    "rebinds $action to $code: old key inert, new key acts",
    ({ action, code, priorCode, run }) => {
      writeShortcutPreference(action, { common: [{ code: priorCode ?? code, modifiers: [] }] });
      const onAbort = vi.fn();
      const { onReject } = renderKeyboardForm(questionsOf(TWO_QUESTIONS), { onAbort });
      if (priorCode) {
        act(() => {
          writeShortcutPreference(action, { common: [{ code, modifiers: [] }] });
        });
      }
      focusCard();

      run({
        press: pressCard,
        progress,
        highlighted: highlightedIndex,
        composer: screen.getByTestId("composer"),
        onReject,
        onAbort,
      });
    },
  );

  it("keeps focus on the card root for a keyboard-activated Next", () => {
    renderKeyboardForm(questionsOf(TWO_QUESTIONS));
    focusCard();

    fireEvent.click(screen.getByTestId("ask-user-question-next"), { detail: 0 });

    expect(progress()).toContain("Question 2 of 2");
    expect(document.activeElement).toBe(card());
  });

  it("moves focus to the card root for a focused Prev button", () => {
    renderKeyboardForm(questionsOf(TWO_QUESTIONS));
    focusCard();
    pressCard({ key: "ArrowRight", code: "ArrowRight" });
    expect(progress()).toContain("Question 2 of 2");

    const prev = screen.getByTestId("ask-user-question-prev");
    act(() => {
      prev.focus();
      prev.click();
    });

    expect(progress()).toContain("Question 1 of 2");
    expect(document.activeElement).toBe(card());
  });

  it("returns focus to the composer for a keyboard-activated Submit", () => {
    const { onSubmit } = renderKeyboardForm(
      questionsOf([{ id: "q1", question: "Only?", options: [{ label: "A" }] }]),
    );
    focusCard();
    pressCard({ key: " ", code: "Space" });

    fireEvent.click(screen.getByTestId("ask-user-question-submit"), { detail: 0 });

    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect(document.activeElement).toBe(screen.getByTestId("composer"));
  });

  it("returns focus to the composer for a keyboard-activated Cancel", () => {
    const { onReject } = renderKeyboardForm(questionsOf(TWO_QUESTIONS));
    focusCard();

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }), { detail: 0 });

    expect(onReject).toHaveBeenCalledTimes(1);
    expect(document.activeElement).toBe(screen.getByTestId("composer"));
  });

  it("returns focus to the composer for a focused Cancel & interrupt", () => {
    const onAbort = vi.fn();
    const { onReject } = renderKeyboardForm(questionsOf(TWO_QUESTIONS), { onAbort });
    focusCard();

    const abort = screen.getByTestId("ask-user-question-abort");
    act(() => {
      abort.focus();
      abort.click();
    });

    expect(onAbort).toHaveBeenCalledTimes(1);
    expect(onReject).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(screen.getByTestId("composer"));
  });

  it("leaves focus alone for a mouse-click Submit", () => {
    const { onSubmit } = renderKeyboardForm(
      questionsOf([{ id: "q1", question: "Only?", options: [{ label: "A" }] }]),
    );
    focusCard();
    pressCard({ key: " ", code: "Space" });

    fireEvent.click(screen.getByTestId("ask-user-question-submit"), { detail: 1 });

    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect(document.activeElement).toBe(card());
    expect(document.activeElement).not.toBe(screen.getByTestId("composer"));
  });

  it("shows no keyboard-hint line in any focus state", () => {
    // Bind the two actions whose defaults are unbound, so every card action
    // would earn a hint segment.
    writeShortcutPreference("questionCardCancel", { common: [{ code: "KeyJ", modifiers: [] }] });
    writeShortcutPreference("questionCardCancelAndInterrupt", {
      common: [{ code: "KeyK", modifiers: [] }],
    });
    renderKeyboardForm(questionsOf(TWO_QUESTIONS), { onAbort: vi.fn() });

    // Mouse clicks (detail 1) leave focus alone, so both questions get an
    // unfocused baseline.
    const firstUnfocused = card().textContent ?? "";
    fireEvent.click(screen.getByTestId("ask-user-question-next"), { detail: 1 });
    const secondUnfocused = card().textContent ?? "";
    fireEvent.click(screen.getByTestId("ask-user-question-prev"), { detail: 1 });

    focusCard();
    pressCard({ key: "ArrowDown", code: "ArrowDown" });
    expect(card().textContent).toBe(firstUnfocused);
    pressCard({ key: "ArrowRight", code: "ArrowRight" });
    expect(card().textContent).toBe(secondUnfocused);

    const hintLiterals = ["Esc", "↵", "leave", "move", "next / submit", "cancel & interrupt"];
    const focused = card().textContent ?? "";
    for (const literal of hintLiterals) {
      expect(focused).not.toContain(literal);
    }

    // A live rebind while focused must not re-introduce the hint line.
    act(() => {
      writeShortcutPreference("questionCardLeave", { common: [{ code: "KeyG", modifiers: [] }] });
    });
    expect(card().textContent).toBe(secondUnfocused);
    for (const literal of hintLiterals) {
      expect(card().textContent).not.toContain(literal);
    }
  });
});

describe("AskUserQuestionForm — disabled Submit", () => {
  it("names the unanswered question that keeps Submit disabled", async () => {
    const [first, second] = questionsOf(TWO_QUESTIONS);
    renderKeyboardForm([{ ...first!, header: "First", isOther: false }, second!]);
    fireEvent.click(screen.getByTestId("ask-user-question-next"));
    expect(screen.getByRole("button", { name: "Submit" })).toBeDisabled();
    act(() => screen.getByRole("group", { name: "Submit" }).focus());
    await waitFor(() =>
      expect(screen.getByRole("tooltip")).toHaveTextContent("Answer question 1 (First) to submit"),
    );

    fireEvent.click(screen.getByTestId("ask-user-question-prev"));
    fireEvent.click(screen.getByLabelText(TWO_QUESTIONS[0]!.options![0]!.label));
    fireEvent.click(screen.getByTestId("ask-user-question-next"));
    expect(screen.getByRole("button", { name: "Submit" })).toBeEnabled();
    expect(screen.queryByRole("group", { name: "Submit" })).not.toBeInTheDocument();
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
  });
});

// Session rows the "@" menu lists from the sidebar; server title search is off.
const sessionList = vi.hoisted(() => ({ loaded: [] as unknown[] }));
const searchSessions = vi.hoisted(() => vi.fn(() => ({ data: undefined })));
vi.mock("@/hooks/useSidebarData", async (importOriginal) => {
  // Like the app's context, the same value until the loaded list changes.
  let cached: { list: unknown[]; value: object } | null = null;
  return {
    ...(await importOriginal<typeof UseSidebarDataModule>()),
    useOptionalLoadedConversations: () => {
      if (cached?.list !== sessionList.loaded) {
        cached = {
          list: sessionList.loaded,
          value: { pages: [{ data: sessionList.loaded }], pageParams: [undefined] },
        };
      }
      return cached.value;
    },
  };
});
vi.mock("@/hooks/useConversations", async (importOriginal) => ({
  ...(await importOriginal<typeof UseConversationsModule>()),
  useSessionSearch: searchSessions,
}));

const framework: ClaudeQuestion = {
  id: "framework",
  question: "Which framework?",
  header: "Framework",
  multiSelect: false,
  options: [{ label: "React" }, { label: "Vue" }],
};
const snacks: ClaudeQuestion = {
  id: "snacks",
  question: "Pick snacks",
  multiSelect: true,
  options: [{ label: "Popcorn" }, { label: "Pretzels" }],
};
const color: ClaudeQuestion = {
  id: "color",
  question: "Pick a color",
  multiSelect: false,
  options: [{ label: "Red" }, { label: "Blue" }],
};

// Each form gets its own elicitation, so no test restores another's unsent draft.
let elicitationCount = 0;
const nextElicitationId = () => `elic_${++elicitationCount}`;

function renderEditorForm(questions: ClaudeQuestion[], options: { codeMirror?: boolean } = {}) {
  const onSubmit = vi.fn();
  const onReject = vi.fn();
  const form = (
    <AskUserQuestionForm
      elicitationId={nextElicitationId()}
      questions={questions}
      onSubmit={onSubmit}
      onReject={onReject}
    />
  );
  render(
    options.codeMirror ? (
      <CapabilitiesProvider info={codeMirrorInfo}>{form}</CapabilitiesProvider>
    ) : (
      form
    ),
  );
  return { onSubmit, onReject };
}

const codeMirrorInfo = { ...FALLBACK_SERVER_INFO, features: { chat_composer_codemirror: true } };

function sessionRow(id: string, title: string) {
  return { id, object: "conversation", title, created_at: 0, updated_at: 0, labels: {} };
}

/** The custom answer's prompt editor, once its chunk has loaded. */
async function promptEditor() {
  await waitFor(() =>
    expect(document.querySelector('[data-composer-engine="codemirror"]')).not.toBeNull(),
  );
  return screen.getByTestId("ask-user-question-custom-input");
}

/** A native edit of a textarea: the value is set, then the input event carries its type. */
function nativeInput(field: HTMLTextAreaElement, value: string, inputType: string) {
  act(() => {
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(
      field,
      value,
    );
    field.dispatchEvent(new InputEvent("input", { bubbles: true, inputType }));
  });
}

/** Stubs `window.matchMedia` so the named media queries match. */
function stubMedia(matches: (query: string) => boolean) {
  const matchMedia = window.matchMedia;
  vi.spyOn(window, "matchMedia").mockImplementation((query) => ({
    ...matchMedia(query),
    matches: matches(query),
  }));
}

const submitFromEditor = (editor: HTMLElement) =>
  fireEvent.keyDown(editor, { key: "Enter", code: "Enter", ctrlKey: true });

describe("AskUserQuestionForm — prompt editor", () => {
  beforeEach(() => resetCodeMirrorComposerLoadForTests(async () => CodeMirrorModule));
  afterEach(() => {
    resetCodeMirrorComposerLoadForTests();
    sessionList.loaded = [];
    vi.restoreAllMocks();
  });

  it("answers in the prompt editor when the flag is on", async () => {
    renderEditorForm([framework], { codeMirror: true });
    expect(await promptEditor()).toHaveClass("cm-content");
  });

  it("keeps the textarea when the flag is off", () => {
    renderEditorForm([framework]);
    expect(screen.getByTestId("ask-user-question-custom-input").tagName).toBe("TEXTAREA");
    expect(document.querySelector("[data-composer-engine]")).toBeNull();
  });

  it("submits a typed answer from the editor", async () => {
    const { onSubmit } = renderEditorForm([framework], { codeMirror: true });
    const editor = await promptEditor();
    act(() => insertComposerText(composerTargetOf(editor)!, "Ramen"));
    // Typing checks the custom row, so the typed answer is the one sent.
    expect(screen.getByTestId("ask-user-question-custom-toggle")).toBeChecked();
    submitFromEditor(editor);
    expect(onSubmit).toHaveBeenCalledWith({ framework: "Ramen" });
  });

  it("advances on Enter from the editor while answers are missing", async () => {
    renderEditorForm([framework, snacks], { codeMirror: true });
    submitFromEditor(await promptEditor());
    expect(progress()).toBe("Question 2 of 2:");
    // Focus follows the question, as it does from the textarea.
    expect(screen.getByTestId("ask-user-question-form")).toHaveFocus();
  });

  it("leaves Shift+Enter in the editor to the editor, without advancing", async () => {
    renderEditorForm([framework, snacks], { codeMirror: true });
    fireEvent.keyDown(await promptEditor(), { key: "Enter", shiftKey: true });
    expect(progress()).toBe("Question 1 of 2:");
  });

  it("keeps the textarea on a narrow viewport even with the flag on", () => {
    stubMedia((query: string) => query.includes("max-width"));
    renderEditorForm([framework], { codeMirror: true });
    expect(screen.getByTestId("ask-user-question-custom-input").tagName).toBe("TEXTAREA");
    expect(document.querySelector("[data-composer-engine]")).toBeNull();
  });

  it("leaves digits typed in the editor to the editor", async () => {
    renderEditorForm([framework], { codeMirror: true });
    fireEvent.keyDown(await promptEditor(), { key: "2" });
    expect(screen.getByLabelText("Vue")).not.toBeChecked();
  });

  it("tags a session with @ and sends its marker and the tool hint", async () => {
    sessionList.loaded = [sessionRow("s_ci", "Fix CI")];
    const { onSubmit } = renderEditorForm([framework], { codeMirror: true });
    const editor = await promptEditor();
    act(() => insertComposerText(composerTargetOf(editor)!, "Use @Fix"));
    expect(screen.getByTitle("Tag session Fix CI")).toBeInTheDocument();
    // Enter attaches the highlighted row; the card shortcut sends the answer.
    fireEvent.keyDown(editor, { key: "Enter" });
    expect(composerTargetOf(editor)!.value).toBe("Use @Fix CI ");
    submitFromEditor(editor);
    expect(onSubmit).toHaveBeenCalledExactlyOnceWith({
      framework: `Use [Session: "Fix CI" id=s_ci subagent=no]\n\n${SESSION_MENTION_HINT}`,
    });
  });

  it("closes the @ menu on Escape before Escape leaves the text field", async () => {
    sessionList.loaded = [sessionRow("s_ci", "Fix CI")];
    const { onReject } = renderEditorForm([framework], { codeMirror: true });
    const editor = await promptEditor();
    act(() => insertComposerText(composerTargetOf(editor)!, "@Fi"));
    expect(screen.getByTestId("file-mention-menu")).toBeInTheDocument();
    fireEvent.keyDown(editor, { key: "Escape", code: "Escape" });
    expect(screen.queryByTestId("file-mention-menu")).toBeNull();
    expect(onReject).not.toHaveBeenCalled();
    fireEvent.keyDown(editor, { key: "Escape", code: "Escape" });
    expect(card()).toHaveFocus();
    expect(onReject).not.toHaveBeenCalled();
  });

  const sentAnswer = (title: string, id: string) =>
    `Base it on the fix from [Session: "${title}" id=${id} subagent=no]\n\n${SESSION_MENTION_HINT}`;

  it("shows a submitted answer's session as a pill, without the marker or hint", async () => {
    sessionList.loaded = [sessionRow("de02c3f0", "Fix flaky CI job")];
    const { container } = render(
      <ApprovalCard
        elicitationId="elic_persisted"
        message="Claude wants to call AskUserQuestion"
        phase="pre_tool_use"
        policyName="claude_native_permission"
        contentPreview=""
        requestedSchema={{}}
        status="responded"
        response={{
          action: "accept",
          content: { "Which framework?": sentAnswer("Fix flaky CI job", "de02c3f0") },
          _meta: { answerFallback: true },
        }}
        askUserQuestion={{ questions: [framework] }}
      />,
    );
    expect(screen.getByTestId("mention-pill")).toHaveTextContent("@Fix flaky CI job");
    expect(container.textContent).toContain("Base it on the fix from");
    expect(container.textContent).not.toContain("[Session:");
    expect(container.textContent).not.toContain("Tagged sessions");
  });

  it("shows the session pill once the card is answered from the editor", async () => {
    sessionList.loaded = [sessionRow("de02c3f0", "Fix flaky CI job")];
    const onSubmit = vi.fn();
    const liveCard = (response: { action: "accept"; content: Record<string, unknown> } | null) => (
      <CapabilitiesProvider info={codeMirrorInfo}>
        <ApprovalCard
          elicitationId="elic_live"
          message="Claude wants to call AskUserQuestion"
          phase="pre_tool_use"
          policyName="claude_native_permission"
          contentPreview=""
          requestedSchema={{}}
          status={response ? "responded" : "pending"}
          response={response === null ? null : { ...response, _meta: { answerFallback: true } }}
          askUserQuestion={{ questions: [framework] }}
          onSubmit={onSubmit}
        />
      </CapabilitiesProvider>
    );
    const { rerender } = render(liveCard(null));
    const editor = await promptEditor();
    act(() => insertComposerText(composerTargetOf(editor)!, "Base it on the fix from @Fix"));
    fireEvent.keyDown(editor, { key: "Enter" });
    submitFromEditor(editor);
    expect(onSubmit).toHaveBeenCalledTimes(1);
    const [, , content] = onSubmit.mock.calls[0]!;
    // The card owns the summary when the separate answer bubble could not be posted.
    rerender(liveCard({ action: "accept", content }));
    expect(screen.getByTestId("mention-pill")).toHaveTextContent("@Fix flaky CI job");
    expect(document.body.textContent).not.toContain("[Session:");
    expect(document.body.textContent).not.toContain("Tagged sessions");
  });

  /** Tags Fix CI in the editor, then deletes its token whole with Backspace. */
  async function tagThenDeleteToken() {
    const onSubmit = vi.fn();
    const elicitationId = nextElicitationId();
    render(
      <CapabilitiesProvider info={codeMirrorInfo}>
        <AskUserQuestionForm
          elicitationId={elicitationId}
          questions={[framework]}
          onSubmit={onSubmit}
          onReject={vi.fn()}
        />
      </CapabilitiesProvider>,
    );
    const editor = await promptEditor();
    act(() => insertComposerText(composerTargetOf(editor)!, "Use @Fix"));
    fireEvent.keyDown(editor, { key: "Enter" });
    expect(composerTargetOf(editor)!.value).toBe("Use @Fix CI ");
    act(() => composerTargetOf(editor)!.setSelectionRange(11, 11));
    fireEvent.keyDown(editor, { key: "Backspace" });
    expect(composerTargetOf(editor)!.value).toBe("Use  ");
    return { editor, onSubmit, elicitationId };
  }

  it("sends a deleted session token typed again as plain text", async () => {
    sessionList.loaded = [sessionRow("s_ci", "Fix CI")];
    const { editor, onSubmit } = await tagThenDeleteToken();
    act(() => insertComposerText(composerTargetOf(editor)!, "@Fix CI", 5, 5));
    const typed = composerTargetOf(editor)!.value;
    submitFromEditor(editor);
    expect(onSubmit).toHaveBeenCalledExactlyOnceWith({ framework: typed });
    expect(typed).toBe("Use  @Fix CI");
  });

  it("restores a deleted session token with undo", async () => {
    sessionList.loaded = [sessionRow("s_ci", "Fix CI")];
    const { editor, onSubmit } = await tagThenDeleteToken();
    act(() => void undo(EditorView.findFromDOM(editor)!));
    expect(composerTargetOf(editor)!.value).toBe("Use @Fix CI ");
    submitFromEditor(editor);
    expect(onSubmit).toHaveBeenCalledExactlyOnceWith({
      framework: `Use [Session: "Fix CI" id=s_ci subagent=no]\n\n${SESSION_MENTION_HINT}`,
    });
  });

  it("keeps a secret draft out of session search and local draft storage", async () => {
    sessionList.loaded = [sessionRow("s_ci", "Fix CI")];
    const secret: ClaudeQuestion = { ...color, id: "secret", isSecret: true };
    const elicitationId = nextElicitationId();
    const onReject = vi.fn();
    render(
      <CapabilitiesProvider info={codeMirrorInfo}>
        <AskUserQuestionForm
          elicitationId={elicitationId}
          questions={[framework, secret]}
          onSubmit={vi.fn()}
          onReject={onReject}
        />
      </CapabilitiesProvider>,
    );
    await promptEditor();
    expect(searchSessions).toHaveBeenCalled();
    searchSessions.mockClear();
    fireEvent.click(screen.getByTestId("ask-user-question-next"));
    const answer = screen.getByTestId("ask-user-question-custom-input");
    expect(answer.tagName).toBe("TEXTAREA");
    fireEvent.change(answer, { target: { value: "Use @Fix" } });
    expect(screen.queryByTestId("file-mention-menu")).toBeNull();
    expect(searchSessions).not.toHaveBeenCalled();
    const draft = getAskUserQuestionDraft(
      elicitationId,
      askUserQuestionSignature([framework, secret]),
    );
    expect(draft?.currentIndex).toBe(1);
    expect(draft?.customInputs.secret).toBe("");
    expect(draft?.sessionMentions).toEqual({});
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onReject).toHaveBeenCalledOnce();
    expect(searchSessions).not.toHaveBeenCalled();
  });

  it("can cancel a legacy card with duplicate secret and ordinary answer IDs", () => {
    searchSessions.mockClear();
    const { onReject, onSubmit } = renderEditorForm(
      [
        { ...framework, isSecret: true },
        { ...color, id: framework.id },
      ],
      { codeMirror: true },
    );
    expect(screen.getByRole("alert")).toHaveTextContent("duplicate answer IDs");
    expect(screen.queryByTestId("ask-user-question-custom-input")).toBeNull();
    expect(searchSessions).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onReject).toHaveBeenCalledOnce();
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("keeps the textarea until an IME composition ends", async () => {
    let finishLoad = () => {};
    const loaded = new Promise<typeof CodeMirrorModule>((resolve) => {
      finishLoad = () => resolve(CodeMirrorModule);
    });
    resetCodeMirrorComposerLoadForTests(() => loaded);
    renderEditorForm([framework], { codeMirror: true });
    const textarea = screen.getByTestId("ask-user-question-custom-input");
    fireEvent.compositionStart(textarea);

    await act(async () => {
      finishLoad();
      await loaded;
    });
    expect(textarea.tagName).toBe("TEXTAREA");
    expect(document.querySelector('[data-composer-engine="codemirror"]')).toBeNull();

    fireEvent.compositionEnd(textarea);
    await promptEditor();
  });

  it("keeps the caret and focus when the editor replaces the textarea", async () => {
    let finishLoad = () => {};
    const loaded = new Promise<typeof CodeMirrorModule>((resolve) => {
      finishLoad = () => resolve(CodeMirrorModule);
    });
    resetCodeMirrorComposerLoadForTests(() => loaded);
    renderEditorForm([framework], { codeMirror: true });
    const textarea = screen.getByTestId("ask-user-question-custom-input") as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: "Ramen soup" } });
    textarea.focus();
    textarea.setSelectionRange(0, 5);
    fireEvent.select(textarea);

    act(() => finishLoad());
    const editor = await promptEditor();
    expect(document.activeElement).toBe(editor);
    const target = composerTargetOf(editor)!;
    expect([target.selectionStart, target.selectionEnd]).toEqual([0, 5]);
    expect(target.value).toBe("Ramen soup");
  });

  it("keeps a tagged session through a narrow viewport and back", async () => {
    sessionList.loaded = [sessionRow("s_ci", "Fix CI")];
    // A live matchMedia: narrowing notifies the mobile query's listeners, as a resize does.
    let narrow = false;
    const listeners = new Set<() => void>();
    const matchMedia = window.matchMedia;
    vi.spyOn(window, "matchMedia").mockImplementation((query) => {
      const real = matchMedia(query);
      const mobile = query.includes("max-width");
      return {
        ...real,
        matches: mobile ? narrow : real.matches,
        addEventListener: (type: string, listener: () => void) =>
          mobile ? listeners.add(listener) : real.addEventListener(type, listener),
        removeEventListener: (type: string, listener: () => void) =>
          mobile ? listeners.delete(listener) : real.removeEventListener(type, listener),
      } as MediaQueryList;
    });
    const resize = (nowNarrow: boolean) => {
      narrow = nowNarrow;
      act(() => listeners.forEach((listener) => listener()));
    };
    const { onSubmit } = renderEditorForm([framework], { codeMirror: true });
    const editor = await promptEditor();
    act(() => insertComposerText(composerTargetOf(editor)!, "Use @Fix"));
    fireEvent.keyDown(editor, { key: "Enter" });
    expect(composerTargetOf(editor)!.value).toBe("Use @Fix CI ");

    resize(true);
    const textarea = await waitFor(() => {
      const field = screen.getByTestId("ask-user-question-custom-input");
      expect(field.tagName).toBe("TEXTAREA");
      return field as HTMLTextAreaElement;
    });
    expect(textarea.value).toBe("Use @Fix CI ");
    // Deleting part of the token releases it; the browser's undo brings it back.
    nativeInput(textarea, "Use @Fix ", "deleteContentBackward");
    nativeInput(textarea, "Use @Fix CI ", "historyUndo");

    resize(false);
    const restored = await promptEditor();
    expect(composerTargetOf(restored)!.value).toBe("Use @Fix CI ");
    submitFromEditor(restored);
    expect(onSubmit).toHaveBeenCalledExactlyOnceWith({
      framework: `Use [Session: "Fix CI" id=s_ci subagent=no]\n\n${SESSION_MENTION_HINT}`,
    });
  });

  it("keeps a session tag to the answer it was picked in", async () => {
    sessionList.loaded = [sessionRow("s_ci", "Fix CI")];
    const notes: ClaudeQuestion = {
      id: "notes",
      question: "Any notes?",
      multiSelect: false,
      options: [{ label: "None" }],
    };
    const { onSubmit } = renderEditorForm([framework, notes], { codeMirror: true });
    const first = await promptEditor();
    act(() => insertComposerText(composerTargetOf(first)!, "Use @Fix"));
    fireEvent.keyDown(first, { key: "Enter" });
    fireEvent.click(screen.getByTestId("ask-user-question-next"));
    // The second answer types the same words as plain text, so it stays literal.
    const second = await promptEditor();
    act(() => insertComposerText(composerTargetOf(second)!, "Ship @Fix CI"));
    fireEvent.click(screen.getByTestId("ask-user-question-submit"));
    expect(onSubmit).toHaveBeenCalledExactlyOnceWith({
      framework: `Use [Session: "Fix CI" id=s_ci subagent=no]\n\n${SESSION_MENTION_HINT}`,
      notes: "Ship @Fix CI",
    });
  });

  it("keeps undo within each answer: a restore in one question stays there", async () => {
    sessionList.loaded = [sessionRow("s_ci", "Fix CI")];
    const notes: ClaudeQuestion = {
      id: "notes",
      question: "Any notes?",
      multiSelect: false,
      options: [{ label: "None" }],
    };
    const { onSubmit } = renderEditorForm([framework, notes], { codeMirror: true });
    const first = await promptEditor();
    act(() => insertComposerText(composerTargetOf(first)!, "Use @Fix"));
    fireEvent.keyDown(first, { key: "Enter" });
    act(() => composerTargetOf(first)!.setSelectionRange(11, 11));
    fireEvent.keyDown(first, { key: "Backspace" });
    expect(composerTargetOf(first)!.value).toBe("Use  ");
    // Undo in the same answer brings the tag back, before any navigation.
    act(() => void undo(EditorView.findFromDOM(first)!));
    expect(composerTargetOf(first)!.value).toBe("Use @Fix CI ");

    fireEvent.click(screen.getByTestId("ask-user-question-next"));
    const second = await promptEditor();
    act(() => insertComposerText(composerTargetOf(second)!, "Ship"));
    // Undo in the second answer restores nothing from the first.
    act(() => void undo(EditorView.findFromDOM(second)!));
    expect(composerTargetOf(second)!.value).toBe("");

    fireEvent.click(screen.getByTestId("ask-user-question-prev"));
    expect(composerTargetOf(await promptEditor())!.value).toBe("Use @Fix CI ");
    fireEvent.click(screen.getByTestId("ask-user-question-next"));
    fireEvent.click(screen.getByLabelText("None"));
    fireEvent.click(screen.getByTestId("ask-user-question-submit"));
    expect(onSubmit).toHaveBeenCalledExactlyOnceWith({
      framework: `Use [Session: "Fix CI" id=s_ci subagent=no]\n\n${SESSION_MENTION_HINT}`,
      notes: "None",
    });
  });

  it("leaves the card's own session out of the @ menu", async () => {
    sessionList.loaded = [sessionRow("s_here", "This session"), sessionRow("s_ci", "Fix CI")];
    render(
      <CapabilitiesProvider info={codeMirrorInfo}>
        <ConversationScopeContext.Provider value="s_here">
          <AskUserQuestionForm
            elicitationId={nextElicitationId()}
            questions={[framework]}
            onSubmit={vi.fn()}
            onReject={vi.fn()}
          />
        </ConversationScopeContext.Provider>
      </CapabilitiesProvider>,
    );
    const editor = await promptEditor();
    act(() => insertComposerText(composerTargetOf(editor)!, "@This"));
    expect(screen.queryByTitle("Tag session This session")).toBeNull();
    act(() => composerTargetOf(editor)!.setSelectionRange(0, 0));
    act(() =>
      insertComposerText(composerTargetOf(editor)!, "", 0, composerTargetOf(editor)!.value.length),
    );
    act(() => insertComposerText(composerTargetOf(editor)!, "@Fix"));
    expect(screen.getByTitle("Tag session Fix CI")).toBeInTheDocument();
  });

  it("keeps a tagged session when the card remounts with its draft", async () => {
    sessionList.loaded = [sessionRow("s_ci", "Fix CI")];
    const elicitationId = nextElicitationId();
    const draftCard = (onSubmit: () => void) => (
      <CapabilitiesProvider info={codeMirrorInfo}>
        <AskUserQuestionForm
          elicitationId={elicitationId}
          questions={[framework]}
          onSubmit={onSubmit}
          onReject={vi.fn()}
        />
      </CapabilitiesProvider>
    );
    const first = render(draftCard(vi.fn()));
    const editor = await promptEditor();
    act(() => insertComposerText(composerTargetOf(editor)!, "Use @Fix"));
    fireEvent.keyDown(editor, { key: "Enter" });
    first.unmount();

    const onSubmit = vi.fn();
    render(draftCard(onSubmit));
    const restored = await promptEditor();
    expect(composerTargetOf(restored)!.value).toBe("Use @Fix CI ");
    submitFromEditor(restored);
    expect(onSubmit).toHaveBeenCalledExactlyOnceWith({
      framework: `Use [Session: "Fix CI" id=s_ci subagent=no]\n\n${SESSION_MENTION_HINT}`,
    });
  });
});
