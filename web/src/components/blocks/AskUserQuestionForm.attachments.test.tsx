import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { castAskUserQuestionPayload } from "@/lib/askUserQuestion";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import type { AttachmentLimits } from "@/lib/attachments";
import type { ServerInfo } from "@/lib/capabilities";
import * as filesApi from "@/lib/filesApi";
import {
  captureQuestionCardInputTarget,
  clearQuestionCardInputTarget,
  insertIntoQuestionCard,
} from "@/lib/questionCardInput";
import { FileViewerContext } from "@/shell/FileViewerContext";
import { AskUserQuestionForm } from "./AskUserQuestionForm";
import { ApprovalCard } from "./ApprovalCard";

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

beforeEach(() => {
  vi.mocked(filesApi.uploadFile).mockReset();
});
afterEach(() => {
  cleanup();
  clearQuestionCardInputTarget("session-a");
});

function renderAsyncForm(
  specs: { id: string; question: string; options?: { label: string }[] }[],
  onReject = vi.fn(),
  limits?: AttachmentLimits,
) {
  const payload = castAskUserQuestionPayload({ questions: specs });
  if (!payload) throw new Error("Expected valid question payload");
  const onSubmit = vi.fn().mockResolvedValue(true);
  render(
    <QueryClientProvider client={new QueryClient()}>
      <CapabilitiesProvider
        info={limits ? ({ attachment_limits: limits } as ServerInfo) : "loading"}
      >
        <FileViewerContext.Provider value={fileViewer}>
          <AskUserQuestionForm
            questions={payload.questions}
            asyncTarget={{ sessionId: "session-a", cardId: "card-a" }}
            onSubmit={onSubmit}
            onReject={onReject}
          />
        </FileViewerContext.Provider>
      </CapabilitiesProvider>
    </QueryClientProvider>,
  );
  return onSubmit;
}

