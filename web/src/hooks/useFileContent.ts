// TanStack Query wrapper around the resources filesystem file-content endpoint:
// `GET /v1/sessions/{sessionId}/resources/environments/{environmentId}/filesystem/{path}`.
//
// Text files have `encoding: "utf-8"` and return content inline.
// Binary files that cannot be decoded as UTF-8 have `encoding: "base64"`.
// The query is disabled when either `conversationId` or `path` is null/undefined.

import { useEffect, useRef } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { withBasePath } from "@/lib/basePath";
import { isDatabricksWorkspace, resolveMediaUrl } from "@/lib/host";
import { authenticatedFetch } from "@/lib/identity";
import { getSessionHost, isHostKeyless } from "@/lib/sessionHost";
import { isAndroidShell, isIOSShell } from "@/lib/nativeBridge";
import { isTempConvId } from "@/lib/tempConversationId";
import { useSessionRunnerOnline } from "@/hooks/RunnerHealthProvider";
import {
  browseLocationBase,
  browseLocationSegment,
  useWorkspaceServeable,
} from "@/hooks/useWorkspaceChangedFiles";
import { fileWritesSettled } from "@/hooks/useWriteFileContent";
import { previewMimeTypeForPath } from "@/shell/codeViewerHelpers";
import { useChatStore } from "@/store/chatStore";

// The primary workspace environment is always "default".  This hook targets
// the primary workspace; pass a different id if terminal environments are needed.
const DEFAULT_ENVIRONMENT_ID = "default";

export interface FileContentResponse {
  object: "session.environment.filesystem.file_content";
  path: string;
  content_type: string | null;
  encoding: "utf-8" | "base64";
  content: string;
  bytes: number;
  truncated?: boolean;
}

/**
 * Build the filesystem URL for a workspace file.
 *
 * Reuses the shared browse-location wire form: a per-segment-encoded path with
 * literal slashes and, for an absolute path (a file opened while browsing
 * outside the workspace), the base named by `?base=host`. Keeping this on the
 * shared helpers means the slash-merge-safe contract lives in one place (see
 * `browseLocationSegment`).
 *
 * :param conversationId: The session/conversation ID, e.g. ``"sess_abc123"``.
 * :param path: Workspace-relative or host-absolute file path.
 * :param query: Extra query parameters, e.g. ``{ download: "true" }``.
 */
function workspaceFileUrl(
  conversationId: string,
  path: string,
  query: Record<string, string> = {},
): string {
  const base = browseLocationBase(path);
  const params = new URLSearchParams(base ? { ...query, base } : query).toString();
  return (
    `/v1/sessions/${encodeURIComponent(conversationId)}` +
    `/resources/environments/${DEFAULT_ENVIRONMENT_ID}/filesystem/${browseLocationSegment(path)}` +
    (params ? `?${params}` : "")
  );
}

/**
 * The host's directly loadable URL for a workspace file's raw bytes, or null
 * when media must be fetched through the authenticated transport instead.
 *
 * A `<video src>` load sends no request headers, so the session host's routing
 * key rides `?omnigent_slice_key=`, as the terminal attach WebSocket does. A
 * host already proven to route keyless is left unkeyed, matching
 * `authenticatedFetch`.
 *
 * :param conversationId: The session/conversation ID, e.g. ``"sess_abc123"``.
 * :param path: Workspace-relative or host-absolute file path.
 */
export function workspaceFileMediaUrl(conversationId: string, path: string): string | null {
  const query: Record<string, string> = { download: "true" };
  const hostId = getSessionHost(conversationId);
  if (hostId && !isHostKeyless(hostId)) query.omnigent_slice_key = hostId;
  return resolveMediaUrl(workspaceFileUrl(conversationId, path, query));
}

/**
 * Maximum file size streamed into browser memory for inline image, PDF, and 3D
 * previews (64 MiB). Matches the server's default artifact preview ceiling and
 * sits below the host tunnel's 72 MiB cap; larger files remain downloadable
 * with no size cap.
 */
export const BINARY_PREVIEW_MAX_BYTES = 64 * 1024 * 1024;

// Server `_MAX_READ_BYTES` cap applied when falling back to the JSON endpoint
// for a sleeping runner (host-tunnel read).
const JSON_FALLBACK_MAX_BYTES = 10 * 1024 * 1024;

