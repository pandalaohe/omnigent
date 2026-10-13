import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { castAskUserQuestionPayload } from "@/lib/askUserQuestion";
import * as filesApi from "@/lib/filesApi";
import { clearQuestionCardInputTarget, insertIntoQuestionCard } from "@/lib/questionCardInput";
import { FileViewerContext } from "@/shell/FileViewerContext";
import { AskUserQuestionForm } from "./AskUserQuestionForm";

vi.mock("@/lib/filesApi", async (importOriginal) => ({
  ...(await importOriginal<typeof filesApi>()),
  uploadFile: vi.fn(),
}));

const fileViewer = {
  openFile: vi.fn(),
  openGithubTab: vi.fn(),
  isChangedPath: () => false,
  conversationId: undefined,
  workspaceRoot: "/opt/work/omnigent/fork/example",
  workspaceHome: "/opt/work/omnigent/fork/example",
};

beforeEach(() => vi.mocked(filesApi.uploadFile).mockReset());
afterEach(() => {
  cleanup();
  clearQuestionCardInputTarget("session-a");
});

function renderAsyncForm(
  specs: { id: string; question: string; options?: { label: string }[] }[],
  onReject = vi.fn(),
) {
  const payload = castAskUserQuestionPayload({ questions: specs });
  if (!payload) throw new Error("Expected valid question payload");
  const onSubmit = vi.fn().mockResolvedValue(true);
  render(
    <QueryClientProvider client={new QueryClient()}>
      <FileViewerContext.Provider value={fileViewer}>
        <AskUserQuestionForm
          questions={payload.questions}
          asyncTarget={{ sessionId: "session-a", cardId: "card-a" }}
          onSubmit={onSubmit}
          onReject={onReject}
        />
      </FileViewerContext.Provider>
    </QueryClientProvider>,
  );
  return onSubmit;
}

