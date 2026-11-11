// Tests for the comment-enabled HTML preview:
//   • standalone (no host fetcher): mints a panel URL, loads it as the iframe
//     `src`, re-mints once on a 410, surfaces non-OK statuses as inline errors.
//   • handshake: init carries the minted nonce; a `ready` with a linked page's
//     pathname fetches that page's source and lifts the joined workspace path.
//   • document replacement: a later iframe `load` clears the previous page's
//     selection and blocks commenting until the new page reports; the entry's
//     first handshake keeps a parent-applied selection.
//   • embed mode (host fetcher installed): keeps the client-injected srcdoc and
//     never mints.
//   • no `ready` within 5 s → an "unavailable" notice.

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { type ReactNode, startTransition, Suspense, useMemo, useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { showToast } from "@/components/ui/toast";
import type { Comment } from "@/hooks/useComments";
import { authenticatedFetch } from "@/lib/identity";
import * as host from "@/lib/host";
import { EmbeddedProvider } from "@/lib/embedded";
import { type ElementAnchorV1, encodeElementAnchor } from "./annotationAnchor";
import { FileViewerContext } from "./FileViewerContext";
import { HtmlCommentViewer, resolveFrameLinkPath } from "./HtmlCommentViewer";
import {
  type ActiveSelection,
  HTML_PANEL_SANDBOX,
  HTML_PREVIEW_SANDBOX,
} from "./codeViewerHelpers";
import { BRIDGE_MSG, BRIDGE_SOURCE } from "./htmlCommentBridge";
import { ANNOTATE_SOURCE } from "./htmlAnnotateBridge";
import { encodeRenderedTextAnchor } from "./renderedTextAnchor";
import { setOmnigentHostConfig, type HtmlPreviewFrameProps } from "@/lib/host";
import { connectHtmlPreviewBridge, requestedScrollRestore } from "@/test/htmlPreviewBridge";
import { getSavedScrollTop, saveScrollTop } from "./useScrollRestore";

// Permissions gate the floating "Add comment" button; default to editable.
vi.mock("@/hooks/usePermissions", () => ({ useCanEdit: vi.fn(() => true) }));
// The mint goes through authenticatedFetch; the artifact GET is a bare fetch.
vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));
vi.mock("@/lib/host", async (importOriginal) => ({
  ...(await importOriginal<typeof host>()),
  hasOmnigentHostFetcher: vi.fn(() => false),
}));
vi.mock("@/components/ui/toast", () => ({ showToast: vi.fn() }));

const toastMock = vi.mocked(showToast);

const NONCE = "nonce-1";
const ENTRY_URL = "/v1/artifacts/tok/index.html";
const ENTRY_PATH = "reports/index.html";
const LINKED_URL = "/v1/artifacts/tok/sub/page2.html";
const OTHER_URL = "/v1/artifacts/tokB/other.html";
const OTHER_PATH = "reports/other.html";
const OTHER_BODY = `<html><body><p>other</p><script data-omni-nonce="nonce-b">bridge()</script></body></html>`;

const ENTRY_BODY = `<html><body><p>entry</p><script data-omni-nonce="${NONCE}">bridge()</script></body></html>`;
const ENTRY_SOURCE = "<html><body><p>entry</p></body></html>";
const LINKED_BODY = `<html><body><p>page two</p><p>page two</p><script data-omni-nonce="${NONCE}">bridge()</script></body></html>`;
const LINKED_SOURCE = "<html><body><p>page two</p><p>page two</p></body></html>";

const authenticatedFetchMock = vi.mocked(authenticatedFetch);

