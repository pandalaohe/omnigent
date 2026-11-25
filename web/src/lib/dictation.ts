// Server-side dictation transport: mic → 16 kHz PCM → WS /v1/dictation/stream.
//
// ComposerMicButton uses this as the fallback when the browser Web Speech API
// has no working backend (Electron, Firefox/Chromium — see
// web/electron/README.md). One DictationSession is one dictation take: it
// owns the microphone stream, an AudioWorklet that downsamples the capture
// rate to 16 kHz mono s16le, and the WebSocket that streams those frames to
// the server and receives transcript events back. The wire protocol is
// documented in omnigent/server/routes/dictation.py; availability is gated
// by the `dictation_available` capability from GET /v1/info.
//
// The WebSocket URL rides the host seam (`resolveWebSocketUrl`) exactly like
// the terminal-attach and session-updates sockets, so embed hosts and the
// Vite dev proxy keep working. Identity rides the ingress/dev proxy on the
// handshake, as with those sockets.

import { resolveWebSocketUrl } from "@/lib/host";
import { authenticatedFetch } from "@/lib/identity";

/**
 * Build the `ws(s)://` URL for the dictation stream.
 *
 * Dictation is not host-scoped: any replica transcribes a take. The client's
 * own slice key, added by {@link resolveWebSocketUrl}, spreads takes across
 * replicas instead of piling a tenant onto one replica's concurrent-take cap
 * ({@link DictationBusyError} / `OMNIGENT_DICTATION_MAX_STREAMS`).
 */
function buildDictationUrl(): string {
  return resolveWebSocketUrl("/v1/dictation/stream");
}

/** A transcript event pushed by the server over the dictation stream. */
export interface Undelivered {
  segments: number;
  seconds: number;
}
export interface FinalMeta {
  raw?: string;
  cleaned?: boolean;
  cleanupHandled?: boolean;
}
export interface ErrorMeta {
  code?: string;
  undelivered?: Undelivered;
}
export interface DictationTail extends FinalMeta {
  text: string;
  undelivered?: Undelivered;
  retry_ranges?: [number, number][];
}
export type DictationEvent =
  | { type: "ready"; whole_take?: boolean }
  | { type: "processing" }
  | { type: "partial"; text: string }
  | ({ type: "final"; text: string } & FinalMeta)
  | ({ type: "stopped" } & DictationTail)
  | ({ type: "error"; message: string } & ErrorMeta);

export interface DictationSessionEvents {
  /** Revisable in-progress utterance (server-throttled to ~6 Hz). */
  onPartial: (text: string) => void;
  /** An utterance completed by a pause; append it and clear the partial. */
  onFinal: (text: string, meta?: FinalMeta) => void;
  /** Fatal error after start. The session has already cleaned itself up. */
  onError: (message: string, meta?: ErrorMeta) => void;
}

/**
 * The server was reachable but at its concurrent-take cap (WS close 1013).
 * Transient by definition — callers should message "busy, try again",
 * not "unavailable".
 */
export class DictationBusyError extends Error {}

const PUNCTUATION_TIMEOUT_MS = 10_000;

/**
 * Restore punctuation in one completed browser Web Speech transcript.
 *
 * The composer treats failures as a soft degradation and inserts the original
 * transcript, so this helper throws on every malformed or unavailable response.
 */
export async function restoreDictationPunctuation(text: string): Promise<string> {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), PUNCTUATION_TIMEOUT_MS);
  try {
    const response = await authenticatedFetch("/v1/dictation/punctuation", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
      signal: controller.signal,
    });
    if (!response.ok) throw new Error(`punctuation request failed: ${response.status}`);
    const body: unknown = await response.json();
    if (typeof body !== "object" || body === null) {
      throw new Error("punctuation response was not an object");
    }
    const restored = (body as { text?: unknown }).text;
    if (typeof restored !== "string" || restored.trim() === "") {
      throw new Error("punctuation response did not contain text");
    }
    return restored.trim();
  } finally {
    window.clearTimeout(timeout);
  }
}