/** Thrown when a binary preview exceeds its inline preview byte ceiling. */
export class FilePreviewTooLargeError extends Error {
  readonly limitBytes: number;

  constructor(limitBytes: number) {
    const limitMiB = Math.round(limitBytes / (1024 * 1024));
    super(`File is too large to preview (${limitMiB} MiB limit).`);
    this.name = "FilePreviewTooLargeError";
    this.limitBytes = limitBytes;
  }
}

/**
 * Thrown when a passive `?download=true&wake=false` request cannot be served by
 * a live runner (runner asleep or predates downloads), signalling the caller to
 * fall back once to the host-backed JSON endpoint.
 */
class RawDownloadUnavailableError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "RawDownloadUnavailableError";
  }
}

export interface BinaryPreviewResponse {
  blob: Blob;
  contentType: string;
  bytes: number;
  /** Content fingerprint so unchanged refetches keep the cached Blob identity. */
  fingerprint: string;
  /** True for small UTF-8 text-backed previews (SVG/OBJ/ASCII STL) that poll while visible. */
  pollable: boolean;
}

// Keep closed binary previews cached briefly (30 s) rather than TanStack Query's
// default 5 minutes so 64 MiB Blobs release memory promptly after closing a tab.
const BINARY_PREVIEW_GC_MS = 30_000;

// Preview extensions whose files can be UTF-8 text edited on disk while open.
const TEXT_POLLABLE_PREVIEW_EXTENSIONS = new Set(["svg", "obj", "stl"]);

function isPollableUtf8Preview(
  path: string,
  chunks: readonly Uint8Array[],
  bytes: number,
): boolean {
  if (bytes > FILE_CONTENT_POLL_MAX_BYTES) return false;
  const ext = path.split(".").pop()?.toLowerCase() ?? "";
  if (!TEXT_POLLABLE_PREVIEW_EXTENSIONS.has(ext)) return false;
  for (const chunk of chunks) {
    if (chunk.includes(0)) return false;
  }
  const decoder = new TextDecoder("utf-8", { fatal: true });
  try {
    for (const chunk of chunks) {
      decoder.decode(chunk, { stream: true });
    }
    decoder.decode();
    return true;
  } catch {
    return false;
  }
}

/**
 * Fast synchronous 64-bit (two 32-bit FNV-1a lanes) fingerprint over the byte
 * stream, invariant to how the response is split into chunks. Works on
 * non-secure LAN HTTP where `crypto.subtle` is unavailable and lets
 * `structuralSharing` keep the same `Blob` reference on unchanged refetches.
 */
function fingerprintChunks(
  chunks: readonly Uint8Array[],
  bytes: number,
  contentType: string,
): string {
  let h1 = 0x811c9dc5;
  let h2 = 0x9e3779b1;
  let offset = 0;
  for (const chunk of chunks) {
    const len = chunk.byteLength;
    for (let i = 0; i < len; i++) {
      const b = chunk[i];
      h1 = Math.imul(h1 ^ b, 0x01000193);
      h2 = Math.imul(h2 ^ ((b + offset) & 0xff), 0x85ebca6b);
      offset++;
    }
  }
  return `${bytes}:${contentType}:${(h1 >>> 0).toString(16)}:${(h2 >>> 0).toString(16)}`;
}

function previewAbortError(): DOMException {
  return new DOMException("The file preview request was cancelled.", "AbortError");
}

function cancelResponseBody(response: Response): void {
  void response.body?.cancel().catch(() => {});
}

/**
 * Read a raw `?download=true` response into a `Blob` up to `maxBytes`.
 *
 * Rejects before buffering when `Content-Length` exceeds `maxBytes`, and also
 * counts streamed chunks in case the header is absent or understated. Cancels
 * the underlying stream on overflow or abort.
 */
