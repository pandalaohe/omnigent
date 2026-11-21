import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./host", () => ({
  hasOmnigentHostFetcher: vi.fn(() => false),
  isDatabricksWorkspace: vi.fn(() => false),
}));

vi.mock("./identity", () => ({
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
import { uploadFile } from "./filesApi";

/** Minimal XHR the upload path drives, so tests can script status + progress. */
class FakeXHR {
  static instances: FakeXHR[] = [];

  readonly HEADERS_RECEIVED = 2;
  readonly LOADING = 3;
  readyState = 0;
  method = "";
  url = "";
  status = 0;
  statusText = "";
  responseText = "";
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onabort: (() => void) | null = null;
  ontimeout: (() => void) | null = null;
  onprogress: (() => void) | null = null;
  onreadystatechange: (() => void) | null = null;
  timeout = 0;
  aborted = false;
  upload: { onprogress: ((event: ProgressEvent) => void) | null; onload: (() => void) | null } = {
    onprogress: null,
    onload: null,
  };
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

  it("reports the upload fraction as it progresses", async () => {
    const onProgress = vi.fn();
    const promise = uploadFile("s1", new File(["bytes"], "clip.mp4"), onProgress);
    const xhr = await nextXhr();

    // A total-less progress event carries no fraction and is ignored.
    xhr.upload.onprogress?.({
      lengthComputable: false,
      loaded: 1,
      total: 0,
    } as ProgressEvent);
    expect(onProgress).not.toHaveBeenCalled();

    xhr.upload.onprogress?.({ lengthComputable: true, loaded: 5, total: 10 } as ProgressEvent);
    expect(onProgress).toHaveBeenCalledWith(0.5);
    // Clamp a reported overflow (progress events are advisory).
    xhr.upload.onprogress?.({ lengthComputable: true, loaded: 12, total: 10 } as ProgressEvent);
    expect(onProgress).toHaveBeenLastCalledWith(1);

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

  it("rejects when the upload times out", async () => {
    const promise = uploadFile("s1", new File(["bytes"], "clip.mp4"));
    const xhr = await nextXhr();
    xhr.ontimeout?.();
    await expect(promise).rejects.toThrow("timed out");
  });

  it("keeps an upload alive while transfer and response activity continue", async () => {
    vi.useFakeTimers();
    try {
      const promise = uploadFile("s1", new File(["bytes"], "clip.mp4"));
      const xhr = await nextXhr();

      await vi.advanceTimersByTimeAsync(59_000);
      xhr.upload.onprogress?.({ lengthComputable: true, loaded: 1, total: 2 } as ProgressEvent);
      await vi.advanceTimersByTimeAsync(59_000);
      xhr.upload.onload?.();
      xhr.readyState = xhr.HEADERS_RECEIVED;
      xhr.onreadystatechange?.();
      await vi.advanceTimersByTimeAsync(59_000);
      xhr.onprogress?.();
      await vi.advanceTimersByTimeAsync(59_000);
      xhr.readyState = xhr.LOADING;
      xhr.onreadystatechange?.();
      await vi.advanceTimersByTimeAsync(59_000);

      expect(xhr.aborted).toBe(false);
      xhr.respond(201, JSON.stringify({ id: "file_1" }));
      await promise;
    } finally {
      vi.useRealTimers();
    }
  });

  it("accepts a first response 90 seconds after the request body is sent", async () => {
    vi.useFakeTimers();
    try {
      const result = uploadFile("s1", new File(["bytes"], "clip.mp4")).catch(
        (error: unknown) => error,
      );
      const xhr = await nextXhr();
      xhr.upload.onload?.();

      await vi.advanceTimersByTimeAsync(90_000);

      expect(xhr.aborted).toBe(false);
      xhr.readyState = xhr.HEADERS_RECEIVED;
      xhr.onreadystatechange?.();
      xhr.respond(201, JSON.stringify({ id: "file_1" }));
      expect(await result).toMatchObject({ id: "file_1" });
    } finally {
      vi.useRealTimers();
    }
  });

  it("returns to the stall limit once the response headers arrive", async () => {
    vi.useFakeTimers();
    try {
      const promise = uploadFile("s1", new File(["bytes"], "clip.mp4"));
      const xhr = await nextXhr();
      const rejection = expect(promise).rejects.toThrow("stalled");
      xhr.upload.onload?.();
      await vi.advanceTimersByTimeAsync(10_000);
      xhr.readyState = xhr.HEADERS_RECEIVED;
      xhr.onreadystatechange?.();

      await vi.advanceTimersByTimeAsync(59_999);
      expect(xhr.aborted).toBe(false);
      await vi.advanceTimersByTimeAsync(1);

      expect(xhr.aborted).toBe(true);
      await rejection;
    } finally {
      vi.useRealTimers();
    }
  });

  it("rejects an upload with no progress after the stall limit", async () => {
    vi.useFakeTimers();
    try {
      const promise = uploadFile("s1", new File(["bytes"], "clip.mp4"));
      const xhr = await nextXhr();
      const rejection = expect(promise).rejects.toThrow("stalled");

      await vi.advanceTimersByTimeAsync(60_000);

      expect(xhr.aborted).toBe(true);
      await rejection;
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
    // The fetch path cannot report transfer progress: the caller hears the
    // upload started (null), never a fraction.
    expect(onProgress).toHaveBeenCalledWith(null);
    expect(onProgress).not.toHaveBeenCalledWith(expect.any(Number));
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