/**
 * Parse one text frame from the dictation socket into a typed event.
 * Returns null for frames that don't match the protocol (ignored for
 * forward compatibility, mirroring the server's posture on control
 * messages it doesn't know).
 */
export function parseDictationEvent(raw: string): DictationEvent | null {
  let data: unknown;
  try {
    data = JSON.parse(raw);
  } catch {
    return null;
  }
  if (typeof data !== "object" || data === null) return null;
  const frame = data as Record<string, unknown>;
  const meta: FinalMeta & ErrorMeta & { retry_ranges?: [number, number][] } = {};
  if (
    Array.isArray(frame.retry_ranges) &&
    frame.retry_ranges.every(
      (range) =>
        Array.isArray(range) &&
        range.length === 2 &&
        range.every(Number.isSafeInteger) &&
        range[0] >= 0 &&
        range[1] > range[0],
    )
  )
    meta.retry_ranges = frame.retry_ranges;
  if (typeof frame.raw === "string") meta.raw = frame.raw;
  if (typeof frame.cleaned === "boolean") meta.cleaned = frame.cleaned;
  if (typeof frame.code === "string") meta.code = frame.code;
  if (typeof frame.undelivered === "object" && frame.undelivered !== null) {
    const loss = frame.undelivered as Undelivered;
    if (
      Number.isFinite(loss.segments) &&
      Number.isFinite(loss.seconds) &&
      loss.segments >= 0 &&
      loss.seconds >= 0
    )
      meta.undelivered = loss;
  }
  switch (frame.type) {
    case "ready":
      return frame.whole_take === true ? { type: "ready", whole_take: true } : { type: "ready" };
    case "processing":
      return { type: "processing" };
    case "partial":
    case "final":
    case "stopped":
      return typeof frame.text === "string"
        ? { type: frame.type, text: frame.text, ...meta }
        : null;
    case "error":
      return typeof frame.message === "string"
        ? { type: "error", message: frame.message, ...meta }
        : null;
    default:
      return null;
  }
}

// Client budgets must exceed the server's own worst cases or takes fail
// spuriously right when they'd have succeeded:
// - ready: engine construction loads model weights on the first take, and
//   the remote-relay path allows the worker 30 s for its own cold load
//   (_REMOTE_READY_TIMEOUT_S in omnigent/server/dictation.py).
// - stop: the relay waits up to 10 s (_REMOTE_STOP_TIMEOUT_S) for the
//   worker to flush the tail; resolving earlier would drop the user's
//   last words even though they were transcribed moments later.
const READY_TIMEOUT_MS = 40_000;
const STOP_TIMEOUT_MS = 15_000;

// How long stop() waits for the worklet to post its final partial chunk
// before tearing the audio graph down. Message-port turnaround is
// milliseconds; this is only a stuck-worklet backstop.
const FLUSH_TIMEOUT_MS = 250;

/** WS close code the server sends when at its concurrent-take cap. */
const WS_CLOSE_TRY_AGAIN_LATER = 1013;

const TARGET_RATE = 16_000;

// AudioWorklet processor, inlined as a Blob module so no separate asset has
// to survive the Vite build. Linear-interpolation downsample from the
// context capture rate to 16 kHz, Float32 → Int16, posted in 100 ms chunks.
// (When the context already runs at 16 kHz — we ask for that — the step is
// 1 and the loop degenerates to a plain format conversion.)
//
// Any message on the port means "flush": the partially-filled chunk is
// posted, then a null marker — so stop() can capture trailing speech that
// hasn't crossed the 100 ms boundary before tearing the graph down.
const WORKLET_SOURCE = `
const TARGET_RATE = ${TARGET_RATE};
const CHUNK_SAMPLES = TARGET_RATE / 10; // 100 ms per posted chunk
class Pcm16Downsampler extends AudioWorkletProcessor {
  constructor() {
    super();
    this.step = sampleRate / TARGET_RATE;
    this.pos = 0; // fractional read position, carried across blocks
    this.pending = new Int16Array(CHUNK_SAMPLES);
    this.filled = 0;
    this.port.onmessage = () => {
      if (this.filled > 0) {
        const out = this.pending.slice(0, this.filled);
        this.filled = 0;
        this.port.postMessage(out, [out.buffer]);
      }
      this.port.postMessage(null);
    };
  }
  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (!channel || channel.length === 0) return true;
    let pos = this.pos;
    while (pos < channel.length) {
      const i = Math.floor(pos);
      const s0 = channel[i];
      const s1 = i + 1 < channel.length ? channel[i + 1] : s0;
      const sample = s0 + (s1 - s0) * (pos - i);
      const clamped = Math.max(-1, Math.min(1, sample));
      this.pending[this.filled++] = clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff;
      if (this.filled === CHUNK_SAMPLES) {
        const out = this.pending;
        this.pending = new Int16Array(CHUNK_SAMPLES);
        this.filled = 0;
        this.port.postMessage(out, [out.buffer]);
      }
      pos += this.step;
    }
    this.pos = pos - channel.length;
    return true;
  }
}
registerProcessor("omnigent-pcm16-downsampler", Pcm16Downsampler);
`;

