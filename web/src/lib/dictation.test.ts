// Tests for the dictation socket protocol parsing. The DictationSession
// transport itself (mic + AudioWorklet + WebSocket) can't run in jsdom;
// its behavior against the component is pinned in ComposerMicButton.test.tsx
// with a mocked session, and the full loop runs in the Playwright e2e test
// against the server's fake engine.

import { afterEach, describe, expect, it, vi } from "vitest";
import { DictationSession, parseDictationEvent, restoreDictationPunctuation } from "./dictation";

afterEach(() => vi.unstubAllGlobals());

describe("parseDictationEvent", () => {
  it("parses the transcript event shapes", () => {
    expect(parseDictationEvent('{"type":"ready"}')).toEqual({ type: "ready" });
    expect(parseDictationEvent('{"type":"partial","text":"hel"}')).toEqual({
      type: "partial",
      text: "hel",
    });
    expect(parseDictationEvent('{"type":"final","text":"hello."}')).toEqual({
      type: "final",
      text: "hello.",
    });
    expect(parseDictationEvent('{"type":"stopped","text":""}')).toEqual({
      type: "stopped",
      text: "",
    });
    expect(parseDictationEvent('{"type":"error","message":"boom"}')).toEqual({
      type: "error",
      message: "boom",
    });
  });

  it("returns null for malformed or unknown frames", () => {
    expect(parseDictationEvent("not json")).toBeNull();
    expect(parseDictationEvent("42")).toBeNull();
    expect(parseDictationEvent("null")).toBeNull();
    expect(parseDictationEvent('{"type":"future-thing"}')).toBeNull();
    // Known types with a missing/mistyped payload are dropped, not crashed on.
    expect(parseDictationEvent('{"type":"partial"}')).toBeNull();
    expect(parseDictationEvent('{"type":"partial","text":7}')).toBeNull();
    expect(parseDictationEvent('{"type":"error"}')).toBeNull();
  });
});

describe("restoreDictationPunctuation", () => {
  it("posts a completed transcript and returns display-ready text", async () => {
    const fetchMock = vi.fn(
      async () =>
        new Response(JSON.stringify({ text: "你好，世界！" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(restoreDictationPunctuation("你好世界")).resolves.toBe("你好，世界！");
    expect(fetchMock).toHaveBeenCalledWith(
      "/v1/dictation/punctuation",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ text: "你好世界" }),
      }),
    );
  });

  it("rejects malformed responses so the composer can preserve raw text", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("{}", { status: 200 })),
    );
    await expect(restoreDictationPunctuation("你好世界")).rejects.toThrow("did not contain text");
  });
});

it("preserves cleaned tail and typed loss metadata", () => {
  expect(
    parseDictationEvent(
      '{"type":"stopped","text":"A.","raw":"a","cleaned":true,"undelivered":{"segments":1,"seconds":0.4}}',
    ),
  ).toEqual({
    type: "stopped",
    text: "A.",
    raw: "a",
    cleaned: true,
    undelivered: { segments: 1, seconds: 0.4 },
  });
  expect(
    parseDictationEvent(
      '{"type":"error","message":"failed","code":"third_party_failed","undelivered":{"segments":2,"seconds":1}}',
    ),
  ).toEqual({
    type: "error",
    message: "failed",
    code: "third_party_failed",
    undelivered: { segments: 2, seconds: 1 },
  });
});

it("parses hosted processing and failed audio ranges", () => {
  expect(parseDictationEvent('{"type":"processing"}')).toEqual({ type: "processing" });
  expect(parseDictationEvent('{"type":"ready","whole_take":true}')).toEqual({
    type: "ready",
    whole_take: true,
  });
  expect(parseDictationEvent('{"type":"stopped","text":"kept","retry_ranges":[[0,640]]}')).toEqual({
    type: "stopped",
    text: "kept",
    retry_ranges: [[0, 640]],
  });
});

it("waits beyond the relay timer for a whole-take result", async () => {
  vi.useFakeTimers();
  const socket = {
    readyState: 1,
    send: vi.fn(),
    close: vi.fn(),
    onmessage: null as ((msg: { data: string }) => void) | null,
  };
  const port = {
    onmessage: null as ((msg: { data: null }) => void) | null,
    postMessage: () => port.onmessage?.({ data: null }),
  };
  const take: DictationSession = Reflect.construct(DictationSession, [
    { onPartial: vi.fn(), onFinal: vi.fn(), onError: vi.fn() },
    socket,
    { getTracks: () => [{ stop: vi.fn() }] },
    { state: "closed" },
    { port },
  ]);
  try {
    const stopped = take.stop();
    await Promise.resolve();
    socket.onmessage?.({ data: '{"type":"processing"}' });
    await vi.advanceTimersByTimeAsync(16000);
    expect(socket.close).not.toHaveBeenCalled();
    socket.onmessage?.({ data: '{"type":"stopped","text":"whole take"}' });
    await expect(stopped).resolves.toEqual({ type: "stopped", text: "whole take" });
  } finally {
    take.cancel();
    vi.useRealTimers();
  }
});

it("retains only failed PCM ranges for an explicit retry", async () => {
  const sent: unknown[] = [];
  const originalSocket = {
    readyState: 1,
    send: vi.fn(),
    close: vi.fn(),
    onmessage: null as ((msg: { data: string }) => void) | null,
  };
  const port = {
    onmessage: null as ((msg: { data: Int16Array | null }) => void) | null,
    postMessage: () => port.onmessage?.({ data: null }),
  };
  const take: DictationSession = Reflect.construct(DictationSession, [
    { onPartial: vi.fn(), onFinal: vi.fn(), onError: vi.fn() },
    originalSocket,
    { getTracks: () => [{ stop: vi.fn() }] },
    { state: "closed" },
    { port },
    true,
  ]);
  port.onmessage?.({ data: new Int16Array([1, 2, 3, 4]) });
  const stopped = take.stop();
  await Promise.resolve();
  originalSocket.onmessage?.({
    data: JSON.stringify({
      type: "stopped",
      text: "kept",
      undelivered: { segments: 1, seconds: 0.000125 },
      retry_ranges: [[2, 6]],
    }),
  });
  await stopped;
  let replayMessage: (data: string) => void = () => {};
  vi.stubGlobal(
    "WebSocket",
    class {
      static OPEN = 1;
      readyState = 1;
      onmessage = null as ((msg: { data: string }) => void) | null;
      constructor() {
        replayMessage = (data) => this.onmessage?.({ data });
      }
      send(data: unknown) {
        sent.push(data);
      }
      close() {}
    },
  );
  const retried = take.retry();
  replayMessage('{"type":"ready","whole_take":true}');
  await Promise.resolve();
  await Promise.resolve();
  expect(new Int16Array(sent[0] as ArrayBuffer)).toEqual(new Int16Array([2, 3]));
  replayMessage('{"type":"stopped","text":"recovered"}');
  await expect(retried).resolves.toMatchObject({ text: "recovered" });
  await expect(take.retry()).resolves.toEqual({ text: "" });
  take.cancel();
});
