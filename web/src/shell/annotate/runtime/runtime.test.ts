// In-frame annotation runtime: the stub asset (web/public/omni-html-annotate.js)
// evaluated like the server's inline <script>, plus the core runtime parts it
// loads (finder, anchor, resolver, markers). The harness mirrors
// htmlCommentBridge.test.ts's startBridge: one JSDOM window per test, the stub
// run with a stubbed document.currentScript, and a MessageChannel driving the
// annotate port.

import { afterEach, describe, expect, it, vi } from "vitest";
// @ts-expect-error jsdom ships no declarations and @types/jsdom is not a dependency
import { JSDOM } from "jsdom";
// @ts-expect-error jsdom ships no declarations and @types/jsdom is not a dependency
import { implForWrapper } from "jsdom/lib/generated/idl/utils.js";
import captureSource from "../../../../public/omni-html-capture.js?raw";
import stubSource from "../../../../public/omni-html-annotate.js?raw";
import anchorSource from "./anchor.js?raw";
import cropSource from "./crop.js?raw";
import finderSource from "./finder.js?raw";
import freezeSource from "./freeze.js?raw";
import markersSource from "./markers.js?raw";
import pickerSource from "./picker.js?raw";
import resolverSource from "./resolver.js?raw";

const NONCE = "annotate-nonce-1";
const SOURCE = "omni-html-annotate";
const BUNDLE_URL = "http://localhost:3000/omni/v1/artifacts/a1.x.y/index.html";
const CORE_SOURCES = [finderSource, anchorSource, resolverSource, markersSource];

// `ns.snapdom` stands in for the real IIFE: jsdom has no canvas, so the fake
// records the call and returns a small canvas-like object.
const SNAPDOM_OK = `
ns.snapdom = {
  toCanvas: function (root, options) {
    ns.__lastCapture = { root: root, options: options };
    var host = document.getElementById("__omni-annotate-host");
    var shield = document.getElementById("__omni-annotate-shield");
    ns.__hostVisibilityDuringCapture = host ? host.style.visibility : null;
    ns.__shieldVisibilityDuringCapture = shield ? shield.style.visibility : null;
    return Promise.resolve({
      width: 800,
      height: 400,
      toDataURL: function () {
        return "data:image/jpeg;base64,/9j/4AAQSkZJRg==";
      },
    });
  },
};
`;
const SNAPDOM_THROWS = `
ns.snapdom = {
  toCanvas: function () {
    return Promise.reject(new Error("boom"));
  },
};
`;
const SNAPDOM_HANGS = `
ns.snapdom = {
  toCanvas: function () {
    return new Promise(function () {});
  },
};
`;
const SNAPDOM_DEFERRED = `
ns.snapdom = {
  toCanvas: function () {
    ns.__captureCalls = (ns.__captureCalls || 0) + 1;
    return new Promise(function (resolve) {
      (ns.__captureResolvers = ns.__captureResolvers || []).push(resolve);
    });
  },
};
`;
const PICK_SOURCES = [freezeSource, pickerSource, SNAPDOM_OK, cropSource];

interface TargetQuote {
  exact: string;
  prefix: string;
  suffix: string;
}

interface Target {
  label: string;
  css: string;
  xpath: string;
  quote: TargetQuote;
  fingerprint: string;
  neighborText: string;
  tag: string;
  id: string;
  role: string;
  ariaLabel: string;
  text: string;
}

interface ResolvedItem {
  id: string;
  found: boolean;
}

interface CaptureResult {
  dataUrl: string;
  width: number;
  height: number;
}

interface CaptureCall {
  root: Element;
  options: Record<string, unknown>;
}

interface FreezeNamespace {
  on: (x?: number, y?: number) => void;
  off: () => void;
  openForHitTest: () => void;
  closeAfterHitTest: () => void;
}

interface AnnotateNamespace {
  nonce: string;
  loaded: { core: boolean; pick: boolean };
  generateTarget: (element: Element) => Target;
  resolveTarget: (target: Target) => Element | null;
  capture: (rect: { x: number; y: number; w: number; h: number }) => Promise<CaptureResult | null>;
  overlayRoot: () => ShadowRoot;
  freeze: FreezeNamespace;
  on: (type: string, handler: (msg: { items?: ResolvedItem[] }) => void) => void;
  send: (msg: Record<string, unknown>) => void;
  __lastCapture?: CaptureCall;
  __hostVisibilityDuringCapture?: string | null;
  __shieldVisibilityDuringCapture?: string | null;
  __captureCalls?: number;
  __captureResolvers?: ((canvas: unknown) => void)[];
}

type FrameWindow = Window & typeof globalThis & { __omniAnnotate: AnnotateNamespace };

type FrameMessage = Record<string, unknown>;

interface FrameChannel {
  channel: MessageChannel;
  messages: FrameMessage[];
  ready: Promise<void>;
}

const windows: FrameWindow[] = [];

// jsdom ships no CSS.escape (real frames have it); finder needs it for its
// selector literals. Minimal spec algorithm, enough for the fixtures' names.
function cssEscape(value: string): string {
  let out = "";
  for (let i = 0; i < value.length; i++) {
    const ch = value[i]!;
    const code = ch.charCodeAt(0);
    if (ch === "\0") {
      out += "\uFFFD";
    } else if ((code >= 1 && code <= 31) || code === 127) {
      out += "\\" + code.toString(16) + " ";
    } else if (i === 0 && code >= 48 && code <= 57) {
      out += "\\" + code.toString(16) + " ";
    } else if (i === 1 && code >= 48 && code <= 57 && value[0] === "-") {
      out += "\\" + code.toString(16) + " ";
    } else if (
      code >= 128 ||
      ch === "-" ||
      ch === "_" ||
      (code >= 48 && code <= 57) ||
      (code >= 65 && code <= 90) ||
      (code >= 97 && code <= 122)
    ) {
      out += ch;
    } else {
      out += "\\" + ch;
    }
  }
  return out;
}

function installCssEscape(win: FrameWindow): void {
  if (!("CSS" in win)) {
    (win as unknown as { CSS: { escape: (value: string) => string } }).CSS = {
      escape: cssEscape,
    };
  }
}

/**
 * jsdom dispatches untrusted events and the runtime acts only on `isTrusted`,
 * so tests mark their synthetic events trusted. `implForWrapper` reaches the
 * impl the wrapper's getter reads; jsdom's dispatch resets a plain property, so
 * the override ignores writes.
 */
function trustEvent(event: Event): void {
  Object.defineProperty(implForWrapper(event), "isTrusted", {
    configurable: true,
    get: () => true,
    set: () => {},
  });
}