export async function readBoundedBinaryPreviewResponse(
  res: Response,
  path: string,
  { signal, maxBytes = BINARY_PREVIEW_MAX_BYTES }: { signal?: AbortSignal; maxBytes?: number } = {},
): Promise<BinaryPreviewResponse> {
  const contentType = previewMimeTypeForPath(path, res.headers?.get("content-type"));
  const declaredRaw = res.headers?.get("content-length");
  if (declaredRaw !== null && declaredRaw !== undefined && declaredRaw.trim() !== "") {
    const declared = Number(declaredRaw);
    if (Number.isFinite(declared) && declared > maxBytes) {
      cancelResponseBody(res);
      throw new FilePreviewTooLargeError(maxBytes);
    }
  }
  if (signal?.aborted) {
    cancelResponseBody(res);
    throw previewAbortError();
  }

  const reader = res.body?.getReader();
  if (!reader) {
    const rawBlob = await res.blob();
    if (signal?.aborted) throw previewAbortError();
    if (rawBlob.size > maxBytes) {
      throw new FilePreviewTooLargeError(maxBytes);
    }
    const blob =
      rawBlob.type === contentType ? rawBlob : rawBlob.slice(0, rawBlob.size, contentType);
    const buf = new Uint8Array(await blob.arrayBuffer());
    return {
      blob,
      contentType,
      bytes: blob.size,
      fingerprint: fingerprintChunks([buf], blob.size, contentType),
      pollable: isPollableUtf8Preview(path, [buf], blob.size),
    };
  }

  const onAbort = () => void reader.cancel().catch(() => {});
  signal?.addEventListener("abort", onAbort, { once: true });
  const chunks: Uint8Array<ArrayBuffer>[] = [];
  let received = 0;
  try {
    // Each read advances the same stream reader sequentially.
    /* oxlint-disable no-await-in-loop */
    for (;;) {
      const { done, value } = await reader.read();
      if (signal?.aborted) throw previewAbortError();
      if (done) break;
      received += value.byteLength;
      if (received > maxBytes) {
        // Reject immediately even if cancelling a stalled stream takes time.
        void reader.cancel().catch(() => {});
        throw new FilePreviewTooLargeError(maxBytes);
      }
      chunks.push(value);
    }
    /* oxlint-enable no-await-in-loop */
  } finally {
    signal?.removeEventListener("abort", onAbort);
  }
  const blob = new Blob(chunks, { type: contentType });
  return {
    blob,
    contentType,
    bytes: received,
    fingerprint: fingerprintChunks(chunks, received, contentType),
    pollable: isPollableUtf8Preview(path, chunks, received),
  };
}

async function isRunnerUnavailableResponse(res: Response): Promise<boolean> {
  if (res.status !== 503) return false;
  try {
    const payload = (await res.json()) as { error?: { code?: unknown } };
    return payload?.error?.code === "runner_unavailable";
  } catch {
    return false;
  }
}

/**
 * Fetch raw binary bytes for an inline image, PDF, or 3D preview via the
 * existing `?download=true&wake=false` route, avoiding JSON+base64 expansion
 * and main-thread `atob()` decoding without waking an asleep runner.
 */
export async function fetchWorkspaceBinaryPreview(
  conversationId: string,
  path: string,
  { signal, maxBytes = BINARY_PREVIEW_MAX_BYTES }: { signal?: AbortSignal; maxBytes?: number } = {},
): Promise<BinaryPreviewResponse> {
  const res = await authenticatedFetch(
    workspaceFileUrl(conversationId, path, { download: "true", wake: "false" }),
    { signal },
  );
  if (!res.ok) {
    if (await isRunnerUnavailableResponse(res)) {
      throw new RawDownloadUnavailableError(`${res.status} ${res.statusText}`);
    }
    cancelResponseBody(res);
    throw new Error(`${res.status} ${res.statusText}`);
  }
  return readBoundedBinaryPreviewResponse(res, path, { signal, maxBytes });
}

/** Read complete media bytes through the host's authenticated transport, with cancellation. */
export async function fetchWorkspaceFileBlob(
  conversationId: string,
  path: string,
  signal?: AbortSignal,
): Promise<Blob> {
  const res = await authenticatedFetch(
    workspaceFileUrl(conversationId, path, { download: "true" }),
    {
      signal,
    },
  );
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.blob();
}

export async function fetchFileContent(
  conversationId: string,
  path: string,
  signal?: AbortSignal,
): Promise<FileContentResponse> {
  const res = await authenticatedFetch(workspaceFileUrl(conversationId, path), { signal });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return (await res.json()) as FileContentResponse;
}

/**
 * Convert a ``FileContentResponse`` into a ``Blob`` with the correct MIME type.
 *
 * Handles both UTF-8 text files and base64-encoded binary files. This is the
 * single source of truth for MIME/encoding handling — callers that already
 * have a response object (e.g. FileViewer) call this directly instead of
 * re-fetching.
 *
 * :param data: The file content response from the filesystem API.
 * :returns: A ``Blob`` suitable for a browser download.
 */
