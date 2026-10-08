// Tests for the parent side of the annotate channel: handshake, shortcut
// push, annotation sync, mode toggling, save and batch send, and orphan
// tracking. The fake frame mirrors HtmlCommentViewer.test.tsx's bridge fakes:
// a real element with a stubbed `contentWindow.postMessage` capturing the port
// transferred at init.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, renderHook, waitFor } from "@testing-library/react";
import { type ReactNode, createElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { showToast } from "@/components/ui/toast";
import type { Comment } from "@/hooks/useComments";
import type * as filesApi from "@/lib/filesApi";
import { authenticatedFetch } from "@/lib/identity";
import {
  KEYBOARD_SHORTCUTS_CHANGED_EVENT,
  KEYBOARD_SHORTCUTS_STORAGE_KEY,
} from "@/lib/keyboardShortcutPreferences";
import {
  type ElementAnchorV1,
  ELEMENT_ANCHOR_PREFIX,
  encodeElementAnchor,
} from "./annotationAnchor";
import { ANNOTATE_SOURCE } from "./htmlAnnotateBridge";
import {
  useHtmlAnnotate,
  type UseHtmlAnnotateArgs,
  type UseHtmlAnnotateResult,
} from "./useHtmlAnnotate";

const mocks = vi.hoisted(() => ({
  loadRuntimeSources: vi.fn(async (part: "core" | "pick") => [`${part}-source`]),
  sender: { mutate: vi.fn(), mutateAsync: vi.fn(), isPending: false },
  useOptionalSender: vi.fn(),
  uploadFile: vi.fn(),
}));

vi.mock("./annotate/runtimeSources", () => ({
  loadRuntimeSources: (part: "core" | "pick") => mocks.loadRuntimeSources(part),
}));
vi.mock("@/hooks/CommentSenderContext", () => ({
  useOptionalCommentSender: () => mocks.useOptionalSender(),
}));
vi.mock("@/components/ui/toast", () => ({ showToast: vi.fn() }));
vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));
vi.mock("@/lib/filesApi", async (importOriginal) => {
  const actual = await importOriginal<typeof filesApi>();
  return { ...actual, uploadFile: (...args: unknown[]) => mocks.uploadFile(...args) };
});

const toastMock = vi.mocked(showToast);
const fetchMock = vi.mocked(authenticatedFetch);

const NONCE = "nonce-1";
const PATH = "reports/q3.html";

function mockResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: "OK",
    headers: new Headers(),
    json: async () => body,
  } as unknown as Response;
}

interface FakeFrame {
  iframe: HTMLIFrameElement;
  postMessage: ReturnType<typeof vi.fn>;
  /** Messages the parent posted over the annotate port. */
  sent: Record<string, unknown>[];
  /** Frame-side port of the latest init (the parent holds the other end). */
  port: MessagePort | null;
}

function createFrame(): FakeFrame {
  const iframe = document.createElement("iframe");
  const postMessage = vi.fn();
  const frame: FakeFrame = { iframe, postMessage, sent: [], port: null };
  postMessage.mockImplementation((_message, _target, ports: MessagePort[]) => {
    frame.port = ports[0] ?? null;
    if (frame.port) {
      frame.port.onmessage = (event) => {
        frame.sent.push(event.data as Record<string, unknown>);
      };
    }
  });
  Object.defineProperty(iframe, "contentWindow", { configurable: true, value: { postMessage } });
  // The handler gates the parent-focus shortcut on the frame being visible.
  vi.spyOn(iframe, "getClientRects").mockReturnValue({ length: 1 } as unknown as DOMRectList);
  return frame;
}