let cachedWorkletUrl: string | null = null;

function workletUrl(): string {
  if (cachedWorkletUrl === null) {
    cachedWorkletUrl = URL.createObjectURL(
      new Blob([WORKLET_SOURCE], { type: "application/javascript" }),
    );
  }
  return cachedWorkletUrl;
}

/**
 * One live dictation take against the server recognizer.
 *
 * Construct via {@link DictationSession.start}, which resolves once the
 * mic, audio graph, and socket handshake are all up — so a resolved
 * session is guaranteed to be streaming. End it with {@link stop} (flushes
 * the tail utterance) or {@link cancel} (immediate teardown, e.g. unmount).
 */
export class DictationSession {
  private readonly events: DictationSessionEvents;
  private ws: WebSocket;
  private readonly wholeTake: boolean;
  private audio: Int16Array<ArrayBuffer>[] = [];
  private pendingFinals: DictationTail[] = [];
  private static retained: DictationSession | null = null;
  private readonly mediaStream: MediaStream;
  private readonly audioContext: AudioContext;
  private readonly workletNode: AudioWorkletNode;
  private stopResolve: ((tail: DictationTail) => void) | null = null;
  private flushResolve: (() => void) | null = null;
  private closed = false;
  private stopTimer: ReturnType<typeof setTimeout> | undefined;

  private constructor(
    events: DictationSessionEvents,
    ws: WebSocket,
    mediaStream: MediaStream,
    audioContext: AudioContext,
    workletNode: AudioWorkletNode,
    wholeTake = false,
  ) {
    this.wholeTake = wholeTake;
    this.events = events;
    this.ws = ws;
    this.mediaStream = mediaStream;
    this.audioContext = audioContext;
    this.workletNode = workletNode;

    workletNode.port.onmessage = (msg: MessageEvent<Int16Array<ArrayBuffer> | null>) => {
      if (msg.data === null) {
        // Flush marker: the worklet has posted everything it had.
        this.flushResolve?.();
        this.flushResolve = null;
        return;
      }
      if (this.wholeTake) {
        this.audio.push(msg.data);
      }
      if (this.ws.readyState === WebSocket.OPEN) this.ws.send(msg.data.buffer);
    };
    this.bindSocket();
  }

  private bindSocket(): void {
    const ws = this.ws;
    ws.onmessage = (msg) => {
      if (typeof msg.data !== "string") return;
      const event = parseDictationEvent(msg.data);
      if (event === null) return;
      if (event.type === "partial") this.events.onPartial(event.text);
      else if (event.type === "final") {
        if (this.wholeTake) this.pendingFinals.push(event);
        else this.events.onFinal(event.text, event);
      } else if (event.type === "processing" && this.stopResolve) clearTimeout(this.stopTimer);
      else if (event.type === "stopped") this.resolveStop(event, true);
      else if (event.type === "error") this.fail(event.message, event);
    };
    ws.onclose = () => {
      // A close during stop() is the normal end of a take; any other
      // close means the server went away mid-dictation.
      if (this.stopResolve !== null) this.resolveStop(this.failedTail());
      else if (this.wholeTake && !this.closed) return;
      else if (!this.closed) this.fail("Dictation connection closed");
    };
  }

