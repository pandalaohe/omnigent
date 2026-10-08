// Early capture script (web/public/omni-html-capture.js): console/network
// rings, resource and rejection errors, the early keydown slot, and its wiring
// into the annotate stub. The harness mirrors htmlCommentBridge.test.ts's
// startBridge: one JSDOM window per test, the asset evaluated like the server's
// inline <script> with a stubbed document.currentScript and a nonce.

import { afterEach, describe, expect, it, vi } from "vitest";
// @ts-expect-error jsdom ships no declarations and @types/jsdom is not a dependency
import { JSDOM } from "jsdom";
// @ts-expect-error jsdom ships no declarations and @types/jsdom is not a dependency
import { implForWrapper } from "jsdom/lib/generated/idl/utils.js";
import captureSource from "../../../public/omni-html-capture.js?raw";
import stubSource from "../../../public/omni-html-annotate.js?raw";

const NONCE = "capture-nonce-1";
const FRAME_URL = "http://localhost:6767/reports/q3.html";
const SOURCE = "omni-html-annotate";

interface ConsoleEntry {
  level: string;
  message: string;
  ts: number;
}

interface NetworkEntry {
  method: string;
  url: string;
  status: number;
  ts: number;
}

interface CaptureNamespace {
  snapshot: () => { console: ConsoleEntry[]; network: NetworkEntry[] };
  setSlot: (type: string, slot: ((event: Event) => void) | null) => void;
}

interface StubNamespace {
  nonce: string;
  diagnostics?: CaptureNamespace;
  loaded: { core: boolean; pick: boolean };
}

type CaptureWindow = Window &
  typeof globalThis & {
    __omniCapture?: CaptureNamespace;
    __omniAnnotate?: StubNamespace;
  };

const windows: CaptureWindow[] = [];

function spawnWindow(
  html = "<!doctype html><html><head></head><body></body></html>",
): CaptureWindow {
  const dom = new JSDOM(html, { url: FRAME_URL, runScripts: "outside-only" });
  const win = dom.window as CaptureWindow;
  windows.push(win);
  return win;
}

/** Evaluate a raw asset like the server's inline <script> with its nonce. */
function evalAsset(win: CaptureWindow, source: string, nonce: string | null): void {
  const script = win.document.createElement("script");
  if (nonce !== null) script.dataset.omniNonce = nonce;
  Object.defineProperty(win.document, "currentScript", { value: script, configurable: true });
  try {
    win.eval(source);
  } finally {
    delete (win.document as { currentScript?: unknown }).currentScript;
  }
}

/**
 * jsdom dispatches untrusted events and the stub acts only on `isTrusted`, so
 * tests mark their synthetic events trusted. `implForWrapper` reaches the impl
 * the wrapper's getter reads; jsdom's dispatch resets a plain property, so the
 * override ignores writes.
 */
function trustEvent(event: Event): void {
  Object.defineProperty(implForWrapper(event), "isTrusted", {
    configurable: true,
    get: () => true,
    set: () => {},
  });
}

/** Evaluate the capture asset with a nonce and return its namespace. */
function capture(win: CaptureWindow, nonce: string | null = NONCE): CaptureNamespace {
  evalAsset(win, captureSource, nonce);
  const ns = win.__omniCapture;
  if (!ns) throw new Error("capture namespace missing");
  return ns;
}

function stubFetch(
  win: CaptureWindow,
  impl: (...args: unknown[]) => unknown,
): ReturnType<typeof vi.fn> {
  const fn = vi.fn(impl);
  win.fetch = fn as unknown as typeof win.fetch;
  return fn;
}

/** A prototype-method XHR stand-in so the capture wrapper can patch open/send. */
class FakeXhr {
  status = 0;
  listeners: Record<string, (() => void)[]> = {};

  open(): void {}

  send(): void {}

  addEventListener(type: string, fn: () => void): void {
    (this.listeners[type] ||= []).push(fn);
  }

  emit(type: string): void {
    for (const fn of this.listeners[type] ?? []) fn();
  }
}

function settle(): Promise<void> {
  // MessagePort deliveries are macrotasks; a few turns flush a request/response.
  return new Promise((resolve) => {
    let turns = 0;
    const step = () => {
      turns++;
      if (turns >= 4) resolve();
      else setTimeout(step, 0);
    };
    setTimeout(step, 0);
  });
}

afterEach(() => {
  for (const win of windows) win.close();
  windows.length = 0;
});