export function fileContentToBlob(data: FileContentResponse): Blob {
  if (data.encoding === "base64") {
    const binary = atob(data.content);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    return new Blob([bytes], { type: data.content_type ?? "application/octet-stream" });
  }
  return new Blob([data.content], { type: data.content_type ?? "text/plain" });
}

/**
 * Programmatically trigger a browser file download for the given ``Blob``.
 *
 * Creates a temporary object URL, clicks a synthetic ``<a>`` element to
 * initiate the download, then immediately cleans up both the element and
 * the URL.
 *
 * :param blob: The file data to download.
 * :param filename: The suggested filename presented to the browser's save dialog.
 */
export function triggerBrowserDownload(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  clickDownloadLink(url, filename);
  URL.revokeObjectURL(url);
}

function clickDownloadLink(href: string, filename: string): void {
  const link = document.createElement("a");
  link.href = href;
  link.download = filename;
  document.body.append(link);
  link.click();
  link.remove();
}

/**
 * Download a workspace file's complete bytes.
 *
 * Asks the filesystem endpoint for the raw file (`download=true`), which the
 * server streams with no size cap, rather than the viewer's JSON envelope,
 * which is truncated past the server's read cap.
 *
 * In a browser this is a plain same-origin link click: the browser streams the
 * attachment straight to disk and shows it in its download UI at once, and the
 * session cookie or proxy identity header rides along as on any request. The
 * managed embed owns the transport (auth headers, replica routing) and the
 * mobile shells save http(s) downloads outside the WebView's session, so those
 * fetch the bytes through `authenticatedFetch` and hand them over as a Blob,
 * which only surfaces once the whole file has arrived.
 *
 * :param conversationId: The session/conversation ID, e.g. ``"sess_abc123"``.
 * :param path: Workspace-relative file path, e.g. ``"src/main.py"``.
 */
export async function downloadWorkspaceFile(conversationId: string, path: string): Promise<void> {
  const url = workspaceFileUrl(conversationId, path, { download: "true" });
  const filename = path.split("/").pop() ?? path;
  if (!isDatabricksWorkspace() && !isIOSShell() && !isAndroidShell()) {
    // A direct anchor navigation, so it must carry the deployment subpath the
    // fetch paths get for free via hostFetch/authenticatedFetch. Without it a
    // stripping proxy (`/proxy/<port>/`) sends it to the origin root and misses
    // the app. The fetch branch below keeps `url` raw: authenticatedFetch
    // prefixes internally, so passing it prefixed would double the base path.
    clickDownloadLink(withBasePath(url), filename);
    return;
  }
  const res = await authenticatedFetch(url);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  triggerBrowserDownload(await res.blob(), filename);
}

/** How often an on-screen text file is re-read to show other writers' edits. */
export const FILE_CONTENT_POLL_MS = 2_000;

/** Larger files refresh only when shown again: every poll downloads the whole file. */
export const FILE_CONTENT_POLL_MAX_BYTES = 256 * 1024;

/** While refreshes keep failing (e.g. the file was deleted), re-check this often. */
export const FILE_CONTENT_FAILING_POLL_MS = 30_000;

/**
 * Decide how often to re-read an on-screen file.
 *
 * Polls only UTF-8 files up to :data:`FILE_CONTENT_POLL_MAX_BYTES`. Binary
 * previews and large (including truncated) files are skipped.
 *
 * :param data: The last successful response, or undefined before the first load.
 * :param failing: True when the latest refresh failed after its retries.
 * :returns: The interval in milliseconds, or ``false`` to not poll.
 */
export function fileContentPollInterval(
  data: FileContentResponse | undefined,
  failing: boolean,
): number | false {
  if (data?.encoding !== "utf-8" || data.bytes > FILE_CONTENT_POLL_MAX_BYTES) return false;
  return failing ? FILE_CONTENT_FAILING_POLL_MS : FILE_CONTENT_POLL_MS;
}

/**
 * Decide how often to re-read an on-screen raw binary preview.
 *
 * Mirrors `fileContentPollInterval`: only small UTF-8 text-backed previews
 * (SVG, OBJ, ASCII STL up to `FILE_CONTENT_POLL_MAX_BYTES`) poll while visible
 * so external edits appear live; binary images, PDFs, and larger files do not.
 */
