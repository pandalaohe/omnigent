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
//   • no `ready` within 4 s → an "unavailable" notice.

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { authenticatedFetch } from "@/lib/identity";
import * as host from "@/lib/host";
import { HtmlCommentViewer } from "./HtmlCommentViewer";
import { type ActiveSelection, HTML_PREVIEW_SANDBOX } from "./codeViewerHelpers";
import { BRIDGE_MSG, BRIDGE_SOURCE } from "./htmlCommentBridge";

// Permissions gate the floating "Add comment" button; default to editable.
vi.mock("@/hooks/usePermissions", () => ({ useCanEdit: vi.fn(() => true) }));
// The mint goes through authenticatedFetch; the artifact GET is a bare fetch.
vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));
vi.mock("@/lib/host", async (importOriginal) => ({
  ...(await importOriginal<typeof host>()),
  hasOmnigentHostFetcher: vi.fn(() => false),
}));

const NONCE = "nonce-1";
const ENTRY_URL = "/v1/artifacts/tok/index.html";
const ENTRY_PATH = "reports/index.html";
const LINKED_URL = "/v1/artifacts/tok/sub/page2.html";

const ENTRY_BODY = `<html><body><p>entry</p><script data-omni-nonce="${NONCE}">bridge()</script></body></html>`;
const ENTRY_SOURCE = "<html><body><p>entry</p></body></html>";
const LINKED_BODY = `<html><body><p>page two</p><p>page two</p><script data-omni-nonce="${NONCE}">bridge()</script></body></html>`;
const LINKED_SOURCE = "<html><body><p>page two</p><p>page two</p></body></html>";

const authenticatedFetchMock = vi.mocked(authenticatedFetch);