function mintResponse(url = ENTRY_URL, nonce = NONCE, expiresAt: number | null = null) {
  return new Response(JSON.stringify({ url, nonce, kind: "bundle", expires_at: expiresAt }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

let artifactResponses: Map<string, Response>;
const defaultArtifactFetch = async (input: RequestInfo | URL) => {
  const url = typeof input === "string" ? input : input.toString();
  const res = artifactResponses.get(url);
  return res ? res.clone() : new Response("missing", { status: 404 });
};
const fetchMock = vi.fn(defaultArtifactFetch);

/** One artifact fetch whose resolution the test controls. */
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

/** React Query provider for the annotate hook's `useAddComment`. */
function Providers({ children }: { children: ReactNode }) {
  const queryClient = useMemo(
    () =>
      new QueryClient({
        defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
      }),
    [],
  );
  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
}

function ViewerContext({
  openFile,
  children,
}: {
  openFile: (path: string) => void;
  children: ReactNode;
}) {
  const value = useMemo(
    () => ({
      openFile,
      openGithubTab: () => {},
      isChangedPath: () => false,
      conversationId: "conv_1",
      workspaceRoot: "/abs/ws",
      workspaceHome: "/abs/home",
    }),
    [openFile],
  );
  return (
    <Providers>
      <FileViewerContext.Provider value={value}>{children}</FileViewerContext.Provider>
    </Providers>
  );
}

function renderViewer(
  props: {
    path?: string;
    content?: string;
    truncated?: boolean;
    embedded?: boolean;
    comments?: Comment[];
    onFrameChange?: (frame: { path: string; source: string } | null) => void;
    onAnnotationOrphansChange?: (ids: ReadonlySet<string>) => void;
    onSetActiveSelection?: (
      sel: { start_index: number; end_index: number; anchor_content: string } | null,
    ) => void;
    openFile?: (path: string) => void;
  } = {},
) {
  const onFrameChange = props.onFrameChange ?? vi.fn();
  const onAnnotationOrphansChange = props.onAnnotationOrphansChange ?? vi.fn();
  const onSetActiveSelection = props.onSetActiveSelection ?? vi.fn();
  const openFile = props.openFile ?? vi.fn();
  const viewer = (
    <ViewerContext openFile={openFile}>
      <HtmlCommentViewer
        conversationId="conv_1"
        path={props.path ?? ENTRY_PATH}
        content={props.content ?? ENTRY_SOURCE}
        truncated={props.truncated ?? false}
        comments={props.comments ?? []}
        activeSelection={null}
        onSetActiveSelection={onSetActiveSelection}
        onFrameChange={onFrameChange}
        onAnnotationOrphansChange={onAnnotationOrphansChange}
      />
    </ViewerContext>
  );
  const utils = render(props.embedded ? <EmbeddedProvider>{viewer}</EmbeddedProvider> : viewer);
  return { ...utils, onFrameChange, onAnnotationOrphansChange, onSetActiveSelection, openFile };
}

function makeAnchor(y: number): ElementAnchorV1 {
  return {
    v: 1,
    kind: "element",
    page: { url: "", title: "", vw: 800, vh: 600, sx: 0, sy: 0, dpr: 1 },
    target: {
      label: "button.option",
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
    rect: { x: 0, y, w: 120, h: 32 },
    region: null,
    selectedText: "",
    console: [],
    network: [],
    screenshot: null,
  };
}

/** One element-anchored comment row for `path` at vertical position `y`. */
function elementComment(id: string, path: string, y: number): Comment {
  const index = y * 10_000;
  return {
    id,
    conversation_id: "conv_1",
    path,
    start_index: index,
    end_index: index,
    body: "note",
    status: "draft",
    created_at: 0,
    updated_at: 0,
    anchor_content: encodeElementAnchor(makeAnchor(y)),
    created_by: null,
  };
}

/** The viewer for one path, for tests that rerender it onto another path. */
function viewerElement(path: string) {
  return (
    <Providers>
      <HtmlCommentViewer
        conversationId="conv_1"
        path={path}
        content={ENTRY_SOURCE}
        truncated={false}
        comments={[]}
        activeSelection={null}
        onSetActiveSelection={() => {}}
      />
    </Providers>
  );
}

/** Wait for the minted iframe, stub its `contentWindow`, and fire `load` after
 * flushing effects so the bridge's `load` listener is already attached. */
async function openFrame() {
  const iframe = (await screen.findByTitle("HTML preview")) as HTMLIFrameElement;
  const postMessage = vi.fn();
  const contentWindowFocus = vi.fn();
  Object.defineProperty(iframe, "contentWindow", {
    configurable: true,
    value: { postMessage, focus: contentWindowFocus },
  });
  await act(async () => {});
  fireEvent.load(iframe);
  return { iframe, postMessage, contentWindowFocus };
}

/** Init posts of one protocol, in load order. */
function initPosts(postMessage: ReturnType<typeof vi.fn>, type: string) {
  return postMessage.mock.calls.filter(([message]) => (message as { type?: string }).type === type);
}

/** Send one bridge message into the parent over the port transferred at init. */
async function sendFromFrame(
  postMessage: ReturnType<typeof vi.fn>,
  message: Record<string, unknown>,
  loadIndex = 0,
) {
  const init = initPosts(postMessage, BRIDGE_MSG.init)[loadIndex]!;
  const port = init[2][0] as MessagePort;
  await act(async () => {
    port.postMessage({
      source: BRIDGE_SOURCE,
      nonce: (init[0] as { nonce: string }).nonce,
      ...message,
    });
    await new Promise((resolve) => {
      setTimeout(resolve, 0);
    });
  });
}

/** Send one annotate-channel message over its own transferred port. */
async function sendAnnotateFromFrame(
  postMessage: ReturnType<typeof vi.fn>,
  message: Record<string, unknown>,
  loadIndex = 0,
) {
  const init = initPosts(postMessage, "annotate:init")[loadIndex]!;
  const port = init[2][0] as MessagePort;
  await act(async () => {
    port.postMessage({
      source: ANNOTATE_SOURCE,
      nonce: (init[0] as { nonce: string }).nonce,
      ...message,
    });
    await new Promise((resolve) => {
      setTimeout(resolve, 0);
    });
  });
}

/** Record the annotate messages the parent posts to the frame and answer its
 * runtime loads, so the hook's ensurePart flow completes as in a real frame. */
function observeAnnotatePort(postMessage: ReturnType<typeof vi.fn>, loadIndex = 0) {
  const init = initPosts(postMessage, "annotate:init")[loadIndex]!;
  const port = init[2][0] as MessagePort;
  const sent: Record<string, unknown>[] = [];
  port.onmessage = (event) => {
    const msg = event.data as Record<string, unknown>;
    sent.push(msg);
    if (msg.type === "annotate:loadRuntime") {
      port.postMessage({
        source: ANNOTATE_SOURCE,
        nonce: (init[0] as { nonce: string }).nonce,
        type: "annotate:runtimeLoaded",
        part: msg.part,
        ok: true,
      });
    }
  };
  return { port, sent };
}

function lastAnnotateOf(sent: Record<string, unknown>[], type: string) {
  for (let i = sent.length - 1; i >= 0; i--) {
    if (sent[i]!.type === type) return sent[i];
  }
  return undefined;
}

function annotateIds(message: Record<string, unknown> | undefined): string[] {
  const items = (message?.items ?? []) as { id: string }[];
  return items.map((item) => item.id);
}

beforeEach(() => {
  setOmnigentHostConfig({});
  vi.clearAllMocks();
  fetchMock.mockReset();
  fetchMock.mockImplementation(defaultArtifactFetch);
  vi.mocked(host.hasOmnigentHostFetcher).mockReturnValue(false);
  // A fresh Response per call: the re-mint test reads the body twice.
  authenticatedFetchMock.mockImplementation(async () => mintResponse());
  artifactResponses = new Map([
    [ENTRY_URL, new Response(ENTRY_BODY, { status: 200 })],
    [LINKED_URL, new Response(LINKED_BODY, { status: 200 })],
  ]);
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  setOmnigentHostConfig({});
  vi.useRealTimers();
  cleanup();
  vi.unstubAllGlobals();
  delete window.__OMNIGENT_BASE_PATH__;
});

function renderScrollViewer(
  content: string,
  truncated = false,
  embedded = false,
  scrollKey: string | null = null,
) {
  vi.mocked(host.hasOmnigentHostFetcher).mockReturnValue(true);
  const viewer = (
    <HtmlCommentViewer
      conversationId="conv_1"
      path="plan.html"
      scrollKey={scrollKey}
      content={content}
      truncated={truncated}
      comments={[]}
      activeSelection={null}
      onSetActiveSelection={() => {}}
    />
  );
  return render(embedded ? <EmbeddedProvider>{viewer}</EmbeddedProvider> : viewer, {
    wrapper: Providers,
  });
}

function HostPreviewFrame({ htmlContent, iframeRef, onLoad }: HtmlPreviewFrameProps) {
  return (
    <iframe
      title="Host preview"
      srcDoc={htmlContent}
      ref={iframeRef}
      onLoad={onLoad}
      sandbox="allow-scripts"
    />
  );
}

describe("HtmlCommentViewer embedded host frame", () => {
  it("warns when the host frame omits its iframe ref", () => {
    vi.mocked(host.hasOmnigentHostFetcher).mockReturnValue(true);
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    try {
      const FrameWithoutRef = ({ htmlContent, onLoad }: HtmlPreviewFrameProps) => (
        <iframe
          title="Host preview without ref"
          srcDoc={htmlContent}
          onLoad={onLoad}
          sandbox="allow-scripts"
        />
      );
      setOmnigentHostConfig({ htmlPreviewFrame: FrameWithoutRef });
      renderViewer({ content: "<p>Host artifact</p>", embedded: true });
      fireEvent.load(screen.getByTitle("Host preview without ref"));
      expect(warn).toHaveBeenCalledWith(
        "HTML comment bridge cannot connect: preview frame did not expose its content window.",
      );
    } finally {
      warn.mockRestore();
    }
  });

  it("prepares HTML and connects the bridge through the supplied iframe", () => {
    vi.mocked(host.hasOmnigentHostFetcher).mockReturnValue(true);
    setOmnigentHostConfig({ htmlPreviewFrame: HostPreviewFrame });
    renderViewer({
      content: "<html><head></head><body><p>Host artifact</p></body></html>",
      embedded: true,
    });
    const iframe = screen.getByTitle<HTMLIFrameElement>("Host preview");
    const srcDoc = iframe.getAttribute("srcdoc") ?? "";
    expect(srcDoc).toContain("<p>Host artifact</p>");
    expect(srcDoc).toContain("data-omni-nonce=");
    expect(srcDoc).not.toContain("htmlCommentBridgeRuntime.js");
    const postMessage = vi.spyOn(iframe.contentWindow!, "postMessage");
    fireEvent.load(iframe);
    expect(postMessage).toHaveBeenCalledWith(
      {
        source: BRIDGE_SOURCE,
        nonce: expect.any(String),
        type: BRIDGE_MSG.init,
        restoreScroll: { top: 0, budgetMs: 1500 },
      },
      "*",
      [expect.anything()],
    );
  });

  it("keeps the default embedded preview on an opaque origin", () => {
    vi.mocked(host.hasOmnigentHostFetcher).mockReturnValue(true);
    renderViewer({ content: "<p>doc</p>", embedded: true });
    const iframe = screen.getByTitle("HTML preview");
    expect(iframe.getAttribute("sandbox")).toContain("allow-scripts");
    expect(iframe.getAttribute("sandbox")).not.toContain("allow-same-origin");
  });
});

describe("HtmlCommentViewer standalone (artifact URL)", () => {
  it("reloads typed anchors in the frame and leaves legacy 0–0 text unresolved", async () => {
    const typed = encodeRenderedTextAnchor({
      v: 1,
      kind: "html",
      text: "Code",
      occ: 1,
      prefix: "Alpha Code Beta ",
      suffix: "",
      selector: "p:nth-of-type(2)",
    })!;
    const base = elementComment("typed", ENTRY_PATH, 100);
    const comments = [
      { ...base, start_index: 0, end_index: 0, anchor_content: typed },
      { ...base, id: "legacy-zero", start_index: 0, end_index: 0, anchor_content: "Code" },
      elementComment("element", ENTRY_PATH, 120),
      { ...base, id: "whole-page", start_index: 0, end_index: 0, anchor_content: null },
    ];
    const { onAnnotationOrphansChange } = renderViewer({ comments });
    const { postMessage } = await openFrame();
    const init = initPosts(postMessage, BRIDGE_MSG.init)[0]!;
    const port = init[2][0] as MessagePort;
    const messages: { type: string; comments?: unknown[] }[] = [];
    port.onmessage = (event) => messages.push(event.data);
    await sendFromFrame(postMessage, { type: BRIDGE_MSG.ready, pathname: ENTRY_URL });
    await waitFor(() => expect(messages.some((m) => m.type === BRIDGE_MSG.setComments)).toBe(true));
    expect(messages.find((m) => m.type === BRIDGE_MSG.setComments)?.comments).toEqual([
      {
        id: "typed",
        anchor_content: "Code",
        anchor: expect.objectContaining({ occ: 1, prefix: "Alpha Code Beta " }),
      },
      { id: "legacy-zero", anchor_content: "", occ: -1 },
    ]);
    await sendFromFrame(postMessage, {
      type: BRIDGE_MSG.orphans,
      ids: ["typed", "legacy-zero", "whole-page"],
    });
    await waitFor(() =>
      expect(onAnnotationOrphansChange).toHaveBeenLastCalledWith(new Set(["typed", "legacy-zero"])),
    );
  });

  it("mints the panel view and loads the URL as the iframe src, never srcdoc", async () => {
    window.__OMNIGENT_BASE_PATH__ = "/proxy/6767";
    artifactResponses.set("/proxy/6767" + ENTRY_URL, new Response(ENTRY_BODY, { status: 200 }));
    renderViewer();

    const iframe = await screen.findByTitle("HTML preview");
    expect(iframe.getAttribute("src")).toBe("/proxy/6767/v1/artifacts/tok/index.html");
    expect(iframe.getAttribute("srcdoc")).toBeNull();

    const sandbox = iframe.getAttribute("sandbox") ?? "";
    expect(sandbox).toBe(HTML_PANEL_SANDBOX);
    // The panel's artifact must not navigate the host page away.
    expect(sandbox).not.toContain("allow-top-navigation");
    expect(sandbox).toContain("allow-downloads");
    // Security-critical invariant: the artifact must never share the app origin.
    expect(sandbox).not.toContain("allow-same-origin");

    expect(authenticatedFetchMock).toHaveBeenCalledWith(
      "/v1/sessions/conv_1/artifacts",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ path: ENTRY_PATH, view: "panel" }),
      }),
    );
  });

  it("sends an absolute path host-rooted without its leading slash", async () => {
    renderViewer({ path: "/abs/reports/index.html" });
    await screen.findByTitle("HTML preview");

    const body = JSON.parse(authenticatedFetchMock.mock.calls[0][1]?.body as string);
    expect(body).toEqual({ path: "abs/reports/index.html", base: "host", view: "panel" });
  });

  it("re-mints once and retries when the first fetch is 410", async () => {
    let calls = 0;
    fetchMock.mockImplementation(async () => {
      calls += 1;
      return calls === 1
        ? new Response("<html>revoked</html>", { status: 410 })
        : new Response(ENTRY_BODY, { status: 200 });
    });
    renderViewer();

    await screen.findByTitle("HTML preview");
    expect(authenticatedFetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("stops after one retry and reports the revoked link", async () => {
    fetchMock.mockImplementation(async () => new Response("<html>revoked</html>", { status: 410 }));
    renderViewer();

    expect(await screen.findByText("Link revoked")).toBeTruthy();
    expect(screen.queryByTitle("HTML preview")).toBeNull();
    // Exactly one re-mint — no unbounded retry loop.
    expect(authenticatedFetchMock).toHaveBeenCalledTimes(2);
  });

  it("re-mints a near-expiry panel link and reloads the frame at its current page", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const expiresAt = Math.floor(Date.now() / 1000) + 5 * 60 + 30;
    const REFRESHED_URL = "/v1/artifacts/tok2/index.html";
    authenticatedFetchMock
      .mockResolvedValueOnce(mintResponse(ENTRY_URL, NONCE, expiresAt))
      .mockResolvedValueOnce(mintResponse(REFRESHED_URL, "nonce-2", expiresAt + 12 * 3600));
    artifactResponses.set(REFRESHED_URL, new Response(ENTRY_BODY, { status: 200 }));

    const { onFrameChange } = renderViewer();
    const { iframe, postMessage } = await openFrame();
    await sendFromFrame(postMessage, { type: BRIDGE_MSG.ready, pathname: LINKED_URL });
    await waitFor(() =>
      expect(onFrameChange).toHaveBeenLastCalledWith(
        expect.objectContaining({ path: "reports/sub/page2.html" }),
      ),
    );

    // Past the 5-minute lead: the fresh token's prefix serves the linked page.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(40_000);
    });

    await waitFor(() =>
      expect(iframe.getAttribute("src")).toBe("/v1/artifacts/tok2/sub/page2.html"),
    );
    expect(authenticatedFetchMock).toHaveBeenCalledTimes(2);

    // The reloaded document re-handshakes; an init carrying the stale nonce
    // would leave the bridge deaf and comments silently broken after a refresh.
    fireEvent.load(iframe);
    expect(postMessage).toHaveBeenCalledTimes(4);
    expect(initPosts(postMessage, BRIDGE_MSG.init)[1][0]).toMatchObject({
      source: BRIDGE_SOURCE,
      nonce: "nonce-2",
      type: BRIDGE_MSG.init,
    });
  });

  it("ignores a refresh that resolves after the viewer switched files", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const expiresAt = Math.floor(Date.now() / 1000) + 5 * 60 + 30;
    const refreshMint = deferred<Response>();
    let entryMints = 0;
    authenticatedFetchMock.mockImplementation(async (_input, init) => {
      const body = JSON.parse(String(init?.body)) as { path: string };
      if (body.path === OTHER_PATH) return mintResponse(OTHER_URL, "nonce-b");
      entryMints += 1;
      return entryMints === 1 ? mintResponse(ENTRY_URL, NONCE, expiresAt) : refreshMint.promise;
    });
    artifactResponses.set(OTHER_URL, new Response(OTHER_BODY, { status: 200 }));
    artifactResponses.set(
      "/v1/artifacts/tok2/index.html",
      new Response(ENTRY_BODY, { status: 200 }),
    );

    const { rerender } = render(viewerElement(ENTRY_PATH));
    await screen.findByTitle("HTML preview");

    // Start the near-expiry refresh, then switch files while it is in flight.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(40_000);
    });
    rerender(viewerElement(OTHER_PATH));
    await waitFor(() =>
      expect(screen.getByTitle("HTML preview").getAttribute("src")).toBe(OTHER_URL),
    );

    await act(async () => {
      refreshMint.resolve(
        mintResponse("/v1/artifacts/tok2/index.html", "nonce-2", expiresAt + 12 * 3600),
      );
      await new Promise((resolve) => {
        setTimeout(resolve, 0);
      });
    });

    expect(screen.getByTitle("HTML preview").getAttribute("src")).toBe(OTHER_URL);
  });

  it("ignores a refresh that fails after the viewer switched files", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const expiresAt = Math.floor(Date.now() / 1000) + 5 * 60 + 30;
    const refreshMint = deferred<Response>();
    let entryMints = 0;
    authenticatedFetchMock.mockImplementation(async (_input, init) => {
      const body = JSON.parse(String(init?.body)) as { path: string };
      if (body.path === OTHER_PATH) return mintResponse(OTHER_URL, "nonce-b");
      entryMints += 1;
      return entryMints === 1 ? mintResponse(ENTRY_URL, NONCE, expiresAt) : refreshMint.promise;
    });
    artifactResponses.set(OTHER_URL, new Response(OTHER_BODY, { status: 200 }));

    const { rerender } = render(viewerElement(ENTRY_PATH));
    await screen.findByTitle("HTML preview");

    await act(async () => {
      await vi.advanceTimersByTimeAsync(40_000);
    });
    rerender(viewerElement(OTHER_PATH));
    await waitFor(() =>
      expect(screen.getByTitle("HTML preview").getAttribute("src")).toBe(OTHER_URL),
    );

    await act(async () => {
      refreshMint.resolve(new Response("down", { status: 503 }));
      await new Promise((resolve) => {
        setTimeout(resolve, 0);
      });
    });

    expect(screen.queryByText("Host offline")).toBeNull();
    expect(screen.getByTitle("HTML preview").getAttribute("src")).toBe(OTHER_URL);
  });

  it("does not schedule a refresh when the panel link has no expiry", async () => {
    vi.useFakeTimers();
    renderViewer();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(24 * 60 * 60 * 1000);
    });

    expect(authenticatedFetchMock).toHaveBeenCalledTimes(1);
  });

  it.each([
    [404, "File not found"],
    [413, "File too large to preview (16777216 bytes limit)"],
    [502, "The session's host or runner needs an update"],
    [503, "Host offline"],
  ])("shows the %s status as an inline error with no iframe", async (status, message) => {
    fetchMock.mockImplementation(
      async () =>
        new Response("<html>err</html>", {
          status,
          headers: status === 413 ? { "X-Omnigent-Artifact-Max-Bytes": "16777216" } : {},
        }),
    );
    renderViewer();

    expect(await screen.findByText(message)).toBeTruthy();
    expect(screen.queryByTitle("HTML preview")).toBeNull();
    // Only a 410 triggers the retry.
    expect(authenticatedFetchMock).toHaveBeenCalledTimes(1);
  });

  it("hands the minted nonce to the frame in the bridge and annotate init messages", async () => {
    renderViewer();
    const { postMessage } = await openFrame();

    // One init per protocol on the same load: the comment bridge first, the
    // annotate stub second (its own MessageChannel).
    expect(postMessage).toHaveBeenCalledTimes(2);
    expect(postMessage.mock.calls[0][0]).toMatchObject({
      source: BRIDGE_SOURCE,
      nonce: NONCE,
      type: BRIDGE_MSG.init,
    });
    expect(postMessage.mock.calls[1][0]).toMatchObject({
      source: ANNOTATE_SOURCE,
      nonce: NONCE,
      type: "annotate:init",
    });
    expect(postMessage.mock.calls[1][2]).toHaveLength(1);
  });

  it("fetches a linked page and lifts the joined workspace path and stripped source", async () => {
    const { onFrameChange } = renderViewer();
    const { postMessage } = await openFrame();

    await sendFromFrame(postMessage, {
      type: BRIDGE_MSG.ready,
      pathname: LINKED_URL,
    });

    await waitFor(() =>
      expect(onFrameChange).toHaveBeenLastCalledWith({
        path: "reports/sub/page2.html",
        source: LINKED_SOURCE,
      }),
    );
    expect(fetchMock).toHaveBeenCalledWith(LINKED_URL);
  });

  it("joins a linked page onto an absolute bundle root", async () => {
    const { onFrameChange } = renderViewer({ path: "/abs/reports/index.html" });
    const { postMessage } = await openFrame();

    await sendFromFrame(postMessage, {
      type: BRIDGE_MSG.ready,
      pathname: LINKED_URL,
    });

    await waitFor(() =>
      expect(onFrameChange).toHaveBeenLastCalledWith({
        path: "/abs/reports/sub/page2.html",
        source: LINKED_SOURCE,
      }),
    );
  });

  it("anchors a selection after in-frame navigation against that page's source", async () => {
    const { onFrameChange, onSetActiveSelection } = renderViewer();
    const { postMessage } = await openFrame();

    await sendFromFrame(postMessage, { type: BRIDGE_MSG.ready, pathname: LINKED_URL });
    await waitFor(() =>
      expect(onFrameChange).toHaveBeenLastCalledWith(
        expect.objectContaining({ path: "reports/sub/page2.html" }),
      ),
    );

    // The second occurrence belongs to the linked page's rendered document.
    await sendFromFrame(postMessage, {
      type: BRIDGE_MSG.selection,
      text: "page two",
      occ: 1,
      prefix: "",
      suffix: "",
      rect: { left: 0, top: 0, right: 1, bottom: 1 },
    });
    fireEvent.click(await screen.findByRole("button", { name: /add comment/i }));

    expect(onSetActiveSelection).toHaveBeenCalledWith(
      expect.objectContaining({
        start_index: 0,
        end_index: 0,
        anchor_content: expect.stringContaining('"text":"page two","occ":1'),
      }),
    );
  });

  it("keeps the selection action inside the preview at its top right edge", async () => {
    renderViewer();
    const { iframe, postMessage } = await openFrame();
    vi.spyOn(iframe, "getBoundingClientRect").mockReturnValue(new DOMRect(600, 0, 380, 500));
    const measure = vi
      .spyOn(HTMLButtonElement.prototype, "getBoundingClientRect")
      .mockReturnValue(new DOMRect(970, -40, 140, 32));
    try {
      await sendFromFrame(postMessage, {
        type: BRIDGE_MSG.selection,
        text: "edge",
        occ: 0,
        prefix: "",
        suffix: "",
        rect: { left: 370, top: 0, right: 380, bottom: 20 },
      });
      const button = await screen.findByRole("button", { name: /add comment/i });
      // 140px action fits within the preview's 980px right edge with an 8px gap.
      expect(button).toHaveStyle({ left: "832px", top: "40px" });
    } finally {
      measure.mockRestore();
    }
  });

  it("re-fetches a linked page on a revisit and reports the new source", async () => {
    const v1 = "<html><body><p>page two v1</p></body></html>";
    const v2 = "<html><body><p>page two v2</p></body></html>";
    let linkedBody = v1;
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url === LINKED_URL) return new Response(linkedBody, { status: 200 });
      const res = artifactResponses.get(url);
      return res ? res.clone() : new Response("missing", { status: 404 });
    });

    const { onFrameChange } = renderViewer();
    const { postMessage } = await openFrame();

    await sendFromFrame(postMessage, { type: BRIDGE_MSG.ready, pathname: LINKED_URL });
    await waitFor(() =>
      expect(onFrameChange).toHaveBeenLastCalledWith({
        path: "reports/sub/page2.html",
        source: v1,
      }),
    );

    // Back to the entry, then to the linked page again after its file changed.
    await sendFromFrame(postMessage, { type: BRIDGE_MSG.ready, pathname: ENTRY_URL });
    await waitFor(() =>
      expect(onFrameChange).toHaveBeenLastCalledWith({
        path: ENTRY_PATH,
        source: ENTRY_SOURCE,
      }),
    );

    linkedBody = v2;
    await sendFromFrame(postMessage, { type: BRIDGE_MSG.ready, pathname: LINKED_URL });
    await waitFor(() =>
      expect(onFrameChange).toHaveBeenLastCalledWith({
        path: "reports/sub/page2.html",
        source: v2,
      }),
    );
    // The second visit fetched again instead of serving the first visit's bytes.
    expect(fetchMock.mock.calls.filter(([url]) => String(url) === LINKED_URL)).toHaveLength(2);
  });

  it("ignores selections while a linked page's source is pending", async () => {
    const pending = deferred<Response>();
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url === LINKED_URL) return pending.promise;
      const res = artifactResponses.get(url);
      return res ? res.clone() : new Response("missing", { status: 404 });
    });

    const { onFrameChange, onSetActiveSelection } = renderViewer();
    const { postMessage } = await openFrame();
    await waitFor(() =>
      expect(onFrameChange).toHaveBeenLastCalledWith({
        path: ENTRY_PATH,
        source: ENTRY_SOURCE,
      }),
    );

    await sendFromFrame(postMessage, { type: BRIDGE_MSG.ready, pathname: LINKED_URL });
    await sendFromFrame(postMessage, {
      type: BRIDGE_MSG.selection,
      text: "page two",
      occ: 1,
      prefix: "",
      suffix: "",
      rect: { left: 0, top: 0, right: 1, bottom: 1 },
    });

    // No source for the new page yet: no composer, no selection callback, and
    // the parent still sees the entry page.
    expect(screen.queryByRole("button", { name: /add comment/i })).toBeNull();
    expect(onSetActiveSelection).toHaveBeenLastCalledWith(null);
    expect(onFrameChange).toHaveBeenLastCalledWith({
      path: ENTRY_PATH,
      source: ENTRY_SOURCE,
    });

    await act(async () => {
      pending.resolve(new Response(LINKED_BODY, { status: 200 }));
      await new Promise((resolve) => {
        setTimeout(resolve, 0);
      });
    });
    await waitFor(() =>
      expect(onFrameChange).toHaveBeenLastCalledWith({
        path: "reports/sub/page2.html",
        source: LINKED_SOURCE,
      }),
    );

    await sendFromFrame(postMessage, {
      type: BRIDGE_MSG.selection,
      text: "page two",
      occ: 1,
      prefix: "",
      suffix: "",
      rect: { left: 0, top: 0, right: 1, bottom: 1 },
    });
    fireEvent.click(await screen.findByRole("button", { name: /add comment/i }));

    expect(onSetActiveSelection).toHaveBeenCalledWith(
      expect.objectContaining({
        start_index: 0,
        end_index: 0,
        anchor_content: expect.stringContaining('"text":"page two","occ":1'),
      }),
    );
  });

  it("clears the previous page's selection when a load replaces the document", async () => {
    const { onSetActiveSelection } = renderViewer();
    const { iframe, postMessage } = await openFrame();
    await sendFromFrame(postMessage, { type: BRIDGE_MSG.ready, pathname: ENTRY_URL });

    await sendFromFrame(postMessage, {
      type: BRIDGE_MSG.selection,
      text: "entry",
      occ: 0,
      prefix: "",
      suffix: "",
      rect: { left: 0, top: 0, right: 1, bottom: 1 },
    });
    expect(await screen.findByRole("button", { name: /add comment/i })).toBeTruthy();

    // A link click replaces the document; the replacement page never reports.
    fireEvent.load(iframe);

    expect(screen.queryByRole("button", { name: /add comment/i })).toBeNull();
    expect(onSetActiveSelection).toHaveBeenLastCalledWith(null);

    // Commenting stays blocked until the replacement page's source arrives.
    await sendFromFrame(
      postMessage,
      {
        type: BRIDGE_MSG.selection,
        text: "entry",
        occ: 0,
        prefix: "",
        suffix: "",
        rect: { left: 0, top: 0, right: 1, bottom: 1 },
      },
      1,
    );
    expect(screen.queryByRole("button", { name: /add comment/i })).toBeNull();
  });

  it("keeps a parent-set comment through the entry's initial load and ready", async () => {
    const selections: (ActiveSelection | null)[] = [];
    const linked: ActiveSelection = {
      start_index: 16,
      end_index: 21,
      anchor_content: "entry",
      comment_id: "c1",
    };
    function Parent() {
      const [active, setActive] = useState<ActiveSelection | null>(linked);
      return (
        <Providers>
          <HtmlCommentViewer
            conversationId="conv_1"
            path={ENTRY_PATH}
            content={ENTRY_SOURCE}
            truncated={false}
            comments={[]}
            activeSelection={active}
            onSetActiveSelection={(sel) => {
              selections.push(sel);
              setActive(sel);
            }}
          />
        </Providers>
      );
    }
    render(<Parent />);
    const { postMessage } = await openFrame();

    // FileViewer applies the ?comment= activation once, so the entry's first
    // handshake must not clear it.
    await sendFromFrame(postMessage, { type: BRIDGE_MSG.ready, pathname: ENTRY_URL });

    expect(selections).toEqual([]);
  });

  it("drops a linked page's source response from a previous mount", async () => {
    const pending = deferred<Response>();
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url === LINKED_URL) return pending.promise;
      const res = artifactResponses.get(url);
      return res ? res.clone() : new Response("missing", { status: 404 });
    });

    const onFrameChange = vi.fn();
    const onSetActiveSelection = vi.fn();
    const tree = (p: string) => (
      <Providers>
        <HtmlCommentViewer
          conversationId="conv_1"
          path={p}
          content={ENTRY_SOURCE}
          truncated={false}
          comments={[]}
          activeSelection={null}
          onSetActiveSelection={onSetActiveSelection}
          onFrameChange={onFrameChange}
        />
      </Providers>
    );
    const { rerender } = render(tree(ENTRY_PATH));
    const { postMessage } = await openFrame();
    await sendFromFrame(postMessage, { type: BRIDGE_MSG.ready, pathname: LINKED_URL });

    // The viewer reopens on the linked page's own path while that fetch is in
    // flight; the old response must not overwrite the new entry's source.
    rerender(tree("reports/sub/page2.html"));
    await waitFor(() =>
      expect(onFrameChange).toHaveBeenLastCalledWith({
        path: "reports/sub/page2.html",
        source: ENTRY_SOURCE,
      }),
    );

    await act(async () => {
      pending.resolve(
        new Response("<html><body><p>stale linked page</p></body></html>", { status: 200 }),
      );
      await new Promise((resolve) => {
        setTimeout(resolve, 0);
      });
    });
    expect(onFrameChange).toHaveBeenLastCalledWith({
      path: "reports/sub/page2.html",
      source: ENTRY_SOURCE,
    });
  });

  it("warns when the frame never reports ready, and clears it on a later ready", async () => {
    vi.useFakeTimers();
    renderViewer();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    const iframe = screen.getByTitle("HTML preview") as HTMLIFrameElement;
    const postMessage = vi.fn();
    Object.defineProperty(iframe, "contentWindow", {
      configurable: true,
      value: { postMessage },
    });
    fireEvent.load(iframe);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(screen.getByText("Comments are unavailable for this preview")).toBeTruthy();
    vi.useRealTimers();

    await sendFromFrame(postMessage, {
      type: BRIDGE_MSG.ready,
      pathname: `/v1/artifacts/tok/${ENTRY_PATH}`,
    });
    await waitFor(() =>
      expect(screen.queryByText("Comments are unavailable for this preview")).toBeNull(),
    );
  });

  it("drops the bridge ready timer when a failed refresh removes the frame", async () => {
    vi.useFakeTimers();
    // Refresh fires inside the 4 s ready window, so the timer is still pending
    // when the failure unmounts the frame.
    const expiresAt = Math.floor(Date.now() / 1000) + 5 * 60 + 3;
    authenticatedFetchMock
      .mockResolvedValueOnce(mintResponse(ENTRY_URL, NONCE, expiresAt))
      .mockResolvedValueOnce(new Response("down", { status: 503 }));
    renderViewer();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });

    const iframe = screen.getByTitle("HTML preview") as HTMLIFrameElement;
    const postMessage = vi.fn();
    Object.defineProperty(iframe, "contentWindow", {
      configurable: true,
      value: { postMessage },
    });
    fireEvent.load(iframe);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3_500);
    });
    expect(screen.queryByTitle("HTML preview")).toBeNull();
    expect(screen.getByText("Host offline")).toBeTruthy();

    // Past when the ready timer would have fired: it must not have outlived
    // the frame it belonged to.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
    });
    expect(screen.queryByText("Comments are unavailable for this preview")).toBeNull();
  });
});

