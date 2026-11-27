import { focusManager, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";
import type * as HostModule from "@/lib/host";
import type * as NativeBridgeModule from "@/lib/nativeBridge";

vi.mock("@/hooks/RunnerHealthProvider", () => ({
  useSessionRunnerOnline: vi.fn(),
  useSessionHostOnline: vi.fn(),
}));
vi.mock("@/store/chatStore", () => ({
  useChatStore: vi.fn(),
}));
vi.mock("@/lib/host", async (importOriginal) => ({
  ...(await importOriginal<typeof HostModule>()),
  isDatabricksWorkspace: vi.fn(() => false),
}));
vi.mock("@/lib/nativeBridge", async (importOriginal) => ({
  ...(await importOriginal<typeof NativeBridgeModule>()),
  isAndroidShell: vi.fn(() => false),
  isIOSShell: vi.fn(() => false),
}));

import { useSessionHostOnline, useSessionRunnerOnline } from "@/hooks/RunnerHealthProvider";
import { isDatabricksWorkspace } from "@/lib/host";
import { isAndroidShell, isIOSShell } from "@/lib/nativeBridge";
import { useChatStore } from "@/store/chatStore";
import {
  BINARY_PREVIEW_MAX_BYTES,
  canDownloadImage,
  downloadImage,
  downloadWorkspaceFile,
  FILE_CONTENT_FAILING_POLL_MS,
  FILE_CONTENT_POLL_MAX_BYTES,
  FILE_CONTENT_POLL_MS,
  FilePreviewTooLargeError,
  fetchWorkspaceBinaryPreview,
  fetchWorkspaceVideoBlob,
  fileContentPollInterval,
  fileContentToBlob,
  readBoundedBinaryPreviewResponse,
  triggerBrowserDownload,
  useBinaryFilePreview,
  useFileContent,
} from "./useFileContent";
import type { FileContentResponse } from "./useFileContent";
import { useWriteFileContent } from "./useWriteFileContent";

const onlineMock = vi.mocked(useSessionRunnerOnline);
const hostOnlineMock = vi.mocked(useSessionHostOnline);
const chatStoreMock = vi.mocked(useChatStore);
const fetchMock = vi.fn();

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: "OK",
    json: async () => body,
  } as unknown as Response;
}

function Wrap({ children }: { children: ReactNode }) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: 0 } },
  });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

function Probe({ id, path }: { id: string | undefined; path: string | null }) {
  useFileContent(id, path);
  return null;
}

async function flushMicrotasks() {
  await new Promise((resolve) => {
    setTimeout(resolve, 10);
  });
}

interface ChatStoreState {
  conversationId: string | null;
  sessionStatus: "idle" | "running" | "waiting" | "failed";
}