function flush(): Promise<void> {
  return new Promise((resolve) => {
    setTimeout(resolve, 0);
  });
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

async function loadFrame(frame: FakeFrame): Promise<void> {
  await act(async () => {
    fireEvent.load(frame.iframe);
    await flush();
  });
}

async function sendFromFrame(frame: FakeFrame, message: Record<string, unknown>): Promise<void> {
  await act(async () => {
    frame.port!.postMessage({ source: ANNOTATE_SOURCE, nonce: NONCE, ...message });
    await flush();
  });
}

/** Run the parent's own toggle through both runtime loads, as the toolbar does. */
async function enterMode(frame: FakeFrame, toggle: () => void): Promise<void> {
  act(toggle);
  await act(async () => {
    await flush();
  });
  await sendFromFrame(frame, { type: "annotate:runtimeLoaded", part: "core", ok: true });
  await sendFromFrame(frame, { type: "annotate:runtimeLoaded", part: "pick", ok: true });
}

function lastType(frame: FakeFrame, type: string): Record<string, unknown> | undefined {
  for (let i = frame.sent.length - 1; i >= 0; i--) {
    if (frame.sent[i]!.type === type) return frame.sent[i];
  }
  return undefined;
}

function makeAnchor(y = 300, x = 50): ElementAnchorV1 {
  return {
    v: 1,
    kind: "element",
    page: { url: "", title: "", vw: 800, vh: 600, sx: 0, sy: 0, dpr: 1 },
    target: {
      label: "div.filter-menu > button.option",
      css: "button.option",
      xpath: "//button",
      quote: { exact: "Alpha", prefix: "", suffix: "" },
      fingerprint: "1:1:abcd",
      neighborText: "",
      tag: "BUTTON",
      id: "",
      role: "",
      ariaLabel: "",
      text: "Alpha",
    },
    rect: { x, y, w: 120, h: 32 },
    region: null,
    selectedText: "",
    console: [],
    network: [],
    screenshot: null,
  };
}

/** A tiny JPEG data URL: `/9j/` decodes to the FF D8 FF magic bytes. */
function jpegDataUrl(): string {
  return "data:image/jpeg;base64,/9j/4AAQSkZJRg==";
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

/** A frame `annotate:picked` message, as the picker posts it (no note/action). */
function pickedMessage(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    type: "annotate:picked",
    anchor: makeAnchor(300, 50),
    screenshot: null,
    viewportRect: { x: 10, y: 20, w: 120, h: 32 },
    ...overrides,
  };
}

/** Run the composer's submit through the hook's API. */
async function submitPick(
  result: { current: UseHtmlAnnotateResult },
  note: string,
  action: "stack" | "send",
): Promise<void> {
  await act(async () => {
    result.current.submitPick(note, action);
    await flush();
  });
}

function makeElementComment(
  id: string,
  path: string,
  y: number,
  status: "draft" | "addressed" = "draft",
): Comment {
  const index = y * 10_000;
  return makeComment({
    id,
    path,
    start_index: index,
    end_index: index,
    status,
    anchor_content: encodeElementAnchor(makeAnchor(y)),
  });
}

function setup(initial: Partial<UseHtmlAnnotateArgs> = {}) {
  const frame = createFrame();
  const onSelectComment = vi.fn();
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const props: UseHtmlAnnotateArgs = {
    iframe: frame.iframe,
    nonce: NONCE,
    path: PATH,
    sessionId: "conv_1",
    comments: [],
    onSelectComment,
    ...initial,
  };
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client: queryClient }, children);
  const view = renderHook((p: UseHtmlAnnotateArgs) => useHtmlAnnotate(p), {
    initialProps: props,
    wrapper,
  });
  return { frame, onSelectComment, ...view };
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  mocks.loadRuntimeSources.mockImplementation(async (part) => [`${part}-source`]);
  mocks.sender.mutateAsync.mockResolvedValue(undefined);
  mocks.useOptionalSender.mockReturnValue(mocks.sender);
  mocks.uploadFile.mockReset();
  fetchMock.mockReset();
});

afterEach(() => {
  localStorage.clear();
});

describe("useHtmlAnnotate handshake and shortcut", () => {
  it("posts init on load and answers ready with the resolved chord", async () => {
    const { frame, result } = setup();
    await loadFrame(frame);

    expect(frame.postMessage).toHaveBeenCalledWith(
      { source: ANNOTATE_SOURCE, nonce: NONCE, type: "annotate:init" },
      "*",
      [expect.any(MessagePort)],
    );
    expect(result.current.available).toBe(false);

    await sendFromFrame(frame, { type: "annotate:ready", pathname: "/v1/artifacts/a/index.html" });

    expect(result.current.available).toBe(true);
    expect(lastType(frame, "annotate:setShortcut")?.bindings).toEqual([
      { code: "Period", ctrl: true, meta: false, alt: false, shift: true },
    ]);
  });

  it("re-sends the shortcut when the binding changes live", async () => {
    const { frame } = setup();
    await loadFrame(frame);
    await sendFromFrame(frame, { type: "annotate:ready" });
    frame.sent.length = 0;

    localStorage.setItem(
      KEYBOARD_SHORTCUTS_STORAGE_KEY,
      JSON.stringify({
        version: 1,
        actions: {
          toggleAnnotationMode: { common: [{ code: "KeyJ", modifiers: ["primary", "alt"] }] },
        },
      }),
    );
    await act(async () => {
      window.dispatchEvent(new Event(KEYBOARD_SHORTCUTS_CHANGED_EVENT));
      await flush();
    });

    expect(lastType(frame, "annotate:setShortcut")?.bindings).toEqual([
      { code: "KeyJ", ctrl: true, meta: false, alt: true, shift: false },
    ]);
  });
});