describe("HtmlCommentViewer annotation toggle", () => {
  it("stays hidden until the annotate channel reports ready", async () => {
    renderViewer();
    const { postMessage } = await openFrame();

    expect(screen.queryByRole("button", { name: "Annotate" })).toBeNull();

    await sendAnnotateFromFrame(postMessage, { type: "annotate:ready" });

    const toggle = await screen.findByRole("button", { name: "Annotate" });
    expect(toggle).toHaveAttribute("aria-pressed", "false");
  });

  it("ignores a frame mode-on and follows the parent toggle and frame mode-off", async () => {
    renderViewer();
    const { postMessage } = await openFrame();
    observeAnnotatePort(postMessage);
    await sendAnnotateFromFrame(postMessage, { type: "annotate:ready" });

    const toggle = await screen.findByRole("button", { name: "Annotate" });
    // Only the parent turns the mode on; the frame cannot forge it.
    await sendAnnotateFromFrame(postMessage, {
      type: "annotate:modeChanged",
      on: true,
      reason: "user",
    });
    expect(toggle).toHaveAttribute("aria-pressed", "false");

    fireEvent.click(toggle);
    await waitFor(() => expect(toggle).toHaveAttribute("aria-pressed", "true"));

    await sendAnnotateFromFrame(postMessage, {
      type: "annotate:modeChanged",
      on: false,
      reason: "escape",
    });
    expect(toggle).toHaveAttribute("aria-pressed", "false");
  });

  it("flips aria-pressed on click without a frame echo", async () => {
    renderViewer();
    const { postMessage } = await openFrame();
    observeAnnotatePort(postMessage);
    await sendAnnotateFromFrame(postMessage, { type: "annotate:ready" });

    const toggle = await screen.findByRole("button", { name: "Annotate" });
    fireEvent.click(toggle);
    await waitFor(() => expect(toggle).toHaveAttribute("aria-pressed", "true"));

    fireEvent.click(toggle);
    await waitFor(() => expect(toggle).toHaveAttribute("aria-pressed", "false"));
  });
});