  /**
   * Acquire the mic, open the socket, and wait for the server's ready
   * handshake. Rejects (with everything torn down) when the mic is
   * denied, the socket fails, the server is at capacity
   * ({@link DictationBusyError}), or the engine never comes up.
   */
  static discardRetainedAudio(): void {
    if (this.retained) {
      this.retained.audio = [];
      this.retained.pendingFinals = [];
    }
    this.retained = null;
  }

  static async start(events: DictationSessionEvents): Promise<DictationSession> {
    this.discardRetainedAudio();
    const mediaStream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
    });

    let ws: WebSocket | null = null;
    let audioContext: AudioContext | null = null;
    try {
      ws = new WebSocket(buildDictationUrl());
      ws.binaryType = "arraybuffer";
      const ready = await waitForReady(ws);
      // Detect a close during the async audio-graph setup below: the
      // handler-swap in the constructor would otherwise never see it and
      // start() would resolve a dead session that silently drops audio.
      let closedDuringSetup = false;
      const markClosed = () => {
        closedDuringSetup = true;
      };
      ws.addEventListener("close", markClosed);

      // Ask for the target rate directly — Chrome/Firefox resample the
      // capture for us and the worklet's downsampler becomes a no-op.
      // Some platforms reject the hint; the worklet handles any rate.
      try {
        audioContext = new AudioContext({ sampleRate: TARGET_RATE });
      } catch {
        audioContext = new AudioContext();
      }
      await audioContext.audioWorklet.addModule(workletUrl());
      const source = audioContext.createMediaStreamSource(mediaStream);
      const node = new AudioWorkletNode(audioContext, "omnigent-pcm16-downsampler");
      // The worklet only renders while it reaches the destination; route
      // it through a muted gain so nothing is audible.
      const mute = audioContext.createGain();
      mute.gain.value = 0;
      source.connect(node);
      node.connect(mute);
      mute.connect(audioContext.destination);

      ws.removeEventListener("close", markClosed);
      if (closedDuringSetup || ws.readyState !== WebSocket.OPEN) {
        throw new Error("dictation connection closed during setup");
      }
      const session = new DictationSession(
        events,
        ws,
        mediaStream,
        audioContext,
        node,
        ready.whole_take === true,
      );
      if (session.wholeTake) this.retained = session;
      return session;
    } catch (error) {
      for (const track of mediaStream.getTracks()) track.stop();
      if (audioContext && audioContext.state !== "closed") void audioContext.close();
      ws?.close();
      throw error;
    }
  }

  /**
   * End the take: flush the worklet's trailing samples (so the last words
   * make it to the recognizer), release the mic, ask the server to flush,
   * and resolve with the flushed tail utterance ("" on timeout/close).
   */
  async stop(): Promise<DictationTail> {
    if (this.closed) return { text: "" };
    await this.flushWorklet();
    this.teardownAudio();
    if (this.ws.readyState !== WebSocket.OPEN) {
      this.closed = true;
      return this.wholeTake ? this.failedTail() : { text: "" };
    }
    return new Promise<DictationTail>((resolve) => {
      this.stopResolve = resolve;
      if (!this.wholeTake) this.waitForStop(STOP_TIMEOUT_MS);
      try {
        this.ws.send(JSON.stringify({ type: "stop" }));
      } catch {
        this.resolveStop(this.failedTail());
      }
    });
  }

  /** Replay only retained failed audio, without opening the microphone again. */
  async retry(): Promise<DictationTail> {
    if (!this.audio.length) return { text: "" };
    const ws = new WebSocket(buildDictationUrl());
    this.ws = ws;
    this.closed = false;
    this.pendingFinals = [];
    try {
      await waitForReady(ws);
      if (this.closed) return { text: "" };
      this.bindSocket();
      for (const data of this.audio) ws.send(data);
      return await this.stop();
    } catch (error) {
      ws.close();
      throw error;
    }
  }

  private failedTail(): DictationTail {
    const bytes = this.audio.reduce((sum, frame) => sum + frame.byteLength, 0);
    return bytes
      ? { text: "", undelivered: { segments: 1, seconds: bytes / 32000 } }
      : { text: "" };
  }

  private retainFailedAudio(tail: DictationTail): void {
    if (!this.wholeTake || !tail.undelivered?.segments) {
      this.audio = [];
      return;
    }
    if (!tail.retry_ranges) return;
    const failed: Int16Array<ArrayBuffer>[] = [];
    let offset = 0;
    for (const frame of this.audio) {
      for (const [start, end] of tail.retry_ranges) {
        const first = Math.max(0, start - offset);
        const last = Math.min(frame.byteLength, end - offset);
        if (last > first) failed.push(frame.subarray(first / 2, last / 2));
      }
      offset += frame.byteLength;
    }
    this.audio = failed;
  }

  /** Immediate teardown without waiting for the tail (unmount, disable). */
  cancel(): void {
    clearTimeout(this.stopTimer);
    this.teardownAudio();
    this.closed = true;
    this.resolveStop({ text: "" });
  }

  /** Ask the worklet to post its partial chunk; wait for its marker. */
  private flushWorklet(): Promise<void> {
    return new Promise<void>((resolve) => {
      this.flushResolve = resolve;
      try {
        this.workletNode.port.postMessage("flush");
      } catch {
        resolve();
        return;
      }
      setTimeout(resolve, FLUSH_TIMEOUT_MS);
    });
  }

  private waitForStop(timeoutMs: number): void {
    clearTimeout(this.stopTimer);
    this.stopTimer = setTimeout(() => this.resolveStop({ text: "" }), timeoutMs);
  }

  private resolveStop(value: DictationTail, commitFinals = false): void {
    let tail = value;
    if (this.wholeTake) {
      if (commitFinals && this.pendingFinals.length) {
        const parts = [...this.pendingFinals, tail];
        tail = {
          ...tail,
          text: parts
            .map((part) => part.text)
            .filter(Boolean)
            .join(" "),
          raw: parts.some((part) => part.raw !== undefined)
            ? parts
                .map((part) => part.raw ?? part.text)
                .filter(Boolean)
                .join(" ")
            : undefined,
          cleaned: parts.every((part) => part.cleaned === true),
        };
      }
      this.pendingFinals = [];
    }
    this.retainFailedAudio(tail);
    clearTimeout(this.stopTimer);
    this.closed = true;
    const resolve = this.stopResolve;
    this.stopResolve = null;
    this.ws.close();
    resolve?.(tail);
  }

  private fail(message: string, meta?: ErrorMeta): void {
    if (this.wholeTake) {
      if (this.stopResolve) this.resolveStop(this.failedTail());
      else this.ws.close();
      return;
    }
    this.closed = true;
    this.teardownAudio();
    this.ws.close();
    this.events.onError(message, meta);
  }

  private teardownAudio(): void {
    for (const track of this.mediaStream.getTracks()) track.stop();
    if (this.audioContext.state !== "closed") void this.audioContext.close();
  }
}

/**
 * Resolve when the server sends its ready frame; reject on error frame,
 * close (typed {@link DictationBusyError} for the 1013 at-capacity close),
 * or timeout.
 */
function waitForReady(ws: WebSocket): Promise<{ whole_take?: boolean }> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      reject(new Error("dictation server did not become ready"));
    }, READY_TIMEOUT_MS);
    ws.onmessage = (msg) => {
      if (typeof msg.data !== "string") return;
      const event = parseDictationEvent(msg.data);
      if (event?.type === "ready") {
        clearTimeout(timer);
        resolve(event);
      } else if (event?.type === "error") {
        // The engine failed to initialize; surface its message rather
        // than the generic close that follows.
        clearTimeout(timer);
        reject(new Error(event.message));
      }
    };
    ws.onerror = () => {
      clearTimeout(timer);
      reject(new Error("dictation connection failed"));
    };
    ws.onclose = (event) => {
      clearTimeout(timer);
      reject(
        event.code === WS_CLOSE_TRY_AGAIN_LATER
          ? new DictationBusyError("dictation is at capacity")
          : new Error("dictation connection closed"),
      );
    };
  });
}
