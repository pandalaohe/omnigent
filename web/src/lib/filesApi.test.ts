import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./host", () => ({
  hasOmnigentHostFetcher: vi.fn(() => false),
  isDatabricksWorkspace: vi.fn(() => false),
}));

vi.mock(import("./identity"), async (importOriginal) => ({
  ...(await importOriginal()),
  authenticatedFetch: vi.fn(),
  authenticatedRequestHeaders: vi.fn(),
  handleUnauthorizedResponse: vi.fn(),
}));

import { hasOmnigentHostFetcher, isDatabricksWorkspace } from "./host";
import {
  authenticatedFetch,
  authenticatedRequestHeaders,
  handleUnauthorizedResponse,
} from "./identity";
import { deleteSessionFile, uploadFile } from "./filesApi";
import { SLICE_KEY_HEADER } from "./routingKey";

/** Minimal XHR the upload path drives, so tests can script status + progress. */
class FakeXHR {
  static instances: FakeXHR[] = [];

  method = "";
  url = "";
  status = 0;
  statusText = "";
  responseText = "";
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onabort: (() => void) | null = null;
  timeout = 0;
  aborted = false;
  upload: { onprogress: ((event: ProgressEvent) => void) | null } = { onprogress: null };
  requestHeaders: Record<string, string> = {};
  sentBody: FormData | null = null;

  constructor() {
    FakeXHR.instances.push(this);
  }

  open(method: string, url: string) {
    this.method = method;
    this.url = url;
  }

  setRequestHeader(name: string, value: string) {
    this.requestHeaders[name] = value;
  }

  send(body: FormData) {
    this.sentBody = body;
  }

  respond(status: number, body: string, statusText = "") {
    this.status = status;
    this.statusText = statusText;
    this.responseText = body;
    this.onload?.();
  }

  fail() {
    this.onerror?.();
  }

  abort() {
    this.aborted = true;
    this.onabort?.();
  }
}

/** Wait until uploadFile has awaited the headers and constructed its XHR. */
async function nextXhr(): Promise<FakeXHR> {
  // The mocked header lookup resolves immediately; two microtask hops let
  // uploadFile's continuation construct and send the XHR.
  await Promise.resolve();
  await Promise.resolve();
  const xhr = FakeXHR.instances.at(-1);
  if (xhr === undefined) throw new Error("uploadFile did not construct an XMLHttpRequest");
  return xhr;
}