describe("HtmlCommentViewer annotation path", () => {
  it("uses the linked page's path and comments after an in-frame navigation", async () => {
    renderViewer({
      comments: [
        elementComment("c-entry", ENTRY_PATH, 100),
        elementComment("c-linked", "reports/sub/page2.html", 200),
      ],
    });
    const { postMessage } = await openFrame();
    const { sent } = observeAnnotatePort(postMessage);
    await sendAnnotateFromFrame(postMessage, { type: "annotate:ready" });
    await waitFor(() =>
      expect(annotateIds(lastAnnotateOf(sent, "annotate:setAnnotations"))).toEqual(["c-entry"]),
    );

    await sendFromFrame(postMessage, { type: BRIDGE_MSG.ready, pathname: LINKED_URL });
    await waitFor(() =>
      expect(annotateIds(lastAnnotateOf(sent, "annotate:setAnnotations"))).toEqual(["c-linked"]),
    );

    // A pick is only honoured while the parent holds the mode on.
    const toggle = await screen.findByRole("button", { name: "Annotate" });
    fireEvent.click(toggle);
    await waitFor(() => expect(toggle).toHaveAttribute("aria-pressed", "true"));

    authenticatedFetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify(elementComment("c-new", "reports/sub/page2.html", 300)), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    await sendAnnotateFromFrame(postMessage, {
      type: "annotate:picked",
      anchor: makeAnchor(300),
      screenshot: null,
      viewportRect: { x: 10, y: 20, w: 120, h: 32 },
    });

    const textarea = await screen.findByPlaceholderText("What should change?");
    fireEvent.change(textarea, { target: { value: "linked note" } });
    fireEvent.keyDown(textarea, { key: "Enter", metaKey: true });

    const post = await waitFor(() => {
      const call = authenticatedFetchMock.mock.calls.find(
        ([url]) => url === "/v1/sessions/conv_1/comments",
      );
      expect(call).toBeTruthy();
      return call!;
    });
    const body = JSON.parse((post[1] as RequestInit).body as string);
    expect(body.path).toBe("reports/sub/page2.html");
    expect(body.body).toBe("linked note");
  });

  it("saves under the linked page while its source fetch is still pending", async () => {
    const pending = deferred<Response>();
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url === LINKED_URL) return pending.promise;
      const res = artifactResponses.get(url);
      return res ? res.clone() : new Response("missing", { status: 404 });
    });

    renderViewer();
    const { postMessage } = await openFrame();
    observeAnnotatePort(postMessage);

    await sendFromFrame(postMessage, { type: BRIDGE_MSG.ready, pathname: LINKED_URL });

    authenticatedFetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify(elementComment("c-new", "reports/sub/page2.html", 300)), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    await sendAnnotateFromFrame(postMessage, { type: "annotate:ready" });

    // A pick is only honoured while the parent holds the mode on.
    const toggle = await screen.findByRole("button", { name: "Annotate" });
    fireEvent.click(toggle);
    await waitFor(() => expect(toggle).toHaveAttribute("aria-pressed", "true"));

    await sendAnnotateFromFrame(postMessage, {
      type: "annotate:picked",
      anchor: makeAnchor(300),
      screenshot: null,
      viewportRect: { x: 10, y: 20, w: 120, h: 32 },
    });

    const textarea = await screen.findByPlaceholderText("What should change?");
    fireEvent.keyDown(textarea, { key: "Enter" });

    const post = await waitFor(() => {
      const call = authenticatedFetchMock.mock.calls.find(
        ([url]) => url === "/v1/sessions/conv_1/comments",
      );
      expect(call).toBeTruthy();
      return call!;
    });
    const body = JSON.parse((post[1] as RequestInit).body as string);
    expect(body.path).toBe("reports/sub/page2.html");

    await act(async () => {
      pending.resolve(new Response(LINKED_BODY, { status: 200 }));
      await new Promise((resolve) => {
        setTimeout(resolve, 0);
      });
    });
  });

  it("renders the owner composer for a pick and returns focus to the frame", async () => {
    renderViewer();
    const { postMessage, contentWindowFocus } = await openFrame();
    const { sent } = observeAnnotatePort(postMessage);
    await sendAnnotateFromFrame(postMessage, { type: "annotate:ready" });

    const toggle = await screen.findByRole("button", { name: "Annotate" });
    fireEvent.click(toggle);
    await waitFor(() => expect(toggle).toHaveAttribute("aria-pressed", "true"));

    authenticatedFetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify(elementComment("c-new", ENTRY_PATH, 300)), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    await sendAnnotateFromFrame(postMessage, {
      type: "annotate:picked",
      anchor: makeAnchor(300),
      screenshot: null,
      viewportRect: { x: 10, y: 20, w: 120, h: 32 },
    });

    const composer = await screen.findByTestId("annotation-composer");
    expect(composer).toBeVisible();
    expect(composer.querySelector("[title]")!.textContent).toBe("button.option");
    const textarea = screen.getByPlaceholderText("What should change?");
    expect(textarea).toHaveFocus();
    fireEvent.change(textarea, { target: { value: "owner note" } });
    fireEvent.keyDown(textarea, { key: "Enter" });

    await waitFor(() => expect(screen.queryByTestId("annotation-composer")).toBeNull());
    expect(contentWindowFocus).toHaveBeenCalledTimes(1);
    expect(lastAnnotateOf(sent, "annotate:pickDone")).toBeDefined();

    // Escape cancels the next pick and returns focus the same way.
    await sendAnnotateFromFrame(postMessage, {
      type: "annotate:picked",
      anchor: makeAnchor(320),
      screenshot: null,
      viewportRect: { x: 10, y: 20, w: 120, h: 32 },
    });
    const second = await screen.findByPlaceholderText("What should change?");
    fireEvent.keyDown(second, { key: "Escape" });

    await waitFor(() => expect(screen.queryByTestId("annotation-composer")).toBeNull());
    expect(contentWindowFocus).toHaveBeenCalledTimes(2);
    expect(lastAnnotateOf(sent, "annotate:pickDone")).toBeDefined();
  });

  it("reports the ids whose anchors resolved as not found", async () => {
    const { onAnnotationOrphansChange } = renderViewer({
      comments: [elementComment("c-entry", ENTRY_PATH, 100)],
    });
    const { postMessage } = await openFrame();
    await sendAnnotateFromFrame(postMessage, { type: "annotate:ready" });

    await sendAnnotateFromFrame(postMessage, {
      type: "annotate:resolved",
      items: [
        { id: "c-entry", found: true },
        { id: "c-gone", found: false },
      ],
    });

    await waitFor(() =>
      expect(onAnnotationOrphansChange).toHaveBeenLastCalledWith(new Set(["c-gone"])),
    );
  });
});