describe("omni-html-capture.js", () => {
  it("captures console.error and warn, forwards to the originals, and skips log", () => {
    const win = spawnWindow();
    const errorSpy = vi.fn();
    const warnSpy = vi.fn();
    const logSpy = vi.fn();
    win.console.error = errorSpy;
    win.console.warn = warnSpy;
    win.console.log = logSpy;
    const ns = capture(win);

    win.console.error("x");
    win.console.warn(new win.Error("warned"));
    win.console.log("not captured");

    expect(errorSpy).toHaveBeenCalledWith("x");
    expect(warnSpy).toHaveBeenCalledTimes(1);
    expect(logSpy).toHaveBeenCalledWith("not captured");
    const snap = ns.snapshot();
    expect(snap.console).toEqual([
      { level: "error", message: "x", ts: expect.any(Number) },
      { level: "warn", message: expect.stringContaining("Error: warned"), ts: expect.any(Number) },
    ]);
    expect(snap.network).toEqual([]);
  });

  it("keeps the 50 newest console entries and truncates without splitting a surrogate", () => {
    const win = spawnWindow();
    win.console.error = vi.fn();
    const ns = capture(win);
    for (let i = 0; i < 60; i++) win.console.error(`e${i}`);
    const entries = ns.snapshot().console;
    expect(entries).toHaveLength(50);
    expect(entries[0]!.message).toBe("e10");
    expect(entries[49]!.message).toBe("e59");

    // 498 a's + a surrogate pair + a trailing code unit: the cap backs off to
    // the pair's boundary instead of leaving half an emoji.
    win.console.error("a".repeat(498) + "😀b");
    const last = ns.snapshot().console.at(-1)!;
    expect(last.message).toBe("a".repeat(498) + "…");
  });

  it("records a failed fetch with the query, hash and credentials dropped", async () => {
    const win = spawnWindow();
    stubFetch(win, async () => ({ ok: false, status: 404 }));
    const ns = capture(win);

    const response = await win.fetch("http://user:pass@localhost:6767/reports/q3.html?t=1#f");
    expect(response.status).toBe(404);
    expect(ns.snapshot().network).toEqual([
      {
        method: "GET",
        url: "http://localhost:6767/reports/q3.html",
        status: 404,
        ts: expect.any(Number),
      },
    ]);
  });

  it("records a rejected fetch as status 0 and rethrows the same rejection", async () => {
    const win = spawnWindow();
    const boom = new win.Error("offline");
    stubFetch(win, () => Promise.reject(boom));
    const ns = capture(win);

    await expect(win.fetch("http://localhost:6767/api/q3.json")).rejects.toBe(boom);
    expect(ns.snapshot().network).toEqual([
      {
        method: "GET",
        url: "http://localhost:6767/api/q3.json",
        status: 0,
        ts: expect.any(Number),
      },
    ]);
  });

  it("records nothing for a successful fetch", async () => {
    const win = spawnWindow();
    stubFetch(win, async () => ({ ok: true, status: 200 }));
    const ns = capture(win);

    await win.fetch("http://localhost:6767/reports/q3.html");
    expect(ns.snapshot().network).toEqual([]);
  });

  it("uppercases the requested method and empties a non-http url", async () => {
    const win = spawnWindow();
    const fetchStub = stubFetch(win, async () => ({
      ok: false,
      status: 500,
    }));
    const ns = capture(win);

    await win.fetch("http://localhost:6767/api/q3.json", { method: "post" });
    expect(ns.snapshot().network).toEqual([
      {
        method: "POST",
        url: "http://localhost:6767/api/q3.json",
        status: 500,
        ts: expect.any(Number),
      },
    ]);

    fetchStub.mockImplementation(() => Promise.reject(new win.Error("nope")));
    await expect(win.fetch("file:///private/reports/q3.html")).rejects.toThrow();
    const last = ns.snapshot().network.at(-1) as NetworkEntry;
    expect(last.url).toBe("");
    expect(last.status).toBe(0);
  });

  it("reads an init method getter zero extra times", async () => {
    const win = spawnWindow();
    const response = { ok: false, status: 500 };
    const fetchStub = stubFetch(win, async () => response);
    const ns = capture(win);
    let reads = 0;
    const init = {
      get method(): string {
        reads++;
        return "POST";
      },
    };

    await win.fetch("http://localhost:6767/api/q3.json", init as RequestInit);

    expect(reads).toBe(0);
    expect(fetchStub.mock.calls[0]![1]).toBe(init);
    expect(ns.snapshot().network).toEqual([
      {
        method: "GET",
        url: "http://localhost:6767/api/q3.json",
        status: 500,
        ts: expect.any(Number),
      },
    ]);
  });

  it("calls the captured native accessors instead of own accessors on the objects", async () => {
    const win = spawnWindow();
    let urlReads = 0;
    let methodReads = 0;
    // Native accessors brand-check their receiver; the captured getters must
    // be called directly, or a page-defined own accessor would run instead.
    class FakeRequest {
      get url(): string {
        if (!(this instanceof FakeRequest)) throw new win.TypeError("illegal receiver");
        return "http://localhost:6767/from-request";
      }

      get method(): string {
        if (!(this instanceof FakeRequest)) throw new win.TypeError("illegal receiver");
        return "post";
      }
    }
    win.Request = FakeRequest as unknown as typeof Request;
    const response = { ok: false, status: 500 };
    const fetchStub = stubFetch(win, async () => response);

    const request = new (win.Request as unknown as new () => object)();
    Object.defineProperty(request, "url", {
      configurable: true,
      get: () => {
        urlReads++;
        return "http://evil.test/own-url";
      },
    });
    Object.defineProperty(request, "method", {
      configurable: true,
      get: () => {
        methodReads++;
        return "PUT";
      },
    });
    const url = new win.URL("http://user:pass@localhost:6767/path?q=1#h");
    Object.defineProperty(url, "href", {
      configurable: true,
      get: () => {
        urlReads++;
        return "http://evil.test/own-href";
      },
    });

    const ns = capture(win);
    await win.fetch(request as unknown as RequestInfo);
    await win.fetch(url as unknown as RequestInfo);

    expect(urlReads).toBe(0);
    expect(methodReads).toBe(0);
    expect(fetchStub.mock.calls[0]![0]).toBe(request);
    expect(fetchStub.mock.calls[1]![0]).toBe(url);
    expect(ns.snapshot().network).toEqual([
      {
        method: "POST",
        url: "http://localhost:6767/from-request",
        status: 500,
        ts: expect.any(Number),
      },
      {
        method: "GET",
        url: "http://localhost:6767/path",
        status: 500,
        ts: expect.any(Number),
      },
    ]);
  });

  it("never inspects an arbitrary fetch input object", async () => {
    const win = spawnWindow();
    const input = {
      get url(): string {
        throw new win.Error("url getter must not be read");
      },
      get method(): string {
        throw new win.Error("method getter must not be read");
      },
    };
    const response = { ok: true, status: 200 };
    const fetchStub = stubFetch(win, async () => response);
    const ns = capture(win);

    await expect(win.fetch(input as unknown as RequestInfo)).resolves.toBe(response);
    expect(fetchStub).toHaveBeenCalledTimes(1);
    expect(fetchStub.mock.calls[0]![0]).toBe(input);
    expect(ns.snapshot().network).toEqual([]);

    // A native throw passes through untouched, exactly as without the script.
    const boom = new win.Error("native failure");
    fetchStub.mockImplementation(() => {
      throw boom;
    });
    expect(() => win.fetch(input as unknown as RequestInfo)).toThrow(boom);
  });

  it("records a failed XMLHttpRequest with the reduced url", () => {
    const win = spawnWindow();
    win.XMLHttpRequest = FakeXhr as unknown as typeof XMLHttpRequest;
    const ns = capture(win);

    const xhr = new (
      win.XMLHttpRequest as unknown as new () => {
        status: number;
        open: (...args: unknown[]) => void;
        send: () => void;
        emit: (type: string) => void;
      }
    )();
    xhr.open("POST", "http://localhost:6767/api/q3.json?token=secret");
    xhr.send();
    xhr.status = 500;
    xhr.emit("loadend");

    expect(ns.snapshot().network).toEqual([
      {
        method: "POST",
        url: "http://localhost:6767/api/q3.json",
        status: 500,
        ts: expect.any(Number),
      },
    ]);
  });

  it("records a resource error as a console failure and a status-0 network entry", () => {
    const win = spawnWindow();
    const ns = capture(win);

    const img = win.document.createElement("img");
    img.src = "http://localhost:6767/reports/missing.png";
    win.document.body.appendChild(img);
    img.dispatchEvent(new win.Event("error"));

    expect(ns.snapshot().console).toEqual([
      {
        level: "error",
        message: "Failed to load img http://localhost:6767/reports/missing.png",
        ts: expect.any(Number),
      },
    ]);
    expect(ns.snapshot().network).toEqual([
      {
        method: "GET",
        url: "http://localhost:6767/reports/missing.png",
        status: 0,
        ts: expect.any(Number),
      },
    ]);
  });

  it("records an uncaught error with its message and location", () => {
    const win = spawnWindow();
    const ns = capture(win);

    const event = new win.ErrorEvent("error", {
      message: "boom",
      filename: "http://localhost:6767/reports/q3.js",
      lineno: 12,
    });
    win.dispatchEvent(event);

    expect(ns.snapshot().console).toEqual([
      {
        level: "error",
        message: "boom http://localhost:6767/reports/q3.js:12",
        ts: expect.any(Number),
      },
    ]);
  });

  it("records an unhandled rejection with the serialized reason", () => {
    const win = spawnWindow();
    const ns = capture(win);

    const event = new win.Event("unhandledrejection");
    Object.defineProperty(event, "reason", { value: new win.Error("kaboom") });
    win.dispatchEvent(event);

    const entries = ns.snapshot().console;
    expect(entries).toHaveLength(1);
    expect(entries[0]!.level).toBe("error");
    expect(entries[0]!.message).toContain("kaboom");
  });

  it("does nothing without a nonce", () => {
    const win = spawnWindow();
    const errorSpy = vi.fn();
    win.console.error = errorSpy;
    const fetchStub = vi.fn();
    win.fetch = fetchStub as unknown as typeof win.fetch;

    evalAsset(win, captureSource, null);

    expect(win.__omniCapture).toBeUndefined();
    win.console.error("x");
    expect(errorSpy).toHaveBeenCalledWith("x");
    expect(win.fetch).toBe(fetchStub);
  });

  it("installs itself only once", () => {
    const win = spawnWindow();
    win.console.error = vi.fn();
    const ns = capture(win);
    const wrapped = win.console.error;

    expect(() => evalAsset(win, captureSource, NONCE)).not.toThrow();
    expect(win.__omniCapture).toBe(ns);
    expect(win.console.error).toBe(wrapped);
  });

  it("calls the keydown slot before page listeners registered after it", () => {
    const win = spawnWindow();
    const ns = capture(win);
    const order: string[] = [];
    ns.setSlot("keydown", () => order.push("slot"));
    win.addEventListener("keydown", () => order.push("page"));

    win.document.body.dispatchEvent(
      new win.KeyboardEvent("keydown", { bubbles: true, cancelable: true, composed: true }),
    );
    expect(order).toEqual(["slot", "page"]);
  });

  it("runs a slot registered for any interactive type before later page listeners", () => {
    const win = spawnWindow();
    const ns = capture(win);
    const order: string[] = [];
    win.addEventListener("click", () => order.push("page"), true);
    ns.setSlot("click", () => order.push("slot"));

    win.document.body.dispatchEvent(
      new win.MouseEvent("click", { bubbles: true, cancelable: true, composed: true }),
    );
    expect(order).toEqual(["slot", "page"]);
  });

  it("runs a focus or blur slot before page listeners registered after it", () => {
    const win = spawnWindow();
    const ns = capture(win);
    const order: string[] = [];
    win.addEventListener("blur", () => order.push("page"), true);
    ns.setSlot("blur", () => order.push("slot"));

    win.dispatchEvent(new win.Event("blur"));
    expect(order).toEqual(["slot", "page"]);
  });

  it("clears a slot and ignores an unknown type", () => {
    const win = spawnWindow();
    const ns = capture(win);
    const slot = vi.fn();
    ns.setSlot("click", slot);
    ns.setSlot("bogus", slot);
    win.document.body.dispatchEvent(new win.MouseEvent("click", { bubbles: true }));

    expect(slot).toHaveBeenCalledTimes(1);
    ns.setSlot("click", null);
    win.document.body.dispatchEvent(new win.MouseEvent("click", { bubbles: true }));
    expect(slot).toHaveBeenCalledTimes(1);
  });

  it("does not route the interactive types the in-frame composer no longer uses", () => {
    const win = spawnWindow();
    const ns = capture(win);
    const slot = vi.fn();
    const deadTypes = ["keyup", "keypress", "beforeinput", "input"];
    for (const type of deadTypes) ns.setSlot(type, slot);
    for (const type of deadTypes) {
      win.document.body.dispatchEvent(
        new win.Event(type, { bubbles: true, cancelable: true, composed: true }),
      );
    }
    expect(slot).not.toHaveBeenCalled();
  });
});