// `useChatStore` is a selector hook: state in, derived value out.
// Our consumer reads `s.conversationId` and `s.sessionStatus`.
function stubChatStore(state: Partial<ChatStoreState> = {}) {
  const full: ChatStoreState = {
    conversationId: null,
    sessionStatus: "idle",
    ...state,
  };
  chatStoreMock.mockImplementation((selector: unknown) => {
    if (typeof selector === "function") {
      return (selector as (s: ChatStoreState) => unknown)(full);
    }
    return undefined;
  });
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockResolvedValue(
    jsonResponse({
      object: "session.environment.filesystem.file_content",
      path: "a.txt",
      content_type: "text/plain",
      encoding: "utf-8",
      content: "hi",
      bytes: 2,
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  // restore (not just reset) so any `vi.spyOn` a test installs is removed even
  // if that test threw before its own cleanup — otherwise a leaked
  // `document.createElement` spy is recaptured as the "original" by the next
  // test and recurses infinitely.
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// fileContentToBlob
// ---------------------------------------------------------------------------

describe("fileContentToBlob", () => {
  it("produces a utf-8 blob with the server content_type", () => {
    const data: FileContentResponse = {
      object: "session.environment.filesystem.file_content",
      path: "hello.txt",
      content_type: "text/plain",
      encoding: "utf-8",
      content: "hello world",
      bytes: 11,
    };
    const blob = fileContentToBlob(data);
    expect(blob.type).toBe("text/plain");
    expect(blob.size).toBe(11);
  });

  it("falls back to text/plain when content_type is null for utf-8 files", () => {
    const data: FileContentResponse = {
      object: "session.environment.filesystem.file_content",
      path: "no-mime.txt",
      content_type: null,
      encoding: "utf-8",
      content: "x",
      bytes: 1,
    };
    expect(fileContentToBlob(data).type).toBe("text/plain");
  });

  it("decodes a base64 payload and uses the server content_type", () => {
    // "abc" in base64 is "YWJj"
    const data: FileContentResponse = {
      object: "session.environment.filesystem.file_content",
      path: "img.png",
      content_type: "image/png",
      encoding: "base64",
      content: "YWJj",
      bytes: 3,
    };
    const blob = fileContentToBlob(data);
    expect(blob.type).toBe("image/png");
    expect(blob.size).toBe(3);
  });

  it("falls back to application/octet-stream for base64 files without content_type", () => {
    const data: FileContentResponse = {
      object: "session.environment.filesystem.file_content",
      path: "bin.dat",
      content_type: null,
      encoding: "base64",
      content: "YWJj",
      bytes: 3,
    };
    expect(fileContentToBlob(data).type).toBe("application/octet-stream");
  });
});

// ---------------------------------------------------------------------------
// triggerBrowserDownload
// ---------------------------------------------------------------------------

describe("triggerBrowserDownload", () => {
  it("creates and clicks a synthetic link then removes it", () => {
    const blob = new Blob(["data"], { type: "text/plain" });
    const createdLinks: HTMLAnchorElement[] = [];
    const origCreateElement = document.createElement.bind(document);
    vi.spyOn(document, "createElement").mockImplementation((tag: string) => {
      const el = origCreateElement(tag);
      if (tag === "a") {
        vi.spyOn(el as HTMLAnchorElement, "click");
        createdLinks.push(el as HTMLAnchorElement);
      }
      return el;
    });
    vi.stubGlobal("URL", {
      createObjectURL: vi.fn(() => "blob:fake"),
      revokeObjectURL: vi.fn(),
    });

    triggerBrowserDownload(blob, "output.txt");

    expect(createdLinks).toHaveLength(1);
    const link = createdLinks[0];
    expect(link.download).toBe("output.txt");
    expect(link.href).toContain("blob:fake");
    expect(link.click).toHaveBeenCalledOnce();
    // Element must have been removed from the DOM.
    expect(document.body.contains(link)).toBe(false);
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:fake");

    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });
});

// ---------------------------------------------------------------------------
// downloadWorkspaceFile
// ---------------------------------------------------------------------------

function blobResponse(blob: Blob): Response {
  return {
    ok: true,
    status: 200,
    statusText: "OK",
    blob: async () => blob,
  } as unknown as Response;
}

/** Capture every synthetic download link the code clicks, as `[href, download]`. */
function captureDownloadClicks(): [string, string][] {
  const clicked: [string, string][] = [];
  const origCreateElement = document.createElement.bind(document);
  vi.spyOn(document, "createElement").mockImplementation((tag: string) => {
    const el = origCreateElement(tag);
    if (tag === "a")
      vi.spyOn(el as HTMLAnchorElement, "click").mockImplementation(() => {
        const a = el as HTMLAnchorElement;
        clicked.push([a.getAttribute("href") ?? "", a.download]);
      });
    return el;
  });
  return clicked;
}

const DOWNLOAD_URL =
  "/v1/sessions/sess_123/resources/environments/default/filesystem/src/main.py?download=true";

describe("downloadWorkspaceFile", () => {
  it("fetches complete video bytes with cancellation and encoded host paths", async () => {
    fetchMock.mockResolvedValueOnce(
      new Response("complete recording", { headers: { "content-type": "video/webm" } }),
    );
    const signal = new AbortController().signal;
    const video = await fetchWorkspaceVideoBlob("sess_123", "/Users/me/demo clip.webm", signal);
    expect(video.type).toBe("video/webm");
    expect(await video.text()).toBe("complete recording");
    expect(fetchMock).toHaveBeenCalledWith(
      "/v1/sessions/sess_123/resources/environments/default/filesystem/Users/me/demo%20clip.webm?download=true&base=host",
      expect.objectContaining({ signal, cache: "no-store" }),
    );
  });

  it("cancels the video body on an HTTP error", async () => {
    const cancel = vi.fn();
    const body = new ReadableStream<Uint8Array>({ cancel });
    fetchMock.mockResolvedValueOnce(
      new Response(body, { status: 503, statusText: "Service Unavailable" }),
    );
    await expect(fetchWorkspaceVideoBlob("sess_123", "demo.webm")).rejects.toThrow(
      "503 Service Unavailable",
    );
    await waitFor(() => expect(cancel).toHaveBeenCalledOnce());
  });

  it("clicks a link to the raw download URL so the browser streams it to disk", async () => {
    // A same-origin navigation shows in the browser's download UI at once and
    // never buffers the file in the page, unlike a fetch-then-Blob.
    const clicked = captureDownloadClicks();

    await downloadWorkspaceFile("sess_123", "src/main.py");

    expect(clicked).toEqual([[DOWNLOAD_URL, "main.py"]]);
    expect(fetchMock).not.toHaveBeenCalled();

    vi.restoreAllMocks();
  });

  it.each([
    ["the managed embed", isDatabricksWorkspace],
    ["the iOS shell", isIOSShell],
    ["the Android shell", isAndroidShell],
  ])("fetches the bytes through the app's transport in %s", async (_label, predicate) => {
    // The embed host supplies auth headers and replica routing that a bare
    // navigation lacks, and the mobile shells save http(s) downloads outside
    // the WebView's session, so the bytes come through authenticatedFetch.
    vi.mocked(predicate).mockReturnValueOnce(true);
    const file = new Blob(["print('hi')"], { type: "text/x-python" });
    fetchMock.mockResolvedValueOnce(blobResponse(file));
    const clicked = captureDownloadClicks();
    const createObjectURL = vi.fn(() => "blob:x");
    vi.stubGlobal("URL", { createObjectURL, revokeObjectURL: vi.fn() });

    await downloadWorkspaceFile("sess_123", "src/main.py");

    // authenticatedFetch adds auth headers and `cache: "no-store"` (see
    // lib/identity.ts) — assert the URL plus that cache-bypass init.
    expect(fetchMock).toHaveBeenCalledWith(
      DOWNLOAD_URL,
      expect.objectContaining({ cache: "no-store" }),
    );
    expect(createObjectURL).toHaveBeenCalledWith(file);
    expect(clicked).toEqual([["blob:x", "main.py"]]);

    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("names the host base for an absolute path alongside download=true", async () => {
    const clicked = captureDownloadClicks();

    await downloadWorkspaceFile("sess_abc", "/Users/me/reports/q3.pdf");

    expect(clicked).toEqual([
      [
        "/v1/sessions/sess_abc/resources/environments/default/filesystem/Users/me/reports/q3.pdf?download=true&base=host",
        "q3.pdf",
      ],
    ]);

    vi.restoreAllMocks();
  });
  it("propagates fetch errors to the caller", async () => {
    vi.mocked(isDatabricksWorkspace).mockReturnValueOnce(true);
    fetchMock.mockResolvedValueOnce({
      ok: false,
      status: 404,
      statusText: "Not Found",
    } as Response);

    await expect(downloadWorkspaceFile("sess_x", "missing.txt")).rejects.toThrow("404");
  });
});

describe("canDownloadImage", () => {
  it.each([
    ["an in-memory blob source in any case", "BLOB:http://app/1", {}, true],
    ["a data source", "data:image/png;base64,AA==", {}, true],
    [
      "a data source of a type it can name, in any case",
      "DATA:IMAGE/SVG+XML,%3Csvg%2F%3E",
      {},
      true,
    ],
    ["a data source of a type it can't name", "data:image/x-unknown;base64,AA==", {}, false],
    ["a data source with no type", "data:;base64,AA==", {}, false],
    ["held bytes for any source", "https://example.com/a.png", { blob: new Blob(["x"]) }, true],
    [
      "an app API path",
      "/base/v1/x",
      { apiPath: "/v1/sessions/s/resources/files/f/content" },
      true,
    ],
    ["an empty API path", "/v1/x", { apiPath: "" }, false],
    ["a remote URL as the API path", "/v1/x", { apiPath: "https://example.com/a.png" }, false],
    ["a remote source with nothing else", "https://example.com/a.png", {}, false],
  ])("decides %s (%s)", (_label, src, source, expected) => {
    expect(canDownloadImage(src, source)).toBe(expected);
  });

  it("accepts nothing before the source resolves", () => {
    expect(canDownloadImage(undefined, { blob: new Blob(["x"]) })).toBe(false);
  });

  it("types an extensionless name from the bytes the Android shell fetches", async () => {
    vi.mocked(isAndroidShell).mockReturnValueOnce(true);
    const image = new Blob(["png"], { type: "image/png" });
    fetchMock.mockResolvedValueOnce(blobResponse(image));
    const clicked = captureDownloadClicks();
    vi.stubGlobal("URL", {
      createObjectURL: vi.fn(() => "blob:fetched"),
      revokeObjectURL: vi.fn(),
    });

    await downloadImage("/v1/x", "file_123", {
      apiPath: "/v1/sessions/s/resources/files/file_123/content",
    });

    expect(clicked).toEqual([["blob:fetched", "file_123.png"]]);
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("refuses to fetch a source it can't save, without a request", async () => {
    await expect(downloadImage("https://example.com/a.png", "a.png")).rejects.toThrow();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it.each([
    ["a page name on SVG bytes", "invoice.html", "image/svg+xml", "invoice.html.svg"],
    ["a last dot that isn't an extension", "Figure 1.2", "image/png", "Figure 1.2.png"],
    ["another image type's extension", "chart.svg", "image/png", "chart.svg.png"],
    ["a matching extension in any case", "photo.JPEG", "image/jpeg", "photo.JPEG"],
    ["a type it can't name", "notes", "application/octet-stream", "notes"],
  ])("names held bytes for their type: %s", async (_label, name, type, saved) => {
    const clicked = captureDownloadClicks();
    vi.stubGlobal("URL", { createObjectURL: vi.fn(() => "blob:held"), revokeObjectURL: vi.fn() });

    await downloadImage("blob:shown", name, { blob: new Blob(["x"], { type }) });

    expect(clicked).toEqual([["blob:held", saved]]);
  });

  it("names a data image for the type it declares", async () => {
    const clicked = captureDownloadClicks();
    const src = "data:image/svg+xml;base64,PHN2Zy8+";

    await downloadImage(src, "invoice.html");

    expect(clicked).toEqual([[src, "invoice.html.svg"]]);
  });

  it("won't save a data image of a type it can't name", async () => {
    const clicked = captureDownloadClicks();

    await expect(
      downloadImage("data:image/x-unknown;base64,AA==", "invoice.html"),
    ).rejects.toThrow();

    expect(clicked).toEqual([]);
  });

  describe("in the iOS shell, whose web view can't save a download", () => {
    afterEach(() => {
      vi.mocked(isIOSShell).mockReturnValue(false);
    });

    it.each([
      ["a blob source", "blob:shown", {}],
      ["a data source", "data:image/png;base64,AA==", {}],
      ["held bytes", "blob:shown", { blob: new Blob(["x"], { type: "image/png" }) }],
      ["an app API path", "/v1/x", { apiPath: "/v1/sessions/s/resources/files/f/content" }],
    ])("offers no save for %s", async (_label, src, source) => {
      vi.mocked(isIOSShell).mockReturnValue(true);
      const clicked = captureDownloadClicks();

      expect(canDownloadImage(src, source)).toBe(false);
      await expect(downloadImage(src, "shot.png", source)).rejects.toThrow();

      expect(clicked).toEqual([]);
      expect(fetchMock).not.toHaveBeenCalled();
    });
  });
});

// ---------------------------------------------------------------------------
// useFileContent gating
// ---------------------------------------------------------------------------

describe("useFileContent gating", () => {
  it("does not fetch when the runner is offline and the host is also down", async () => {
    onlineMock.mockReturnValue(false);
    hostOnlineMock.mockReturnValue(false);
    stubChatStore();

    render(
      <Wrap>
        <Probe id="conv_dead" path="src/a.txt" />
      </Wrap>,
    );
    await flushMicrotasks();

    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("fetches when the runner is offline but the host can serve the file", async () => {
    // Regression: opening a file while the agent is asleep must still fetch
    // — the server reads the bytes over the host tunnel. A blank viewer here
    // is exactly the bug this gate fix addresses.
    onlineMock.mockReturnValue(false);
    hostOnlineMock.mockReturnValue(true);
    stubChatStore();

    render(
      <Wrap>
        <Probe id="conv_asleep" path="src/a.txt" />
      </Wrap>,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());

    expect(fetchMock.mock.calls[0][0]).toBe(
      "/v1/sessions/conv_asleep/resources/environments/default/filesystem/src/a.txt",
    );
  });

  it("fetches an absolute file path by its bare path plus base=host", async () => {
    // A file opened while browsing OUTSIDE the workspace has an absolute path.
    // It must be requested WITHOUT a leading "%2F" (which a reverse proxy such
    // as the Databricks Apps front door would merge away) and named
    // `base=host` -- otherwise it is looked up by its bare name under the
    // workspace root and 404s (the regression this covers).
    onlineMock.mockReturnValue(true);
    hostOnlineMock.mockReturnValue(true);
    stubChatStore();

    render(
      <Wrap>
        <Probe id="conv_abs" path="/tmp/dbexec.log" />
      </Wrap>,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());

    expect(fetchMock.mock.calls[0][0]).toBe(
      "/v1/sessions/conv_abs/resources/environments/default/filesystem/tmp/dbexec.log?base=host",
    );
  });

  it("does not fetch when path is null", async () => {
    onlineMock.mockReturnValue(true);
    stubChatStore();

    render(
      <Wrap>
        <Probe id="conv_live" path={null} />
      </Wrap>,
    );
    await flushMicrotasks();

    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("fetches when online and a path is given", async () => {
    onlineMock.mockReturnValue(true);
    stubChatStore();

    render(
      <Wrap>
        <Probe id="conv_live" path="src/a.txt" />
      </Wrap>,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());

    expect(fetchMock.mock.calls[0][0]).toBe(
      "/v1/sessions/conv_live/resources/environments/default/filesystem/src/a.txt",
    );
  });

  it("fetches when status is unknown (undefined)", async () => {
    onlineMock.mockReturnValue(undefined);
    stubChatStore();

    render(
      <Wrap>
        <Probe id="conv_unknown" path="src/a.txt" />
      </Wrap>,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
  });

  it("does not fetch when conversationId is undefined", async () => {
    onlineMock.mockReturnValue(true);
    stubChatStore();

    render(
      <Wrap>
        <Probe id={undefined} path="src/a.txt" />
      </Wrap>,
    );
    await flushMicrotasks();

    // Undefined conversationId gates the enabled flag — no session to fetch from.
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("does not fetch for a temp id while the new session is still being created", async () => {
    // The file viewer can stay open across a navigate-first new chat; its
    // `temp:*` id names no server session, so the request could only fail.
    onlineMock.mockReturnValue(true);
    hostOnlineMock.mockReturnValue(true);
    stubChatStore({ conversationId: "temp:0a1b2c3d", sessionStatus: "running" });

    render(
      <Wrap>
        <Probe id="temp:0a1b2c3d" path="src/a.txt" />
      </Wrap>,
    );
    await flushMicrotasks();

    expect(fetchMock).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// useFileContent trailing invalidation
// ---------------------------------------------------------------------------
//
// Each test creates one QueryClient and one StableWrap component that closes
// over it.  Both render() and rerender() receive the *same* StableWrap
// reference, so React reconciles the provider in place (no remount, same
// client).  Using the Wrap helper instead would create a new QueryClient on
// every rerender call, racing with the invalidation under test.

// Returns a wrapper component that always uses the given QueryClient.
// Call once per test and reuse the returned reference for both render()
// and rerender() so the QueryClient is stable across all renders.
function stableWrap(qc: QueryClient) {
  return function StableWrap({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
  };
}

describe("useFileContent trailing invalidation", () => {
  it("refetches once when session transitions from running to idle", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 0 } } });
    const StableWrap = stableWrap(qc);
    onlineMock.mockReturnValue(true);
    stubChatStore({ conversationId: "conv_a", sessionStatus: "running" });

    const { rerender } = render(
      <StableWrap>
        <Probe id="conv_a" path="src/a.txt" />
      </StableWrap>,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    // Transition: running → idle — trailing invalidation must fire one refetch.
    stubChatStore({ conversationId: "conv_a", sessionStatus: "idle" });
    rerender(
      <StableWrap>
        <Probe id="conv_a" path="src/a.txt" />
      </StableWrap>,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
  });

  it("refetches once when session transitions from waiting to idle", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 0 } } });
    const StableWrap = stableWrap(qc);
    onlineMock.mockReturnValue(true);
    stubChatStore({ conversationId: "conv_b", sessionStatus: "waiting" });

    const { rerender } = render(
      <StableWrap>
        <Probe id="conv_b" path="src/b.txt" />
      </StableWrap>,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    stubChatStore({ conversationId: "conv_b", sessionStatus: "idle" });
    rerender(
      <StableWrap>
        <Probe id="conv_b" path="src/b.txt" />
      </StableWrap>,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
  });

  it("does not refetch when session stays idle", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 0 } } });
    const StableWrap = stableWrap(qc);
    onlineMock.mockReturnValue(true);
    stubChatStore({ conversationId: "conv_c", sessionStatus: "idle" });

    const { rerender } = render(
      <StableWrap>
        <Probe id="conv_c" path="src/c.txt" />
      </StableWrap>,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    // Still idle — no extra invalidation.
    rerender(
      <StableWrap>
        <Probe id="conv_c" path="src/c.txt" />
      </StableWrap>,
    );
    await flushMicrotasks();

    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("invalidates the current path when path changes mid-session and session goes idle", async () => {
    // If the user navigates to a different file while the agent is running,
    // the trailing invalidation must fire for the path that is open when
    // the session finishes — not for the path that was open when the session
    // started.
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 0 } } });
    const StableWrap = stableWrap(qc);
    onlineMock.mockReturnValue(true);
    stubChatStore({ conversationId: "conv_nav", sessionStatus: "running" });

    const { rerender } = render(
      <StableWrap>
        <Probe id="conv_nav" path="src/old.txt" />
      </StableWrap>,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    // Navigate to a different file while the session is still running.
    rerender(
      <StableWrap>
        <Probe id="conv_nav" path="src/new.txt" />
      </StableWrap>,
    );
    // src/new.txt now fetches for the first time (fetch #2).
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));

    // Session goes idle — trailing invalidation fires for the current path (src/new.txt).
    stubChatStore({ conversationId: "conv_nav", sessionStatus: "idle" });
    rerender(
      <StableWrap>
        <Probe id="conv_nav" path="src/new.txt" />
      </StableWrap>,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    // Verify the last refetch targeted the current path, not the old one.
    expect(fetchMock.mock.calls[2][0]).toContain("src/new.txt");
  });

  it("does not refetch a second time when re-rendering while still idle", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 0 } } });
    const StableWrap = stableWrap(qc);
    onlineMock.mockReturnValue(true);
    stubChatStore({ conversationId: "conv_d", sessionStatus: "running" });

    const { rerender } = render(
      <StableWrap>
        <Probe id="conv_d" path="src/d.txt" />
      </StableWrap>,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    // First transition: running → idle — trailing invalidation fires (fetch #2).
    stubChatStore({ conversationId: "conv_d", sessionStatus: "idle" });
    rerender(
      <StableWrap>
        <Probe id="conv_d" path="src/d.txt" />
      </StableWrap>,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));

    // Second re-render while still idle — must NOT fire a third fetch.
    rerender(
      <StableWrap>
        <Probe id="conv_d" path="src/d.txt" />
      </StableWrap>,
    );
    await flushMicrotasks();

    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});

// ---------------------------------------------------------------------------
// useFileContent refresh while on screen
// ---------------------------------------------------------------------------

function textFile(content: string, bytes = content.length): FileContentResponse {
  return {
    object: "session.environment.filesystem.file_content",
    path: "notes.md",
    content_type: "text/markdown",
    encoding: "utf-8",
    content,
    bytes,
  };
}

describe("fileContentPollInterval", () => {
  it.each([
    ["a small text file", textFile("hi"), false, FILE_CONTENT_POLL_MS],
    [
      "a text file at the size limit",
      textFile("", FILE_CONTENT_POLL_MAX_BYTES),
      false,
      FILE_CONTENT_POLL_MS,
    ],
    ["a small text file whose refresh fails", textFile("hi"), true, FILE_CONTENT_FAILING_POLL_MS],
    [
      "a larger or truncated text file",
      textFile("", FILE_CONTENT_POLL_MAX_BYTES + 1),
      false,
      false,
    ],
    ["a binary preview", { ...textFile("YWJj"), encoding: "base64" as const }, false, false],
    ["a file that has not loaded", undefined, false, false],
  ])("%s", (_name, data, failing, expected) => {
    expect(fileContentPollInterval(data, failing)).toBe(expected);
  });
});

describe("useFileContent on-screen refresh", () => {
  // Fake timers drive both React Query's poll interval and its notify scheduler.
  beforeEach(() => {
    vi.useFakeTimers();
    onlineMock.mockReturnValue(true);
    stubChatStore({ conversationId: "conv_poll", sessionStatus: "idle" });
  });
  afterEach(() => {
    focusManager.setFocused(undefined);
    vi.useRealTimers();
  });

  function ScreenProbe({ visible }: { visible: boolean }) {
    useFileContent("conv_poll", "notes.md", { visible });
    return null;
  }

  it("re-reads an idle session's file only while it is on screen", async () => {
    const StableWrap = stableWrap(
      new QueryClient({ defaultOptions: { queries: { retry: false } } }),
    );
    const view = (visible: boolean) => (
      <StableWrap>
        <ScreenProbe visible={visible} />
      </StableWrap>
    );
    const { rerender } = render(view(true));
    await vi.advanceTimersByTimeAsync(0);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(FILE_CONTENT_POLL_MS);
    expect(fetchMock).toHaveBeenCalledTimes(2);

    rerender(view(false));
    await vi.advanceTimersByTimeAsync(FILE_CONTENT_POLL_MS * 3);
    expect(fetchMock).toHaveBeenCalledTimes(2);

    // Shown again: catch up at once, then keep polling.
    rerender(view(true));
    await vi.advanceTimersByTimeAsync(0);
    expect(fetchMock).toHaveBeenCalledTimes(3);
    await vi.advanceTimersByTimeAsync(FILE_CONTENT_POLL_MS);
    expect(fetchMock).toHaveBeenCalledTimes(4);
  });

  it.each([
    ["a large text file", textFile("", FILE_CONTENT_POLL_MAX_BYTES + 1)],
    ["a binary preview", { ...textFile("YWJj"), encoding: "base64" as const }],
  ])("does not poll %s, but re-reads it when shown again", async (_name, file) => {
    fetchMock.mockResolvedValue(jsonResponse(file));
    const StableWrap = stableWrap(
      new QueryClient({ defaultOptions: { queries: { retry: false } } }),
    );
    const view = (visible: boolean) => (
      <StableWrap>
        <ScreenProbe visible={visible} />
      </StableWrap>
    );
    const { rerender } = render(view(true));
    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(FILE_CONTENT_POLL_MS * 3);
    expect(fetchMock).toHaveBeenCalledTimes(1);

    rerender(view(false));
    rerender(view(true));
    await vi.advanceTimersByTimeAsync(0);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("re-checks less often while refreshes fail, e.g. after the file is deleted", async () => {
    render(
      <Wrap>
        <ScreenProbe visible />
      </Wrap>,
    );
    await vi.advanceTimersByTimeAsync(0);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    fetchMock.mockResolvedValue({ ok: false, status: 404, statusText: "Not Found" } as Response);
    await vi.advanceTimersByTimeAsync(FILE_CONTENT_POLL_MS);
    expect(fetchMock).toHaveBeenCalledTimes(2);

    await vi.advanceTimersByTimeAsync(FILE_CONTENT_POLL_MS * 3);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    // Recovery: once a re-check succeeds, polling returns to the normal interval.
    fetchMock.mockResolvedValue(jsonResponse(textFile("back")));
    await vi.advanceTimersByTimeAsync(FILE_CONTENT_FAILING_POLL_MS);
    const afterRecovery = fetchMock.mock.calls.length;
    expect(afterRecovery).toBeGreaterThan(2);
    await vi.advanceTimersByTimeAsync(FILE_CONTENT_POLL_MS);
    expect(fetchMock).toHaveBeenCalledTimes(afterRecovery + 1);
  });

  it("pauses in a hidden browser tab and catches up when the tab returns", async () => {
    render(
      <Wrap>
        <ScreenProbe visible />
      </Wrap>,
    );
    await vi.advanceTimersByTimeAsync(0);
    expect(fetchMock).toHaveBeenCalledTimes(1);

    // Hidden for less than staleTime: returning still re-reads at once.
    act(() => focusManager.setFocused(false));
    await vi.advanceTimersByTimeAsync(FILE_CONTENT_POLL_MS * 2);
    expect(fetchMock).toHaveBeenCalledTimes(1);

    act(() => focusManager.setFocused(true));
    await vi.advanceTimersByTimeAsync(0);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});

// ---------------------------------------------------------------------------
// useFileContent reads never overlap this tab's own write
// ---------------------------------------------------------------------------

describe("useFileContent and this tab's writes", () => {
  const KEY = ["file-content", "conv_w", "notes.md"];
  let serverContent: string;
  let finishWrite: () => void;
  let failWrite: () => void;
  let answerRead: (content: string) => void;
  let holdReads: boolean;

  // A one-file server: a write lands when finishWrite() runs; held reads answer
  // whenever the test chooses.
  beforeEach(() => {
    onlineMock.mockReturnValue(true);
    stubChatStore();
    serverContent = "before";
    holdReads = false;
    fetchMock.mockImplementation((_url: string, init?: RequestInit) => {
      if (init?.method === "PUT") {
        const { content } = JSON.parse(init.body as string) as { content: string };
        return new Promise((resolve) => {
          finishWrite = () => {
            serverContent = content;
            resolve(jsonResponse({}));
          };
          failWrite = () =>
            resolve({ ok: false, status: 500, statusText: "Internal Server Error" } as Response);
        });
      }
      if (!holdReads) return Promise.resolve(jsonResponse(textFile(serverContent)));
      return new Promise((resolve) => {
        answerRead = (content) => resolve(jsonResponse(textFile(content)));
      });
    });
  });

  const calls = (method: "GET" | "PUT") =>
    fetchMock.mock.calls.filter(([, init]) => (init?.method ?? "GET") === method).length;

  function renderFile() {
    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    const hook = renderHook(
      () => ({
        read: useFileContent("conv_w", "notes.md"),
        write: useWriteFileContent("conv_w"),
      }),
      { wrapper: stableWrap(qc) },
    );
    return { qc, ...hook };
  }

  it("waits for the write before re-reading the file", async () => {
    const { qc, result } = renderFile();
    await waitFor(() => expect(result.current.read.data?.content).toBe("before"));
    let saving!: Promise<void>;
    act(() => {
      saving = result.current.write.mutateAsync({ path: "notes.md", content: "saved" });
    });
    await waitFor(() => expect(calls("PUT")).toBe(1));

    void qc.invalidateQueries({ queryKey: KEY });
    await flushMicrotasks();
    expect(calls("GET")).toBe(1);

    await act(async () => {
      finishWrite();
      await saving;
    });
    await waitFor(() => expect(calls("GET")).toBeGreaterThan(1));
    await waitFor(() => expect(result.current.read.data?.content).toBe("saved"));
  });

  it("still re-reads after a write that fails", async () => {
    const { qc, result } = renderFile();
    await waitFor(() => expect(result.current.read.data?.content).toBe("before"));
    let saving!: Promise<void>;
    act(() => {
      saving = result.current.write.mutateAsync({ path: "notes.md", content: "rejected" });
    });
    await waitFor(() => expect(calls("PUT")).toBe(1));
    void qc.invalidateQueries({ queryKey: KEY });
    await flushMicrotasks();
    expect(calls("GET")).toBe(1);

    await act(async () => {
      failWrite();
      await saving.catch(() => {});
    });
    await waitFor(() => expect(calls("GET")).toBeGreaterThan(1));
    expect(result.current.read.data?.content).toBe("before");
  });

  it("never sends a read that was cancelled while waiting for the write", async () => {
    const { qc, result } = renderFile();
    await waitFor(() => expect(result.current.read.data?.content).toBe("before"));
    let saving!: Promise<void>;
    act(() => {
      saving = result.current.write.mutateAsync({ path: "notes.md", content: "saved" });
    });
    await waitFor(() => expect(calls("PUT")).toBe(1));
    void qc.refetchQueries({ queryKey: KEY });
    await flushMicrotasks();
    await qc.cancelQueries({ queryKey: KEY });

    holdReads = true;
    await act(async () => {
      finishWrite();
      await saving;
    });
    // Only the save's own follow-up read goes out; the cancelled one never did.
    await waitFor(() => expect(calls("GET")).toBe(2));
    await flushMicrotasks();
    expect(calls("GET")).toBe(2);
  });

  it("drops a read that was in flight when the write started", async () => {
    const { qc, result } = renderFile();
    await waitFor(() => expect(result.current.read.data?.content).toBe("before"));
    holdReads = true;
    void qc.invalidateQueries({ queryKey: KEY });
    await waitFor(() => expect(calls("GET")).toBe(2));
    let saving!: Promise<void>;
    act(() => {
      saving = result.current.write.mutateAsync({ path: "notes.md", content: "saved" });
    });
    await waitFor(() => expect(calls("PUT")).toBe(1));

    // The request is aborted, and an answer sent anyway is ignored.
    const [, heldReadInit] = fetchMock.mock.calls
      .filter(([, init]) => init?.method !== "PUT")
      .at(-1)!;
    expect((heldReadInit as RequestInit).signal?.aborted).toBe(true);
    answerRead("saved");
    await flushMicrotasks();
    expect(qc.getQueryData<FileContentResponse>(KEY)?.content).toBe("before");

    holdReads = false;
    await act(async () => {
      finishWrite();
      await saving;
    });
    expect(qc.getQueryData<FileContentResponse>(KEY)?.content).toBe("saved");
  });
});

// ---------------------------------------------------------------------------
// fetchWorkspaceBinaryPreview / useBinaryFilePreview
// ---------------------------------------------------------------------------

function streamResponse(
  chunks: Uint8Array[],
  headers: Record<string, string> = {},
): { response: Response; cancelSpy: ReturnType<typeof vi.fn> } {
  const cancelSpy = vi.fn(() => Promise.resolve());
  let idx = 0;
  const body = new ReadableStream<Uint8Array>({
    pull(controller) {
      if (idx < chunks.length) {
        controller.enqueue(chunks[idx++]);
      } else {
        controller.close();
      }
    },
    cancel() {
      return cancelSpy();
    },
  });
  const response = new Response(body, { status: 200, headers });
  return { response, cancelSpy };
}

describe("fetchWorkspaceBinaryPreview", () => {
  it("streams raw bytes from ?download=true without calling atob", async () => {
    const atobSpy = vi.spyOn(globalThis, "atob");
    const partA = new Uint8Array([0x89, 0x50, 0x4e, 0x47]);
    const partB = new Uint8Array([0x0d, 0x0a, 0x1a, 0x0a]);
    const { response } = streamResponse([partA, partB], {
      "content-type": "image/png",
      "content-length": "8",
    });
    fetchMock.mockResolvedValueOnce(response);

    const result = await fetchWorkspaceBinaryPreview("sess_raw", "assets/photo.png");

    expect(fetchMock).toHaveBeenCalledWith(
      "/v1/sessions/sess_raw/resources/environments/default/filesystem/assets/photo.png?download=true&wake=false",
      expect.objectContaining({ cache: "no-store" }),
    );
    expect(result.contentType).toBe("image/png");
    expect(result.bytes).toBe(8);
    expect(new Uint8Array(await result.blob.arrayBuffer())).toEqual(
      new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    );
    expect(atobSpy).not.toHaveBeenCalled();
  });

  it("carries base=host for host-absolute paths and maps SVG octet-stream to image/svg+xml", async () => {
    const svgBytes = new TextEncoder().encode("<svg/>");
    const { response } = streamResponse([svgBytes], {
      "content-type": "application/octet-stream",
    });
    fetchMock.mockResolvedValueOnce(response);

    const result = await fetchWorkspaceBinaryPreview("sess_raw", "/opt/work/icon.svg");

    expect(fetchMock).toHaveBeenCalledWith(
      "/v1/sessions/sess_raw/resources/environments/default/filesystem/opt/work/icon.svg?download=true&wake=false&base=host",
      expect.objectContaining({ cache: "no-store" }),
    );
    expect(result.contentType).toBe("image/svg+xml");
    expect(result.blob.type).toBe("image/svg+xml");
  });

  it("rejects immediately and cancels the stream when Content-Length exceeds maxBytes", async () => {
    const { response, cancelSpy } = streamResponse([new Uint8Array(16)], {
      "content-type": "application/pdf",
      "content-length": String(BINARY_PREVIEW_MAX_BYTES + 1),
    });

    await expect(readBoundedBinaryPreviewResponse(response, "big.pdf")).rejects.toBeInstanceOf(
      FilePreviewTooLargeError,
    );
    expect(cancelSpy).toHaveBeenCalledTimes(1);
  });

  it("rejects and cancels the stream when streamed chunks exceed maxBytes without Content-Length", async () => {
    const { response, cancelSpy } = streamResponse([new Uint8Array(60), new Uint8Array(60)], {
      "content-type": "model/stl",
    });

    await expect(
      readBoundedBinaryPreviewResponse(response, "part.stl", { maxBytes: 100 }),
    ).rejects.toBeInstanceOf(FilePreviewTooLargeError);
    expect(cancelSpy).toHaveBeenCalledTimes(1);
  });

  it("cancels the stream when the caller aborts", async () => {
    const controller = new AbortController();
    controller.abort();
    const { response, cancelSpy } = streamResponse([new Uint8Array(8)], {
      "content-type": "image/png",
    });

    await expect(
      readBoundedBinaryPreviewResponse(response, "photo.png", { signal: controller.signal }),
    ).rejects.toMatchObject({ name: "AbortError" });
    expect(cancelSpy).toHaveBeenCalledTimes(1);
  });
});

describe("useBinaryFilePreview", () => {
  it("loads raw bytes via ?download=true when the runner is online", async () => {
    onlineMock.mockReturnValue(true);
    hostOnlineMock.mockReturnValue(true);
    stubChatStore();
    const { response } = streamResponse([new Uint8Array([1, 2, 3, 4])], {
      "content-type": "application/pdf",
      "content-length": "4",
    });
    fetchMock.mockResolvedValueOnce(response);

    const { result } = renderHook(() => useBinaryFilePreview("conv_live", "docs/report.pdf"), {
      wrapper: Wrap,
    });

    await waitFor(() => expect(result.current.data?.bytes).toBe(4));
    expect(fetchMock).toHaveBeenCalledWith(
      "/v1/sessions/conv_live/resources/environments/default/filesystem/docs/report.pdf?download=true&wake=false",
      expect.objectContaining({ cache: "no-store" }),
    );
    expect(result.current.data?.contentType).toBe("application/pdf");
  });

  it("falls back once to host-backed JSON when ?download=true&wake=false answers 503 runner_unavailable", async () => {
    // Cold deep link or stale health: runner is actually asleep (or legacy without downloads).
    onlineMock.mockReturnValue(undefined);
    hostOnlineMock.mockReturnValue(true);
    stubChatStore();
    fetchMock
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({ error: { code: "runner_unavailable", message: "runner offline" } }),
          { status: 503, statusText: "Service Unavailable" },
        ),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          object: "session.environment.filesystem.file_content",
          path: "assets/pic.png",
          content_type: "image/png",
          encoding: "base64",
          content: "YWJj",
          bytes: 3,
          truncated: false,
        }),
      );

    const { result } = renderHook(() => useBinaryFilePreview("conv_cold", "assets/pic.png"), {
      wrapper: Wrap,
    });

    await waitFor(() => expect(result.current.data?.bytes).toBe(3));
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      "/v1/sessions/conv_cold/resources/environments/default/filesystem/assets/pic.png?download=true&wake=false",
    );
    expect(fetchMock.mock.calls[1]?.[0]).toBe(
      "/v1/sessions/conv_cold/resources/environments/default/filesystem/assets/pic.png",
    );
  });

  it("preserves Blob identity across chunk-varied unchanged refetches and replaces it when bytes change", async () => {
    onlineMock.mockReturnValue(true);
    hostOnlineMock.mockReturnValue(true);
    stubChatStore();
    fetchMock
      .mockResolvedValueOnce(
        streamResponse([new Uint8Array([1, 2, 3, 4, 5, 6, 7, 8])], {
          "content-type": "application/pdf",
          "content-length": "8",
        }).response,
      )
      .mockResolvedValueOnce(
        streamResponse([new Uint8Array([1, 2, 3]), new Uint8Array([4, 5, 6, 7, 8])], {
          "content-type": "application/pdf",
          "content-length": "8",
        }).response,
      )
      .mockResolvedValueOnce(
        streamResponse([new Uint8Array([1, 2, 3, 4]), new Uint8Array([5, 6, 7, 8])], {
          "content-type": "application/pdf",
          "content-length": "8",
        }).response,
      )
      .mockResolvedValueOnce(
        streamResponse([new Uint8Array([1, 2, 3, 4, 5, 6, 7, 9])], {
          "content-type": "application/pdf",
          "content-length": "8",
        }).response,
      );

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { result } = renderHook(() => useBinaryFilePreview("conv_same", "docs/report.pdf"), {
      wrapper: stableWrap(qc),
    });

    await waitFor(() => expect(result.current.data?.bytes).toBe(8));
    const firstBlob = result.current.data?.blob;

    await act(async () => {
      await qc.refetchQueries({
        queryKey: ["file-binary-preview", "conv_same", "docs/report.pdf"],
      });
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    await waitFor(() => expect(result.current.isFetching).toBe(false));
    expect(result.current.data?.blob).toBe(firstBlob);

    await act(async () => {
      await qc.refetchQueries({
        queryKey: ["file-binary-preview", "conv_same", "docs/report.pdf"],
      });
    });
    expect(fetchMock).toHaveBeenCalledTimes(3);
    await waitFor(() => expect(result.current.isFetching).toBe(false));
    expect(result.current.data?.blob).toBe(firstBlob);

    await act(async () => {
      await qc.refetchQueries({
        queryKey: ["file-binary-preview", "conv_same", "docs/report.pdf"],
      });
    });
    expect(fetchMock).toHaveBeenCalledTimes(4);
    await waitFor(() => expect(result.current.data?.blob).not.toBe(firstBlob));
    expect(new Uint8Array(await result.current.data!.blob.arrayBuffer())).toEqual(
      new Uint8Array([1, 2, 3, 4, 5, 6, 7, 9]),
    );
  });

  it("polls visible small UTF-8 SVG previews for live updates but does not poll binary PNGs", async () => {
    vi.useFakeTimers();
    try {
      onlineMock.mockReturnValue(true);
      hostOnlineMock.mockReturnValue(true);
      stubChatStore();
      const v1 = new TextEncoder().encode("<svg><circle r='1'/></svg>");
      const v2 = new TextEncoder().encode("<svg><circle r='2'/></svg>");
      fetchMock
        .mockResolvedValueOnce(
          streamResponse([v1], {
            "content-type": "text/plain; charset=utf-8",
            "content-length": String(v1.byteLength),
          }).response,
        )
        .mockResolvedValueOnce(
          streamResponse([v2], {
            "content-type": "image/svg+xml",
            "content-length": String(v2.byteLength),
          }).response,
        );

      const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      const { result, unmount } = renderHook(
        () => useBinaryFilePreview("conv_svg_poll", "icons/logo.svg", { visible: true }),
        { wrapper: stableWrap(qc) },
      );

      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(fetchMock).toHaveBeenCalledTimes(1);
      expect(result.current.data?.contentType).toBe("image/svg+xml");
      expect(result.current.data?.pollable).toBe(true);
      const firstSvgBlob = result.current.data?.blob;

      await act(async () => {
        await vi.advanceTimersByTimeAsync(FILE_CONTENT_POLL_MS);
      });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(10);
      });
      expect(fetchMock).toHaveBeenCalledTimes(2);
      expect(result.current.data?.blob).not.toBe(firstSvgBlob);
      expect(await result.current.data!.blob.text()).toBe("<svg><circle r='2'/></svg>");
      unmount();

      // Binary PNG stays non-pollable while visible.
      fetchMock.mockClear();
      fetchMock.mockResolvedValueOnce(
        streamResponse([new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x00])], {
          "content-type": "image/png",
          "content-length": "5",
        }).response,
      );
      const { result: pngResult } = renderHook(
        () => useBinaryFilePreview("conv_png_nopoll", "assets/pic.png", { visible: true }),
        { wrapper: stableWrap(qc) },
      );
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(pngResult.current.data?.pollable).toBe(false);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(FILE_CONTENT_POLL_MS * 3);
      });
      expect(fetchMock).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("falls back to host-backed JSON without waking an asleep runner", async () => {
    onlineMock.mockReturnValue(false);
    hostOnlineMock.mockReturnValue(true);
    stubChatStore();
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        object: "session.environment.filesystem.file_content",
        path: "assets/pic.png",
        content_type: "image/png",
        encoding: "base64",
        content: "YWJj",
        bytes: 3,
        truncated: false,
      }),
    );

    const { result } = renderHook(() => useBinaryFilePreview("conv_asleep", "assets/pic.png"), {
      wrapper: Wrap,
    });

    await waitFor(() => expect(result.current.data?.bytes).toBe(3));
    expect(fetchMock).toHaveBeenCalledWith(
      "/v1/sessions/conv_asleep/resources/environments/default/filesystem/assets/pic.png",
      expect.objectContaining({ cache: "no-store" }),
    );
    expect(result.current.data?.contentType).toBe("image/png");
  });

  it("surfaces FilePreviewTooLargeError without retrying when over limit", async () => {
    onlineMock.mockReturnValue(true);
    hostOnlineMock.mockReturnValue(true);
    stubChatStore();
    const { response } = streamResponse([new Uint8Array(16)], {
      "content-type": "application/pdf",
      "content-length": String(BINARY_PREVIEW_MAX_BYTES + 1),
    });
    fetchMock.mockResolvedValue(response);

    const qc = new QueryClient({
      defaultOptions: { queries: { retry: 2, retryDelay: 1 } },
    });
    const { result } = renderHook(() => useBinaryFilePreview("conv_big", "huge.pdf"), {
      wrapper: stableWrap(qc),
    });

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error).toBeInstanceOf(FilePreviewTooLargeError);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