describe("resolveFrameLinkPath", () => {
  const ROOT = "/abs/ws";
  const HOME = "/abs/home";
  const bundled = (tail: string) => `http://host/v1/artifacts/tok/${tail}`;
  const entry = "reports/index.html";

  it.each([
    ["page2.html", bundled("index.html"), entry, "reports/page2.html", "relative sibling"],
    [
      "../outside.html",
      bundled("sub/page2.html"),
      entry,
      "reports/outside.html",
      "../ escapes to a workspace file",
    ],
    [
      "../../outside.html",
      bundled("index.html"),
      "index.html",
      null,
      "../ climbs above the workspace root",
    ],
    ["../../x.html", bundled("sub/"), entry, "x.html", "<base href=sub/> base"],
    ["y.html", "http://host/x/", entry, "/x/y.html", "base outside the artifact route"],
    ["/abs/ws/x.html", bundled("index.html"), entry, "x.html", "host-absolute path"],
    ["file:///abs/ws/x.html", bundled("index.html"), entry, "x.html", "file: URL"],
    ["http://host/abs/ws/x.html", bundled("index.html"), entry, "x.html", "same-origin http URL"],
    ["http://other/abs/ws/x.html", bundled("index.html"), entry, null, "cross-origin http URL"],
    [
      "page2.html?q=1#sec",
      bundled("index.html"),
      entry,
      "reports/page2.html",
      "query and fragment stripped",
    ],
    ["", bundled("index.html"), entry, null, "empty href"],
    ["#sec", bundled("index.html"), entry, null, "fragment-only href"],
  ])("%s from %s -> %s (%s)", (href, base, entryPath, expected, _why) => {
    expect(resolveFrameLinkPath(href, base, entryPath, ROOT, HOME)).toBe(expected);
  });
});