describe("async question answer images", () => {
  it("uploads an image-only answer, preserving its question key", async () => {
    vi.mocked(filesApi.uploadFile).mockResolvedValue({ id: "image-a" } as Awaited<
      ReturnType<typeof filesApi.uploadFile>
    >);
    const onSubmit = renderAsyncForm([{ id: "q1", question: "Show the problem" }]);
    const image = new File(["png"], "problem.png", { type: "image/png" });
    fireEvent.change(screen.getByLabelText("Choose answer images"), { target: { files: [image] } });
    expect(screen.getByRole("button", { name: "Remove problem.png" })).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("ask-user-question-submit"));

    await waitFor(() =>
      expect(onSubmit).toHaveBeenCalledWith(
        { q1: "" },
        { omnigent_question_images: { q1: ["image-a"] } },
      ),
    );
    expect(filesApi.uploadFile).toHaveBeenCalledWith("session-a", image);
  });

  it("lets the user remove a pasted image before sending", async () => {
    const onSubmit = renderAsyncForm([{ id: "q1", question: "Explain" }]);
    const input = screen.getByTestId("ask-user-question-custom-input");
    const image = new File(["png"], "paste.png", { type: "image/png" });
    fireEvent.paste(input, {
      clipboardData: { items: [{ kind: "file", getAsFile: () => image }] },
    });
    expect(screen.getByRole("button", { name: "Remove paste.png" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Remove paste.png" }));
    expect(screen.queryByRole("button", { name: "Remove paste.png" })).not.toBeInTheDocument();
    fireEvent.change(input, { target: { value: "Text instead" } });
    fireEvent.click(screen.getByTestId("ask-user-question-submit"));
    await waitFor(() => expect(onSubmit).toHaveBeenCalledWith({ q1: "Text instead" }, undefined));
    expect(filesApi.uploadFile).not.toHaveBeenCalled();
  });

  it("keeps each question's text and image separate and retains drafts after a failed upload", async () => {
    vi.mocked(filesApi.uploadFile)
      .mockRejectedValueOnce(new Error("Upload failed"))
      .mockResolvedValueOnce({ id: "image-b" } as Awaited<ReturnType<typeof filesApi.uploadFile>>);
    const onSubmit = renderAsyncForm([
      { id: "q1", question: "First" },
      { id: "q2", question: "Second" },
    ]);
    const first = screen.getByTestId("ask-user-question-custom-input");
    fireEvent.focus(first);
    act(() => expect(insertIntoQuestionCard("session-a", "> first quote")).toBe("inserted"));
    expect(first).toHaveValue("> first quote");
    fireEvent.click(screen.getByTestId("ask-user-question-next"));
    expect(insertIntoQuestionCard("session-a", "stale question")).toBe("absent");
    const second = screen.getByTestId("ask-user-question-custom-input");
    fireEvent.focus(second);
    const image = new File(["png"], "second.png", { type: "image/png" });
    fireEvent.change(screen.getByLabelText("Choose answer images"), { target: { files: [image] } });
    fireEvent.click(screen.getByTestId("ask-user-question-submit"));
    expect(await screen.findByRole("alert")).toHaveTextContent("Upload failed");
    expect(second).toHaveValue("");
    expect(screen.getByRole("button", { name: "Remove second.png" })).toBeInTheDocument();
    expect(onSubmit).not.toHaveBeenCalled();

    fireEvent.click(screen.getByTestId("ask-user-question-submit"));
    await waitFor(() =>
      expect(onSubmit).toHaveBeenCalledWith(
        { q1: "> first quote", q2: "" },
        { omnigent_question_images: { q2: ["image-b"] } },
      ),
    );
  });

  it("freezes edits, deletion, navigation, cancellation and injection during upload", async () => {
    let finishUpload: ((value: Awaited<ReturnType<typeof filesApi.uploadFile>>) => void) | null =
      null;
    vi.mocked(filesApi.uploadFile).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finishUpload = resolve;
        }),
    );
    const onReject = vi.fn();
    const onSubmit = renderAsyncForm(
      [
        { id: "q1", question: "First" },
        { id: "q2", question: "Second" },
      ],
      onReject,
    );
    fireEvent.change(screen.getByTestId("ask-user-question-custom-input"), {
      target: { value: "original answer" },
    });
    fireEvent.click(screen.getByTestId("ask-user-question-next"));
    fireEvent.focus(screen.getByTestId("ask-user-question-custom-input"));
    const image = new File(["png"], "pending.png", { type: "image/png" });
    fireEvent.change(screen.getByLabelText("Choose answer images"), { target: { files: [image] } });
    fireEvent.click(screen.getByTestId("ask-user-question-submit"));
    await waitFor(() => expect(finishUpload).not.toBeNull());

    const second = screen.getByTestId("ask-user-question-custom-input");
    expect(second).toBeDisabled();
    expect(screen.getByRole("button", { name: "Remove pending.png" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
    expect(screen.getByTestId("ask-user-question-prev")).toBeDisabled();
    fireEvent.change(second, { target: { value: "changed after submit" } });
    fireEvent.click(screen.getByRole("button", { name: "Remove pending.png" }));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(insertIntoQuestionCard("session-a", "late quote")).toBe("rejected");
    expect(onReject).not.toHaveBeenCalled();
    expect(onSubmit).not.toHaveBeenCalled();

    await act(async () => {
      finishUpload!({ id: "image-pending" } as Awaited<ReturnType<typeof filesApi.uploadFile>>);
    });
    await waitFor(() =>
      expect(onSubmit).toHaveBeenCalledWith(
        { q1: "original answer", q2: "" },
        { omnigent_question_images: { q2: ["image-pending"] } },
      ),
    );
  });

  it("omits comment ids after its staged screenshot is removed", async () => {
    const onSubmit = renderAsyncForm([{ id: "q1", question: "Review" }]);
    const field = screen.getByTestId("ask-user-question-custom-input");
    fireEvent.focus(field);
    const screenshot = new File(["png"], "comment.png", { type: "image/png" });
    act(() =>
      expect(
        insertIntoQuestionCard(
          "session-a",
          "Please fix the marked line.",
          [screenshot],
          ["comment-a"],
        ),
      ).toBe("inserted"),
    );
    fireEvent.click(screen.getByRole("button", { name: "Remove comment.png" }));
    fireEvent.click(screen.getByTestId("ask-user-question-submit"));
    await waitFor(() =>
      expect(onSubmit).toHaveBeenCalledWith({ q1: "Please fix the marked line." }, undefined),
    );
  });

  it("omits comment ids when the staged text is replaced or its custom answer is deselected", async () => {
    const onSubmit = renderAsyncForm([
      { id: "q1", question: "Review", options: [{ label: "Skip" }] },
    ]);
    const field = screen.getByTestId("ask-user-question-custom-input");
    fireEvent.focus(field);
    act(() =>
      expect(
        insertIntoQuestionCard("session-a", "Please fix the marked line.", [], ["comment-a"]),
      ).toBe("inserted"),
    );
    fireEvent.change(field, { target: { value: "Different request" } });
    fireEvent.click(screen.getByTestId("ask-user-question-submit"));
    await waitFor(() =>
      expect(onSubmit).toHaveBeenCalledWith({ q1: "Different request" }, undefined),
    );

    onSubmit.mockClear();
    fireEvent.change(field, { target: { value: "Please fix the marked line." } });
    fireEvent.click(screen.getByRole("radio", { name: "Skip" }));
    fireEvent.click(screen.getByTestId("ask-user-question-submit"));
    await waitFor(() => expect(onSubmit).toHaveBeenCalledWith({ q1: "Skip" }, undefined));
  });
});
