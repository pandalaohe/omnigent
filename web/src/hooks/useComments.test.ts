// Unit tests for the comments API helpers and TanStack Query hooks:
// the URL/encoding/method contract of each request, the throw-on-non-2xx
// guard, the cache-invalidation fan-out on mutation success, and the
// send-to-agent dispatch that calls useChatStore.send().

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useChatStore } from "@/store/chatStore";
import {
  commentsQueryKey,
  fetchComments,
  useAddComment,
  useComments,
  useDeleteComment,
  useSendCommentsToAgent,
  useUpdateComment,
  type Comment,
} from "./useComments";

// The send-to-agent hook dispatches via the chat store on success; mock
// the store so we can assert the dispatch without standing up zustand.
vi.mock("@/store/chatStore", () => ({
  useChatStore: { getState: vi.fn() },
}));

function mockResponse(
  body: unknown,
  init?: { ok?: boolean; status?: number; headers?: Record<string, string> },
): Response {
  return {
    ok: init?.ok ?? true,
    status: init?.status ?? 200,
    statusText: "OK",
    headers: new Headers(init?.headers),
    json: async () => body,
    blob: async () => body,
  } as unknown as Response;
}

const fetchMock = vi.fn();
const sendMock = vi.fn();
const enqueueMock = vi.fn();
const chatState = {
  conversationId: "conv_1" as string | null,
  status: "idle" as "idle" | "streaming",
  sessionStatus: "idle",
  queuedMessages: [] as { conversationId: string }[],
  send: sendMock,
  enqueueMessage: enqueueMock,
};

beforeEach(() => {
  fetchMock.mockReset();
  sendMock.mockReset();
  sendMock.mockResolvedValue(true);
  enqueueMock.mockReset();
  chatState.conversationId = "conv_1";
  chatState.status = "idle";
  chatState.sessionStatus = "idle";
  chatState.queuedMessages = [];
  vi.mocked(useChatStore.getState).mockReturnValue(
    chatState as unknown as ReturnType<typeof useChatStore.getState>,
  );
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function wrapperWith(queryClient: QueryClient) {
  return ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client: queryClient }, children);
}

function makeComment(overrides: Partial<Comment> & { id: string; path: string }): Comment {
  return {
    conversation_id: "conv_1",
    start_index: 0,
    end_index: 1,
    body: "note",
    status: "draft",
    created_at: 0,
    updated_at: 0,
    anchor_content: null,
    created_by: null,
    ...overrides,
  };
}

describe("commentsQueryKey", () => {
  it("scopes the key by path only when a path is given", () => {
    // useCommentInbox shares this key; a path-less key must NOT collide
    // with the per-file key or the two caches would clobber each other.
    expect(commentsQueryKey("conv_1")).toEqual(["comments", "conv_1"]);
    expect(commentsQueryKey("conv_1", "a.ts")).toEqual(["comments", "conv_1", "a.ts"]);
  });
});

describe("fetchComments", () => {
  it("GETs the session list endpoint with include_visitor_drafts=1", async () => {
    // Without the flag the server omits visitor drafts — the owner's panels
    // and inbox would silently lose every unsent visitor comment.
    fetchMock.mockResolvedValueOnce(mockResponse([]));
    await fetchComments("conv_1");
    expect(fetchMock.mock.calls[0][0]).toBe(
      "/v1/sessions/conv_1/comments?include_visitor_drafts=1",
    );
  });

  it("appends an encoded path query when filtering to one file", async () => {
    // The path filter must url-encode so files with slashes/spaces don't
    // produce a malformed query the server rejects.
    fetchMock.mockResolvedValueOnce(mockResponse([]));
    await fetchComments("conv with space", "src/a b.ts");
    expect(fetchMock.mock.calls[0][0]).toBe(
      "/v1/sessions/conv%20with%20space/comments?include_visitor_drafts=1&path=src%2Fa%20b.ts",
    );
  });

  it("returns the parsed comment array", async () => {
    const comments = [makeComment({ id: "c1", path: "a.ts" })];
    fetchMock.mockResolvedValueOnce(mockResponse(comments));
    await expect(fetchComments("conv_1")).resolves.toEqual(comments);
  });

  it("throws on non-2xx", async () => {
    // A failed fetch must reject so the query surfaces an error state
    // rather than caching an empty/garbage list.
    fetchMock.mockResolvedValueOnce(mockResponse({}, { ok: false, status: 500 }));
    await expect(fetchComments("conv_1")).rejects.toThrow(/500/);
  });
});