function mintResponse(url = ENTRY_URL, nonce = NONCE) {
  return new Response(JSON.stringify({ url, nonce, kind: "bundle" }), {
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

function renderViewer(
  props: {
    path?: string;
    content?: string;
    truncated?: boolean;
    onFrameChange?: (frame: { path: string; source: string } | null) => void;
    onSetActiveSelection?: (
      sel: { start_index: number; end_index: number; anchor_content: string } | null,
    ) => void;
  } = {},
) {
  const onFrameChange = props.onFrameChange ?? vi.fn();
  const onSetActiveSelection = props.onSetActiveSelection ?? vi.fn();
  const utils = render(
    <HtmlCommentViewer
      conversationId="conv_1"
      path={props.path ?? ENTRY_PATH}
      content={props.content ?? ENTRY_SOURCE}
      truncated={props.truncated ?? false}
      comments={[]}
      activeSelection={null}
      onSetActiveSelection={onSetActiveSelection}
      onFrameChange={onFrameChange}
    />,
  );
  return { ...utils, onFrameChange, onSetActiveSelection };
}

/** Wait for the minted iframe, stub its `contentWindow`, and fire `load`. */
async function openFrame() {
  const iframe = (await screen.findByTitle("HTML preview")) as HTMLIFrameElement;
  const postMessage = vi.fn();
  Object.defineProperty(iframe, "contentWindow", {
    configurable: true,
    value: { postMessage },
  });
  fireEvent.load(iframe);
  return { iframe, postMessage };
}

/** Send one bridge message into the parent over the port transferred at init. */
async function sendFromFrame(
  postMessage: ReturnType<typeof vi.fn>,
  message: Record<string, unknown>,
  loadIndex = 0,
) {
  const init = postMessage.mock.calls[loadIndex][0] as { nonce: string };
  const port = postMessage.mock.calls[loadIndex][2][0] as MessagePort;
  await act(async () => {
    port.postMessage({ source: BRIDGE_SOURCE, nonce: init.nonce, ...message });
    await new Promise((resolve) => {
      setTimeout(resolve, 0);
    });
  });
}

beforeEach(() => {
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
  cleanup();
  vi.unstubAllGlobals();
  delete window.__OMNIGENT_BASE_PATH__;
});

describe("HtmlCommentViewer standalone (artifact URL)", () => {
  it("mints the panel view and loads the URL as the iframe src, never srcdoc", async () => {
    window.__OMNIGENT_BASE_PATH__ = "/proxy/6767";
    artifactResponses.set("/proxy/6767" + ENTRY_URL, new Response(ENTRY_BODY, { status: 200 }));
    renderViewer();

    const iframe = await screen.findByTitle("HTML preview");
    expect(iframe.getAttribute("src")).toBe("/proxy/6767/v1/artifacts/tok/index.html");
    expect(iframe.getAttribute("srcdoc")).toBeNull();

    const sandbox = iframe.getAttribute("sandbox") ?? "";
    expect(sandbox).toBe(HTML_PREVIEW_SANDBOX);
    expect(sandbox).toContain("allow-top-navigation");
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

  it.each([
    [404, "File not found"],
    [413, "File too large to preview (10 MiB limit)"],
    [502, "The session's host or runner needs an update"],
    [503, "Host offline"],
  ])("shows the %s status as an inline error with no iframe", async (status, message) => {
    fetchMock.mockImplementation(async () => new Response("<html>err</html>", { status }));
    renderViewer();

    expect(await screen.findByText(message)).toBeTruthy();
    expect(screen.queryByTitle("HTML preview")).toBeNull();
    // Only a 410 triggers the retry.
    expect(authenticatedFetchMock).toHaveBeenCalledTimes(1);
  });

  it("hands the minted nonce to the frame in the init message", async () => {
    renderViewer();
    const { postMessage } = await openFrame();

    expect(postMessage).toHaveBeenCalledTimes(1);
    expect(postMessage.mock.calls[0][0]).toMatchObject({
      source: BRIDGE_SOURCE,
      nonce: NONCE,
      type: BRIDGE_MSG.init,
    });
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

    // The second occurrence of the anchor only resolves against page2's source;
    // against the entry's source findAnchorInSource would fall back to 0.
    await sendFromFrame(postMessage, {
      type: BRIDGE_MSG.selection,
      text: "page two",
      occ: 1,
      rect: { left: 0, top: 0, right: 1, bottom: 1 },
    });
    fireEvent.click(await screen.findByRole("button", { name: /add comment/i }));

    const expectedStart = LINKED_SOURCE.lastIndexOf("page two");
    expect(expectedStart).toBeGreaterThan(0);
    expect(onSetActiveSelection).toHaveBeenCalledWith({
      start_index: expectedStart,
      end_index: expectedStart + "page two".length,
      anchor_content: "page two",
    });
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
      rect: { left: 0, top: 0, right: 1, bottom: 1 },
    });
    fireEvent.click(await screen.findByRole("button", { name: /add comment/i }));

    const expectedStart = LINKED_SOURCE.lastIndexOf("page two");
    expect(expectedStart).toBeGreaterThan(0);
    expect(onSetActiveSelection).toHaveBeenCalledWith({
      start_index: expectedStart,
      end_index: expectedStart + "page two".length,
      anchor_content: "page two",
    });
  });

  it("clears the previous page's selection when a load replaces the document", async () => {
    const { onSetActiveSelection } = renderViewer();
    const { iframe, postMessage } = await openFrame();
    await sendFromFrame(postMessage, { type: BRIDGE_MSG.ready, pathname: ENTRY_URL });

    await sendFromFrame(postMessage, {
      type: BRIDGE_MSG.selection,
      text: "entry",
      occ: 0,
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
      await vi.advanceTimersByTimeAsync(4000);
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

    expect(authenticatedFetchMock).not.toHaveBeenCalled();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("shows the truncated banner in embed mode", () => {
    vi.mocked(host.hasOmnigentHostFetcher).mockReturnValue(true);
    const { rerender } = render(
      <HtmlCommentViewer
        conversationId="conv_1"
        path={ENTRY_PATH}
        content="<body>x</body>"
        truncated={false}
        comments={[]}
        activeSelection={null}
        onSetActiveSelection={() => {}}
      />,
    );
    expect(screen.queryByText(/truncated/i)).toBeNull();
    rerender(
      <HtmlCommentViewer
        conversationId="conv_1"
        path={ENTRY_PATH}
        content="<body>x</body>"
        truncated={true}
        comments={[]}
        activeSelection={null}
        onSetActiveSelection={() => {}}
      />,
    );
    expect(screen.getByText(/truncated/i)).toBeTruthy();
  });
});