export function binaryPreviewPollInterval(
  data: BinaryPreviewResponse | undefined,
  failing: boolean,
): number | false {
  if (!data?.pollable || data.bytes > FILE_CONTENT_POLL_MAX_BYTES) return false;
  return failing ? FILE_CONTENT_FAILING_POLL_MS : FILE_CONTENT_POLL_MS;
}

/**
 * Fetch the content of a workspace file for the given conversation.
 *
 * Disabled (no request made) when `conversationId` or `path` is
 * null / undefined, or when `conversationId` is a client-only temp id.
 *
 * Keeps the viewer current with writes from the agent, other sessions and
 * other processes:
 *   - While `visible`, re-reads at {@link fileContentPollInterval}. Hidden
 *     browser tabs pause.
 *   - Re-reads once when the viewer or its browser tab is shown again, and
 *     when the focused session goes from active (running/waiting) to idle.
 * An unchanged read keeps the cached data, so the editors see no change.
 * Changed content is applied in place by `useMarkdownEditorSync`.
 *
 * :param options.visible: True while the viewer is on screen, e.g. an open
 *     panel in the active viewport.
 */
export function useFileContent(
  conversationId: string | undefined,
  path: string | null,
  options: { retry?: boolean; visible?: boolean } = {},
) {
  const visible = options.visible ?? false;
  // A `temp:*` id (navigate-first new-chat window) has no server session yet,
  // so never request `/v1/sessions/temp:*/resources/*` for it.
  const serverId = isTempConvId(conversationId) ? undefined : conversationId;
  const focusedId = useChatStore((s) => s.conversationId);
  const sessionStatus = useChatStore((s) => s.sessionStatus);
  const sessionActive =
    !!serverId &&
    serverId === focusedId &&
    (sessionStatus === "running" || sessionStatus === "waiting");
  // Serveable when the runner is online OR (runner offline but) the host can
  // read the workspace from disk — keeps the viewer live while the agent is
  // asleep. `false` only when neither source can answer.
  const serveable = useWorkspaceServeable(serverId);
  const queryClient = useQueryClient();

  const prevRef = useRef<{ id: string | undefined; active: boolean; visible: boolean }>({
    id: serverId,
    active: sessionActive,
    visible,
  });
  useEffect(() => {
    const sameSession = prevRef.current.id === serverId;
    const justWentIdle = sameSession && prevRef.current.active && !sessionActive;
    // A hidden viewer doesn't poll, so catch up as soon as it is shown again.
    const justShown = visible && !prevRef.current.visible;
    prevRef.current = { id: serverId, active: sessionActive, visible };
    if ((justWentIdle || justShown) && serverId && path) {
      void queryClient.invalidateQueries({
        queryKey: ["file-content", serverId, path],
      });
    }
  }, [serverId, path, sessionActive, visible, queryClient]);

  return useQuery({
    queryKey: ["file-content", serverId, path],
    queryFn: async ({ signal }) => {
      await fileWritesSettled(serverId!, path!);
      // Cancelled while waiting (a newer write started): don't send the read.
      signal.throwIfAborted();
      return fetchFileContent(serverId!, path!, signal);
    },
    enabled: !!serverId && !!path && serveable !== false,
    staleTime: 5_000,
    refetchInterval: visible
      ? (query) => fileContentPollInterval(query.state.data, query.state.status === "error")
      : false,
    // Catch up as soon as the browser tab returns, even within staleTime.
    refetchOnWindowFocus: visible ? "always" : false,
    ...(options.retry === undefined ? {} : { retry: options.retry }),
  });
}

/**
 * Fetch raw bytes for a previewable binary file (image, PDF, or 3D model).
 *
 * Uses the streaming `?download=true` route whenever the runner is online (or
 * liveness is still unknown), eliminating JSON+base64 wire overhead and
 * main-thread `atob()` decoding. When the runner is known-offline but the host
 * can still serve the workspace (asleep session), falls back to the host-backed
 * JSON endpoint so viewing a preview neither wakes the agent nor fails for
 * read-only viewers.
 */