describe("useComments", () => {
  it("does not fetch when sessionId is falsy", () => {
    // The query is disabled without a session id; firing a request to
    // /v1/sessions//comments would 404 noisily on every cold render.
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    renderHook(() => useComments(undefined), { wrapper: wrapperWith(queryClient) });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("fetches and returns data when sessionId is present", async () => {
    const comments = [makeComment({ id: "c1", path: "a.ts" })];
    fetchMock.mockResolvedValueOnce(mockResponse(comments));
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { result } = renderHook(() => useComments("conv_1"), {
      wrapper: wrapperWith(queryClient),
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual(comments);
    expect(fetchMock.mock.calls[0][0]).toBe(
      "/v1/sessions/conv_1/comments?include_visitor_drafts=1",
    );
  });
});

describe("useAddComment", () => {
  function renderAdd(queryClient: QueryClient) {
    return renderHook(() => useAddComment("conv_1"), { wrapper: wrapperWith(queryClient) });
  }

  it("POSTs the payload to the session comments endpoint", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse(makeComment({ id: "c1", path: "a.ts" })));
    const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    const { result } = renderAdd(queryClient);
    result.current.mutate({ path: "a.ts", start_index: 0, end_index: 5, body: "hi" });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/sessions/conv_1/comments");
    expect(init.method).toBe("POST");
    expect(new Headers(init.headers).get("Content-Type")).toBe("application/json");
    expect(JSON.parse(init.body as string)).toEqual({
      path: "a.ts",
      start_index: 0,
      end_index: 5,
      body: "hi",
    });
  });

  it("invalidates both the session-wide list AND the per-file list on success", async () => {
    // The new comment must refresh the sidebar (session-wide) and any
    // open per-file view; missing either leaves a stale list until reload.
    fetchMock.mockResolvedValueOnce(mockResponse(makeComment({ id: "c1", path: "a.ts" })));
    const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
    const { result } = renderAdd(queryClient);
    result.current.mutate({ path: "a.ts", start_index: 0, end_index: 5, body: "hi" });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["comments", "conv_1"] });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["comments", "conv_1", "a.ts"] });
  });

  it("throws on non-2xx", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse({}, { ok: false, status: 422 }));
    const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    const { result } = renderAdd(queryClient);
    result.current.mutate({ path: "a.ts", start_index: 0, end_index: 5, body: "hi" });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error).toMatchObject({ message: expect.stringContaining("422") });
  });
});

describe("useDeleteComment", () => {
  function renderDelete(queryClient: QueryClient) {
    return renderHook(() => useDeleteComment("conv_1"), { wrapper: wrapperWith(queryClient) });
  }

  it("DELETEs the encoded comment endpoint and invalidates the session list", async () => {
    // The comment id is url-encoded so ids with reserved chars hit the
    // right route; success refreshes the session-wide list.
    fetchMock.mockResolvedValueOnce(mockResponse(undefined));
    const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
    const { result } = renderDelete(queryClient);
    result.current.mutate("c 1");
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/sessions/conv_1/comments/c%201");
    expect(init.method).toBe("DELETE");
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["comments", "conv_1"] });
  });

  it("throws on non-2xx", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse({}, { ok: false, status: 404 }));
    const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    const { result } = renderDelete(queryClient);
    result.current.mutate("c1");
    await waitFor(() => expect(result.current.isError).toBe(true));
  });
});