beforeEach(() => {
  FakeXHR.instances = [];
  vi.stubGlobal("XMLHttpRequest", FakeXHR);
  vi.mocked(hasOmnigentHostFetcher).mockReturnValue(false);
  vi.mocked(isDatabricksWorkspace).mockReturnValue(false);
  vi.mocked(authenticatedRequestHeaders).mockResolvedValue(
    new Headers({ "X-Forwarded-Email": "user@example.com" }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.resetAllMocks();
});

describe("uploadFile", () => {
  it.each([
    ["screenshot.png", "screenshot.png"],
    ["", "image.png"],
  ])("uploads %j as %j without renaming the original File", async (name, expected) => {
    const file = new File([new Uint8Array(4)], name, { type: "image/png" });
    const promise = uploadFile("session/1", file);
    const xhr = await nextXhr();

    expect(xhr.method).toBe("POST");
    expect(xhr.url).toBe("/v1/sessions/session%2F1/resources/files");
    // Headers iteration lowercases names (HTTP names are case-insensitive).
    expect(xhr.requestHeaders).toEqual({ "x-forwarded-email": "user@example.com" });
    expect((xhr.sentBody!.get("file") as File).name).toBe(expected);

    xhr.respond(201, JSON.stringify({ id: "file_1" }));
    const uploaded = await promise;

    expect(uploaded.filename).toBe(expected);
    expect(file.name).toBe(name);
  });

  it("prefers the server's authoritative filename", async () => {
    const promise = uploadFile("session_1", new File([], "", { type: "image/png" }));
    const xhr = await nextXhr();
    xhr.respond(
      201,
      JSON.stringify({
        id: "file_1",
        name: "resource-name",
        metadata: { filename: "stored.png" },
      }),
    );
    const uploaded = await promise;
    expect(uploaded.filename).toBe("stored.png");
  });

  it("reuses the upload on the default replica once when host routing misses", async () => {
    vi.mocked(authenticatedRequestHeaders).mockResolvedValue(
      new Headers({ [SLICE_KEY_HEADER]: "host-key", "X-Forwarded-Email": "user@example.com" }),
    );
    const promise = uploadFile("s1", new File(["bytes"], "document.txt"));
    const first = await nextXhr();
    expect(first.requestHeaders[SLICE_KEY_HEADER.toLowerCase()]).toBe("host-key");
    first.respond(400, JSON.stringify({ error: { code: "wrong_replica" } }));

    await vi.waitFor(() => expect(FakeXHR.instances).toHaveLength(2));
    const retry = FakeXHR.instances[1];
    expect(retry.requestHeaders).toEqual({ "x-forwarded-email": "user@example.com" });
    expect(retry.sentBody).toBe(first.sentBody);
    retry.respond(201, JSON.stringify({ id: "file_1" }));

    await expect(promise).resolves.toMatchObject({ id: "file_1" });
    expect(FakeXHR.instances).toHaveLength(2);
  });

  it("reports the upload fraction as it progresses", async () => {
    const onProgress = vi.fn();
    const promise = uploadFile("s1", new File(["bytes"], "clip.mp4"), onProgress);
    const xhr = await nextXhr();

    // A total-less progress event still reports the bytes sent.
    xhr.upload.onprogress?.({
      lengthComputable: false,
      loaded: 1,
      total: 0,
    } as ProgressEvent);
    expect(onProgress).toHaveBeenCalledWith({ loaded: 1, total: null, fraction: null });

    xhr.upload.onprogress?.({ lengthComputable: true, loaded: 5, total: 10 } as ProgressEvent);
    expect(onProgress).toHaveBeenCalledWith({ loaded: 5, total: 10, fraction: 0.5 });
    // Clamp a reported overflow (progress events are advisory).
    xhr.upload.onprogress?.({ lengthComputable: true, loaded: 12, total: 10 } as ProgressEvent);
    expect(onProgress).toHaveBeenLastCalledWith({ loaded: 12, total: 10, fraction: 1 });

    xhr.respond(201, JSON.stringify({ id: "file_1" }));
    await promise;
  });

  it("maps an error response through apiErrorFromResponse, 413 detail included", async () => {
    const detail = "Attachment upload request exceeds the 2 GiB limit";
    const promise = uploadFile("s1", new File(["bytes"], "clip.mp4"));
    const xhr = await nextXhr();
    xhr.respond(413, JSON.stringify({ detail }), "Payload Too Large");

    const error = await promise.then(
      () => {
        throw new Error("expected the upload to fail");
      },
      (caught: unknown) =>
        caught as {
          message: string;
          status: number;
        },
    );
    expect(error.status).toBe(413);
    expect(error.message).toBe(detail);
  });

  it("routes a standalone 401 through the shared authenticatedFetch handler", async () => {
    const promise = uploadFile("s1", new File(["bytes"], "clip.mp4"));
    const xhr = await nextXhr();
    xhr.respond(401, JSON.stringify({ detail: "Authentication required" }), "Unauthorized");

    await expect(promise).rejects.toMatchObject({ status: 401 });
    expect(handleUnauthorizedResponse).toHaveBeenCalledTimes(1);
    expect(vi.mocked(handleUnauthorizedResponse).mock.calls[0]![0]).toBe(
      "/v1/sessions/s1/resources/files",
    );
  });

  it("rejects when the network fails", async () => {
    const promise = uploadFile("s1", new File(["bytes"], "clip.mp4"));
    const xhr = await nextXhr();
    xhr.fail();
    await expect(promise).rejects.toThrow("Network error");
  });

  it("rejects when the upload is aborted", async () => {
    const promise = uploadFile("s1", new File(["bytes"], "clip.mp4"));
    const xhr = await nextXhr();
    xhr.abort();
    await expect(promise).rejects.toThrow("aborted");
  });

  it("aborts through the caller's cancellation signal", async () => {
    const controller = new AbortController();
    const promise = uploadFile("s1", new File(["bytes"], "clip.mp4"), undefined, controller.signal);
    const xhr = await nextXhr();
    controller.abort();
    await expect(promise).rejects.toMatchObject({ name: "AbortError" });
    expect(xhr.aborted).toBe(true);
  });

  it("keeps a slow upload alive without a time limit", async () => {
    vi.useFakeTimers();
    try {
      const promise = uploadFile("s1", new File(["bytes"], "clip.mp4"));
      const xhr = await nextXhr();
      expect(xhr.timeout).toBe(0);
      await vi.advanceTimersByTimeAsync(10 * 60_000);
      expect(xhr.aborted).toBe(false);
      xhr.respond(201, JSON.stringify({ id: "file_1" }));
      await expect(promise).resolves.toMatchObject({ id: "file_1" });
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("uploadFile via the embedded host transport", () => {
  it("uses authenticatedFetch and reports no percentage when a host fetcher is set", async () => {
    vi.mocked(hasOmnigentHostFetcher).mockReturnValue(true);
    vi.mocked(authenticatedFetch).mockResolvedValue(
      new Response(JSON.stringify({ id: "file_1", metadata: { filename: "clip.mp4", bytes: 5 } }), {
        status: 201,
        statusText: "Created",
      }),
    );
    const onProgress = vi.fn();

    const uploaded = await uploadFile("s1", new File(["bytes"], "clip.mp4"), onProgress);

    expect(FakeXHR.instances).toHaveLength(0);
    expect(authenticatedFetch).toHaveBeenCalledTimes(1);
    const [path, init] = vi.mocked(authenticatedFetch).mock.calls[0]!;
    expect(path).toBe("/v1/sessions/s1/resources/files");
    expect(init).toMatchObject({ method: "POST" });
    expect((init!.body as FormData).get("file")).toBeInstanceOf(File);
    expect(uploaded).toEqual({
      id: "file_1",
      filename: "clip.mp4",
      bytes: 5,
      created_at: 0,
    });
    // The fetch path cannot report transfer progress, so it reports unknown
    // bytes while the upload is in flight.
    expect(onProgress).toHaveBeenCalledWith({ loaded: 0, total: null, fraction: null });
    expect(onProgress).not.toHaveBeenCalledWith(
      expect.objectContaining({ fraction: expect.any(Number) }),
    );
  });

  it("uses authenticatedFetch on a Databricks workspace", async () => {
    vi.mocked(isDatabricksWorkspace).mockReturnValue(true);
    vi.mocked(authenticatedFetch).mockResolvedValue(
      new Response(JSON.stringify({ id: "file_2" }), { status: 201 }),
    );

    const uploaded = await uploadFile("s1", new File(["bytes"], "clip.mp4"));

    expect(FakeXHR.instances).toHaveLength(0);
    expect(authenticatedFetch).toHaveBeenCalledTimes(1);
    expect(uploaded.id).toBe("file_2");
  });

  it("maps an upload failure through apiErrorFromResponse", async () => {
    vi.mocked(hasOmnigentHostFetcher).mockReturnValue(true);
    vi.mocked(authenticatedFetch).mockResolvedValue(
      new Response(JSON.stringify({ detail: "Unsupported attachment type" }), {
        status: 415,
        statusText: "Unsupported Media Type",
      }),
    );

    await expect(uploadFile("s1", new File(["bytes"], "clip.mp4"))).rejects.toMatchObject({
      status: 415,
      message: "Unsupported attachment type",
    });
  });
});

describe("deleteSessionFile", () => {
  it("encodes both resource identifiers and accepts an empty response", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValue(new Response(null, { status: 204 }));

    await expect(deleteSessionFile("session/1", "file/1")).resolves.toBeUndefined();

    expect(authenticatedFetch).toHaveBeenCalledWith(
      "/v1/sessions/session%2F1/resources/files/file%2F1",
      { method: "DELETE" },
    );
  });

  it("preserves the server's deletion error", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValue(
      new Response(JSON.stringify({ detail: "Cannot delete this file" }), { status: 403 }),
    );

    await expect(deleteSessionFile("session_1", "file_1")).rejects.toMatchObject({
      message: "Cannot delete this file",
      status: 403,
    });
  });
});