export function useBinaryFilePreview(
  conversationId: string | undefined,
  path: string | null,
  options: { retry?: boolean; visible?: boolean; maxBytes?: number } = {},
) {
  const visible = options.visible ?? false;
  const maxBytes = options.maxBytes ?? BINARY_PREVIEW_MAX_BYTES;
  const serverId = isTempConvId(conversationId) ? undefined : conversationId;
  const focusedId = useChatStore((s) => s.conversationId);
  const sessionStatus = useChatStore((s) => s.sessionStatus);
  const sessionActive =
    !!serverId &&
    serverId === focusedId &&
    (sessionStatus === "running" || sessionStatus === "waiting");
  const runnerOnline = useSessionRunnerOnline(serverId);
  const serveable = useWorkspaceServeable(serverId);
  const queryClient = useQueryClient();

  const prevRef = useRef<{ id: string | undefined; active: boolean; visible: boolean }>({
    id: serverId,
    active: sessionActive,
    visible,
  });
  useEffect(() => {
    const sameSession = prevRef.current.id === serverId;
    const justWentIdle = sameSession && prevRef.current.active && !sessionActive;
    const justShown = visible && !prevRef.current.visible;
    prevRef.current = { id: serverId, active: sessionActive, visible };
    if ((justWentIdle || justShown) && serverId && path) {
      void queryClient.invalidateQueries({
        queryKey: ["file-binary-preview", serverId, path],
      });
    }
  }, [serverId, path, sessionActive, visible, queryClient]);

  const defaultRetry = queryClient.getDefaultOptions().queries?.retry;
  const retryOption =
    options.retry === false || defaultRetry === false
      ? false
      : (failureCount: number, error: Error) => {
          if (error instanceof FilePreviewTooLargeError) return false;
          if (typeof defaultRetry === "function") return defaultRetry(failureCount, error);
          const maxRetries = typeof defaultRetry === "number" ? defaultRetry : 3;
          return failureCount < maxRetries;
        };

  const fetchJsonFallback = async (signal?: AbortSignal): Promise<BinaryPreviewResponse> => {
    const data = await fetchFileContent(serverId!, path!, signal);
    if (data.truncated) {
      throw new FilePreviewTooLargeError(JSON_FALLBACK_MAX_BYTES);
    }
    const blob = fileContentToBlob(data);
    const contentType = previewMimeTypeForPath(path!, data.content_type);
    const typedBlob = blob.type === contentType ? blob : blob.slice(0, blob.size, contentType);
    const bytes = new Uint8Array(await typedBlob.arrayBuffer());
    return {
      blob: typedBlob,
      contentType,
      bytes: typedBlob.size,
      fingerprint: fingerprintChunks([bytes], typedBlob.size, contentType),
      pollable: data.encoding === "utf-8" && typedBlob.size <= FILE_CONTENT_POLL_MAX_BYTES,
    };
  };

  return useQuery({
    queryKey: ["file-binary-preview", serverId, path],
    queryFn: async ({ signal }): Promise<BinaryPreviewResponse> => {
      await fileWritesSettled(serverId!, path!);
      signal.throwIfAborted();
      if (runnerOnline === false) {
        // Runner is asleep; use host-backed JSON fallback without waking it.
        return fetchJsonFallback(signal);
      }
      try {
        return await fetchWorkspaceBinaryPreview(serverId!, path!, { signal, maxBytes });
      } catch (error) {
        // Cold open, stale health, or legacy runner without download support:
        // fall back once to host-backed JSON without waking the runner.
        if (error instanceof RawDownloadUnavailableError) {
          return fetchJsonFallback(signal);
        }
        throw error;
      }
    },
    enabled: !!serverId && !!path && serveable !== false,
    staleTime: 5_000,
    gcTime: BINARY_PREVIEW_GC_MS,
    refetchInterval: visible
      ? (query) => binaryPreviewPollInterval(query.state.data, query.state.status === "error")
      : false,
    refetchOnWindowFocus: visible ? "always" : false,
    retry: retryOption,
    structuralSharing: (oldData, newData) => {
      const prev = oldData as BinaryPreviewResponse | undefined;
      const next = newData as BinaryPreviewResponse | undefined;
      if (
        prev &&
        next &&
        prev.bytes === next.bytes &&
        prev.contentType === next.contentType &&
        prev.fingerprint === next.fingerprint &&
        prev.pollable === next.pollable
      ) {
        return prev;
      }
      return newData;
    },
  });
}