describe("async question answer images", () => {
  it("accepts a GIF with blank browser MIME as an answer image", () => {
    renderAsyncForm([{ id: "q1", question: "Show the animation" }], vi.fn(), {
      image_bytes: 1,
      upload_bytes: 100,
    });
    const gif = new File(["GIF89a"], "moving.gif");
    fireEvent.change(screen.getByLabelText("Choose answer images"), {
      target: { files: [gif] },
    });
    expect(screen.getByRole("button", { name: "Remove moving.gif" })).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("rejects an image over the server's configured cap", () => {
    renderAsyncForm([{ id: "q1", question: "Show the problem" }], vi.fn(), {
      image_bytes: 10,
    });
    const image = new File(["sixteen bytes!!!"], "large.png", { type: "image/png" });
    fireEvent.change(screen.getByLabelText("Choose answer images"), { target: { files: [image] } });
    expect(screen.getByRole("alert")).toHaveTextContent("large.png");
    expect(screen.queryByRole("button", { name: "Remove large.png" })).not.toBeInTheDocument();
  });

  it("accepts a configured image cap beyond the older client fallback", () => {
    renderAsyncForm([{ id: "q1", question: "Show the problem" }], vi.fn(), {
      image_bytes: 64 * 1024 * 1024,
    });
    const image = new File(["png"], "large.png", { type: "image/png" });
    Object.defineProperty(image, "size", { value: 51 * 1024 * 1024 });
    fireEvent.change(screen.getByLabelText("Choose answer images"), { target: { files: [image] } });
    expect(screen.getByRole("button", { name: "Remove large.png" })).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("uploads an image-only answer, preserving its question key", async () => {
    vi.mocked(filesApi.uploadFile).mockResolvedValue({ id: "image-a" } as Awaited<
      ReturnType<typeof filesApi.uploadFile>
    >);
    const onSubmit = renderAsyncForm([{ id: "q1", question: "Show the problem" }]);
    const image = new File(["png"], "problem.png", { type: "image/png" });
    fireEvent.change(screen.getByLabelText("Choose answer images"), { target: { files: [image] } });
    expect(screen.getByTestId("ask-user-question-custom-input")).toHaveValue("[image 1] ");
    expect(screen.getByRole("button", { name: "Remove problem.png" })).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("ask-user-question-submit"));

    await waitFor(() =>
      expect(onSubmit).toHaveBeenCalledWith(
        { q1: "[image 1]" },
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
    expect(input).toHaveValue("[image 1] ");
    expect(screen.getByRole("button", { name: "Remove paste.png" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Remove paste.png" }));
    expect(screen.queryByRole("button", { name: "Remove paste.png" })).not.toBeInTheDocument();
    expect(input).toHaveValue("");
    fireEvent.change(input, { target: { value: "Text instead" } });
    fireEvent.click(screen.getByTestId("ask-user-question-submit"));
    await waitFor(() => expect(onSubmit).toHaveBeenCalledWith({ q1: "Text instead" }, undefined));
    expect(filesApi.uploadFile).not.toHaveBeenCalled();
  });

  it("inserts pasted and picked image tokens at the saved selection", async () => {
    const onSubmit = renderAsyncForm([{ id: "q1", question: "Explain" }]);
    const input = screen.getByTestId("ask-user-question-custom-input") as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: "before after" } });
    act(() => input.setSelectionRange(7, 7));
    fireEvent.select(input);
    const pasted = new File(["a"], "pasted.png", { type: "image/png" });
    fireEvent.paste(input, {
      clipboardData: { items: [{ kind: "file", getAsFile: () => pasted }] },
    });
    expect(input).toHaveValue("before [image 1] after");
    expect(input.selectionStart).toBe("before [image 1] ".length);

    act(() => input.setSelectionRange(0, 7));
    fireEvent.select(input);
    const picked = new File(["b"], "picked.png", { type: "image/png" });
    fireEvent.change(screen.getByLabelText("Choose answer images"), {
      target: { files: [picked] },
    });
    expect(input).toHaveValue("[image 2] [image 1] after");
    vi.mocked(filesApi.uploadFile).mockImplementation(
      async (_session, file) =>
        ({
          id: file === pasted ? "pasted-id" : "picked-id",
        }) as Awaited<ReturnType<typeof filesApi.uploadFile>>,
    );
    fireEvent.click(screen.getByTestId("ask-user-question-submit"));
    await waitFor(() =>
      expect(onSubmit).toHaveBeenCalledWith(
        { q1: "[image 2] [image 1] after" },
        { omnigent_question_images: { q1: ["pasted-id", "picked-id"] } },
      ),
    );
  });

  it("removes a live token as a unit while keeping its image attached", async () => {
    const onSubmit = renderAsyncForm([{ id: "q1", question: "Explain" }]);
    const input = screen.getByTestId("ask-user-question-custom-input") as HTMLTextAreaElement;
    const image = new File(["a"], "kept.png", { type: "image/png" });
    fireEvent.change(screen.getByLabelText("Choose answer images"), { target: { files: [image] } });
    input.setSelectionRange(input.value.length, input.value.length);
    expect(fireEvent.keyDown(input, { key: "Backspace" })).toBe(false);
    expect(input).toHaveValue("");
    expect(screen.getByRole("button", { name: "Remove kept.png" })).toBeInTheDocument();
    vi.mocked(filesApi.uploadFile).mockResolvedValue({ id: "kept-id" } as Awaited<
      ReturnType<typeof filesApi.uploadFile>
    >);
    fireEvent.click(screen.getByTestId("ask-user-question-submit"));
    await waitFor(() =>
      expect(onSubmit).toHaveBeenCalledWith(
        { q1: "" },
        { omnigent_question_images: { q1: ["kept-id"] } },
      ),
    );
  });

  it("compacts numbers after removing a thumbnail and keeps later additions distinct", async () => {
    const onSubmit = renderAsyncForm([{ id: "q1", question: "Explain" }]);
    const input = screen.getByTestId("ask-user-question-custom-input") as HTMLTextAreaElement;
    const first = new File(["a"], "first.png", { type: "image/png" });
    const second = new File(["b"], "second.png", { type: "image/png" });
    const third = new File(["c"], "third.png", { type: "image/png" });
    fireEvent.change(screen.getByLabelText("Choose answer images"), {
      target: { files: [first, second] },
    });
    expect(input).toHaveValue("[image 1] [image 2] ");
    fireEvent.click(screen.getByRole("button", { name: "Remove first.png" }));
    expect(input).toHaveValue("[image 1] ");
    fireEvent.change(screen.getByLabelText("Choose answer images"), { target: { files: [third] } });
    expect(input).toHaveValue("[image 1] [image 2] ");
    vi.mocked(filesApi.uploadFile).mockImplementation(
      async (_session, file) =>
        ({
          id: file === second ? "second-id" : "third-id",
        }) as Awaited<ReturnType<typeof filesApi.uploadFile>>,
    );
    fireEvent.click(screen.getByTestId("ask-user-question-submit"));
    await waitFor(() =>
      expect(onSubmit).toHaveBeenCalledWith(
        { q1: "[image 1] [image 2]" },
        { omnigent_question_images: { q1: ["second-id", "third-id"] } },
      ),
    );
  });

  it("leaves unknown literal markers alone and sends duplicate live markers with one image", async () => {
    const onSubmit = renderAsyncForm([{ id: "q1", question: "Explain" }]);
    const input = screen.getByTestId("ask-user-question-custom-input");
    fireEvent.change(input, { target: { value: "literal [image 9]" } });
    const image = new File(["a"], "one.png", { type: "image/png" });
    fireEvent.change(screen.getByLabelText("Choose answer images"), { target: { files: [image] } });
    fireEvent.change(input, { target: { value: "literal [image 9] [image 1] [image 1]" } });
    vi.mocked(filesApi.uploadFile).mockResolvedValue({ id: "one-id" } as Awaited<
      ReturnType<typeof filesApi.uploadFile>
    >);
    fireEvent.click(screen.getByTestId("ask-user-question-submit"));
    await waitFor(() =>
      expect(onSubmit).toHaveBeenCalledWith(
        { q1: "literal [image 9] [image 1] [image 1]" },
        { omnigent_question_images: { q1: ["one-id"] } },
      ),
    );
  });

  it("targets only a selected custom answer and follows the shown question", () => {
    renderAsyncForm([
      { id: "q1", question: "First", options: [{ label: "Skip" }] },
      { id: "q2", question: "Second", options: [{ label: "Skip" }] },
    ]);
    expect(insertIntoQuestionCard("session-a", "before selection")).toBe("absent");
    fireEvent.click(screen.getByTestId("ask-user-question-custom-toggle"));
    act(() => expect(insertIntoQuestionCard("session-a", "first answer")).toBe("inserted"));
    expect(screen.getByTestId("ask-user-question-custom-input")).toHaveValue("first answer");
    fireEvent.click(screen.getByRole("radio", { name: "Skip" }));
    expect(insertIntoQuestionCard("session-a", "after switch")).toBe("absent");
    fireEvent.click(screen.getByTestId("ask-user-question-custom-toggle"));
    fireEvent.click(screen.getByTestId("ask-user-question-next"));
    expect(insertIntoQuestionCard("session-a", "second not selected")).toBe("absent");
    fireEvent.click(screen.getByTestId("ask-user-question-custom-toggle"));
    act(() => expect(insertIntoQuestionCard("session-a", "second answer")).toBe("inserted"));
    expect(screen.getByTestId("ask-user-question-custom-input")).toHaveValue("second answer");
  });

  it("keeps the caret after repeated routed insertions", () => {
    renderAsyncForm([{ id: "q1", question: "Explain" }]);
    fireEvent.click(screen.getByTestId("ask-user-question-custom-toggle"));
    act(() => expect(insertIntoQuestionCard("session-a", "first")).toBe("inserted"));
    act(() => expect(insertIntoQuestionCard("session-a", "second")).toBe("inserted"));
    expect(screen.getByTestId("ask-user-question-custom-input")).toHaveValue("first\n\nsecond");
  });

  it("detaches a multi-select custom target when the custom option is unchecked", () => {
    const payload = castAskUserQuestionPayload({
      questions: [{ id: "q1", question: "Pick", options: [{ label: "A" }], multiSelect: true }],
    });
    if (!payload) throw new Error("Expected valid question payload");
    render(
      <QueryClientProvider client={new QueryClient()}>
        <FileViewerContext.Provider value={fileViewer}>
          <AskUserQuestionForm
            questions={payload.questions}
            asyncTarget={{ sessionId: "session-a", cardId: "card-a" }}
            onSubmit={vi.fn()}
            onReject={vi.fn()}
          />
        </FileViewerContext.Provider>
      </QueryClientProvider>,
    );
    fireEvent.click(screen.getByTestId("ask-user-question-custom-toggle"));
    act(() => expect(insertIntoQuestionCard("session-a", "selected")).toBe("inserted"));
    fireEvent.click(screen.getByTestId("ask-user-question-custom-toggle"));
    expect(insertIntoQuestionCard("session-a", "unselected")).toBe("absent");
  });

  it("sends to the lower selected card even after the upper card receives focus", () => {
    const payload = castAskUserQuestionPayload({
      questions: [{ id: "q1", question: "Explain", options: [{ label: "Skip" }] }],
    });
    if (!payload) throw new Error("Expected valid question payload");
    render(
      <QueryClientProvider client={new QueryClient()}>
        <FileViewerContext.Provider value={fileViewer}>
          <AskUserQuestionForm
            questions={payload.questions}
            asyncTarget={{ sessionId: "session-a", cardId: "upper" }}
            onSubmit={vi.fn()}
            onReject={vi.fn()}
          />
          <AskUserQuestionForm
            questions={payload.questions}
            asyncTarget={{ sessionId: "session-a", cardId: "lower" }}
            onSubmit={vi.fn()}
            onReject={vi.fn()}
          />
        </FileViewerContext.Provider>
      </QueryClientProvider>,
    );
    const toggles = screen.getAllByTestId("ask-user-question-custom-toggle");
    fireEvent.click(toggles[1]);
    fireEvent.click(toggles[0]);
    const fields = screen.getAllByTestId("ask-user-question-custom-input");
    fireEvent.focus(fields[0]);
    act(() => expect(insertIntoQuestionCard("session-a", "targeted")).toBe("inserted"));
    expect(fields[0]).toHaveValue("");
    expect(fields[1]).toHaveValue("targeted");
  });

  it("clears a selected card target on cancellation without waiting for unmount", async () => {
    renderAsyncForm([{ id: "q1", question: "Explain" }]);
    fireEvent.click(screen.getByTestId("ask-user-question-custom-toggle"));
    expect(insertIntoQuestionCard("session-a", "selected")).toBe("inserted");
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(insertIntoQuestionCard("session-a", "resolved")).toBe("absent"));
  });

  it("rejects insertion while cancelling and restores the selected target when cancellation fails", async () => {
    let finishCancel!: (accepted: boolean) => void;
    const onReject = vi.fn(
      () =>
        new Promise<boolean>((resolve) => {
          finishCancel = resolve;
        }),
    );
    renderAsyncForm([{ id: "q1", question: "Explain" }], onReject);
    fireEvent.change(screen.getByTestId("ask-user-question-custom-input"), {
      target: { value: "draft" },
    });
    const captured = captureQuestionCardInputTarget("session-a");
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(insertIntoQuestionCard("session-a", "late", [], [], captured)).toBe("rejected");
    expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
    await act(async () => finishCancel(false));
    act(() =>
      expect(insertIntoQuestionCard("session-a", "after failed cancellation")).toBe("inserted"),
    );
    expect(screen.getByTestId("ask-user-question-custom-input")).toHaveValue(
      "draft\n\nafter failed cancellation",
    );
  });

  it("preserves the selected target when the approval consumer reports a failed decline", async () => {
    const submit = vi.fn().mockResolvedValue(false);
    render(
      <QueryClientProvider client={new QueryClient()}>
        <FileViewerContext.Provider value={fileViewer}>
          <ApprovalCard
            elicitationId="card-a"
            cardSessionId="session-a"
            asyncKind="question"
            message="Question"
            phase=""
            policyName=""
            contentPreview=""
            requestedSchema={{}}
            status="pending"
            response={null}
            askUserQuestion={{ questions: [{ id: "q1", question: "Explain" }] }}
            onSubmit={submit}
          />
        </FileViewerContext.Provider>
      </QueryClientProvider>,
    );
    fireEvent.click(screen.getByTestId("ask-user-question-custom-toggle"));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() =>
      expect(screen.getByText("Could not cancel the question. Try again.")).toBeInTheDocument(),
    );
    act(() => expect(insertIntoQuestionCard("session-a", "retry answer")).toBe("inserted"));
    expect(submit).toHaveBeenCalledWith("card-a", "decline");
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
    fireEvent.click(screen.getByTestId("ask-user-question-custom-toggle"));
    act(() => expect(insertIntoQuestionCard("session-a", "> first quote")).toBe("inserted"));
    expect(first).toHaveValue("> first quote");
    fireEvent.click(screen.getByTestId("ask-user-question-next"));
    expect(insertIntoQuestionCard("session-a", "stale question")).toBe("absent");
    const second = screen.getByTestId("ask-user-question-custom-input");
    const image = new File(["png"], "second.png", { type: "image/png" });
    fireEvent.change(screen.getByLabelText("Choose answer images"), { target: { files: [image] } });
    expect(second).toHaveValue("[image 1] ");
    fireEvent.click(screen.getByTestId("ask-user-question-submit"));
    expect(await screen.findByRole("alert")).toHaveTextContent("Upload failed");
    expect(second).toHaveValue("[image 1] ");
    expect(screen.getByRole("button", { name: "Remove second.png" })).toBeInTheDocument();
    expect(onSubmit).not.toHaveBeenCalled();

    fireEvent.click(screen.getByTestId("ask-user-question-submit"));
    await waitFor(() =>
      expect(onSubmit).toHaveBeenCalledWith(
        { q1: "> first quote", q2: "[image 1]" },
        { omnigent_question_images: { q2: ["image-b"] } },
      ),
    );
  });

  it("numbers delivered images across questions and preserves question order when uploads finish out of order", async () => {
    let finishFirst: ((value: Awaited<ReturnType<typeof filesApi.uploadFile>>) => void) | null =
      null;
    vi.mocked(filesApi.uploadFile).mockImplementation((_session, file) =>
      file?.name === "first.png"
        ? new Promise((resolve) => {
            finishFirst = resolve;
          })
        : Promise.resolve({ id: "second-id" } as Awaited<ReturnType<typeof filesApi.uploadFile>>),
    );
    const onSubmit = renderAsyncForm([
      { id: "q1", question: "First" },
      { id: "q2", question: "Second" },
    ]);
    const first = new File(["a"], "first.png", { type: "image/png" });
    const second = new File(["b"], "second.png", { type: "image/png" });
    fireEvent.change(screen.getByLabelText("Choose answer images"), { target: { files: [first] } });
    expect(screen.getByTestId("ask-user-question-custom-input")).toHaveValue("[image 1] ");
    fireEvent.click(screen.getByTestId("ask-user-question-next"));
    fireEvent.change(screen.getByLabelText("Choose answer images"), {
      target: { files: [second] },
    });
    expect(screen.getByTestId("ask-user-question-custom-input")).toHaveValue("[image 1] ");
    fireEvent.click(screen.getByTestId("ask-user-question-submit"));
    await waitFor(() => expect(finishFirst).not.toBeNull());
    await act(async () => {
      finishFirst!({ id: "first-id" } as Awaited<ReturnType<typeof filesApi.uploadFile>>);
    });
    expect(onSubmit).toHaveBeenCalledWith(
      { q1: "[image 1]", q2: "[image 2]" },
      { omnigent_question_images: { q1: ["first-id"], q2: ["second-id"] } },
    );
    expect(filesApi.uploadFile).toHaveBeenCalledWith("session-a", first);
    expect(filesApi.uploadFile).toHaveBeenCalledWith("session-a", second);
    expect(vi.mocked(filesApi.uploadFile).mock.calls).toHaveLength(2);
    const meta = onSubmit.mock.calls[0][1] as {
      omnigent_question_images: Record<string, string[]>;
    };
    expect(Object.keys(meta.omnigent_question_images)).toEqual(["q1", "q2"]);
  });

  it("matches the serialized image order when question ids are numeric strings", async () => {
    vi.mocked(filesApi.uploadFile)
      .mockResolvedValueOnce({ id: "first-id" } as Awaited<ReturnType<typeof filesApi.uploadFile>>)
      .mockResolvedValueOnce({ id: "second-id" } as Awaited<
        ReturnType<typeof filesApi.uploadFile>
      >);
    const onSubmit = renderAsyncForm([
      { id: "2", question: "First" },
      { id: "1", question: "Second" },
    ]);
    fireEvent.change(screen.getByLabelText("Choose answer images"), {
      target: { files: [new File(["a"], "first.png", { type: "image/png" })] },
    });
    fireEvent.click(screen.getByTestId("ask-user-question-next"));
    fireEvent.change(screen.getByLabelText("Choose answer images"), {
      target: { files: [new File(["b"], "second.png", { type: "image/png" })] },
    });
    fireEvent.click(screen.getByTestId("ask-user-question-submit"));
    await waitFor(() =>
      expect(onSubmit).toHaveBeenCalledWith(
        { "2": "[image 2]", "1": "[image 1]" },
        { omnigent_question_images: { "2": ["first-id"], "1": ["second-id"] } },
      ),
    );
    const meta = onSubmit.mock.calls[0][1] as {
      omnigent_question_images: Record<string, string[]>;
    };
    expect(Object.values(JSON.parse(JSON.stringify(meta.omnigent_question_images))).flat()).toEqual(
      ["second-id", "first-id"],
    );
  });

  it("renumbers delivery after an earlier question's images change following a failed upload", async () => {
    vi.mocked(filesApi.uploadFile).mockRejectedValueOnce(new Error("Upload failed"));
    vi.mocked(filesApi.uploadFile).mockImplementationOnce(
      async () => ({ id: "second-id" }) as Awaited<ReturnType<typeof filesApi.uploadFile>>,
    );
    const onSubmit = renderAsyncForm([
      { id: "q1", question: "First" },
      { id: "q2", question: "Second" },
    ]);
    fireEvent.change(screen.getByLabelText("Choose answer images"), {
      target: { files: [new File(["a"], "old.png", { type: "image/png" })] },
    });
    fireEvent.click(screen.getByTestId("ask-user-question-next"));
    fireEvent.change(screen.getByLabelText("Choose answer images"), {
      target: { files: [new File(["b"], "second.png", { type: "image/png" })] },
    });
    fireEvent.click(screen.getByTestId("ask-user-question-submit"));
    expect(await screen.findByRole("alert")).toHaveTextContent("Upload failed");
    expect(onSubmit).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId("ask-user-question-prev"));
    fireEvent.click(screen.getByRole("button", { name: "Remove old.png" }));
    fireEvent.change(screen.getByLabelText("Choose answer images"), {
      target: {
        files: [
          new File(["c"], "new.png", { type: "image/png" }),
          new File(["d"], "extra.png", { type: "image/png" }),
        ],
      },
    });
    vi.mocked(filesApi.uploadFile).mockImplementation(
      async (_session, file) =>
        ({ id: file!.name.replace(".png", "-id") }) as Awaited<
          ReturnType<typeof filesApi.uploadFile>
        >,
    );
    fireEvent.click(screen.getByTestId("ask-user-question-next"));
    expect(screen.getByTestId("ask-user-question-custom-input")).toHaveValue("[image 1] ");
    fireEvent.click(screen.getByTestId("ask-user-question-submit"));
    await waitFor(() =>
      expect(onSubmit).toHaveBeenCalledWith(
        { q1: "[image 1] [image 2]", q2: "[image 3]" },
        { omnigent_question_images: { q1: ["new-id", "extra-id"], q2: ["second-id"] } },
      ),
    );
  });

  it("gives a reused File its own label in each question", () => {
    renderAsyncForm([
      { id: "q1", question: "First" },
      { id: "q2", question: "Second" },
    ]);
    const first = new File(["a"], "first.png", { type: "image/png" });
    const shared = new File(["b"], "shared.png", { type: "image/png" });
    fireEvent.change(screen.getByLabelText("Choose answer images"), {
      target: { files: [first, shared] },
    });
    expect(screen.getByTestId("ask-user-question-custom-input")).toHaveValue(
      "[image 1] [image 2] ",
    );
    fireEvent.click(screen.getByTestId("ask-user-question-next"));
    fireEvent.change(screen.getByLabelText("Choose answer images"), {
      target: { files: [shared] },
    });
    expect(screen.getByTestId("ask-user-question-custom-input")).toHaveValue("[image 1] ");
    fireEvent.click(screen.getByTestId("ask-user-question-prev"));
    expect(screen.getByTestId("ask-user-question-custom-input")).toHaveValue(
      "[image 1] [image 2] ",
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
        { q1: "original answer", q2: "[image 1]" },
        { omnigent_question_images: { q2: ["image-pending"] } },
      ),
    );
  });

  it("omits comment ids after its staged screenshot is removed", async () => {
    const onSubmit = renderAsyncForm([{ id: "q1", question: "Review" }]);
    const field = screen.getByTestId("ask-user-question-custom-input");
    fireEvent.click(screen.getByTestId("ask-user-question-custom-toggle"));
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
    expect(field).toHaveValue("Please fix the marked line. [image 1] ");
    fireEvent.click(screen.getByRole("button", { name: "Remove comment.png" }));
    expect(field).toHaveValue("Please fix the marked line. ");
    fireEvent.click(screen.getByTestId("ask-user-question-submit"));
    await waitFor(() =>
      expect(onSubmit).toHaveBeenCalledWith({ q1: "Please fix the marked line." }, undefined),
    );
  });

  it("keeps a staged comment image marker and its comment association on submit", async () => {
    const onSubmit = renderAsyncForm([{ id: "q1", question: "Review" }]);
    fireEvent.click(screen.getByTestId("ask-user-question-custom-toggle"));
    const screenshot = new File(["png"], "marked.png", { type: "image/png" });
    act(() =>
      expect(
        insertIntoQuestionCard("session-a", "Review this line.", [screenshot], ["comment-a"]),
      ).toBe("inserted"),
    );
    expect(screen.getByTestId("ask-user-question-custom-input")).toHaveValue(
      "Review this line. [image 1] ",
    );
    vi.mocked(filesApi.uploadFile).mockResolvedValue({ id: "marked-id" } as Awaited<
      ReturnType<typeof filesApi.uploadFile>
    >);
    fireEvent.click(screen.getByTestId("ask-user-question-submit"));
    await waitFor(() =>
      expect(onSubmit).toHaveBeenCalledWith(
        { q1: "Review this line. [image 1]" },
        { omnigent_question_images: { q1: ["marked-id"] }, omnigent_comment_ids: ["comment-a"] },
      ),
    );
  });

  it("omits comment ids when the staged text is replaced or its custom answer is deselected", async () => {
    const onSubmit = renderAsyncForm([
      { id: "q1", question: "Review", options: [{ label: "Skip" }] },
    ]);
    const field = screen.getByTestId("ask-user-question-custom-input");
    fireEvent.click(screen.getByTestId("ask-user-question-custom-toggle"));
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
