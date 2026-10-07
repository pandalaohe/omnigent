import { attachmentFilename } from "./attachments";
import { withBasePath } from "./basePath";
import { hasOmnigentHostFetcher, isDatabricksWorkspace } from "./host";
import {
  authenticatedFetch,
  authenticatedRequestHeaders,
  handleUnauthorizedResponse,
} from "./identity";
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
 * slice-key routing, the wrong-replica retry and the 401 redirect, and the
 * XHR path cannot reproduce them. Progress is only reportable on the XHR
 * path, so `onProgress` receives `null` (unknown) on the fetch path and the
 * composer shows the upload without a percentage.
 *
 * HTTP failures are mapped through `apiErrorFromResponse`, so the server's
 * reason ("Unsupported attachment type …" for 415, the size cap for 413)
 * reaches the caller rather than a bare status number.
 *
 * @param sessionId Destination session id.
 * @param file File to upload.
 * @param onProgress Optional callback with the upload fraction (0–1), or
 *     `null` when the transport cannot report one.
 * @returns The stored file's identity.
 */
export async function uploadFile(
  sessionId: string,
  file: File,
  onProgress?: (fraction: number | null) => void,
): Promise<UploadedFile> {
  const filename = attachmentFilename(file);
  const path = `/v1/sessions/${encodeURIComponent(sessionId)}/resources/files`;
  const form = new FormData();
  form.append("file", file, filename);

  if (hasOmnigentHostFetcher() || isDatabricksWorkspace()) {
    onProgress?.(null);
    const res = await authenticatedFetch(path, { method: "POST", body: form });
    if (!res.ok) throw await apiErrorFromResponse(res);
    const resource = (await res.json()) as UploadResourcePayload;
    return uploadedFileFromResource(resource, file, filename);
  }

  const headers = await authenticatedRequestHeaders(path);
  return new Promise<UploadedFile>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", withBasePath(path));
    for (const [name, value] of headers.entries()) {
      xhr.setRequestHeader(name, value);
    }
    xhr.upload.onprogress = (event) => {
      if (onProgress && event.lengthComputable && event.total > 0) {
        onProgress(Math.min(1, event.loaded / event.total));
      }
    };
    xhr.onload = () => {
      const response = new Response(xhr.responseText, {
        status: xhr.status,
        statusText: xhr.statusText,
      });
      if (xhr.status < 200 || xhr.status >= 300) {
        // Reuse authenticatedFetch's 401 handling so a standalone session
        // expiry redirects exactly as every other API call would.
        handleUnauthorizedResponse(path, response);
        void apiErrorFromResponse(response).then(reject, reject);
        return;
      }
      let resource: UploadResourcePayload;
      try {
        resource = JSON.parse(xhr.responseText) as UploadResourcePayload;
      } catch (error) {
        reject(error);
        return;
      }
      resolve(uploadedFileFromResource(resource, file, filename));
    };
    xhr.onerror = () => reject(new Error("Network error while uploading the file"));
    xhr.send(form);
  });
}