describe("useUpdateComment", () => {
  function renderUpdate(queryClient: QueryClient) {
    return renderHook(() => useUpdateComment("conv_1"), { wrapper: wrapperWith(queryClient) });
  }

  it("PATCHes only the mutable fields, excluding commentId from the body", async () => {
    // commentId belongs in the URL, not the body; leaking it into the
    // body could let the server treat it as a writable field.
    fetchMock.mockResolvedValueOnce(mockResponse(makeComment({ id: "c1", path: "a.ts" })));
    const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    const { result } = renderUpdate(queryClient);
    result.current.mutate({ commentId: "c1", status: "addressed", body: "done" });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/sessions/conv_1/comments/c1");
    expect(init.method).toBe("PATCH");
    expect(JSON.parse(init.body as string)).toEqual({ status: "addressed", body: "done" });
  });

  it("invalidates the session list on success", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse(makeComment({ id: "c1", path: "a.ts" })));
    const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
    const { result } = renderUpdate(queryClient);
    result.current.mutate({ commentId: "c1", status: "addressed" });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["comments", "conv_1"] });
  });

  it("throws on non-2xx", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse({}, { ok: false, status: 409 }));
    const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    const { result } = renderUpdate(queryClient);
    result.current.mutate({ commentId: "c1", body: "x" });
    await waitFor(() => expect(result.current.isError).toBe(true));
  });
});