describe("HtmlCommentViewer openPath handling", () => {
  function rects(iframe: HTMLIFrameElement) {
    vi.spyOn(iframe, "getClientRects").mockReturnValue({ length: 1 } as unknown as DOMRectList);
  }

  it("opens a resolved workspace file when the frame has client rects", async () => {
    const { openFile } = renderViewer();
    const { iframe, postMessage } = await openFrame();
    rects(iframe);

    await sendFromFrame(postMessage, {
      type: BRIDGE_MSG.openPath,
      href: "../outside.html",
      base: "http://host/v1/artifacts/tok/sub/page2.html",
    });

    expect(openFile).toHaveBeenCalledWith("reports/outside.html");
    expect(toastMock).not.toHaveBeenCalled();
  });

  it("ignores an openPath message while the frame is hidden", async () => {
    const { openFile } = renderViewer();
    const { postMessage } = await openFrame();

    await sendFromFrame(postMessage, {
      type: BRIDGE_MSG.openPath,
      href: "../outside.html",
      base: "http://host/v1/artifacts/tok/sub/page2.html",
    });

    expect(openFile).not.toHaveBeenCalled();
    expect(toastMock).not.toHaveBeenCalled();
  });

  it("toasts a link that escapes above the workspace root", async () => {
    const { openFile } = renderViewer({ path: "index.html" });
    const { iframe, postMessage } = await openFrame();
    rects(iframe);

    await sendFromFrame(postMessage, {
      type: BRIDGE_MSG.openPath,
      href: "../outside.html",
      base: "http://host/v1/artifacts/tok/index.html",
    });

    expect(openFile).not.toHaveBeenCalled();
    expect(toastMock).toHaveBeenCalledWith("Can't open ../outside.html");
  });
});

