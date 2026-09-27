// Tests for the archived-session comments banner: copy the formatted prompt
// without marking anything, or continue in a new session and mark the carried
// comments only after the agent actually received the message.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Comment } from "@/hooks/useComments";
import { ArchivedCommentsBanner } from "./ArchivedCommentsBanner";

const mocks = vi.hoisted(() => ({
  send: vi.fn(),
  continueSession: vi.fn(),
  navigate: vi.fn(),
  copyText: vi.fn(),
  toastError: vi.fn(),
  fetch: vi.fn(),
}));

vi.mock("@/store/chatStore", () => ({
  useChatStore: { getState: () => ({ send: mocks.send }) },
}));
vi.mock("@/lib/sessionsApi", () => ({ continueArchivedSession: mocks.continueSession }));
vi.mock("@/lib/routing", () => ({ useNavigate: () => mocks.navigate }));
vi.mock("@/lib/clipboard", () => ({ copyText: mocks.copyText }));
vi.mock("@/lib/identity", () => ({ authenticatedFetch: mocks.fetch }));
vi.mock("sonner", () => ({ toast: { error: mocks.toastError } }));

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function makeComment(
  id: string,
  status: Comment["status"],
  createdBy: string | null = null,
): Comment {
  return {
    id,
    conversation_id: "conv_arch",
    path: "report.html",
    start_index: 0,
    end_index: 5,
    body: "note",
    status,
    created_at: 0,
    updated_at: 0,
    anchor_content: null,
    created_by: createdBy,
  };
}

const COMMENTS = [
  makeComment("c1", "draft", "visitor:Alice"),
  makeComment("c2", "draft", null),
  makeComment("c3", "addressed"),
];

function renderBanner(comments: Comment[] = COMMENTS) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const invalidateSpy = vi.spyOn(client, "invalidateQueries");
  const view = render(
    <QueryClientProvider client={client}>
      <ArchivedCommentsBanner
        sessionId="conv_arch"
        title="Old work"
        directory="/repo"
        agentId="ag_1"
        comments={comments}
      />
    </QueryClientProvider>,
  );
  return { invalidateSpy, view };
}

/** Route the /send POST and the addressed PATCHes for a delivered continue. */
function mockSendAndPatch(sentCommentIds: string[] = ["c1", "c2"]): void {
  mocks.fetch.mockImplementation((url: string, init?: RequestInit) => {
    if (String(url).endsWith("/comments/send")) {
      return Promise.resolve(
        jsonResponse({
          formatted_message: "Please address these.",
          sent_comment_ids: sentCommentIds,
        }),
      );
    }
    if (init?.method === "PATCH") return Promise.resolve(jsonResponse({}));
    return Promise.resolve(jsonResponse({}, 500));
  });
}

beforeEach(() => {
  mocks.send.mockReset();
  mocks.continueSession.mockReset();
  mocks.navigate.mockReset();
  mocks.copyText.mockReset();
  mocks.toastError.mockReset();
  mocks.fetch.mockReset();
  mocks.copyText.mockResolvedValue(undefined);
});

afterEach(cleanup);

describe("ArchivedCommentsBanner", () => {
  it("renders nothing when every comment is addressed", () => {
    const { view } = renderBanner([makeComment("c1", "addressed")]);
    expect(view.container).toBeEmptyDOMElement();
  });

  it("copies the formatted prompt without marking anything", async () => {
    mockSendAndPatch();
    renderBanner();

    fireEvent.click(screen.getByRole("button", { name: "Copy as prompt" }));

    await waitFor(() => expect(mocks.copyText).toHaveBeenCalled());
    const [url, init] = mocks.fetch.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/sessions/conv_arch/comments/send");
    expect(JSON.parse(init.body as string)).toEqual({
      comment_ids: ["c1", "c2"],
      mark_addressed: false,
    });
    expect(mocks.copyText).toHaveBeenCalledWith(
      'Continuing from archived session "Old work" (conv_arch) in /repo. Please address these.',
    );
    // Copying is not sending: nothing may be marked or dispatched.
    expect(
      mocks.fetch.mock.calls.filter(
        ([, opts]) => (opts as RequestInit | undefined)?.method === "PATCH",
      ),
    ).toEqual([]);
    expect(mocks.send).not.toHaveBeenCalled();
    expect(mocks.continueSession).not.toHaveBeenCalled();
  });

  it("continues in a new session and marks the comments after delivery", async () => {
    mockSendAndPatch();
    mocks.continueSession.mockResolvedValue({ id: "conv_new" });
    mocks.send.mockResolvedValue(true);
    const { invalidateSpy } = renderBanner();

    fireEvent.click(screen.getByRole("button", { name: "Continue in a new session" }));

    await waitFor(() => expect(mocks.send).toHaveBeenCalled());
    expect(mocks.continueSession).toHaveBeenCalledWith("conv_arch");
    expect(mocks.navigate).toHaveBeenCalledWith("/c/conv_new");
    // The prompt is delivered into (and pinned to) the new session.
    expect(mocks.send).toHaveBeenCalledWith(
      'Continuing from archived session "Old work" (conv_arch) in /repo. Please address these.',
      "ag_1",
      [],
      { pinnedConversationId: "conv_new" },
    );
    await waitFor(() => {
      const patches = mocks.fetch.mock.calls.filter(
        ([, opts]) => (opts as RequestInit | undefined)?.method === "PATCH",
      );
      expect(patches.map(([url]) => String(url))).toEqual([
        "/v1/sessions/conv_arch/comments/c1",
        "/v1/sessions/conv_arch/comments/c2",
      ]);
    });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["comments", "conv_arch"] });
  });

  it("marks nothing when the continuation message was not delivered", async () => {
    mockSendAndPatch();
    mocks.continueSession.mockResolvedValue({ id: "conv_new" });
    mocks.send.mockResolvedValue(false);
    const { invalidateSpy } = renderBanner();

    fireEvent.click(screen.getByRole("button", { name: "Continue in a new session" }));

    await waitFor(() => expect(mocks.send).toHaveBeenCalled());
    // The send failed, so the comments stay unhandled — the prompt is
    // restored to the new session's composer by the store.
    await waitFor(() =>
      expect(mocks.toastError).toHaveBeenCalledWith(
        "The message didn't reach the agent; the comments stay unhandled.",
      ),
    );
    expect(
      mocks.fetch.mock.calls.filter(
        ([, opts]) => (opts as RequestInit | undefined)?.method === "PATCH",
      ),
    ).toEqual([]);
    expect(invalidateSpy).not.toHaveBeenCalled();
  });

  it("surfaces a continue failure and marks nothing", async () => {
    mockSendAndPatch();
    mocks.continueSession.mockRejectedValue(new Error("host offline"));
    const { invalidateSpy } = renderBanner();

    fireEvent.click(screen.getByRole("button", { name: "Continue in a new session" }));

    await waitFor(() => expect(mocks.toastError).toHaveBeenCalled());
    expect(mocks.send).not.toHaveBeenCalled();
    expect(
      mocks.fetch.mock.calls.filter(
        ([, opts]) => (opts as RequestInit | undefined)?.method === "PATCH",
      ),
    ).toEqual([]);
    expect(invalidateSpy).not.toHaveBeenCalled();
  });
});
