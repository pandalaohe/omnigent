// Async-card behaviour of the AskUserQuestion form: the free markdown
// context renders through the chat markdown stack (links clickable), and
// a question with no options falls back to its free-text row instead of
// crashing on `options.map`.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { type ClaudeQuestion, castAskUserQuestionPayload } from "@/lib/askUserQuestion";
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

    expect(screen.getByTestId("ask-user-question-context")).toBeDefined();
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