describe("HtmlCommentViewer embed mode", () => {
  it("keeps the srcdoc bridge and never mints when a host fetcher is installed", () => {
    vi.mocked(host.hasOmnigentHostFetcher).mockReturnValue(true);
    renderViewer({ content: "<html><head></head><body><p>doc</p></body></html>" });

    const iframe = screen.getByTitle("HTML preview");
    const srcdoc = iframe.getAttribute("srcdoc") ?? "";
    expect(srcdoc).toContain('<script data-omni-nonce="');
    expect(srcdoc).toContain("omni-html-comment");
    expect(srcdoc).toContain('<base target="_blank">');
    expect(iframe.getAttribute("src")).toBeNull();
    // The embed preview keeps the full-browser sandbox (out of scope here).
    const sandbox = iframe.getAttribute("sandbox") ?? "";
    expect(sandbox).toBe(HTML_PREVIEW_SANDBOX);
    expect(sandbox).toContain("allow-top-navigation");

    expect(authenticatedFetchMock).not.toHaveBeenCalled();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("injects the bridge inline in embed mode without an external script", () => {
    vi.mocked(host.hasOmnigentHostFetcher).mockReturnValue(true);
    renderViewer({ content: "<html><head></head><body><p>doc</p></body></html>" });

    const iframe = screen.getByTitle("HTML preview");
    const srcDoc = iframe.getAttribute("srcdoc") ?? "";
    expect(srcDoc).toContain("<script data-omni-nonce=");
    expect(srcDoc).not.toContain("<script src=");
  });

  it("loads the static bridge runtime externally when embedded", () => {
    vi.mocked(host.hasOmnigentHostFetcher).mockReturnValue(true);
    renderViewer({
      content: "<html><head></head><body><p>doc</p></body></html>",
      embedded: true,
    });

    const iframe = screen.getByTitle("HTML preview");
    const srcDoc = iframe.getAttribute("srcdoc") ?? "";
    expect(iframe.getAttribute("sandbox")).toContain("allow-scripts");
    expect(iframe.getAttribute("sandbox")).not.toContain("allow-same-origin");
    expect(srcDoc).toContain("<script src=");
    expect(srcDoc).toContain("omni-html-bridge");
    expect(srcDoc).toContain("data-omni-nonce=");
  });

  it("starts the diagnostic timer only after the iframe loads", () => {
    vi.mocked(host.hasOmnigentHostFetcher).mockReturnValue(true);
    vi.useFakeTimers();
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    renderViewer({ content: "<body><p>doc</p></body>" });

    const iframe = screen.getByTitle("HTML preview") as HTMLIFrameElement;
    const postMessage = vi.fn();
    Object.defineProperty(iframe, "contentWindow", {
      configurable: true,
      value: { postMessage },
    });

    expect(vi.getTimerCount()).toBe(0);
    act(() => vi.advanceTimersByTime(5_000));
    expect(warn).not.toHaveBeenCalled();

    act(() => fireEvent.load(iframe));
    expect(vi.getTimerCount()).toBeGreaterThan(0);
    act(() => vi.advanceTimersByTime(5_000));

    expect(warn).toHaveBeenCalledWith(
      "HTML comment bridge did not become ready; comments are unavailable.",
    );
    warn.mockRestore();
  });

  it("shows the truncated banner in embed mode", () => {
    vi.mocked(host.hasOmnigentHostFetcher).mockReturnValue(true);
    const viewer = (truncated: boolean) => (
      <Providers>
        <HtmlCommentViewer
          conversationId="conv_1"
          path={ENTRY_PATH}
          content="<body>x</body>"
          truncated={truncated}
          comments={[]}
          activeSelection={null}
          onSetActiveSelection={() => {}}
        />
      </Providers>
    );
    const { rerender } = render(viewer(false));
    expect(screen.queryByText(/truncated/i)).toBeNull();
    rerender(viewer(true));
    expect(screen.getByText(/truncated/i)).toBeTruthy();
  });
});

describe("HtmlCommentViewer scroll position", () => {
  it("saves the frame's reported offset and restores it when the preview remounts", async () => {
    const key = "html-preview:conv_1:plan.html";
    const first = renderScrollViewer("<body><p>plan</p></body>", false, false, key);
    expect(requestedScrollRestore()).toBeNull();
    const { port, send } = await connectHtmlPreviewBridge(
      screen.getByTitle<HTMLIFrameElement>("HTML preview"),
    );
    send({ type: BRIDGE_MSG.scroll, top: 760 });
    await waitFor(() => expect(getSavedScrollTop(key)).toBe(760));
    port.close();
    first.unmount();

    // Switching back to the tab or session mounts a fresh preview for the file.
    renderScrollViewer("<body><p>plan</p></body>", false, false, key);
    expect(requestedScrollRestore()).toMatchObject({ top: 760 });
  });

  it("returns to the saved offset when the agent rewrites the file", async () => {
    const key = "html-preview:conv_1:live.html";
    saveScrollTop(key, 300);
    const { rerender } = renderScrollViewer("<body><p>v1</p></body>", false, false, key);
    const rewrite = (content: string) =>
      rerender(
        <HtmlCommentViewer
          conversationId="conv_1"
          path="plan.html"
          scrollKey={key}
          content={content}
          truncated={false}
          comments={[]}
          activeSelection={null}
          onSetActiveSelection={() => {}}
        />,
      );
    saveScrollTop(key, 512);
    rewrite("<body><p>v2</p></body>");
    expect(requestedScrollRestore()).toMatchObject({ top: 512 });

    // The rewritten frame reports its own offset, which the next rewrite restores.
    const { send } = await connectHtmlPreviewBridge(
      screen.getByTitle<HTMLIFrameElement>("HTML preview"),
    );
    send({ type: BRIDGE_MSG.scroll, top: 140 });
    await waitFor(() => expect(getSavedScrollTop(key)).toBe(140));
    rewrite("<body><p>v3</p></body>");
    expect(requestedScrollRestore()).toMatchObject({ top: 140 });
  });

  it("saves a frame's offset under its own file while navigation to another file suspends", async () => {
    vi.mocked(host.hasOmnigentHostFetcher).mockReturnValue(true);
    const key = "html-preview:conv_1:suspended.html";
    const nextKey = "html-preview:conv_1:next.html";
    const pending = new Promise<void>(() => {});
    const attempted = vi.fn();
    function Suspend({ blocked }: { blocked: boolean }) {
      if (blocked) {
        attempted();
        throw pending;
      }
      return null;
    }
    const tree = (scrollKey: string, blocked: boolean) => (
      <Suspense fallback={<div>Loading navigation</div>}>
        <HtmlCommentViewer
          conversationId="conv_1"
          path="plan.html"
          scrollKey={scrollKey}
          content="<body><p>plan</p></body>"
          truncated={false}
          comments={[]}
          activeSelection={null}
          onSetActiveSelection={() => {}}
        />
        <Suspend blocked={blocked} />
      </Suspense>
    );
    const view = render(tree(key, false), { wrapper: Providers });
    const { send } = await connectHtmlPreviewBridge(
      screen.getByTitle<HTMLIFrameElement>("HTML preview"),
    );

    // The viewer renders for the next file, but the navigation never commits.
    act(() => {
      startTransition(() => {
        view.rerender(tree(nextKey, true));
      });
    });
    expect(attempted).toHaveBeenCalled();
    send({ type: BRIDGE_MSG.scroll, top: 420 });
    await waitFor(() => expect(getSavedScrollTop(key)).toBe(420));
    expect(getSavedScrollTop(nextKey)).toBeUndefined();
    view.rerender(tree(key, false));
  });
});

describe("HtmlCommentViewer zoom", () => {
  beforeEach(() => vi.mocked(host.hasOmnigentHostFetcher).mockReturnValue(true));
  /** Return the preview iframe and its zoom wrapper. */
  function previewParts() {
    const iframe = screen.getByTitle<HTMLIFrameElement>("HTML preview");
    return { iframe, zoomBox: iframe.parentElement as HTMLElement };
  }

  it("scales only the preview for zoom requests from the frame and shows the level", async () => {
    renderViewer({ content: "<body><p>doc</p></body>" });
    const { iframe, zoomBox } = previewParts();
    const { port, send } = await connectHtmlPreviewBridge(iframe);
    const badge = screen.getByTestId("html-preview-zoom");
    expect(zoomBox.style.transform).toBe("");
    expect(badge.textContent).toBe("");

    send({ type: BRIDGE_MSG.zoom, action: "in" });
    await waitFor(() => expect(zoomBox.style.transform).toBe("scale(1.1)"));
    expect(zoomBox.style.width).toBe(`${100 / 1.1}%`);
    expect(badge.textContent).toBe("Preview zoom 110%");

    send({ type: BRIDGE_MSG.zoom, action: "in" });
    await waitFor(() => expect(zoomBox.style.transform).toBe("scale(1.25)"));
    send({ type: BRIDGE_MSG.zoom, action: "out" });
    send({ type: BRIDGE_MSG.zoom, action: "out" });
    send({ type: BRIDGE_MSG.zoom, action: "out" });
    await waitFor(() => expect(zoomBox.style.transform).toBe("scale(0.9)"));
    expect(badge.textContent).toBe("Preview zoom 90%");

    send({ type: BRIDGE_MSG.zoom, action: "reset" });
    await waitFor(() => expect(zoomBox.style.transform).toBe(""));
    expect(badge.textContent).toBe("Preview zoom 100%");
    port.close();
  });

  it("hides the zoom level shortly after the last change", async () => {
    renderViewer({ content: "<body><p>doc</p></body>" });
    const { iframe, zoomBox } = previewParts();
    const { port, send } = await connectHtmlPreviewBridge(iframe);
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    send({ type: BRIDGE_MSG.zoom, action: "in" });
    await vi.waitFor(() => expect(zoomBox.style.transform).toBe("scale(1.1)"));
    act(() => vi.advanceTimersByTime(1_000));
    send({ type: BRIDGE_MSG.zoom, action: "in" });
    await vi.waitFor(() => expect(zoomBox.style.transform).toBe("scale(1.25)"));
    // The second change restarts the timer.
    act(() => vi.advanceTimersByTime(1_000));
    expect(screen.getByTestId("html-preview-zoom").textContent).toBe("Preview zoom 125%");
    act(() => vi.advanceTimersByTime(600));
    expect(screen.getByTestId("html-preview-zoom").textContent).toBe("");
    port.close();
  });

  it("keeps the zoom when the content reloads", async () => {
    const { rerender, openFile } = renderViewer({ content: "<body><p>first</p></body>" });
    const { send, port } = await connectHtmlPreviewBridge(previewParts().iframe);
    send({ type: BRIDGE_MSG.zoom, action: "in" });
    await waitFor(() => expect(previewParts().zoomBox.style.transform).toBe("scale(1.1)"));
    rerender(
      <ViewerContext openFile={openFile}>
        <HtmlCommentViewer
          conversationId="conv_1"
          path={ENTRY_PATH}
          content="<body><p>rewritten</p></body>"
          truncated={false}
          comments={[]}
          activeSelection={null}
          onSetActiveSelection={() => {}}
        />
      </ViewerContext>,
    );
    expect(previewParts().iframe.getAttribute("srcdoc")).toContain("rewritten");
    expect(previewParts().zoomBox.style.transform).toBe("scale(1.1)");
    port.close();
  });

  it("places the Add-comment button over a selection at the zoomed position", async () => {
    renderViewer({ content: "<body><p>Quarterly revenue</p></body>" });
    const { iframe, zoomBox } = previewParts();
    // jsdom has no layout; give the selection action's edge clamp a visible preview.
    vi.spyOn(iframe, "getBoundingClientRect").mockReturnValue(new DOMRect(0, 0, 800, 600));
    const { port, send } = await connectHtmlPreviewBridge(iframe);
    const selectRevenue = (reflow = false) =>
      send({
        type: BRIDGE_MSG.selection,
        text: "revenue",
        occ: 0,
        prefix: "",
        suffix: "",
        rect: { left: 40, top: 30, right: 90, bottom: 45 },
        reflow,
      });
    selectRevenue();
    await screen.findByRole("button", { name: "Add comment" });

    // A shortcut that leaves the level unchanged keeps the button in place.
    send({ type: BRIDGE_MSG.zoom, action: "reset" });
    await waitFor(() =>
      expect(screen.getByTestId("html-preview-zoom").textContent).toBe("Preview zoom 100%"),
    );
    expect(screen.getByRole("button", { name: "Add comment" })).toBeTruthy();

    // A real change hides it until the frame reports the reflowed selection.
    for (let step = 0; step < 5; step++) send({ type: BRIDGE_MSG.zoom, action: "in" });
    await waitFor(() => expect(zoomBox.style.transform).toBe("scale(2)"));
    expect(screen.queryByRole("button", { name: "Add comment" })).toBeNull();

    selectRevenue(true);
    const button = await screen.findByRole("button", { name: "Add comment" });
    // The frame is at the origin, so the button sits at the scaled rect.
    expect(button.style.left).toBe("80px");
    expect(button.style.top).toBe("54px");

    // Once dismissed, a later resize of the frame does not bring it back.
    fireEvent.mouseDown(document.body);
    expect(screen.queryByRole("button", { name: "Add comment" })).toBeNull();
    const stateFlushed = new Promise((resolve) => {
      port.onmessage = (event: MessageEvent<{ type: string }>) => {
        if (event.data.type === BRIDGE_MSG.setComments) resolve(null);
      };
    });
    selectRevenue(true);
    // The viewer answers ready with its state, after handling the earlier report.
    send({ type: BRIDGE_MSG.ready });
    await act(() => stateFlushed);
    expect(screen.queryByRole("button", { name: "Add comment" })).toBeNull();
    port.close();
  });

  it("does not activate a draft comment from a reflow report", async () => {
    const content = "<body><p>Quarterly revenue</p></body>";
    const start = content.indexOf("revenue");
    const draft: Comment = {
      id: "draft_1",
      conversation_id: "conv_1",
      path: "report.html",
      start_index: start,
      end_index: start + "revenue".length,
      body: "Check this",
      status: "draft",
      created_at: 0,
      updated_at: 0,
      anchor_content: "revenue",
      created_by: null,
    };
    const onSetActiveSelection = vi.fn();
    renderViewer({ content, comments: [draft], onSetActiveSelection });
    const { port, send } = await connectHtmlPreviewBridge(previewParts().iframe);
    send({
      type: BRIDGE_MSG.selection,
      text: "revenue",
      occ: 0,
      prefix: "",
      suffix: "",
      rect: { left: 40, top: 30, right: 90, bottom: 45 },
      reflow: true,
    });
    // A no-op reset only shows the badge, so it marks the earlier report as handled.
    send({ type: BRIDGE_MSG.zoom, action: "reset" });
    await waitFor(() =>
      expect(screen.getByTestId("html-preview-zoom").textContent).toBe("Preview zoom 100%"),
    );
    expect(onSetActiveSelection).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "Add comment" })).toBeNull();
    port.close();
  });
});
