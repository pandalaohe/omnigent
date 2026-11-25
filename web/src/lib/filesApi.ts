import { attachmentFilename } from "./attachments";
import { withBasePath } from "./basePath";
import { hasOmnigentHostFetcher, isDatabricksWorkspace } from "./host";
import {
  authenticatedFetch,
  authenticatedRequestHeaders,
  handleUnauthorizedResponse,
  isWrongReplicaResponse,
} from "./identity";
import { SLICE_KEY_HEADER } from "./routingKey";
import { apiErrorFromResponse } from "./sessionsApi";

export interface UploadedFile {
  id: string;
  filename: string;
  bytes: number;
  created_at: number;
}

/** Wire shape of the session file resource the upload route returns. */
interface UploadResourcePayload {
  id: string;
  name?: string;
  metadata?: {
    filename?: string;
    bytes?: number;
    created_at?: number;
  };
}

export interface FileUploadProgress {
  loaded: number;
  total: number | null;
  fraction: number | null;
}

function uploadedFileFromResource(
  resource: UploadResourcePayload,
  file: File,
  fallbackName: string,
): UploadedFile {
  return {
    id: resource.id,
    filename: resource.metadata?.filename ?? resource.name ?? fallbackName,
    bytes: resource.metadata?.bytes ?? file.size,
    created_at: resource.metadata?.created_at ?? 0,
  };
}

/**
 * Upload one attachment, reporting transfer progress.
 *
 * The standalone path uses `XMLHttpRequest` because only it exposes
 * `upload.onprogress`; the auth headers come from
 * {@link authenticatedRequestHeaders}. When an embed host owns transport
 * (`hasOmnigentHostFetcher`) or the server is a Databricks workspace, the
 * upload goes through {@link authenticatedFetch} instead: that path owns the
 * host routing and 401 redirects through the embedded transport. Standalone
 * sharded uploads retry a wrong-replica response on the default replica.
 * Progress is only reportable on the XHR
 * path, so `onProgress` reports zero known bytes and an unknown total on the
 * fetch path.
 *
 * HTTP failures are mapped through `apiErrorFromResponse`, so the server's
 * reason ("Unsupported attachment type …" for 415, the size cap for 413)
 * reaches the caller rather than a bare status number.
 *
 * @param sessionId Destination session id.
 * @param file File to upload.
 * @param onProgress Optional callback with bytes and the fraction when known.
 * @param signal Optional cancellation signal for the in-flight request.
 * @returns The stored file's identity.
 */
export async function uploadFile(
  sessionId: string,
  file: File,
  onProgress?: (progress: FileUploadProgress) => void,
  signal?: AbortSignal,
): Promise<UploadedFile> {
  signal?.throwIfAborted();
  const filename = attachmentFilename(file);
  const path = `/v1/sessions/${encodeURIComponent(sessionId)}/resources/files`;
  const form = new FormData();
  form.append("file", file, filename);

  if (hasOmnigentHostFetcher() || isDatabricksWorkspace()) {
    onProgress?.({ loaded: 0, total: null, fraction: null });
    const res = await authenticatedFetch(path, { method: "POST", body: form, signal });
    if (!res.ok) throw await apiErrorFromResponse(res);
    const resource = (await res.json()) as UploadResourcePayload;
    return uploadedFileFromResource(resource, file, filename);
  }

  const headers = await authenticatedRequestHeaders(path);
  let response = await uploadWithProgress(path, form, headers, onProgress, signal);
  if (headers.has(SLICE_KEY_HEADER) && (await isWrongReplicaResponse(response))) {
    const keylessHeaders = new Headers(headers);
    keylessHeaders.delete(SLICE_KEY_HEADER);
    response = await uploadWithProgress(path, form, keylessHeaders, onProgress, signal);
  }
  if (!response.ok) {
    handleUnauthorizedResponse(path, response);
    throw await apiErrorFromResponse(response);
  }
  const resource = (await response.json()) as UploadResourcePayload;
  return uploadedFileFromResource(resource, file, filename);
}

function uploadWithProgress(
  path: string,
  form: FormData,
  headers: Headers,
  onProgress?: (progress: FileUploadProgress) => void,
  signal?: AbortSignal,
): Promise<Response> {
  signal?.throwIfAborted();
  return new Promise<Response>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const abortFromSignal = () => {
      reject(signal?.reason);
      xhr.abort();
    };
    const settle = () => signal?.removeEventListener("abort", abortFromSignal);
    xhr.open("POST", withBasePath(path));
    for (const [name, value] of headers.entries()) {
      xhr.setRequestHeader(name, value);
    }
    xhr.upload.onprogress = (event) => {
      const total = event.lengthComputable && event.total > 0 ? event.total : null;
      onProgress?.({
        loaded: event.loaded,
        total,
        fraction: total === null ? null : Math.min(1, event.loaded / total),
      });
    };
    xhr.onload = () => {
      settle();
      try {
        resolve(
          new Response(xhr.responseText, {
            status: xhr.status,
            statusText: xhr.statusText,
          }),
        );
      } catch (error) {
        reject(error);
      }
    };
    xhr.onerror = () => {
      settle();
      reject(new Error("Network error while uploading the file"));
    };
    xhr.onabort = () => {
      settle();
      reject(new Error("File upload was aborted"));
    };
    signal?.addEventListener("abort", abortFromSignal, { once: true });
    try {
      xhr.send(form);
    } catch (error) {
      settle();
      reject(error);
    }
  });
}

/**
 * Download a session's uploaded file back into a `File`, e.g. to attach it again.
 *
 * @param sessionId - The session the file was uploaded to.
 * @param fileId - The file's id from its message block.
 * @param filename - Name for the returned file, e.g. ``"chart.png"``.
 * @throws ApiError when the server refuses the download.
 */
export async function downloadSessionFile(
  sessionId: string,
  fileId: string,
  filename: string,
): Promise<File> {
  const res = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(sessionId)}/resources/files/${encodeURIComponent(fileId)}/content`,
  );
  if (!res.ok) throw await apiErrorFromResponse(res);
  const blob = await res.blob();
  // Its bytes, not the Blob itself: a File from another implementation (e.g. a test
  // DOM's) doesn't recognize the fetch Blob and stores "[object Blob]".
  return new File([await blob.arrayBuffer()], filename, { type: blob.type });
}