describe("annotate stub integration", () => {
  it("reads diagnostics from the capture namespace and uses its keydown slot", async () => {
    const win = spawnWindow();
    win.console.error = vi.fn();
    const ns = capture(win);
    evalAsset(win, stubSource, NONCE);

    const annotate = win.__omniAnnotate!;
    expect(annotate.diagnostics).toBe(ns);
    win.console.error("early");
    expect(annotate.diagnostics!.snapshot().console[0]!.message).toBe("early");

    const channel = new MessageChannel();
    const messages: Record<string, unknown>[] = [];
    let markReady: () => void = () => {};
    const ready = new Promise<void>((resolve) => {
      markReady = resolve;
    });
    channel.port2.onmessage = (ev) => {
      messages.push(ev.data as Record<string, unknown>);
      if ((ev.data as { type?: string }).type === "annotate:ready") markReady();
    };
    win.dispatchEvent(
      new win.MessageEvent("message", {
        data: { source: SOURCE, nonce: NONCE, type: "annotate:init" },
        ports: [channel.port1],
      }),
    );
    await ready;

    channel.port2.postMessage({
      source: SOURCE,
      nonce: NONCE,
      type: "annotate:setShortcut",
      bindings: [{ code: "Period", ctrl: false, meta: true, alt: false, shift: true }],
    });
    await settle();

    const chord: KeyboardEventInit = {
      code: "Period",
      key: ".",
      metaKey: true,
      shiftKey: true,
    };
    const first = new win.KeyboardEvent("keydown", {
      bubbles: true,
      cancelable: true,
      composed: true,
      ...chord,
    });
    trustEvent(first);
    win.document.body.dispatchEvent(first);
    await settle();
    expect(messages.some((m) => m.type === "annotate:toggleRequested")).toBe(true);

    // The stub routed through the slot instead of adding its own window
    // listener: clearing the slot stops the chord from reaching it.
    ns.setSlot("keydown", null);
    messages.length = 0;
    const second = new win.KeyboardEvent("keydown", {
      bubbles: true,
      cancelable: true,
      composed: true,
      ...chord,
    });
    trustEvent(second);
    win.document.body.dispatchEvent(second);
    await settle();
    expect(messages.filter((m) => m.type === "annotate:toggleRequested")).toHaveLength(0);

    channel.port1.close();
    channel.port2.close();
  });

  it("ignores an untrusted shortcut chord and still swallows it", async () => {
    const win = spawnWindow();
    win.console.error = vi.fn();
    capture(win);
    evalAsset(win, stubSource, NONCE);

    const channel = new MessageChannel();
    const messages: Record<string, unknown>[] = [];
    let markReady: () => void = () => {};
    const ready = new Promise<void>((resolve) => {
      markReady = resolve;
    });
    channel.port2.onmessage = (ev) => {
      messages.push(ev.data as Record<string, unknown>);
      if ((ev.data as { type?: string }).type === "annotate:ready") markReady();
    };
    win.dispatchEvent(
      new win.MessageEvent("message", {
        data: { source: SOURCE, nonce: NONCE, type: "annotate:init" },
        ports: [channel.port1],
      }),
    );
    await ready;
    channel.port2.postMessage({
      source: SOURCE,
      nonce: NONCE,
      type: "annotate:setShortcut",
      bindings: [{ code: "Period", ctrl: false, meta: true, alt: false, shift: true }],
    });
    await settle();

    const chord: KeyboardEventInit = {
      code: "Period",
      key: ".",
      metaKey: true,
      shiftKey: true,
    };
    const page = vi.fn();
    win.addEventListener("keydown", page);
    win.document.body.dispatchEvent(
      new win.KeyboardEvent("keydown", {
        bubbles: true,
        cancelable: true,
        composed: true,
        ...chord,
      }),
    );
    await settle();
    expect(messages.filter((m) => m.type === "annotate:toggleRequested")).toHaveLength(0);
    expect(page).not.toHaveBeenCalled();

    const trusted = new win.KeyboardEvent("keydown", {
      bubbles: true,
      cancelable: true,
      composed: true,
      ...chord,
    });
    trustEvent(trusted);
    win.document.body.dispatchEvent(trusted);
    await settle();
    expect(messages.filter((m) => m.type === "annotate:toggleRequested")).toHaveLength(1);

    // Page JS can write a bypass flag on the namespace; an untrusted chord
    // must still not act.
    (win.__omniAnnotate as StubNamespace & { __acceptUntrusted?: boolean }).__acceptUntrusted =
      true;
    win.document.body.dispatchEvent(
      new win.KeyboardEvent("keydown", {
        bubbles: true,
        cancelable: true,
        composed: true,
        ...chord,
      }),
    );
    await settle();
    expect(messages.filter((m) => m.type === "annotate:toggleRequested")).toHaveLength(1);

    channel.port1.close();
    channel.port2.close();
  });
});