describe("useHtmlAnnotate annotations", () => {
  it("loads core and pushes this path's annotations in start order", async () => {
    const text = makeComment({ id: "c-text", path: PATH, anchor_content: "plain" });
    const { frame } = setup({
      comments: [
        makeElementComment("c2", PATH, 900),
        makeElementComment("c1", PATH, 100),
        makeElementComment("c-other", "reports/other.html", 50),
        text,
      ],
    });
    await loadFrame(frame);
    await sendFromFrame(frame, { type: "annotate:ready" });

    expect(lastType(frame, "annotate:loadRuntime")).toMatchObject({
      part: "core",
      sources: ["core-source"],
    });

    await sendFromFrame(frame, { type: "annotate:runtimeLoaded", part: "core", ok: true });

    const items = lastType(frame, "annotate:setAnnotations")?.items as { id: string; n: number }[];
    expect(items.map((item) => [item.id, item.n])).toEqual([
      ["c1", 1],
      ["c2", 2],
    ]);
  });

  it("tracks orphaned annotations from the resolve report", async () => {
    const { frame, result } = setup();
    await loadFrame(frame);
    await sendFromFrame(frame, { type: "annotate:ready" });

    await sendFromFrame(frame, {
      type: "annotate:resolved",
      items: [
        { id: "a", found: true },
        { id: "b", found: false },
      ],
    });

    expect([...result.current.orphanIds]).toEqual(["b"]);
  });

  it("routes a marker click to the comment selection callback", async () => {
    const { frame, onSelectComment } = setup();
    await loadFrame(frame);
    await sendFromFrame(frame, { type: "annotate:ready" });

    await sendFromFrame(frame, { type: "annotate:markerClick", id: "c1" });

    expect(onSelectComment).toHaveBeenCalledWith("c1");
  });
});

describe("useHtmlAnnotate mode", () => {
  it("loads core then pick before setting the mode on", async () => {
    const { frame, result } = setup();
    await loadFrame(frame);
    await sendFromFrame(frame, { type: "annotate:ready" });

    act(() => result.current.toggle());
    await act(async () => {
      await flush();
    });
    expect(lastType(frame, "annotate:loadRuntime")).toMatchObject({ part: "core" });

    await sendFromFrame(frame, { type: "annotate:runtimeLoaded", part: "core", ok: true });
    expect(lastType(frame, "annotate:loadRuntime")).toMatchObject({
      part: "pick",
      sources: ["pick-source"],
    });

    await sendFromFrame(frame, { type: "annotate:runtimeLoaded", part: "pick", ok: true });
    expect(lastType(frame, "annotate:setMode")).toMatchObject({ on: true });
    expect(result.current.on).toBe(true);
  });

  it("ignores a forged frame mode-on and any pick that follows it", async () => {
    const { frame, result } = setup();
    await loadFrame(frame);
    await sendFromFrame(frame, { type: "annotate:ready" });

    await sendFromFrame(frame, { type: "annotate:modeChanged", on: true, reason: "user" });
    expect(result.current.on).toBe(false);

    await sendFromFrame(frame, pickedMessage());

    expect(result.current.pendingPick).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
    expect(mocks.uploadFile).not.toHaveBeenCalled();
    expect(mocks.sender.mutateAsync).not.toHaveBeenCalled();
  });

  it("ignores a pick while the mode is off and honours a frame mode-off", async () => {
    const { frame, result } = setup();
    await loadFrame(frame);
    await sendFromFrame(frame, { type: "annotate:ready" });

    await sendFromFrame(frame, pickedMessage());
    expect(result.current.pendingPick).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
    expect(mocks.sender.mutateAsync).not.toHaveBeenCalled();

    await enterMode(frame, result.current.toggle);
    expect(result.current.on).toBe(true);

    await sendFromFrame(frame, { type: "annotate:modeChanged", on: false, reason: "escape" });
    expect(result.current.on).toBe(false);
  });

  it("mirrors a parent toggle without waiting for the frame's echo", async () => {
    const { frame, result } = setup();
    await loadFrame(frame);
    await sendFromFrame(frame, { type: "annotate:ready" });

    act(() => result.current.toggle());
    await act(async () => {
      await flush();
    });
    await sendFromFrame(frame, { type: "annotate:runtimeLoaded", part: "core", ok: true });
    await sendFromFrame(frame, { type: "annotate:runtimeLoaded", part: "pick", ok: true });

    expect(lastType(frame, "annotate:setMode")).toMatchObject({ on: true });
    expect(result.current.on).toBe(true);

    act(() => result.current.toggle());
    await act(async () => {
      await flush();
    });
    expect(lastType(frame, "annotate:setMode")).toMatchObject({ on: false });
    expect(result.current.on).toBe(false);
  });

  it("toggles from the frame's pre-runtime shortcut request", async () => {
    const { frame } = setup();
    await loadFrame(frame);
    await sendFromFrame(frame, { type: "annotate:ready" });

    await sendFromFrame(frame, { type: "annotate:toggleRequested" });

    expect(lastType(frame, "annotate:loadRuntime")).toMatchObject({ part: "core" });
  });

  it("toggles from the parent-focus shortcut", async () => {
    const { frame } = setup();
    await loadFrame(frame);
    await sendFromFrame(frame, { type: "annotate:ready" });

    await act(async () => {
      fireEvent.keyDown(window, { key: ".", code: "Period", ctrlKey: true, shiftKey: true });
      await flush();
    });

    expect(lastType(frame, "annotate:loadRuntime")).toMatchObject({ part: "core" });
  });

  it("stays off and toasts when the runtime fails to load", async () => {
    const { frame, result } = setup();
    await loadFrame(frame);
    await sendFromFrame(frame, { type: "annotate:ready" });

    act(() => result.current.toggle());
    await act(async () => {
      await flush();
    });
    await sendFromFrame(frame, { type: "annotate:runtimeLoaded", part: "core", ok: false });

    expect(toastMock).toHaveBeenCalledWith("Annotation is unavailable on this page");
    expect(result.current.on).toBe(false);
    expect(lastType(frame, "annotate:setMode")).toBeUndefined();
  });
});