/** Evaluate a raw asset like the server's inline <script> with its nonce. */
function evalAsset(win: FrameWindow, source: string, nonce: string | null): void {
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
 * Evaluate the stub like the server's inline <script> in a window of its own.
 * `withCapture` first runs the head capture script, as the server's head
 * injection does, so the picker wires through its early slots.
 */
function startFrame(html: string, nonce: string | null = NONCE, withCapture = false): FrameWindow {
  const dom = new JSDOM(`<!doctype html><html><head></head><body>${html}</body></html>`, {
    url: BUNDLE_URL,
    runScripts: "dangerously",
  });
  const win = dom.window as FrameWindow;
  installCssEscape(win);
  windows.push(win);
  if (withCapture) evalAsset(win, captureSource, nonce);
  evalAsset(win, stubSource, nonce);
  return win;
}

/** Post annotate:init with a fresh port; collect everything the frame sends. */
function initFrame(win: FrameWindow, data: Record<string, unknown> = {}): FrameChannel {
  const channel = new MessageChannel();
  const messages: FrameMessage[] = [];
  let markReady: () => void = () => {};
  const ready = new Promise<void>((resolve) => {
    markReady = resolve;
  });
  channel.port2.onmessage = (ev) => {
    const msg = ev.data as FrameMessage;
    messages.push(msg);
    if (msg.type === "annotate:ready") markReady();
  };
  win.dispatchEvent(
    new win.MessageEvent("message", {
      data: { source: SOURCE, nonce: NONCE, type: "annotate:init", ...data },
      ports: [channel.port1],
    }),
  );
  return { channel, messages, ready };
}

function settle(): Promise<void> {
  // MessagePort deliveries are macrotasks and a request/response pair is two
  // hops; a few turns flush both without observing timing.
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

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

function waitFor(check: () => boolean, timeoutMs = 2000): Promise<void> {
  return new Promise((resolve) => {
    const deadline = Date.now() + timeoutMs;
    const poll = () => {
      if (check() || Date.now() > deadline) resolve();
      else setTimeout(poll, 5);
    };
    poll();
  });
}

function lastOf(messages: FrameMessage[], type: string): FrameMessage | undefined {
  for (let i = messages.length - 1; i >= 0; i--) {
    if (messages[i].type === type) return messages[i];
  }
  return undefined;
}

async function loadCore(win: FrameWindow): Promise<FrameChannel> {
  const frame = initFrame(win);
  await frame.ready;
  frame.messages.length = 0;
  frame.channel.port2.postMessage({
    source: SOURCE,
    nonce: NONCE,
    type: "annotate:loadRuntime",
    part: "core",
    sources: CORE_SOURCES,
  });
  await waitFor(() => lastOf(frame.messages, "annotate:runtimeLoaded") !== undefined);
  expect(lastOf(frame.messages, "annotate:runtimeLoaded")).toMatchObject({
    part: "core",
    ok: true,
  });
  return frame;
}

async function loadPick(win: FrameWindow, sources: string[] = PICK_SOURCES): Promise<FrameChannel> {
  const frame = await loadCore(win);
  frame.messages.length = 0;
  frame.channel.port2.postMessage({
    source: SOURCE,
    nonce: NONCE,
    type: "annotate:loadRuntime",
    part: "pick",
    sources,
  });
  await waitFor(() => lastOf(frame.messages, "annotate:runtimeLoaded") !== undefined);
  expect(lastOf(frame.messages, "annotate:runtimeLoaded")).toMatchObject({
    part: "pick",
    ok: true,
  });
  expect(win.__omniAnnotate.loaded.pick).toBe(true);
  frame.messages.length = 0;
  return frame;
}

async function setMode(frame: FrameChannel, on: boolean): Promise<void> {
  frame.channel.port2.postMessage({ source: SOURCE, nonce: NONCE, type: "annotate:setMode", on });
  await settle();
}

async function setShortcut(
  frame: FrameChannel,
  bindings: Record<string, unknown>[],
): Promise<void> {
  frame.channel.port2.postMessage({
    source: SOURCE,
    nonce: NONCE,
    type: "annotate:setShortcut",
    bindings,
  });
  await settle();
}

/** jsdom has no layout: give the element the rect the picker would read. */
function stubRect(el: Element, left: number, top: number, width: number, height: number): void {
  el.getBoundingClientRect = () =>
    ({
      x: left,
      y: top,
      left,
      top,
      width,
      height,
      right: left + width,
      bottom: top + height,
      toJSON: () => ({}),
    }) as DOMRect;
}

/** jsdom does not implement hit testing; every point maps to `element`. */
function stubElementFromPoint(win: FrameWindow, element: Element | null): void {
  (
    win.document as unknown as { elementFromPoint: (x: number, y: number) => Element | null }
  ).elementFromPoint = () => element;
}

/** jsdom ships no rAF pair; count the frames the runtime schedules. */
function installRaf(win: FrameWindow): () => number {
  let frames = 0;
  const target = win as unknown as {
    requestAnimationFrame: (cb: () => void) => number;
    cancelAnimationFrame: (id: number) => void;
  };
  target.requestAnimationFrame = (cb) => {
    frames++;
    return win.setTimeout(cb, 0) as unknown as number;
  };
  target.cancelAnimationFrame = (id) => {
    win.clearTimeout(id as unknown as number);
  };
  return () => frames;
}

function press(
  win: FrameWindow,
  target: EventTarget,
  init: KeyboardEventInit,
  trusted = true,
): void {
  const event = new win.KeyboardEvent("keydown", {
    bubbles: true,
    cancelable: true,
    composed: true,
    ...init,
  });
  if (trusted) trustEvent(event);
  target.dispatchEvent(event);
}

function clickAt(win: FrameWindow, target: Element, x: number, y: number, trusted = true): void {
  for (const type of ["pointerdown", "mousedown", "pointerup", "mouseup", "click"]) {
    const event = new win.MouseEvent(type, {
      bubbles: true,
      cancelable: true,
      composed: true,
      clientX: x,
      clientY: y,
    });
    if (trusted) trustEvent(event);
    target.dispatchEvent(event);
  }
}

async function setAnnotations(
  frame: FrameChannel,
  items: { id: string; n: number; anchor: { target: Target } }[],
): Promise<FrameMessage> {
  frame.messages.length = 0;
  frame.channel.port2.postMessage({
    source: SOURCE,
    nonce: NONCE,
    type: "annotate:setAnnotations",
    items,
  });
  await waitFor(() => lastOf(frame.messages, "annotate:resolved") !== undefined);
  const resolved = lastOf(frame.messages, "annotate:resolved");
  expect(resolved).toBeDefined();
  return resolved!;
}

afterEach(() => {
  for (const win of windows) win.close();
  windows.length = 0;
});

// ---------------------------------------------------------------------------
// omni-html-annotate.js — handshake and runtime loading
// ---------------------------------------------------------------------------

describe("omni-html-annotate.js stub", () => {
  it("stays inert without a nonce", async () => {
    const win = startFrame("<p>x</p>", null);
    expect((win as Partial<FrameWindow>).__omniAnnotate).toBeUndefined();

    const { channel, messages } = initFrame(win);
    await settle();
    expect(messages).toEqual([]);
    channel.port1.close();
    channel.port2.close();
  });

  it("ignores init with a wrong nonce or source", async () => {
    const win = startFrame("<p>x</p>");
    const wrongNonce = initFrame(win, { nonce: "other" });
    const wrongSource = initFrame(win, { source: "evil" });
    await settle();
    expect(wrongNonce.messages).toEqual([]);
    expect(wrongSource.messages).toEqual([]);
  });

  it("adopts the port and reports its pathname", async () => {
    const win = startFrame("<p>x</p>");
    const frame = initFrame(win);
    await frame.ready;
    expect(frame.messages[0]).toMatchObject({
      source: SOURCE,
      nonce: NONCE,
      type: "annotate:ready",
      pathname: "/omni/v1/artifacts/a1.x.y/index.html",
    });
  });

  it("loads the core sources and marks them loaded", async () => {
    const win = startFrame("<p>x</p>");
    await loadCore(win);
    expect(win.__omniAnnotate.loaded.core).toBe(true);
    expect(typeof win.__omniAnnotate.generateTarget).toBe("function");
    expect(typeof win.__omniAnnotate.resolveTarget).toBe("function");
    expect(typeof win.__omniAnnotate.overlayRoot).toBe("function");
  });

  it("reports a throwing source as not loaded", async () => {
    const win = startFrame("<p>x</p>");
    const frame = initFrame(win);
    await frame.ready;
    frame.messages.length = 0;
    frame.channel.port2.postMessage({
      source: SOURCE,
      nonce: NONCE,
      type: "annotate:loadRuntime",
      part: "pick",
      sources: ['ns.__boom = 1; throw new Error("boom");'],
    });
    await waitFor(() => lastOf(frame.messages, "annotate:runtimeLoaded") !== undefined);
    expect(lastOf(frame.messages, "annotate:runtimeLoaded")).toMatchObject({
      part: "pick",
      ok: false,
    });
    expect(win.__omniAnnotate.loaded.pick).toBe(false);
  });

  it("reports a syntax-error source as not loaded", async () => {
    const win = startFrame("<p>x</p>");
    const frame = initFrame(win);
    await frame.ready;
    frame.messages.length = 0;
    frame.channel.port2.postMessage({
      source: SOURCE,
      nonce: NONCE,
      type: "annotate:loadRuntime",
      part: "pick",
      sources: ["function ({"],
    });
    await waitFor(() => lastOf(frame.messages, "annotate:runtimeLoaded") !== undefined);
    expect(lastOf(frame.messages, "annotate:runtimeLoaded")).toMatchObject({
      part: "pick",
      ok: false,
    });
    expect(win.__omniAnnotate.loaded.pick).toBe(false);
  });

  it("replaces the previous port on re-init", async () => {
    const win = startFrame("<p>x</p>");
    const first = initFrame(win);
    await first.ready;
    const second = initFrame(win);
    await second.ready;
    first.messages.length = 0;
    second.messages.length = 0;
    second.channel.port2.postMessage({
      source: SOURCE,
      nonce: NONCE,
      type: "annotate:loadRuntime",
      part: "core",
      sources: [],
    });
    await waitFor(() => lastOf(second.messages, "annotate:runtimeLoaded") !== undefined);
    expect(lastOf(second.messages, "annotate:runtimeLoaded")).toMatchObject({
      part: "core",
      ok: true,
    });
    expect(first.messages).toEqual([]);
  });

  it("compiles every runtime source as a function body and the stub as a script", () => {
    for (const source of [...CORE_SOURCES, ...PICK_SOURCES]) {
      expect(() => new Function("ns", source)).not.toThrow();
    }
    expect(() => new Function(stubSource)).not.toThrow();
  });
});

// ---------------------------------------------------------------------------
// stub shortcut — chord matching before and after the pick runtime loads
// ---------------------------------------------------------------------------

describe("stub shortcut", () => {
  const CHORD = [{ code: "Period", ctrl: false, meta: true, alt: false, shift: true }];
  const CHORD_INIT: KeyboardEventInit = {
    code: "Period",
    key: ".",
    metaKey: true,
    shiftKey: true,
  };

  it("asks the parent to load the runtime when pressed before pick is loaded", async () => {
    const win = startFrame("<p>x</p>");
    const frame = await loadCore(win);
    await setShortcut(frame, CHORD);
    press(win, win.document.body, { ...CHORD_INIT, key: "Control", ctrlKey: true, metaKey: false });
    await settle();
    expect(lastOf(frame.messages, "annotate:toggleRequested")).toBeUndefined();

    press(win, win.document.body, CHORD_INIT);
    await settle();
    expect(lastOf(frame.messages, "annotate:toggleRequested")).toBeDefined();
    expect(win.__omniAnnotate.loaded.pick).toBe(false);
  });

  it("asks the parent to turn the mode on and leaves it in-frame when on", async () => {
    const win = startFrame("<p>x</p>");
    const frame = await loadPick(win);
    await setShortcut(frame, CHORD);
    frame.messages.length = 0;

    // Off: only the parent may turn the mode on.
    press(win, win.document.body, CHORD_INIT);
    await settle();
    expect(lastOf(frame.messages, "annotate:toggleRequested")).toBeDefined();
    expect(lastOf(frame.messages, "annotate:modeChanged")).toBeUndefined();
    const root = win.__omniAnnotate.overlayRoot();
    expect(root.querySelectorAll("[data-omni-pick]")).toHaveLength(0);

    // On: the frame leaves locally and reports the change.
    await setMode(frame, true);
    frame.messages.length = 0;
    press(win, win.document.body, CHORD_INIT);
    await settle();
    expect(lastOf(frame.messages, "annotate:modeChanged")).toMatchObject({
      on: false,
      reason: "shortcut",
    });
    expect(root.querySelectorAll("[data-omni-pick]")).toHaveLength(0);
  });

  it("ignores a chord whose modifiers do not match exactly", async () => {
    const win = startFrame("<p>x</p>");
    const frame = await loadPick(win);
    await setShortcut(frame, CHORD);
    frame.messages.length = 0;

    press(win, win.document.body, { ...CHORD_INIT, shiftKey: false });
    press(win, win.document.body, { ...CHORD_INIT, altKey: true });
    press(win, win.document.body, { ...CHORD_INIT, code: "Comma" });
    await settle();
    expect(frame.messages).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// generateTarget — anchor fields and caps
// ---------------------------------------------------------------------------

const FIXTURE =
  '<div id="panel">' +
  '<div class="filter-menu"><button class="option primary">Alpha</button></div>' +
  '<button id="unique-widget">Unique</button>' +
  '<button aria-label="Close"><svg viewBox="0 0 10 10"></svg></button>' +
  "</div>";

describe("generateTarget", () => {
  it("captures id, css and xpath for an element with an id", async () => {
    const win = startFrame(FIXTURE);
    await loadCore(win);
    const target = win.__omniAnnotate.generateTarget(win.document.getElementById("unique-widget")!);
    expect(target.id).toBe("unique-widget");
    expect(target.css).toContain("#unique-widget");
    expect(target.xpath).toBe("//button[@id='unique-widget']");
    expect(target.tag).toBe("BUTTON");
    expect(target.text).toBe("Unique");
  });

  it("builds a label path from word-like classes and first two classes", async () => {
    const win = startFrame(FIXTURE);
    await loadCore(win);
    const target = win.__omniAnnotate.generateTarget(win.document.querySelector(".option")!);
    expect(target.id).toBe("");
    expect(target.label.endsWith("div.filter-menu > button.option.primary")).toBe(true);
    expect(target.label.split(" > ").length).toBeLessThanOrEqual(4);
    expect(target.css).toContain(".option");
  });

  it("skips hash-like class names in the label", async () => {
    const win = startFrame('<div class="sc-bdfBQB real-name"><button class="r0">Go</button></div>');
    await loadCore(win);
    const target = win.__omniAnnotate.generateTarget(win.document.querySelector("button")!);
    expect(target.label.endsWith("div.real-name > button")).toBe(true);
  });

  it("captures aria-label and fingerprint for an icon button without text", async () => {
    const html = '<button aria-label="Close"><svg viewBox="0 0 10 10"></svg></button>';
    const win = startFrame(html);
    await loadCore(win);
    const target = win.__omniAnnotate.generateTarget(win.document.querySelector("button")!);
    expect(target.ariaLabel).toBe("Close");
    expect(target.role).toBe("");
    expect(target.text).toBe("");
    expect(target.quote.exact).toBe("");
    expect(target.fingerprint).toMatch(/^\d+:\d+:[0-9a-z]+$/);

    // T6: the anchor resolves on a fresh load of the same page.
    const reloaded = startFrame(html);
    await loadCore(reloaded);
    expect(reloaded.__omniAnnotate.resolveTarget(target)).toBe(
      reloaded.document.querySelector("button"),
    );
  });

  it("joins open-shadow-tree selectors with the deep boundary", async () => {
    const win = startFrame("");
    await loadCore(win);
    const host = win.document.createElement("div");
    host.id = "shadow-host";
    win.document.body.appendChild(host);
    const root = host.attachShadow({ mode: "open" });
    root.innerHTML = '<span class="deep-label">Shadow text</span>';
    const inner = root.querySelector(".deep-label")!;

    const target = win.__omniAnnotate.generateTarget(inner);
    expect(target.css).toContain(" >>> ");
    expect(target.css.split(" >>> ")).toHaveLength(2);
    expect(win.__omniAnnotate.resolveTarget(target)).toBe(inner);
  });

  it("caps long text, quotes, ids and paths", async () => {
    const win = startFrame('<p id="before">Before</p><p id="long"></p>');
    await loadCore(win);
    const long = win.document.getElementById("long")!;
    long.textContent = "A".repeat(10000);

    const target = win.__omniAnnotate.generateTarget(long);
    expect(target.text.length).toBeLessThanOrEqual(200);
    expect(target.quote.exact.length).toBeLessThanOrEqual(200);
    expect(target.quote.prefix).toBe("Before");
    expect(target.quote.prefix.length).toBeLessThanOrEqual(32);
    expect(target.quote.suffix.length).toBeLessThanOrEqual(32);
    expect(target.label.length).toBeLessThanOrEqual(200);
    expect(target.css.length).toBeLessThanOrEqual(700);
    expect(target.xpath.length).toBeLessThanOrEqual(900);
    expect(target.fingerprint.length).toBeLessThanOrEqual(120);
    expect(target.neighborText.length).toBeLessThanOrEqual(80);
    expect(target.tag.length).toBeLessThanOrEqual(32);

    const longId = win.document.createElement("div");
    longId.id = "x".repeat(300);
    win.document.body.appendChild(longId);
    expect(win.__omniAnnotate.generateTarget(longId).id).toBe("");
  });
});

// ---------------------------------------------------------------------------
// resolveTarget — re-anchoring after DOM changes
// ---------------------------------------------------------------------------

describe("resolveTarget", () => {
  it("still resolves after the selector's class changes", async () => {
    const win = startFrame(
      '<div class="card"><button class="option primary">Delete</button></div>',
    );
    await loadCore(win);
    const ns = win.__omniAnnotate;
    const button = win.document.querySelector("button")!;
    const target = ns.generateTarget(button);
    button.className = "renamed";
    expect(ns.resolveTarget(target)).toBe(button);
  });

  it("returns null once the element is gone", async () => {
    const win = startFrame('<div class="card"><button class="option">Delete</button></div>');
    await loadCore(win);
    const ns = win.__omniAnnotate;
    const button = win.document.querySelector("button")!;
    const target = ns.generateTarget(button);
    button.remove();
    expect(ns.resolveTarget(target)).toBeNull();
  });

  it("picks the right one of near-identical siblings", async () => {
    const win = startFrame(
      '<ul id="list"><li class="row">Same</li><li class="row">Same</li><li class="row">Same</li></ul>',
    );
    await loadCore(win);
    const ns = win.__omniAnnotate;
    const rows = win.document.querySelectorAll("li");
    const second = rows[1]!;
    const target = ns.generateTarget(second);
    // Break the stored class selector; only verification can pick the row.
    for (let i = 0; i < rows.length; i++) rows[i]!.removeAttribute("class");
    expect(ns.resolveTarget(target)).toBe(second);
  });

  it("resolves through the xpath candidate after the id and css stop matching", async () => {
    // A dynamic id finder refuses: the CSS is class-based while the XPath
    // carries the DOM id. Dropping the stored id and rewriting the classes
    // leaves the XPath candidate as the one that still resolves the button.
    const win = startFrame(
      '<div class="card"><button id=":r0:" class="option primary">Delete</button></div>',
    );
    await loadCore(win);
    const ns = win.__omniAnnotate;
    const button = win.document.querySelector("button")!;
    const target = ns.generateTarget(button);
    expect(target.id).toBe(":r0:");
    expect(target.xpath).toBe("//button[@id=':r0:']");
    expect(target.css).not.toContain(":r0:");
    target.id = "";
    button.className = "renamed";
    expect(ns.resolveTarget(target)).toBe(button);
  });
});

// ---------------------------------------------------------------------------
// picker — mode lifecycle, hover outline, click pick and composer
// ---------------------------------------------------------------------------

describe("picker", () => {
  it("outlines the hovered element, consumes page events and cleans up on exit", async () => {
    const win = startFrame('<button id="target">Pick me</button>');
    const frame = await loadPick(win);
    const ns = win.__omniAnnotate;
    const button = win.document.getElementById("target")!;
    let pageClicks = 0;
    button.addEventListener("click", () => pageClicks++);

    stubRect(button, 10, 20, 100, 30);
    stubElementFromPoint(win, button);
    await setMode(frame, true);
    expect(ns.overlayRoot().querySelectorAll("[data-omni-pick]").length).toBeGreaterThan(0);
    expect(win.document.getElementById("__omni-annotate-cursor")).not.toBeNull();

    const move = new win.MouseEvent("pointermove", {
      bubbles: true,
      cancelable: true,
      composed: true,
      clientX: 50,
      clientY: 30,
    });
    trustEvent(move);
    button.dispatchEvent(move);
    const outline = ns.overlayRoot().querySelector('[data-omni-pick="outline"]') as HTMLElement;
    const hoverLabel = ns
      .overlayRoot()
      .querySelector('[data-omni-pick="hover-label"]') as HTMLElement;
    expect(outline.style.display).toBe("block");
    expect(outline.style.width).toBe("100px");
    expect(hoverLabel.textContent).toBe("button  100×30");

    button.dispatchEvent(
      new win.MouseEvent("click", { bubbles: true, cancelable: true, composed: true }),
    );
    expect(pageClicks).toBe(0);

    await setMode(frame, false);
    expect(ns.overlayRoot().querySelectorAll("[data-omni-pick]")).toHaveLength(0);
    expect(win.document.getElementById("__omni-annotate-cursor")).toBeNull();
    button.dispatchEvent(
      new win.MouseEvent("click", { bubbles: true, cancelable: true, composed: true }),
    );
    expect(pageClicks).toBe(1);
  });

  it("drills through an open shadow root when hit testing", async () => {
    const win = startFrame('<div id="host"></div>');
    const frame = await loadPick(win);
    const ns = win.__omniAnnotate;
    const hostEl = win.document.getElementById("host")!;
    const shadow = hostEl.attachShadow({ mode: "open" });
    const inner = win.document.createElement("span");
    inner.textContent = "deep";
    shadow.appendChild(inner);
    // jsdom ships no ShadowRoot.elementFromPoint; real frames have it.
    (shadow as unknown as { elementFromPoint: () => Element }).elementFromPoint = () => inner;
    stubRect(inner, 5, 6, 40, 10);
    stubElementFromPoint(win, hostEl);
    await setMode(frame, true);

    const move = new win.MouseEvent("pointermove", {
      bubbles: true,
      cancelable: true,
      composed: true,
      clientX: 10,
      clientY: 10,
    });
    trustEvent(move);
    hostEl.dispatchEvent(move);
    const hoverLabel = ns
      .overlayRoot()
      .querySelector('[data-omni-pick="hover-label"]') as HTMLElement;
    expect(hoverLabel.textContent).toBe("span  40×10");
  });

  it("posts the pick with its viewport rect, keeps the outline and ignores further picks", async () => {
    const win = startFrame('<button id="target">Pick me</button>');
    const frame = await loadPick(win);
    const ns = win.__omniAnnotate;
    const button = win.document.getElementById("target")!;
    stubRect(button, 10, 20, 100, 30);
    stubElementFromPoint(win, button);
    await setMode(frame, true);

    clickAt(win, button, 50, 30);
    await settle();

    const picked = lastOf(frame.messages, "annotate:picked");
    expect(picked).toBeDefined();
    expect(frame.messages.filter((m) => m.type === "annotate:picked")).toHaveLength(1);
    expect(picked).not.toHaveProperty("note");
    expect(picked).not.toHaveProperty("action");
    expect(picked!.viewportRect).toEqual({ x: 10, y: 20, w: 100, h: 30 });
    const anchor = picked!.anchor as {
      kind: string;
      target: Target;
      rect: Record<string, number>;
    };
    expect(anchor.kind).toBe("element");
    expect(anchor.rect).toMatchObject({ x: 10, y: 20, w: 100, h: 30 });
    // The click path still captures with the crop margin around the element.
    expect(ns.__lastCapture?.options.clip).toMatchObject({ x: 0, y: 4, width: 126, height: 62 });
    // The anchor's css resolves back to the picked element.
    expect(win.document.querySelector(anchor.target.css)).toBe(button);

    const root = ns.overlayRoot();
    const selected = root.querySelector('[data-omni-pick="selected"]') as HTMLElement;
    expect(selected.style.display).toBe("block");
    expect(selected.style.width).toBe("100px");

    // A second pick is consumed but ignored until the parent closes the pick.
    clickAt(win, button, 50, 30);
    await settle();
    expect(frame.messages.filter((m) => m.type === "annotate:picked")).toHaveLength(1);

    frame.channel.port2.postMessage({ source: SOURCE, nonce: NONCE, type: "annotate:pickDone" });
    await settle();
    expect(selected.style.display).toBe("none");

    clickAt(win, button, 50, 30);
    await settle();
    expect(frame.messages.filter((m) => m.type === "annotate:picked")).toHaveLength(2);
  });

  it("leaves the mode on Escape with a pending pick and clears the outline", async () => {
    const win = startFrame('<button id="target">Pick me</button>');
    const frame = await loadPick(win);
    const ns = win.__omniAnnotate;
    const button = win.document.getElementById("target")!;
    stubRect(button, 10, 20, 100, 30);
    stubElementFromPoint(win, button);
    await setMode(frame, true);

    clickAt(win, button, 50, 30);
    await settle();
    expect(
      (ns.overlayRoot().querySelector('[data-omni-pick="selected"]') as HTMLElement).style.display,
    ).toBe("block");

    press(win, win.document.body, { key: "Escape", code: "Escape" });
    await settle();
    expect(lastOf(frame.messages, "annotate:modeChanged")).toMatchObject({
      on: false,
      reason: "escape",
    });
    expect(ns.overlayRoot().querySelectorAll("[data-omni-pick]")).toHaveLength(0);
  });

  it("picks a region on a drag and captures the drawn box", async () => {
    const win = startFrame('<p id="para">The quick brown fox jumps over the lazy dog</p>');
    const frame = await loadPick(win);
    const ns = win.__omniAnnotate;
    const para = win.document.getElementById("para")!;
    stubRect(para, 10, 20, 200, 40);
    stubElementFromPoint(win, para);
    // jsdom has no layout: the paragraph's text range covers the box.
    const rectLike = { left: 10, top: 20, right: 210, bottom: 60 } as DOMRect;
    (
      win.Range.prototype as unknown as { getClientRects: () => ArrayLike<DOMRect> }
    ).getClientRects = () => [rectLike];
    await setMode(frame, true);

    const drop = (type: string, clientX: number, clientY: number) => {
      const event = new win.MouseEvent(type, {
        bubbles: true,
        cancelable: true,
        composed: true,
        clientX,
        clientY,
      });
      trustEvent(event);
      para.dispatchEvent(event);
    };
    drop("pointerdown", 20, 30);
    drop("pointermove", 100, 60);
    drop("pointerup", 100, 60);
    await settle();

    const picked = lastOf(frame.messages, "annotate:picked");
    expect(picked).toBeDefined();
    expect(frame.messages.filter((m) => m.type === "annotate:picked")).toHaveLength(1);
    expect(picked!.viewportRect).toEqual({ x: 20, y: 30, w: 80, h: 30 });
    const anchor = picked!.anchor as {
      kind: string;
      rect: Record<string, number>;
      region: Record<string, number>;
      selectedText: string;
    };
    expect(anchor.kind).toBe("region");
    expect(anchor.region).toEqual({ x: 20, y: 30, w: 80, h: 30 });
    expect(anchor.rect).toEqual({ x: 10, y: 20, w: 200, h: 40 });
    expect(anchor.selectedText).toBe("The quick brown fox jumps over the lazy dog");
    expect(ns.__lastCapture?.options.clip).toMatchObject({ x: 20, y: 30, width: 80, height: 30 });

    // The selected outline is the drawn region box, not the covering element.
    const selected = ns.overlayRoot().querySelector('[data-omni-pick="selected"]') as HTMLElement;
    expect(selected.style.display).toBe("block");
    expect(selected.style.left).toBe("20px");
    expect(selected.style.width).toBe("80px");
  });

  it("tears the whole mode down on pagehide", async () => {
    const win = startFrame('<button id="target">Pick me</button>');
    const frame = await loadPick(win);
    const ns = win.__omniAnnotate;
    const button = win.document.getElementById("target")!;
    stubRect(button, 10, 20, 100, 30);
    stubElementFromPoint(win, button);
    installRaf(win);
    const originalRequest = win.requestAnimationFrame;

    await setMode(frame, true);
    expect(win.document.getElementById("__omni-annotate-shield")).not.toBeNull();
    expect(win.requestAnimationFrame).not.toBe(originalRequest);

    win.dispatchEvent(new win.Event("pagehide"));
    await settle();
    expect(ns.overlayRoot().querySelectorAll("[data-omni-pick]")).toHaveLength(0);
    expect(win.document.getElementById("__omni-annotate-cursor")).toBeNull();
    expect(win.document.getElementById("__omni-annotate-shield")).toBeNull();
    expect(win.requestAnimationFrame).toBe(originalRequest);
  });

  it("accepts one capture at a time and drops a capture from a left mode", async () => {
    const win = startFrame('<button id="target">Pick me</button>');
    const frame = await loadPick(win, [freezeSource, pickerSource, SNAPDOM_DEFERRED, cropSource]);
    const ns = win.__omniAnnotate;
    const button = win.document.getElementById("target")!;
    const canvas = {
      width: 10,
      height: 10,
      toDataURL: () => "data:image/jpeg;base64,/9j/",
    };
    stubRect(button, 10, 20, 100, 30);
    stubElementFromPoint(win, button);
    await setMode(frame, true);

    clickAt(win, button, 50, 30);
    await settle();
    expect(ns.__captureCalls).toBe(1);
    // The second pick is consumed but the pending capture owns the slot.
    clickAt(win, button, 50, 30);
    await settle();
    expect(ns.__captureCalls).toBe(1);
    expect(lastOf(frame.messages, "annotate:picked")).toBeUndefined();

    // The mode is left while the capture is pending: its result is discarded.
    await setMode(frame, false);
    ns.__captureResolvers![0]!(canvas);
    await settle();
    expect(ns.overlayRoot().querySelectorAll("[data-omni-pick]")).toHaveLength(0);
    expect(lastOf(frame.messages, "annotate:picked")).toBeUndefined();

    // A fresh entry starts a fresh capture; the stale slot is already freed.
    await setMode(frame, true);
    clickAt(win, button, 50, 30);
    await settle();
    expect(ns.__captureCalls).toBe(2);
    ns.__captureResolvers![1]!(canvas);
    await settle();
    expect(lastOf(frame.messages, "annotate:picked")).toBeDefined();
    expect(
      (ns.overlayRoot().querySelector('[data-omni-pick="selected"]') as HTMLElement).style.display,
    ).toBe("block");
  });

  it("restores capture-hidden chrome on exit and keeps a stale capture out of the next mode", async () => {
    const win = startFrame('<button id="target">Pick me</button>');
    const frame = await loadPick(win, [freezeSource, pickerSource, SNAPDOM_DEFERRED, cropSource]);
    const ns = win.__omniAnnotate;
    const button = win.document.getElementById("target")!;
    const canvas = {
      width: 10,
      height: 10,
      toDataURL: () => "data:image/jpeg;base64,/9j/",
    };
    stubRect(button, 10, 20, 100, 30);
    stubElementFromPoint(win, button);

    await setMode(frame, true);
    const host = win.document.getElementById("__omni-annotate-host")!;
    clickAt(win, button, 50, 30);
    await settle();
    expect(ns.__captureCalls).toBe(1);
    const shield = win.document.getElementById("__omni-annotate-shield")!;
    expect(host.style.visibility).toBe("hidden");
    expect(shield.style.visibility).toBe("hidden");

    // Leaving the mode releases the hidden chrome at once, not when the
    // in-flight capture settles.
    await setMode(frame, false);
    expect(host.style.visibility).toBe("");
    expect(win.document.getElementById("__omni-annotate-shield")).toBeNull();

    // The stale capture settles only after a fresh mode and capture exist: it
    // must not touch the new mode's nodes or depth.
    await setMode(frame, true);
    // The freeze may have pinned the host (it sits in the body's hover
    // subtree in this harness), so "restored" is the value right before the
    // capture hid it.
    const hostBeforeCapture = host.style.visibility;
    clickAt(win, button, 50, 30);
    await settle();
    expect(ns.__captureCalls).toBe(2);
    const newShield = win.document.getElementById("__omni-annotate-shield")!;
    expect(host.style.visibility).toBe("hidden");
    expect(newShield.style.visibility).toBe("hidden");

    ns.__captureResolvers![0]!(canvas);
    await settle();
    expect(lastOf(frame.messages, "annotate:picked")).toBeUndefined();
    expect(host.style.visibility).toBe("hidden");
    expect(newShield.style.visibility).toBe("hidden");

    ns.__captureResolvers![1]!(canvas);
    await settle();
    expect(host.style.visibility).toBe(hostBeforeCapture);
    expect(newShield.style.visibility).toBe("");
    expect(lastOf(frame.messages, "annotate:picked")).toBeDefined();
    expect(
      (ns.overlayRoot().querySelector('[data-omni-pick="selected"]') as HTMLElement).style.display,
    ).toBe("block");
  });

  it("ignores untrusted picker and mode-leave events", async () => {
    const win = startFrame('<button id="target">Pick me</button>');
    const frame = await loadPick(win);
    const ns = win.__omniAnnotate;
    const button = win.document.getElementById("target")!;
    stubRect(button, 10, 20, 100, 30);
    stubElementFromPoint(win, button);
    await setMode(frame, true);

    // A synthetic click must not pick anything.
    clickAt(win, button, 50, 30, false);
    await settle();
    expect(lastOf(frame.messages, "annotate:picked")).toBeUndefined();
    expect(
      (ns.overlayRoot().querySelector('[data-omni-pick="selected"]') as HTMLElement).style.display,
    ).not.toBe("block");

    // A synthetic Escape must not leave the mode.
    press(win, win.document.body, { key: "Escape", code: "Escape" }, false);
    await settle();
    expect(lastOf(frame.messages, "annotate:modeChanged")).toBeUndefined();
    expect(ns.overlayRoot().querySelectorAll("[data-omni-pick]").length).toBeGreaterThan(0);

    // A synthetic shortcut chord must not toggle the mode.
    await setShortcut(frame, [
      { code: "Period", ctrl: false, meta: true, alt: false, shift: true },
    ]);
    press(
      win,
      win.document.body,
      { code: "Period", key: ".", metaKey: true, shiftKey: true },
      false,
    );
    await settle();
    expect(lastOf(frame.messages, "annotate:modeChanged")).toBeUndefined();
    expect(ns.overlayRoot().querySelectorAll("[data-omni-pick]").length).toBeGreaterThan(0);
  });

  it("does not let a page flag make an untrusted pick act", async () => {
    const win = startFrame('<button id="target">Pick me</button>');
    const frame = await loadPick(win);
    const ns = win.__omniAnnotate as AnnotateNamespace & { __acceptUntrusted?: boolean };
    const button = win.document.getElementById("target")!;
    stubRect(button, 10, 20, 100, 30);
    stubElementFromPoint(win, button);
    await setMode(frame, true);

    // Page JS shares the realm and can write anything on the namespace; an
    // untrusted click must still not pick.
    ns.__acceptUntrusted = true;
    clickAt(win, button, 50, 30, false);
    await settle();

    expect(lastOf(frame.messages, "annotate:picked")).toBeUndefined();
    expect(
      (ns.overlayRoot().querySelector('[data-omni-pick="selected"]') as HTMLElement).style.display,
    ).not.toBe("block");
  });
});

// ---------------------------------------------------------------------------
// freeze — pseudo-state pinning, animations and the hit-test shield
// ---------------------------------------------------------------------------

describe("freeze", () => {
  function stubHoveredButton(win: FrameWindow, button: Element): void {
    stubElementFromPoint(win, button);
    const realGetComputedStyle = win.getComputedStyle.bind(win);
    (win as unknown as { getComputedStyle: typeof win.getComputedStyle }).getComputedStyle = ((
      element: Element,
    ) =>
      element === button
        ? ({
            getPropertyValue: (property: string) =>
              property === "background-color" ? "rgb(1, 2, 3)" : "",
          } as CSSStyleDeclaration)
        : realGetComputedStyle(element)) as typeof win.getComputedStyle;
  }

  it("pins hover styles, pauses animations, mounts the shield and restores on off", async () => {
    const win = startFrame('<button id="target">Pick me</button>');
    await loadPick(win);
    const ns = win.__omniAnnotate;
    const button = win.document.getElementById("target")!;
    stubRect(button, 10, 20, 100, 30);
    stubHoveredButton(win, button);

    const animation = {
      playState: "running",
      pause: vi.fn(),
      play: vi.fn(),
      finish: vi.fn(() => {
        throw new Error("infinite animation");
      }),
    };
    (win.document as unknown as { getAnimations: () => unknown[] }).getAnimations = () => [
      animation,
    ];

    ns.freeze.on(50, 30);

    expect(button.style.getPropertyValue("background-color")).toBe("rgb(1, 2, 3)");
    expect(button.style.getPropertyPriority("background-color")).toBe("important");
    expect(animation.pause).toHaveBeenCalledTimes(1);
    expect(win.document.getElementById("__omni-annotate-shield")).not.toBeNull();

    ns.freeze.off();

    expect(button.style.getPropertyValue("background-color")).toBe("");
    expect(win.document.getElementById("__omni-annotate-shield")).toBeNull();
    expect(animation.play).toHaveBeenCalledTimes(1);
  });

  it("uses the :hover chain when no pointer position was seen", async () => {
    const win = startFrame('<button id="target">Pick me</button>');
    await loadPick(win);
    const ns = win.__omniAnnotate;
    const button = win.document.getElementById("target")!;
    const originalQuerySelectorAll = win.document.querySelectorAll.bind(win.document);
    (
      win.document as unknown as { querySelectorAll: (selector: string) => ArrayLike<Element> }
    ).querySelectorAll = (selector: string) =>
      selector === ":hover" ? [button] : originalQuerySelectorAll(selector);
    stubHoveredButton(win, button);

    ns.freeze.on();

    expect(button.style.getPropertyValue("background-color")).toBe("rgb(1, 2, 3)");
    expect(button.style.getPropertyPriority("background-color")).toBe("important");
    ns.freeze.off();
    expect(button.style.getPropertyValue("background-color")).toBe("");
  });

  it("creates the shield and style nodes without recorder privacy hooks", async () => {
    const win = startFrame('<button id="target">Pick me</button>');
    await loadPick(win);
    const ns = win.__omniAnnotate;
    const button = win.document.getElementById("target")!;
    stubRect(button, 10, 20, 100, 30);
    stubElementFromPoint(win, button);

    ns.freeze.on(50, 30);
    const shield = win.document.getElementById("__omni-annotate-shield")!;
    const pointerStyle = win.document.querySelector("[data-omni-frozen-pointer-events]")!;
    for (const node of [shield, pointerStyle]) {
      expect(node.hasAttribute("data-rr-block")).toBe(false);
      expect(node.hasAttribute("data-clarity-mask")).toBe(false);
      expect(node.classList.contains("ph-no-capture")).toBe(false);
    }
    ns.freeze.off();
  });

  it("keeps a page style write made while frozen", async () => {
    const win = startFrame('<button id="target">Pick me</button>');
    await loadPick(win);
    const ns = win.__omniAnnotate;
    const button = win.document.getElementById("target")!;
    stubRect(button, 10, 20, 100, 30);
    stubElementFromPoint(win, button);
    const realGetComputedStyle = win.getComputedStyle.bind(win);
    (win as unknown as { getComputedStyle: typeof win.getComputedStyle }).getComputedStyle = ((
      element: Element,
    ) =>
      element === button
        ? ({
            getPropertyValue: (property: string) =>
              property === "color"
                ? "rgb(1, 2, 3)"
                : property === "background-color"
                  ? "rgb(9, 9, 9)"
                  : "",
          } as CSSStyleDeclaration)
        : realGetComputedStyle(element)) as typeof win.getComputedStyle;

    ns.freeze.on(50, 30);
    expect(button.style.getPropertyValue("color")).toBe("rgb(1, 2, 3)");
    button.style.color = "blue";

    ns.freeze.off();
    expect(button.style.getPropertyValue("color")).toBe("blue");
    expect(button.style.getPropertyValue("background-color")).toBe("");
  });

  it("resumes a paused animation instead of finishing it", async () => {
    const win = startFrame('<button id="target">Pick me</button>');
    await loadPick(win);
    const ns = win.__omniAnnotate;
    const button = win.document.getElementById("target")!;
    stubRect(button, 10, 20, 100, 30);
    stubHoveredButton(win, button);
    const animation = {
      playState: "running",
      currentTime: 400,
      pause: vi.fn(),
      play: vi.fn(),
      finish: vi.fn(),
    };
    animation.pause.mockImplementation(() => {
      animation.playState = "paused";
    });
    animation.play.mockImplementation(() => {
      animation.playState = "running";
    });
    const alreadyPaused = {
      playState: "paused",
      pause: vi.fn(),
      play: vi.fn(),
      finish: vi.fn(),
    };
    (win.document as unknown as { getAnimations: () => unknown[] }).getAnimations = () => [
      animation,
      alreadyPaused,
    ];

    ns.freeze.on(50, 30);
    expect(animation.pause).toHaveBeenCalledTimes(1);
    expect(animation.playState).toBe("paused");
    expect(alreadyPaused.pause).not.toHaveBeenCalled();

    ns.freeze.off();
    expect(animation.finish).not.toHaveBeenCalled();
    expect(animation.play).toHaveBeenCalledTimes(1);
    expect(animation.playState).toBe("running");
    expect(animation.currentTime).toBe(400);
    expect(alreadyPaused.play).not.toHaveBeenCalled();
    expect(alreadyPaused.playState).toBe("paused");
  });

  it("pauses only running SVG roots and unpauses only those", async () => {
    const win = startFrame('<svg id="running"></svg><svg id="paused"></svg>');
    await loadPick(win);
    const ns = win.__omniAnnotate;
    const running = win.document.getElementById("running") as unknown as {
      animationsPaused: () => boolean;
      pauseAnimations: ReturnType<typeof vi.fn>;
      unpauseAnimations: ReturnType<typeof vi.fn>;
    };
    const paused = win.document.getElementById("paused") as unknown as typeof running;
    running.animationsPaused = () => false;
    running.pauseAnimations = vi.fn();
    running.unpauseAnimations = vi.fn();
    paused.animationsPaused = () => true;
    paused.pauseAnimations = vi.fn();
    paused.unpauseAnimations = vi.fn();
    stubElementFromPoint(win, null);

    ns.freeze.on(5, 5);
    expect(running.pauseAnimations).toHaveBeenCalledTimes(1);
    expect(paused.pauseAnimations).not.toHaveBeenCalled();

    ns.freeze.off();
    expect(running.unpauseAnimations).toHaveBeenCalledTimes(1);
    expect(paused.unpauseAnimations).not.toHaveBeenCalled();
    expect(paused.animationsPaused()).toBe(true);
  });

  it("flushes held rAF callbacks at off and leaves no held handle behind", async () => {
    const win = startFrame('<button id="target">Pick me</button>');
    await loadPick(win);
    const ns = win.__omniAnnotate;
    const button = win.document.getElementById("target")!;
    stubRect(button, 10, 20, 100, 30);
    stubElementFromPoint(win, button);
    const frames = installRaf(win);
    const originalRequest = win.requestAnimationFrame;
    const originalCancel = win.cancelAnimationFrame;
    const ran: string[] = [];

    ns.freeze.on(50, 30);
    expect(win.requestAnimationFrame).not.toBe(originalRequest);
    const held = win.requestAnimationFrame(() => {
      ran.push("held");
      // A held loop's re-schedule must land on the restored native pair.
      win.requestAnimationFrame(() => ran.push("next"));
    });
    const cancelled = win.requestAnimationFrame(() => ran.push("cancelled"));
    win.cancelAnimationFrame(cancelled);
    await sleep(10);
    expect(ran).toEqual([]);
    expect(frames()).toBe(0);

    ns.freeze.off();
    // The flush is synchronous at off; the callback cancelled while frozen is
    // skipped and the natives are already back.
    expect(ran).toEqual(["held"]);
    expect(win.requestAnimationFrame).toBe(originalRequest);
    expect(win.cancelAnimationFrame).toBe(originalCancel);

    // The held id was never a native one: cancelling it after off is a no-op.
    expect(() => win.cancelAnimationFrame(held)).not.toThrow();
    await sleep(10);
    expect(ran).toEqual(["held", "next"]);
    expect(frames()).toBe(1);
  });

  it("stops a not-yet-run held rAF callback cancelled during the flush", async () => {
    const win = startFrame('<button id="target">Pick me</button>');
    await loadPick(win);
    const ns = win.__omniAnnotate;
    const button = win.document.getElementById("target")!;
    stubRect(button, 10, 20, 100, 30);
    stubElementFromPoint(win, button);
    installRaf(win);
    const ran: string[] = [];

    ns.freeze.on(50, 30);
    let second = 0;
    win.requestAnimationFrame(() => {
      ran.push("first");
      // Native rAF would drop the not-yet-run second callback.
      win.cancelAnimationFrame(second);
    });
    second = win.requestAnimationFrame(() => ran.push("second"));

    ns.freeze.off();
    expect(ran).toEqual(["first"]);
    await sleep(10);
    expect(ran).toEqual(["first"]);
  });

  it("invokes held rAF callbacks with window as this", async () => {
    const win = startFrame('<button id="target">Pick me</button>');
    await loadPick(win);
    const ns = win.__omniAnnotate;
    const button = win.document.getElementById("target")!;
    stubRect(button, 10, 20, 100, 30);
    stubElementFromPoint(win, button);
    installRaf(win);
    const receivers: unknown[] = [];

    ns.freeze.on(50, 30);
    win.requestAnimationFrame(function (this: unknown) {
      receivers.push(this);
    });

    ns.freeze.off();
    expect(receivers).toEqual([win]);
  });

  it("rethrows a held callback error asynchronously so window.onerror still sees it", async () => {
    const win = startFrame('<button id="target">Pick me</button>');
    await loadPick(win);
    const ns = win.__omniAnnotate;
    const button = win.document.getElementById("target")!;
    stubRect(button, 10, 20, 100, 30);
    stubElementFromPoint(win, button);
    const errors: string[] = [];
    win.addEventListener("error", (event) => errors.push((event as ErrorEvent).message));
    const later = vi.fn();

    ns.freeze.on(50, 30);
    win.requestAnimationFrame(() => {
      throw new win.Error("held boom");
    });
    win.requestAnimationFrame(later);
    expect(() => ns.freeze.off()).not.toThrow();
    expect(later).toHaveBeenCalledTimes(1);
    expect(errors).toEqual([]);

    await sleep(20);
    expect(errors.join(" ")).toContain("held boom");
  });

  it("pins hover-revealed descendants before installing the shield", async () => {
    const win = startFrame(
      '<div id="wrap"><button id="trigger">Menu</button><ul id="menu"><li>One</li></ul></div>',
    );
    await loadPick(win);
    const ns = win.__omniAnnotate;
    const trigger = win.document.getElementById("trigger")!;
    const menu = win.document.getElementById("menu")!;
    stubRect(trigger, 10, 20, 100, 30);
    stubElementFromPoint(win, trigger);
    const realGetComputedStyle = win.getComputedStyle.bind(win);
    (win as unknown as { getComputedStyle: typeof win.getComputedStyle }).getComputedStyle = ((
      element: Element,
    ) => {
      if (element === trigger) {
        return {
          getPropertyValue: (property: string) =>
            property === "background-color" ? "rgb(1, 2, 3)" : "",
        } as CSSStyleDeclaration;
      }
      if (element === menu) {
        return {
          getPropertyValue: (property: string) =>
            property === "display"
              ? "block"
              : property === "visibility"
                ? "visible"
                : property === "opacity"
                  ? "1"
                  : "",
        } as CSSStyleDeclaration;
      }
      return realGetComputedStyle(element);
    }) as typeof win.getComputedStyle;

    ns.freeze.on(50, 30);
    expect(menu.style.getPropertyValue("display")).toBe("block");
    expect(menu.style.getPropertyPriority("display")).toBe("important");
    expect(menu.style.getPropertyValue("visibility")).toBe("visible");
    expect(menu.style.getPropertyValue("opacity")).toBe("1");

    ns.freeze.off();
    expect(menu.style.getPropertyValue("display")).toBe("");
    expect(menu.style.getPropertyValue("visibility")).toBe("");
    expect(menu.style.getPropertyValue("opacity")).toBe("");
  });

  it("keeps the shield hit-test open across nested opens", async () => {
    const win = startFrame('<button id="target">Pick me</button>');
    await loadPick(win);
    const ns = win.__omniAnnotate;
    const button = win.document.getElementById("target")!;
    stubRect(button, 10, 20, 100, 30);
    stubElementFromPoint(win, button);
    ns.freeze.on(50, 30);
    const panel = win.document.getElementById("__omni-annotate-shield")!
      .firstElementChild as HTMLElement;

    ns.freeze.openForHitTest();
    ns.freeze.openForHitTest();
    ns.freeze.closeAfterHitTest();
    expect(panel.style.pointerEvents).toBe("none");
    ns.freeze.closeAfterHitTest();
    expect(panel.style.pointerEvents).toBe("auto");
    ns.freeze.closeAfterHitTest();
    expect(panel.style.pointerEvents).toBe("auto");
    ns.freeze.off();
  });

  it("blocks a page window blur listener through the early slot while frozen", async () => {
    const win = startFrame('<button id="target">Pick me</button>', NONCE, true);
    const frame = await loadPick(win);
    const pageBlur = vi.fn();
    win.addEventListener("blur", pageBlur, true);

    await setMode(frame, true);
    win.dispatchEvent(new win.Event("blur"));
    expect(pageBlur).not.toHaveBeenCalled();

    await setMode(frame, false);
    win.dispatchEvent(new win.Event("blur"));
    expect(pageBlur).toHaveBeenCalledTimes(1);
  });

  it("blocks a page window pointerout listener through the early slot while frozen", async () => {
    const win = startFrame('<button id="target">Pick me</button>', NONCE, true);
    const frame = await loadPick(win);
    const button = win.document.getElementById("target")!;
    const menu = win.document.createElement("div");
    menu.id = "menu";
    win.document.body.appendChild(menu);
    const pageOut = vi.fn(() => menu.remove());
    win.addEventListener("pointerout", pageOut, true);

    await setMode(frame, true);
    button.dispatchEvent(
      new win.MouseEvent("pointerout", { bubbles: true, cancelable: true, composed: true }),
    );
    expect(pageOut).not.toHaveBeenCalled();
    expect(win.document.getElementById("menu")).not.toBeNull();

    await setMode(frame, false);
    button.dispatchEvent(
      new win.MouseEvent("pointerout", { bubbles: true, cancelable: true, composed: true }),
    );
    expect(pageOut).toHaveBeenCalledTimes(1);
    expect(win.document.getElementById("menu")).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// capture-slot interception — page listeners never see mode events
// ---------------------------------------------------------------------------

describe("capture-slot interception", () => {
  const SLOT_TYPES = [
    "pointermove",
    "pointerdown",
    "pointerup",
    "click",
    "mousedown",
    "mouseup",
    "contextmenu",
    "dblclick",
    "keydown",
    "pointerover",
    "pointerout",
    "pointerenter",
    "pointerleave",
    "mouseover",
    "mouseout",
    "mouseenter",
    "mouseleave",
    "focus",
    "blur",
    "focusin",
    "focusout",
  ];

  it("intercepts window clicks before page listeners registered after the head script", async () => {
    const win = startFrame('<button id="target">Pick me</button>', NONCE, true);
    const frame = await loadPick(win);
    const button = win.document.getElementById("target")!;
    stubRect(button, 10, 20, 100, 30);
    stubElementFromPoint(win, button);
    const pageClicks = vi.fn();
    win.addEventListener("click", pageClicks, true);

    await setMode(frame, true);
    clickAt(win, button, 50, 30);
    expect(pageClicks).not.toHaveBeenCalled();

    await setMode(frame, false);
    clickAt(win, button, 50, 30);
    expect(pageClicks).toHaveBeenCalledTimes(1);
  });

  it("leaves the mode on Escape before a page keydown handler can react", async () => {
    const win = startFrame('<button id="target">Pick me</button>', NONCE, true);
    const frame = await loadPick(win);
    const pageEscape = vi.fn();
    win.addEventListener("keydown", pageEscape, true);
    await setMode(frame, true);

    press(win, win.document.body, { key: "Escape", code: "Escape" });
    await settle();

    expect(pageEscape).not.toHaveBeenCalled();
    expect(lastOf(frame.messages, "annotate:modeChanged")).toMatchObject({
      on: false,
      reason: "escape",
    });
  });

  it("clears every capture slot when the mode is left", async () => {
    const win = startFrame('<button id="target">Pick me</button>', NONCE, true);
    const frame = await loadPick(win);
    const pageEvents = vi.fn();
    for (const type of SLOT_TYPES) win.addEventListener(type, pageEvents, true);

    await setMode(frame, true);
    await setMode(frame, false);

    for (const type of SLOT_TYPES) {
      pageEvents.mockClear();
      win.dispatchEvent(new win.Event(type, { bubbles: true, cancelable: true, composed: true }));
      expect(pageEvents).toHaveBeenCalledTimes(1);
    }
  });
});

// ---------------------------------------------------------------------------
// crop — snapdom capture call, caps and failure paths
// ---------------------------------------------------------------------------

describe("crop", () => {
  it("captures the picked rect through snapdom and restores our chrome", async () => {
    const win = startFrame(
      '<div id="__omni-annotate-host" style="visibility: visible"></div>' +
        '<div id="__omni-annotate-shield"></div><p>x</p>',
    );
    await loadPick(win);
    const ns = win.__omniAnnotate;
    const host = win.document.getElementById("__omni-annotate-host")!;
    const shield = win.document.getElementById("__omni-annotate-shield")!;

    const result = await ns.capture({ x: 10, y: 20, w: 100, h: 50 });

    expect(result).toEqual({
      dataUrl: "data:image/jpeg;base64,/9j/4AAQSkZJRg==",
      width: 800,
      height: 400,
    });
    expect(ns.__lastCapture?.root).toBe(win.document.body);
    expect(ns.__lastCapture?.options).toMatchObject({
      clip: { x: 10, y: 20, width: 100, height: 50 },
      dpr: 1,
      embedFonts: true,
      fast: true,
      placeholders: true,
      exclude: "#__omni-annotate-host",
    });
    expect(ns.__hostVisibilityDuringCapture).toBe("hidden");
    expect(ns.__shieldVisibilityDuringCapture).toBe("hidden");
    expect(host.style.visibility).toBe("visible");
    expect(shield.style.visibility).toBe("");
  });

  async function captureOverlapping(resolveInStartOrder: boolean): Promise<{
    host: HTMLElement;
    shield: HTMLElement;
  }> {
    const win = startFrame(
      '<div id="__omni-annotate-host" style="visibility: visible"></div>' +
        '<div id="__omni-annotate-shield"></div><p>x</p>',
    );
    await loadPick(win, [freezeSource, pickerSource, SNAPDOM_DEFERRED, cropSource]);
    const ns = win.__omniAnnotate;
    const host = win.document.getElementById("__omni-annotate-host")!;
    const shield = win.document.getElementById("__omni-annotate-shield")!;
    const canvas = {
      width: 10,
      height: 10,
      toDataURL: () => "data:image/jpeg;base64,/9j/",
    };

    const first = ns.capture({ x: 0, y: 0, w: 10, h: 10 });
    const second = ns.capture({ x: 0, y: 0, w: 10, h: 10 });
    expect(ns.__captureCalls).toBe(2);

    if (resolveInStartOrder) {
      ns.__captureResolvers![0]!(canvas);
      await first;
      ns.__captureResolvers![1]!(canvas);
      await second;
    } else {
      ns.__captureResolvers![1]!(canvas);
      await second;
      ns.__captureResolvers![0]!(canvas);
      await first;
    }
    return { host, shield };
  }

  it("restores chrome once for overlapping captures resolved in start order", async () => {
    const { host, shield } = await captureOverlapping(true);
    expect(host.style.visibility).toBe("visible");
    expect(shield.style.visibility).toBe("");
  });

  it("restores chrome once for overlapping captures resolved in reverse order", async () => {
    const { host, shield } = await captureOverlapping(false);
    expect(host.style.visibility).toBe("visible");
    expect(shield.style.visibility).toBe("");
  });

  it("returns null and warns when snapdom throws", async () => {
    const win = startFrame("<p>x</p>");
    await loadPick(win, [freezeSource, pickerSource, SNAPDOM_THROWS, cropSource]);
    const warn = vi.spyOn(win.console, "warn").mockImplementation(() => {});

    await expect(win.__omniAnnotate.capture({ x: 0, y: 0, w: 10, h: 10 })).resolves.toBeNull();
    expect(warn).toHaveBeenCalledTimes(1);

    warn.mockRestore();
  });

  it("returns null after the capture timeout when snapdom never resolves", async () => {
    const win = startFrame("<p>x</p>");
    await loadPick(win, [freezeSource, pickerSource, SNAPDOM_HANGS, cropSource]);
    const ns = win.__omniAnnotate;
    const realSetTimeout = win.setTimeout;
    const realClearTimeout = win.clearTimeout;

    vi.useFakeTimers();
    try {
      // crop.js runs in the frame, so the frame's timer functions need the fakes.
      win.setTimeout = setTimeout;
      win.clearTimeout = clearTimeout;
      const pending = ns.capture({ x: 0, y: 0, w: 10, h: 10 });
      await vi.advanceTimersByTimeAsync(5000);
      await expect(pending).resolves.toBeNull();
    } finally {
      win.setTimeout = realSetTimeout;
      win.clearTimeout = realClearTimeout;
      vi.useRealTimers();
    }
  });
});

// ---------------------------------------------------------------------------
// markers — overlay painting and mutation re-resolution
// ---------------------------------------------------------------------------

describe("markers", () => {
  it("paints found markers, reports found flags, and routes badge clicks", async () => {
    const win = startFrame('<div id="present">Target</div><section id="gone">Gone</section>');
    const frame = await loadCore(win);
    const ns = win.__omniAnnotate;
    const present = win.document.getElementById("present")!;
    const gone = win.document.getElementById("gone")!;
    const target = ns.generateTarget(present);
    const goneTarget = ns.generateTarget(gone);
    gone.remove();

    const resolved = await setAnnotations(frame, [
      { id: "a", n: 1, anchor: { target } },
      { id: "b", n: 2, anchor: { target: goneTarget } },
    ]);
    expect(resolved.items).toEqual([
      { id: "a", found: true },
      { id: "b", found: false },
    ]);

    const root = ns.overlayRoot();
    const badges = root.querySelectorAll(".omni-annotate-marker");
    expect(badges).toHaveLength(1);
    expect(badges[0]!.textContent).toBe("1");
    // The host is in the page but its shadow root stays closed.
    expect(win.document.getElementById("__omni-annotate-host")!.shadowRoot).toBeNull();

    badges[0]!.dispatchEvent(new win.MouseEvent("click", { bubbles: true, cancelable: true }));
    await waitFor(() => lastOf(frame.messages, "annotate:markerClick") !== undefined);
    expect(lastOf(frame.messages, "annotate:markerClick")).toMatchObject({ id: "a" });
  });

  it("re-attaches to a replacement node without a found flip", async () => {
    const win = startFrame('<div id="target">Target</div>');
    const frame = await loadCore(win);
    const ns = win.__omniAnnotate;
    const old = win.document.getElementById("target")!;
    stubRect(old, 10, 20, 50, 20);
    const target = ns.generateTarget(old);

    const first = await setAnnotations(frame, [{ id: "a", n: 1, anchor: { target } }]);
    expect(first.items).toEqual([{ id: "a", found: true }]);
    expect((ns.overlayRoot().querySelector(".omni-annotate-marker") as HTMLElement).style.top).toBe(
      "20px",
    );

    // T19: the re-found replacement carries a distinct rect; the badge must
    // move to it after the debounce and no message may report a loss.
    const replacement = old.cloneNode(true) as Element;
    stubRect(replacement, 40, 60, 50, 20);
    old.replaceWith(replacement);
    frame.messages.length = 0;
    await sleep(400);

    const lost = frame.messages.filter(
      (m) =>
        m.type === "annotate:resolved" && (m.items as ResolvedItem[]).some((item) => !item.found),
    );
    expect(lost).toEqual([]);
    const badge = ns.overlayRoot().querySelector(".omni-annotate-marker") as HTMLElement;
    expect(badge.style.left).toBe("40px");
    expect(badge.style.top).toBe("60px");
  });

  it("repositions a retained element after a layout change without a message", async () => {
    const win = startFrame('<div id="target">Target</div>');
    const frame = await loadCore(win);
    const ns = win.__omniAnnotate;
    const element = win.document.getElementById("target")!;
    stubRect(element, 10, 20, 50, 20);
    const target = ns.generateTarget(element);

    await setAnnotations(frame, [{ id: "a", n: 1, anchor: { target } }]);
    const badge = ns.overlayRoot().querySelector(".omni-annotate-marker") as HTMLElement;
    expect(badge.style.top).toBe("20px");

    stubRect(element, 10, 200, 50, 20);
    element.className = "moved";
    frame.messages.length = 0;
    await sleep(400);

    // Same element, so no resolved message: only the badge moved.
    expect(frame.messages).toEqual([]);
    expect(ns.overlayRoot().querySelector(".omni-annotate-marker")).toBe(badge);
    expect(badge.style.top).toBe("200px");
  });

  it("repositions badges on a nested scroll without re-resolving", async () => {
    const win = startFrame('<div id="scroller"><div id="target">Target</div></div>');
    const frame = await loadCore(win);
    const ns = win.__omniAnnotate;
    const element = win.document.getElementById("target")!;
    stubRect(element, 30, 40, 50, 20);
    const target = ns.generateTarget(element);

    await setAnnotations(frame, [{ id: "a", n: 1, anchor: { target } }]);
    const badge = ns.overlayRoot().querySelector(".omni-annotate-marker") as HTMLElement;
    expect(badge.style.top).toBe("40px");
    frame.messages.length = 0;

    const frames = installRaf(win);
    stubRect(element, 30, 5, 50, 20);
    const scroller = win.document.getElementById("scroller")!;
    // Non-bubbling scroll events: only the window's capture listener sees them.
    scroller.dispatchEvent(new win.Event("scroll"));
    scroller.dispatchEvent(new win.Event("scroll"));
    await sleep(20);

    expect(frames()).toBe(1);
    expect(badge.style.left).toBe("30px");
    expect(badge.style.top).toBe("5px");
    expect(frame.messages).toEqual([]);
  });

  it("stops tracking scroll once the marker list is emptied", async () => {
    const win = startFrame('<div id="scroller"><div id="target">Target</div></div>');
    const frame = await loadCore(win);
    const ns = win.__omniAnnotate;
    const target = ns.generateTarget(win.document.getElementById("target")!);
    const frames = installRaf(win);

    await setAnnotations(frame, [{ id: "a", n: 1, anchor: { target } }]);
    await setAnnotations(frame, []);
    win.document.getElementById("scroller")!.dispatchEvent(new win.Event("scroll"));
    await sleep(20);
    expect(frames()).toBe(0);
  });

  it("posts every marker's state when one found value flips", async () => {
    // A div, like the overlay host: the resolver must skip our own chrome, so
    // the removed marker still reports as lost rather than re-anchoring.
    const win = startFrame('<div id="kept">Kept</div><div id="doomed">Doomed</div>');
    const frame = await loadCore(win);
    const ns = win.__omniAnnotate;
    const kept = ns.generateTarget(win.document.getElementById("kept")!);
    const doomed = ns.generateTarget(win.document.getElementById("doomed")!);

    await setAnnotations(frame, [
      { id: "a", n: 1, anchor: { target: kept } },
      { id: "b", n: 2, anchor: { target: doomed } },
    ]);
    frame.messages.length = 0;

    win.document.getElementById("doomed")!.remove();
    await waitFor(() => frame.messages.some((m) => m.type === "annotate:resolved"));
    expect(frame.messages.filter((m) => m.type === "annotate:resolved")).toEqual([
      {
        source: SOURCE,
        nonce: NONCE,
        type: "annotate:resolved",
        items: [
          { id: "a", found: true },
          { id: "b", found: false },
        ],
      },
    ]);
  });

  it("reports a removed element once, after the debounce", async () => {
    const win = startFrame('<div id="target">Target</div>');
    const frame = await loadCore(win);
    const ns = win.__omniAnnotate;
    const target = ns.generateTarget(win.document.getElementById("target")!);

    await setAnnotations(frame, [{ id: "a", n: 1, anchor: { target } }]);
    frame.messages.length = 0;

    win.document.getElementById("target")!.remove();
    await waitFor(() => frame.messages.some((m) => m.type === "annotate:resolved"));
    expect(frame.messages.filter((m) => m.type === "annotate:resolved")).toEqual([
      {
        source: SOURCE,
        nonce: NONCE,
        type: "annotate:resolved",
        items: [{ id: "a", found: false }],
      },
    ]);
    expect(ns.overlayRoot().querySelectorAll(".omni-annotate-marker")).toHaveLength(0);
  });

  it("does not re-anchor a removed div to the marker overlay", async () => {
    // The textless div's fingerprint alone matches the overlay host's, so the
    // scan fallback would return the host unless the resolver skips our chrome.
    const win = startFrame('<div id="doomed" class="box"></div>');
    const frame = await loadCore(win);
    const ns = win.__omniAnnotate;
    const doomed = ns.generateTarget(win.document.getElementById("doomed")!);

    const first = await setAnnotations(frame, [{ id: "a", n: 1, anchor: { target: doomed } }]);
    expect(first.items).toEqual([{ id: "a", found: true }]);
    expect(win.document.getElementById("__omni-annotate-host")).not.toBeNull();

    frame.messages.length = 0;
    win.document.getElementById("doomed")!.remove();
    expect(ns.resolveTarget(doomed)).toBeNull();

    await waitFor(() => frame.messages.some((m) => m.type === "annotate:resolved"));
    expect(lastOf(frame.messages, "annotate:resolved")).toMatchObject({
      items: [{ id: "a", found: false }],
    });
  });
});