describe("useSendCommentsToAgent", () => {
  function renderSend(queryClient: QueryClient) {
    return renderHook(() => useSendCommentsToAgent("conv_1", "ag_1"), {
      wrapper: wrapperWith(queryClient),
    });
  }

  const sendResponse = {
    formatted_message: "please address these",
    sent_comment_ids: ["c1", "c2"],
  };

  const attachedResponse = {
    ...sendResponse,
    attachments: [
      { comment_id: "c1", file_id: "f1", filename: "shot-1.jpg" },
      { comment_id: "c2", file_id: "f2", filename: "shot-2.png" },
    ],
  };

  function fileResponse(type: string): Response {
    return mockResponse(new Blob(["image-bytes"]), { headers: { "Content-Type": type } });
  }

  function addressedPatchCalls() {
    return fetchMock.mock.calls.filter(([, init]) => (init as RequestInit).method === "PATCH");
  }

  it("POSTs the comment ids with mark_addressed: false", async () => {
    fetchMock.mockResolvedValue(mockResponse(sendResponse));
    const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    const { result } = renderSend(queryClient);
    result.current.mutate({ comment_ids: ["c1"], instruction: "fix" });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/sessions/conv_1/comments/send");
    expect(init.method).toBe("POST");
    // Formatting must not mark: the comments become addressed only after
    // the message actually reached the agent.
    expect(JSON.parse(init.body as string)).toEqual({
      comment_ids: ["c1"],
      instruction: "fix",
      mark_addressed: false,
    });
  });

  it("dispatches the message pinned to the session, then marks the sent ids", async () => {
    // The whole point of this hook: the server-formatted message is sent
    // to the agent immediately, pinned to the session whose comments were
    // formatted, and the comments are marked only after delivery.
    fetchMock.mockResolvedValue(mockResponse(sendResponse));
    const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
    const { result } = renderSend(queryClient);
    result.current.mutate({ comment_ids: ["c1", "c2"] });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(sendMock).toHaveBeenCalledWith("please address these", "ag_1", [], {
      pinnedConversationId: "conv_1",
    });
    const patchCalls = fetchMock.mock.calls.filter(
      ([, init]) => (init as RequestInit).method === "PATCH",
    );
    expect(patchCalls.map(([url]) => String(url))).toEqual([
      "/v1/sessions/conv_1/comments/c1",
      "/v1/sessions/conv_1/comments/c2",
    ]);
    for (const [, init] of patchCalls) {
      expect(JSON.parse((init as RequestInit).body as string)).toEqual({ status: "addressed" });
    }
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["comments", "conv_1"] });
  });

  it("fetches attachments in order and passes them as Files", async () => {
    fetchMock.mockResolvedValue(mockResponse(sendResponse));
    fetchMock
      .mockResolvedValueOnce(mockResponse(attachedResponse))
      .mockResolvedValueOnce(fileResponse("image/jpeg"))
      .mockResolvedValueOnce(fileResponse("image/png"));
    const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    const { result } = renderSend(queryClient);
    result.current.mutate({ comment_ids: ["c1", "c2"] });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(fetchMock.mock.calls[1]?.[0]).toBe("/v1/sessions/conv_1/resources/files/f1/content");
    expect(fetchMock.mock.calls[2]?.[0]).toBe("/v1/sessions/conv_1/resources/files/f2/content");
    const files = sendMock.mock.calls[0][2] as File[];
    expect(files.map((file) => file.name)).toEqual(["shot-1.jpg", "shot-2.png"]);
    expect(files.map((file) => file.type)).toEqual(["image/jpeg", "image/png"]);
  });

  it("aborts with nothing sent when an attachment fetch fails", async () => {
    fetchMock
      .mockResolvedValueOnce(mockResponse(attachedResponse))
      .mockResolvedValueOnce(mockResponse({}, { ok: false, status: 500 }));
    const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    const { result } = renderSend(queryClient);
    result.current.mutate({ comment_ids: ["c1", "c2"], respectQueue: true });
    await waitFor(() => expect(result.current.isError).toBe(true));

    expect((result.current.error as Error).message).toBe(
      "Couldn't attach screenshot 1; nothing was sent",
    );
    expect(sendMock).not.toHaveBeenCalled();
    expect(enqueueMock).not.toHaveBeenCalled();
    expect(addressedPatchCalls()).toEqual([]);
  });

  it("queues the batch when the agent is busy, then patches the sent ids", async () => {
    chatState.status = "streaming";
    fetchMock.mockResolvedValue(mockResponse(sendResponse));
    const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    const { result } = renderSend(queryClient);
    result.current.mutate({ comment_ids: ["c1", "c2"], respectQueue: true });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(enqueueMock).toHaveBeenCalledWith("please address these", []);
    expect(sendMock).not.toHaveBeenCalled();
    expect(addressedPatchCalls().map(([url]) => String(url))).toEqual([
      "/v1/sessions/conv_1/comments/c1",
      "/v1/sessions/conv_1/comments/c2",
    ]);
  });

  it("refuses to send when the active session changed", async () => {
    chatState.conversationId = "conv_other";
    fetchMock.mockResolvedValueOnce(mockResponse(sendResponse));
    const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    const { result } = renderSend(queryClient);
    result.current.mutate({ comment_ids: ["c1"], respectQueue: true });
    await waitFor(() => expect(result.current.isError).toBe(true));

    expect((result.current.error as Error).message).toBe("Return to this session before sending.");
    expect(sendMock).not.toHaveBeenCalled();
    expect(enqueueMock).not.toHaveBeenCalled();
    expect(addressedPatchCalls()).toEqual([]);
  });

  it("marks nothing when the message was not delivered", async () => {
    sendMock.mockResolvedValue(false);
    fetchMock.mockResolvedValueOnce(mockResponse(sendResponse));
    const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
    const { result } = renderSend(queryClient);
    result.current.mutate({ comment_ids: ["c1", "c2"] });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(result.current.data).toMatchObject({ delivered: false });
    expect(
      fetchMock.mock.calls.filter(([, init]) => (init as RequestInit).method === "PATCH"),
    ).toEqual([]);
    expect(invalidateSpy).not.toHaveBeenCalled();
  });

  it("throws on non-2xx and never dispatches to the agent", async () => {
    // A failed send must NOT call the chat store, or the user sees a
    // message dispatched while the comments stay unsent server-side.
    fetchMock.mockResolvedValueOnce(mockResponse({}, { ok: false, status: 500 }));
    const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    const { result } = renderSend(queryClient);
    result.current.mutate({ comment_ids: ["c1"] });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(sendMock).not.toHaveBeenCalled();
  });
});