describe("useHtmlAnnotate save and send", () => {
  it("holds a pick as pendingPick without saving, then saves it on stack", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse(makeElementComment("c-new", PATH, 300)));
    const { frame, result } = setup();
    await loadFrame(frame);
    await sendFromFrame(frame, { type: "annotate:ready" });
    await enterMode(frame, result.current.toggle);

    await sendFromFrame(frame, pickedMessage());
    expect(result.current.pendingPick).toMatchObject({
      label: "div.filter-menu > button.option",
      viewportRect: { x: 10, y: 20, w: 120, h: 32 },
    });
    expect(fetchMock).not.toHaveBeenCalled();
    expect(mocks.uploadFile).not.toHaveBeenCalled();

    await submitPick(result, "make it blue", "stack");

    expect(result.current.pendingPick).toBeNull();
    expect(lastType(frame, "annotate:pickDone")).toMatchObject({ type: "annotate:pickDone" });
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/sessions/conv_1/comments");
    expect(init.method).toBe("POST");
    const body = JSON.parse(init.body as string);
    expect(body.path).toBe(PATH);
    expect(body.body).toBe("make it blue");
    expect(body.start_index).toBe(3_000_050);
    expect(body.end_index).toBe(3_000_050);
    expect(body.anchor_content.startsWith(ELEMENT_ANCHOR_PREFIX)).toBe(true);
    const anchor = JSON.parse(body.anchor_content.slice(ELEMENT_ANCHOR_PREFIX.length));
    expect(anchor.screenshot).toBeNull();
    expect(mocks.sender.mutateAsync).not.toHaveBeenCalled();
  });

  it("ignores a second pick while one is pending and trims the submitted note", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse(makeElementComment("c-new", PATH, 300)));
    const { frame, result } = setup();
    await loadFrame(frame);
    await sendFromFrame(frame, { type: "annotate:ready" });
    await enterMode(frame, result.current.toggle);

    await sendFromFrame(frame, pickedMessage({ anchor: makeAnchor(300, 50) }));
    await sendFromFrame(frame, pickedMessage({ anchor: makeAnchor(700, 90) }));
    expect(result.current.pendingPick!.anchor.rect.y).toBe(300);

    await submitPick(result, "  spaced  ", "stack");
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string).body).toBe("spaced");
  });

  it("cancels the pending pick without a fetch and posts pickDone", async () => {
    const { frame, result } = setup();
    await loadFrame(frame);
    await sendFromFrame(frame, { type: "annotate:ready" });
    await enterMode(frame, result.current.toggle);

    await sendFromFrame(frame, pickedMessage());
    await act(async () => {
      result.current.cancelPick();
      await flush();
    });

    expect(result.current.pendingPick).toBeNull();
    expect(lastType(frame, "annotate:pickDone")).toMatchObject({ type: "annotate:pickDone" });
    expect(fetchMock).not.toHaveBeenCalled();
    expect(mocks.uploadFile).not.toHaveBeenCalled();
    expect(mocks.sender.mutateAsync).not.toHaveBeenCalled();
  });

  it("drops the pending pick when the parent turns the mode off", async () => {
    const { frame, result } = setup();
    await loadFrame(frame);
    await sendFromFrame(frame, { type: "annotate:ready" });
    await enterMode(frame, result.current.toggle);

    await sendFromFrame(frame, pickedMessage());
    await act(async () => {
      result.current.toggle();
      await flush();
    });

    expect(result.current.on).toBe(false);
    expect(result.current.pendingPick).toBeNull();
    await flush();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("drops the pending pick when the document reloads", async () => {
    const { frame, result } = setup();
    await loadFrame(frame);
    await sendFromFrame(frame, { type: "annotate:ready" });
    await enterMode(frame, result.current.toggle);

    await sendFromFrame(frame, pickedMessage());
    await loadFrame(frame);

    expect(result.current.pendingPick).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("drops the pending pick when the session changes", async () => {
    const { frame, result, rerender } = setup();
    await loadFrame(frame);
    await sendFromFrame(frame, { type: "annotate:ready" });
    await enterMode(frame, result.current.toggle);

    await sendFromFrame(frame, pickedMessage());
    await act(async () => {
      rerender({
        iframe: frame.iframe,
        nonce: NONCE,
        path: PATH,
        sessionId: "conv_2",
        comments: [],
        onSelectComment: vi.fn(),
      });
    });

    expect(result.current.pendingPick).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("saves then sends this path's draft element comments", async () => {
    const { frame, result } = setup({
      comments: [
        makeElementComment("c-existing", PATH, 100),
        makeElementComment("c-addressed", PATH, 150, "addressed"),
        makeElementComment("c-other", "reports/other.html", 200),
        makeComment({ id: "c-text", path: PATH, anchor_content: "plain" }),
      ],
    });
    fetchMock.mockResolvedValueOnce(mockResponse(makeElementComment("c-new", PATH, 900)));
    await loadFrame(frame);
    await sendFromFrame(frame, { type: "annotate:ready" });
    await enterMode(frame, result.current.toggle);

    await sendFromFrame(frame, pickedMessage({ anchor: makeAnchor(900, 10) }));
    await submitPick(result, "later", "send");

    await waitFor(() => expect(mocks.sender.mutateAsync).toHaveBeenCalledTimes(1));
    expect(mocks.sender.mutateAsync).toHaveBeenCalledWith({
      comment_ids: ["c-existing", "c-new"],
      respectQueue: true,
    });
  });

  it("keeps the saved draft and toasts when no agent is bound", async () => {
    mocks.useOptionalSender.mockReturnValue(null);
    fetchMock.mockResolvedValueOnce(mockResponse(makeElementComment("c-new", PATH, 300)));
    const { frame, result } = setup();
    await loadFrame(frame);
    await sendFromFrame(frame, { type: "annotate:ready" });
    await enterMode(frame, result.current.toggle);

    await sendFromFrame(frame, pickedMessage());
    await submitPick(result, "send me", "send");

    await waitFor(() =>
      expect(toastMock).toHaveBeenCalledWith("No agent bound to this session yet."),
    );
    expect(mocks.sender.mutateAsync).not.toHaveBeenCalled();
  });

  it("does not send when the session changed while the save was in flight", async () => {
    const pendingSave = deferred<Response>();
    fetchMock.mockReturnValueOnce(pendingSave.promise);
    const { frame, result, rerender } = setup({
      comments: [makeElementComment("c-existing", PATH, 100)],
    });
    await loadFrame(frame);
    await sendFromFrame(frame, { type: "annotate:ready" });
    await enterMode(frame, result.current.toggle);

    await sendFromFrame(frame, pickedMessage());
    await submitPick(result, "one", "send");
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    await act(async () => {
      rerender({
        iframe: frame.iframe,
        nonce: NONCE,
        path: PATH,
        sessionId: "conv_2",
        comments: [],
        onSelectComment: vi.fn(),
      });
    });
    await act(async () => {
      pendingSave.resolve(mockResponse(makeElementComment("c-new", PATH, 300)));
      await flush();
    });

    expect(mocks.sender.mutateAsync).not.toHaveBeenCalled();
    expect(toastMock).toHaveBeenCalledWith("Return to this session before sending.");
  });

  it("saves into the originating session when the session changes during upload", async () => {
    const pendingUpload = deferred<{
      id: string;
      filename: string;
      bytes: number;
      created_at: number;
    }>();
    mocks.uploadFile.mockReturnValue(pendingUpload.promise);
    fetchMock.mockResolvedValueOnce(mockResponse(makeElementComment("c-new", PATH, 300)));
    const { frame, result, rerender } = setup({
      comments: [makeElementComment("c-existing", PATH, 100)],
    });
    await loadFrame(frame);
    await sendFromFrame(frame, { type: "annotate:ready" });
    await enterMode(frame, result.current.toggle);

    await sendFromFrame(
      frame,
      pickedMessage({ screenshot: { dataUrl: jpegDataUrl(), width: 800, height: 400 } }),
    );
    await submitPick(result, "one", "send");
    await waitFor(() => expect(mocks.uploadFile).toHaveBeenCalledTimes(1));

    await act(async () => {
      rerender({
        iframe: frame.iframe,
        nonce: NONCE,
        path: PATH,
        sessionId: "conv_2",
        comments: [],
        onSelectComment: vi.fn(),
      });
    });
    await act(async () => {
      pendingUpload.resolve({
        id: "file_1",
        filename: "annotation-1.jpg",
        bytes: 10,
        created_at: 0,
      });
      await flush();
    });

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const [url] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/sessions/conv_1/comments");
    expect(mocks.sender.mutateAsync).not.toHaveBeenCalled();
    expect(toastMock).toHaveBeenCalledWith("Return to this session before sending.");
  });

  it("serializes send picks so a burst never repeats an already-sent draft", async () => {
    const saves: ((value: Response) => void)[] = [];
    fetchMock.mockImplementation(
      () =>
        new Promise<Response>((resolve) => {
          saves.push(resolve);
        }),
    );
    const { frame, result } = setup({ comments: [makeElementComment("c-existing", PATH, 100)] });
    await loadFrame(frame);
    await sendFromFrame(frame, { type: "annotate:ready" });
    await enterMode(frame, result.current.toggle);

    await sendFromFrame(frame, pickedMessage({ anchor: makeAnchor(200) }));
    await submitPick(result, "one", "send");
    await sendFromFrame(frame, pickedMessage({ anchor: makeAnchor(300) }));
    await submitPick(result, "two", "send");
    // The second pick waits for the first save instead of racing it.
    expect(fetchMock).toHaveBeenCalledTimes(1);

    await act(async () => {
      saves[0]!(mockResponse(makeElementComment("c-one", PATH, 200)));
      await flush();
    });
    expect(mocks.sender.mutateAsync).toHaveBeenNthCalledWith(1, {
      comment_ids: ["c-existing", "c-one"],
      respectQueue: true,
    });

    await act(async () => {
      saves[1]!(mockResponse(makeElementComment("c-two", PATH, 300)));
      await flush();
    });
    expect(mocks.sender.mutateAsync).toHaveBeenNthCalledWith(2, {
      comment_ids: ["c-two"],
      respectQueue: true,
    });
    expect(mocks.sender.mutateAsync).toHaveBeenCalledTimes(2);
  });

  it("adds a queued stack's saved row to the next send's batch", async () => {
    const saves: ((value: Response) => void)[] = [];
    fetchMock.mockImplementation(
      () =>
        new Promise<Response>((resolve) => {
          saves.push(resolve);
        }),
    );
    const { frame, result } = setup({ comments: [makeElementComment("c-existing", PATH, 100)] });
    await loadFrame(frame);
    await sendFromFrame(frame, { type: "annotate:ready" });
    await enterMode(frame, result.current.toggle);

    await sendFromFrame(frame, pickedMessage({ anchor: makeAnchor(200) }));
    await submitPick(result, "stacked", "stack");
    await sendFromFrame(frame, pickedMessage({ anchor: makeAnchor(300) }));
    await submitPick(result, "send", "send");
    // The send waits for the stack's save instead of racing it.
    expect(fetchMock).toHaveBeenCalledTimes(1);

    await act(async () => {
      saves[0]!(mockResponse(makeElementComment("c-stack", PATH, 200)));
      await flush();
    });
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    await act(async () => {
      saves[1]!(mockResponse(makeElementComment("c-send", PATH, 300)));
      await flush();
    });

    await waitFor(() => expect(mocks.sender.mutateAsync).toHaveBeenCalledTimes(1));
    expect(mocks.sender.mutateAsync).toHaveBeenCalledWith({
      comment_ids: ["c-existing", "c-stack", "c-send"],
      respectQueue: true,
    });
  });

  it("does not resend delivered ids when a refetch lands between queued sends", async () => {
    const saves: ((value: Response) => void)[] = [];
    fetchMock.mockImplementation(
      () =>
        new Promise<Response>((resolve) => {
          saves.push(resolve);
        }),
    );
    const { frame, result, rerender } = setup({
      comments: [makeElementComment("c-existing", PATH, 100)],
    });
    await loadFrame(frame);
    await sendFromFrame(frame, { type: "annotate:ready" });
    await enterMode(frame, result.current.toggle);

    await sendFromFrame(frame, pickedMessage({ anchor: makeAnchor(200) }));
    await submitPick(result, "one", "send");
    await sendFromFrame(frame, pickedMessage({ anchor: makeAnchor(300) }));
    await submitPick(result, "two", "send");
    expect(fetchMock).toHaveBeenCalledTimes(1);

    await act(async () => {
      saves[0]!(mockResponse(makeElementComment("c-one", PATH, 200)));
      await flush();
    });
    await waitFor(() => expect(mocks.sender.mutateAsync).toHaveBeenCalledTimes(1));
    expect(mocks.sender.mutateAsync).toHaveBeenNthCalledWith(1, {
      comment_ids: ["c-existing", "c-one"],
      respectQueue: true,
    });

    // The refetch catches up with the delivered drafts (addressed) while the
    // queued step still holds its arrival snapshot; it must not resend them.
    await act(async () => {
      rerender({
        iframe: frame.iframe,
        nonce: NONCE,
        path: PATH,
        sessionId: "conv_1",
        comments: [
          makeElementComment("c-existing", PATH, 100, "addressed"),
          makeElementComment("c-one", PATH, 200, "addressed"),
        ],
        onSelectComment: vi.fn(),
      });
    });
    await act(async () => {
      saves[1]!(mockResponse(makeElementComment("c-two", PATH, 300)));
      await flush();
    });

    await waitFor(() => expect(mocks.sender.mutateAsync).toHaveBeenCalledTimes(2));
    expect(mocks.sender.mutateAsync).toHaveBeenNthCalledWith(2, {
      comment_ids: ["c-two"],
      respectQueue: true,
    });
  });

  it("keeps queued sends serialized across a document reload and posts only pickDone", async () => {
    const saves: ((value: Response) => void)[] = [];
    fetchMock.mockImplementation(
      () =>
        new Promise<Response>((resolve) => {
          saves.push(resolve);
        }),
    );
    const { frame, result } = setup({ comments: [makeElementComment("c-existing", PATH, 100)] });
    await loadFrame(frame);
    await sendFromFrame(frame, { type: "annotate:ready" });
    await enterMode(frame, result.current.toggle);

    await sendFromFrame(frame, pickedMessage({ anchor: makeAnchor(200) }));
    await submitPick(result, "one", "send");
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    // The reload resets the mode; the user turns it on again on the new page.
    await loadFrame(frame);
    await sendFromFrame(frame, { type: "annotate:ready" });
    await enterMode(frame, result.current.toggle);
    frame.sent.length = 0;
    await sendFromFrame(frame, pickedMessage({ anchor: makeAnchor(300) }));
    await submitPick(result, "two", "send");
    // The reload keeps the queue: the second save waits for the first.
    expect(fetchMock).toHaveBeenCalledTimes(1);

    await act(async () => {
      saves[0]!(mockResponse(makeElementComment("c-one", PATH, 200)));
      await flush();
    });
    await waitFor(() => expect(mocks.sender.mutateAsync).toHaveBeenCalledTimes(1));
    expect(mocks.sender.mutateAsync).toHaveBeenNthCalledWith(1, {
      comment_ids: ["c-existing", "c-one"],
      respectQueue: true,
    });

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    await act(async () => {
      saves[1]!(mockResponse(makeElementComment("c-two", PATH, 300)));
      await flush();
    });
    await waitFor(() => expect(mocks.sender.mutateAsync).toHaveBeenCalledTimes(2));
    expect(mocks.sender.mutateAsync).toHaveBeenNthCalledWith(2, {
      comment_ids: ["c-two"],
      respectQueue: true,
    });

    // The old document's steps saved and sent; the only message the new port
    // saw was the current submit's pickDone.
    expect(frame.sent).toEqual([
      expect.objectContaining({ type: "annotate:pickDone", source: ANNOTATE_SOURCE, nonce: NONCE }),
    ]);
  });

  it("leaves a batch's ids eligible again when the send fails", async () => {
    mocks.sender.mutateAsync.mockRejectedValueOnce(new Error("offline"));
    fetchMock.mockResolvedValueOnce(mockResponse(makeElementComment("c-one", PATH, 200)));
    const { frame, result, rerender } = setup({
      comments: [makeElementComment("c-existing", PATH, 100)],
    });
    await loadFrame(frame);
    await sendFromFrame(frame, { type: "annotate:ready" });
    await enterMode(frame, result.current.toggle);

    await sendFromFrame(frame, pickedMessage({ anchor: makeAnchor(200) }));
    await submitPick(result, "one", "send");
    await waitFor(() => expect(mocks.sender.mutateAsync).toHaveBeenCalledTimes(1));
    expect(mocks.sender.mutateAsync).toHaveBeenLastCalledWith({
      comment_ids: ["c-existing", "c-one"],
      respectQueue: true,
    });

    // The refetch surfaces the saved draft; a later pick retries it.
    await act(async () => {
      rerender({
        iframe: frame.iframe,
        nonce: NONCE,
        path: PATH,
        sessionId: "conv_1",
        comments: [
          makeElementComment("c-existing", PATH, 100),
          makeElementComment("c-one", PATH, 200),
        ],
        onSelectComment: vi.fn(),
      });
    });
    fetchMock.mockResolvedValueOnce(mockResponse(makeElementComment("c-two", PATH, 300)));
    await sendFromFrame(frame, pickedMessage({ anchor: makeAnchor(300) }));
    await submitPick(result, "two", "send");
    await waitFor(() => expect(mocks.sender.mutateAsync).toHaveBeenCalledTimes(2));
    expect(mocks.sender.mutateAsync).toHaveBeenLastCalledWith({
      comment_ids: ["c-existing", "c-one", "c-two"],
      respectQueue: true,
    });
  });

  it("starts clean when the document reloads while a runtime load is pending", async () => {
    const { frame, result } = setup();
    await loadFrame(frame);
    await sendFromFrame(frame, { type: "annotate:ready" });

    act(() => result.current.toggle());
    await act(async () => {
      await flush();
    });
    expect(lastType(frame, "annotate:loadRuntime")).toMatchObject({ part: "core" });

    // The reload resolves the stale waiter with false; the old flow must not
    // post a mode change onto the new document's port.
    await loadFrame(frame);
    await sendFromFrame(frame, { type: "annotate:ready" });

    expect(lastType(frame, "annotate:setMode")).toBeUndefined();
    expect(result.current.on).toBe(false);
  });
});

describe("useHtmlAnnotate screenshot upload", () => {
  it("uploads a valid screenshot and stores its file id in the anchor", async () => {
    mocks.uploadFile.mockResolvedValue({
      id: "file_1",
      filename: "annotation-1.jpg",
      bytes: 10,
      created_at: 0,
    });
    fetchMock.mockResolvedValueOnce(mockResponse(makeElementComment("c-new", PATH, 300)));
    const { frame, result } = setup();
    await loadFrame(frame);
    await sendFromFrame(frame, { type: "annotate:ready" });
    await enterMode(frame, result.current.toggle);

    await sendFromFrame(
      frame,
      pickedMessage({ screenshot: { dataUrl: jpegDataUrl(), width: 800, height: 400 } }),
    );
    expect(mocks.uploadFile).not.toHaveBeenCalled();
    await submitPick(result, "make it blue", "stack");
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    expect(mocks.uploadFile).toHaveBeenCalledTimes(1);
    const [sessionId, file] = mocks.uploadFile.mock.calls[0] as [string, File];
    expect(sessionId).toBe("conv_1");
    expect(file).toBeInstanceOf(File);
    expect(file.name).toMatch(/^annotation-\d+\.jpg$/);
    expect(file.type).toBe("image/jpeg");

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(init.body as string);
    const anchor = JSON.parse(body.anchor_content.slice(ELEMENT_ANCHOR_PREFIX.length));
    expect(anchor.screenshot).toEqual({
      file_id: "file_1",
      filename: "annotation-1.jpg",
      width: 800,
      height: 400,
    });
  });

  it("saves without a screenshot when the bytes do not match the type", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    fetchMock.mockResolvedValueOnce(mockResponse(makeElementComment("c-new", PATH, 300)));
    const { frame, result } = setup();
    await loadFrame(frame);
    await sendFromFrame(frame, { type: "annotate:ready" });
    await enterMode(frame, result.current.toggle);

    await sendFromFrame(
      frame,
      pickedMessage({
        screenshot: {
          dataUrl: `data:image/jpeg;base64,${btoa("not an image")}`,
          width: 800,
          height: 400,
        },
      }),
    );
    await submitPick(result, "no image", "stack");
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    expect(mocks.uploadFile).not.toHaveBeenCalled();
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(init.body as string);
    const anchor = JSON.parse(body.anchor_content.slice(ELEMENT_ANCHOR_PREFIX.length));
    expect(anchor.screenshot).toBeNull();
    expect(body.body).toBe("no image");
    expect(warn).toHaveBeenCalledTimes(1);
    warn.mockRestore();
  });

  it("saves without a screenshot when the upload rejects", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    mocks.uploadFile.mockRejectedValue(new Error("upload failed"));
    fetchMock.mockResolvedValueOnce(mockResponse(makeElementComment("c-new", PATH, 300)));
    const { frame, result } = setup();
    await loadFrame(frame);
    await sendFromFrame(frame, { type: "annotate:ready" });
    await enterMode(frame, result.current.toggle);

    await sendFromFrame(
      frame,
      pickedMessage({ screenshot: { dataUrl: jpegDataUrl(), width: 800, height: 400 } }),
    );
    await submitPick(result, "still saved", "stack");
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    expect(mocks.uploadFile).toHaveBeenCalledTimes(1);
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(init.body as string);
    const anchor = JSON.parse(body.anchor_content.slice(ELEMENT_ANCHOR_PREFIX.length));
    expect(anchor.screenshot).toBeNull();
    expect(body.body).toBe("still saved");
    expect(warn).toHaveBeenCalledTimes(1);
    warn.mockRestore();
  });
});
