import { afterEach, describe, expect, it, vi } from "vitest";
// @ts-expect-error jsdom ships no declarations and @types/jsdom is not a dependency
import { JSDOM } from "jsdom";
import annotateSource from "../../public/omni-html-annotate.js?raw";
import bridgeSource from "../../public/omni-html-bridge.js?raw";
import captureSource from "../../public/omni-html-capture.js?raw";
import {
  anchorOccurrence,
  BRIDGE_MSG,
  BRIDGE_SOURCE,
  buildAnnotateScript,
  buildBridgeScript,
  buildCaptureScript,
  HTML_COMMENT_BRIDGE_RUNTIME,
  injectCommentBridge,
  parseBridgeMessage,
} from "./htmlCommentBridge";
import { SCROLL_RESTORE_BUDGET_MS } from "./useScrollRestore";

// ---------------------------------------------------------------------------
// injectCommentBridge — script placement (mirrors prepareHtmlPreviewDoc)
// ---------------------------------------------------------------------------

describe("injectCommentBridge", () => {
  const NONCE = "test-nonce-123";
  const SCRIPT_OPEN = `<script data-omni-nonce="${NONCE}">`;
  const LOADER_URL = "https://app.example/assets/html-comment-bridge.js";

  // The prepared head carries its own <script> (same-page anchors), so the
  // bridge script is located by its nonce.
  it("injects the bridge script before </body> when present", () => {
    const html = "<html><head></head><body><p>hi</p></body></html>";
    const out = injectCommentBridge(html, NONCE);
    const scriptAt = out.indexOf(buildBridgeScript(NONCE));
    const bodyCloseAt = out.indexOf("</body>");
    expect(scriptAt).toBeGreaterThan(-1);
    expect(scriptAt).toBeLessThan(bodyCloseAt);
  });

  it("falls back to before </html> when there is no body", () => {
    const html = "<html><head></head><p>hi</p></html>";
    const out = injectCommentBridge(html, NONCE);
    expect(out.indexOf(buildBridgeScript(NONCE))).toBeLessThan(out.indexOf("</html>"));
  });

  it("appends to a bare fragment with no body/html", () => {
    const out = injectCommentBridge("<p>just a fragment</p>", NONCE, LOADER_URL);
    // prepareHtmlPreviewDoc prepends its head markup for a bare fragment; the
    // bridge is then appended at the end since there's no </body>/</html> to
    // inject before.
    expect(out).toContain("<p>just a fragment</p>");
    const fragAt = out.indexOf("<p>just a fragment</p>");
    expect(out.indexOf(`data-omni-nonce="${NONCE}"`)).toBeGreaterThan(fragAt);
  });

  it("puts the capture script first and appends the bridge in a bare fragment", () => {
    const out = injectCommentBridge("<p>just a fragment</p>", NONCE);
    // The capture script goes at the very start (nothing to displace) and the
    // bridge is appended at the end since there's no </body>/</html>.
    const fragAt = out.indexOf("<p>just a fragment</p>");
    expect(out.indexOf(buildCaptureScript(NONCE))).toBe(0);
    expect(out.indexOf(buildBridgeScript(NONCE))).toBeGreaterThan(fragAt);
  });

  it("injects the capture script inside <head> before page scripts", () => {
    const html = '<html><head><script src="page.js"></script></head><body>hi</body></html>';
    const out = injectCommentBridge(html, NONCE);
    const captureAt = out.indexOf(buildCaptureScript(NONCE));
    const pageScriptAt = out.indexOf('<script src="page.js">');
    expect(out.indexOf("<head>")).toBeLessThan(captureAt);
    expect(captureAt).toBeLessThan(out.indexOf('<base target="_blank">'));
    expect(captureAt).toBeLessThan(pageScriptAt);
    expect(out.indexOf(buildBridgeScript(NONCE))).toBeLessThan(out.indexOf("</body>"));
    expect(out.indexOf(buildAnnotateScript(NONCE))).toBeLessThan(out.indexOf("</body>"));
    expect(buildCaptureScript(NONCE)).toContain(`data-omni-nonce="${NONCE}"`);
  });

  it("falls back to right after <html> when there is no head", () => {
    const out = injectCommentBridge("<html><body><p>hi</p></body></html>", NONCE);
    const captureAt = out.indexOf(buildCaptureScript(NONCE));
    // prepareHtmlPreviewDoc inserts <head><base> after <html>; the capture
    // script still lands before the body and after the html element.
    expect(out.indexOf("<html>")).toBeLessThan(captureAt);
    expect(captureAt).toBeLessThan(out.indexOf("<body>"));
    expect(out.indexOf(buildBridgeScript(NONCE))).toBeLessThan(out.indexOf("</body>"));
  });

  it.each([
    ["<header>", "<html><body><header>top</header></body></html>"],
    ["a commented <head>", "<html><!-- <head> --><body><p>hi</p></body></html>"],
  ])("does not mistake %s for the head", (_label, html) => {
    const out = injectCommentBridge(html, NONCE);
    const captureAt = out.indexOf(buildCaptureScript(NONCE));
    // prepareHtmlPreviewDoc creates <head> right after <html>; capture leads it.
    expect(out.indexOf("<html><head>")).toBe(captureAt - "<html><head>".length);
  });

  it("gives all three injected scripts the same nonce", () => {
    const out = injectCommentBridge("<html><head></head><body></body></html>", NONCE);
    expect(out.split(`data-omni-nonce="${NONCE}"`).length - 1).toBe(3);
  });

  it("preserves the prepared <base target=_blank> link behavior", () => {
    const out = injectCommentBridge("<html><head></head><body></body></html>", NONCE, LOADER_URL);
    expect(out).toContain('<base target="_blank">');
  });

  it("wraps the shared bridge asset in a nonce-carrying script tag", () => {
    const script = buildBridgeScript(NONCE);
    expect(script.startsWith(SCRIPT_OPEN)).toBe(true);
    expect(script.endsWith("</script>")).toBe(true);
    expect(script).toContain(bridgeSource.replace(/<\/script/gi, "<\\/script"));
  });

  it("includes the highlight style for the Custom Highlight ranges", () => {
    const out = injectCommentBridge("<body></body>", NONCE);
    expect(out).toContain("::highlight(omni-comment)");
    expect(out).toContain("::highlight(omni-comment-active)");
  });

  it("loads the static runtime externally for a no-inline CSP", () => {
    const out = injectCommentBridge("<body></body>", NONCE, LOADER_URL);
    expect(out).toContain(`src="${LOADER_URL}"`);
    expect(out).toContain(`data-omni-nonce="${NONCE}"`);
    expect(out).not.toContain(HTML_COMMENT_BRIDGE_RUNTIME);
    // The annotation runtime is evaluated inline, so that mode carries the bridge only.
    expect(out).not.toContain(buildCaptureScript(NONCE));
    expect(out).not.toContain(buildAnnotateScript(NONCE));
    expect(out.split(`data-omni-nonce="${NONCE}"`).length - 1).toBe(1);
  });

  it("injects the annotate stub right after the bridge with the same nonce", () => {
    const out = injectCommentBridge("<html><head></head><body></body></html>", NONCE);
    const bridgeAt = out.indexOf(buildBridgeScript(NONCE));
    const annotateAt = out.indexOf(buildAnnotateScript(NONCE));
    expect(bridgeAt).toBeGreaterThan(-1);
    expect(annotateAt).toBeGreaterThan(bridgeAt);
    expect(out.indexOf("</body>")).toBeGreaterThan(annotateAt);
    expect(buildAnnotateScript(NONCE)).toContain(`data-omni-nonce="${NONCE}"`);
  });

  it("escapes an HTML-special nonce in the attribute", () => {
    expect(buildBridgeScript('a"b&c<d')).toContain('data-omni-nonce="a&quot;b&amp;c&lt;d"');
  });

  it("produces syntactically valid scripts (guards escaping in the assets)", () => {
    // The assets run verbatim in the frame; an escaping typo would only surface
    // at runtime. new Function throws on a syntax error.
    expect(() => new Function(bridgeSource)).not.toThrow();
    expect(() => new Function(annotateSource)).not.toThrow();
    expect(() => new Function(captureSource)).not.toThrow();
  });

  it("injects the same runtime inline without a network dependency", () => {
    const out = injectCommentBridge("<body></body>", NONCE);
    expect(out).toContain(`<script data-omni-nonce="${NONCE}">`);
    expect(out).toContain(HTML_COMMENT_BRIDGE_RUNTIME);
    expect(out).not.toContain("<script src=");
  });

  it("keeps the inline runtime body free of closing script tags", () => {
    expect(HTML_COMMENT_BRIDGE_RUNTIME).not.toMatch(/<\/script/i);
  });

  it("escapes runtime URLs and nonces as HTML attributes", () => {
    const out = injectCommentBridge(
      "<body></body>",
      "nonce&\"'<>value",
      'https://app.example/a?x=1&y="2"\'<>',
    );
    expect(out).toContain('src="https://app.example/a?x=1&amp;y=&quot;2&quot;&#39;&lt;&gt;"');
    expect(out).toContain('data-omni-nonce="nonce&amp;&quot;&#39;&lt;&gt;value"');
  });
});

// ---------------------------------------------------------------------------
// omni-html-bridge.js — the asset's inline constants must not drift
// ---------------------------------------------------------------------------

describe("omni-html-bridge.js constants", () => {
  const NONCE = "scroll-test-nonce";
  const LOADER_URL = "/omni-html-bridge.js";
  it("matches BRIDGE_SOURCE and every BRIDGE_MSG value", () => {
    const src = /var\s+SRC\s*=\s*"([^"]*)"/.exec(bridgeSource);
    expect(src?.[1]).toBe(BRIDGE_SOURCE);

    const tBody = /var\s+T\s*=\s*\{([\s\S]*?)\};/.exec(bridgeSource)?.[1] ?? "";
    const t = new Map([...tBody.matchAll(/(\w+)\s*:\s*"([^"]*)"/g)].map((m) => [m[1], m[2]]));
    for (const [key, value] of Object.entries(BRIDGE_MSG)) {
      expect(t.get(key), `T.${key}`).toBe(value);
    }
  });

  it("keeps the asset loadable under the embed's strict script-src", () => {
    // No unsafe-eval in the frame's CSP, so the runtime must not eval.
    expect(bridgeSource).not.toMatch(/\b(?:eval|Function)\s*\(/);
  });

  it("hands a saved scroll offset and the restore budget to the runtime", () => {
    const out = injectCommentBridge("<body></body>", NONCE, LOADER_URL, 640);
    const doc = new DOMParser().parseFromString(out, "text/html");
    const script = doc.querySelector(`script[data-omni-nonce="${NONCE}"]`);
    expect(JSON.parse(script?.getAttribute("data-omni-restore-scroll") ?? "")).toEqual({
      top: 640,
      budgetMs: SCROLL_RESTORE_BUDGET_MS,
    });
  });

  it.each([0, -5, Number.NaN, Number.POSITIVE_INFINITY])(
    "asks for no restore when the saved offset is %s",
    (top) => {
      const out = injectCommentBridge("<body></body>", NONCE, LOADER_URL, top);
      expect(out).not.toContain("data-omni-restore-scroll");
      expect(out).not.toContain("data-omni-restore-hide");
    },
  );

  it.each([
    ["a document with a head", "<!DOCTYPE html><html><head><title>Plan</title></head><body>"],
    ["a document without a head", "<!DOCTYPE html><html><body><p>Plan</p></body></html>"],
    ["a bare fragment", "<p>Plan</p>"],
  ])("hides %s from the start of its head while restoring", (_label, html) => {
    const out = injectCommentBridge(html, NONCE, LOADER_URL, 640);
    if (html.startsWith("<!DOCTYPE html>")) expect(out.startsWith("<!DOCTYPE html>")).toBe(true);
    const hide = new DOMParser().parseFromString(out, "text/html").head.firstElementChild;
    expect(hide?.getAttribute("data-omni-restore-hide")).toBe(NONCE);
    expect(hide?.textContent).toBe(
      `html{animation:omni-restore-hide ${2 * SCROLL_RESTORE_BUDGET_MS}ms!important}` +
        "@keyframes omni-restore-hide{from,to{opacity:0;pointer-events:none}}",
    );
  });
});

// ---------------------------------------------------------------------------
// Bridge runtime harness
// ---------------------------------------------------------------------------

const RUNTIME_NONCE = "runtime-nonce";
const runtimeListeners: [EventTarget, string, EventListenerOrEventListenerObject, unknown][] = [];

afterEach(() => {
  for (const [target, type, listener, options] of runtimeListeners.splice(0)) {
    target.removeEventListener(type, listener, options as EventListenerOptions);
  }
  vi.restoreAllMocks();
});

/** Run the runtime in this document, as the injected script would, and connect a port. */
function startRuntime({
  connect = true,
  beforeConnect,
  restoreScroll,
}: {
  connect?: boolean;
  beforeConnect?: () => void;
  /** Value of the script's `data-omni-restore-scroll` attribute. */
  restoreScroll?: string;
} = {}) {
  for (const target of [window, document]) {
    const original = target.addEventListener.bind(target);
    vi.spyOn(target, "addEventListener").mockImplementation((type, listener, options) => {
      if (listener) runtimeListeners.push([target, type, listener, options]);
      original(type, listener, options);
    });
  }
  const script = document.createElement("script");
  script.setAttribute("data-omni-nonce", RUNTIME_NONCE);
  script.setAttribute(
    "data-omni-protocol",
    JSON.stringify({ source: BRIDGE_SOURCE, types: BRIDGE_MSG }),
  );
  if (restoreScroll !== undefined) script.setAttribute("data-omni-restore-scroll", restoreScroll);
  vi.spyOn(document, "currentScript", "get").mockReturnValue(script);
  new Function(HTML_COMMENT_BRIDGE_RUNTIME)();
  beforeConnect?.();

  const port = { postMessage: vi.fn(), onmessage: null };
  if (connect) {
    const init = new Event("message");
    Object.defineProperties(init, {
      data: { value: { source: BRIDGE_SOURCE, nonce: RUNTIME_NONCE, type: BRIDGE_MSG.init } },
      ports: { value: [port] },
    });
    window.dispatchEvent(init);
    port.postMessage.mockClear(); // drop the ready message
  }
  vi.mocked(window.addEventListener).mockRestore();
  vi.mocked(document.addEventListener).mockRestore();
  return port;
}

// ---------------------------------------------------------------------------
// Bridge runtime — zoom shortcuts inside the preview
// ---------------------------------------------------------------------------

describe("bridge runtime zoom shortcuts", () => {
  function press(init: KeyboardEventInit): boolean {
    const event = new KeyboardEvent("keydown", { bubbles: true, cancelable: true, ...init });
    document.body.dispatchEvent(event);
    return event.defaultPrevented;
  }

  function sentZoomActions(port: { postMessage: ReturnType<typeof vi.fn> }) {
    return port.postMessage.mock.calls.map(([msg]) => {
      expect(msg).toMatchObject({
        source: BRIDGE_SOURCE,
        nonce: RUNTIME_NONCE,
        type: BRIDGE_MSG.zoom,
      });
      return (msg as { action: string }).action;
    });
  }

  it.each([
    [{ key: "=", metaKey: true }, "in"],
    [{ key: "+", metaKey: true, shiftKey: true }, "in"],
    [{ key: "=", ctrlKey: true }, "in"],
    [{ key: "Unidentified", code: "NumpadAdd", ctrlKey: true }, "in"],
    [{ key: "-", metaKey: true }, "out"],
    [{ key: "_", ctrlKey: true, shiftKey: true }, "out"],
    [{ key: "Unidentified", code: "NumpadSubtract", metaKey: true }, "out"],
    [{ key: "0", metaKey: true }, "reset"],
    [{ key: "Insert", code: "Numpad0", ctrlKey: true }, "reset"],
  ])("cancels %o and asks the parent to zoom %s", (init, action) => {
    const port = startRuntime();
    expect(press(init)).toBe(true);
    expect(sentZoomActions(port)).toEqual([action]);
  });

  it.each([
    { key: "=" },
    { key: "-", shiftKey: true },
    { key: "=", metaKey: true, altKey: true },
    { key: "9", metaKey: true },
    { key: "f", ctrlKey: true },
  ])("leaves %o to the page", (init) => {
    const port = startRuntime();
    expect(press(init)).toBe(false);
    expect(port.postMessage).not.toHaveBeenCalled();
  });

  it("keeps the app's zoom until the parent connects", () => {
    const port = startRuntime({ connect: false });
    expect(press({ key: "=", metaKey: true })).toBe(false);
    expect(port.postMessage).not.toHaveBeenCalled();
  });

  it.each([
    [["resize"], true],
    [["selectionchange"], false],
    [["resize", "selectionchange"], false],
  ])("re-reports the selection after %o with reflow %s", (events, reflow) => {
    vi.useFakeTimers();
    const getClientRects = Object.getOwnPropertyDescriptor(Range.prototype, "getClientRects");
    try {
      const port = startRuntime();
      document.body.innerHTML = "<p>Quarterly revenue</p>";
      // jsdom has no layout, so give the selected range a rect.
      Object.defineProperty(Range.prototype, "getClientRects", {
        configurable: true,
        value: () => [{ left: 1, top: 2, right: 3, bottom: 4 }],
      });
      document.getSelection()?.selectAllChildren(document.body);
      vi.runAllTimers();
      port.postMessage.mockClear();

      for (const type of events) {
        (type === "resize" ? window : document).dispatchEvent(new Event(type));
      }
      vi.advanceTimersByTime(150);
      expect(port.postMessage.mock.calls).toEqual([
        [
          expect.objectContaining({
            type: BRIDGE_MSG.selection,
            text: "Quarterly revenue",
            reflow,
          }),
        ],
      ]);
    } finally {
      if (getClientRects) Object.defineProperty(Range.prototype, "getClientRects", getClientRects);
      else delete (Range.prototype as Partial<Range>).getClientRects;
      document.getSelection()?.removeAllRanges();
      document.body.innerHTML = "";
      vi.useRealTimers();
    }
  });

  it.each([
    ["document listener that cancels it", () => document, (e: Event) => e.preventDefault()],
    ["window listener that cancels it", () => window, (e: Event) => e.preventDefault()],
    ["listener that stops its propagation", () => document, (e: Event) => e.stopPropagation()],
  ])("leaves the shortcut to an artifact %s", (_, target, handler) => {
    // Artifact scripts run after the injected bridge but before the parent connects.
    const port = startRuntime({
      beforeConnect: () => target().addEventListener("keydown", handler),
    });
    press({ key: "=", metaKey: true });
    expect(port.postMessage).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// Bridge runtime — scroll restore and scroll reports
// ---------------------------------------------------------------------------

describe("bridge runtime scroll position", () => {
  const BUDGET_MS = 1_000;

  afterEach(() => {
    vi.useRealTimers();
    document.head.querySelectorAll("style[data-omni-restore-hide]").forEach((el) => el.remove());
    document.documentElement.style.removeProperty("overflow-anchor");
  });

  /** The style injected with a saved offset to hide the page, as the runtime finds it. */
  function addRestoreHide(nonce = RUNTIME_NONCE) {
    const style = document.createElement("style");
    style.setAttribute("data-omni-restore-hide", nonce);
    document.head.append(style);
    return style;
  }

  /**
   * Fake frames and the clock, and give jsdom a scrollable page whose offset
   * clamps to `pageHeight`, as a browser's does.
   */
  function fakeScrolling({ pageHeight = 100_000 } = {}) {
    vi.useFakeTimers({
      toFake: ["requestAnimationFrame", "cancelAnimationFrame", "performance"],
    });
    let scrollY = 0;
    let height = pageHeight;
    let viewportHeight = window.innerHeight;
    const clamp = (top: number) => Math.max(0, Math.min(top, height - viewportHeight));
    vi.spyOn(window, "scrollY", "get").mockImplementation(() => scrollY);
    vi.spyOn(window, "innerHeight", "get").mockImplementation(() => viewportHeight);
    vi.spyOn(document.documentElement, "scrollHeight", "get").mockImplementation(() => height);
    const scrollTo = vi
      .spyOn(window, "scrollTo")
      .mockImplementation((options?: ScrollToOptions | number) => {
        if (typeof options === "object" && typeof options.top === "number") {
          const previous = scrollY;
          scrollY = clamp(options.top);
          // Like a browser, deliver the scroll event on the next frame, before its callbacks.
          if (scrollY !== previous) {
            requestAnimationFrame(() => window.dispatchEvent(new Event("scroll")));
          }
        }
      });
    const userScrollTo = (top: number) => {
      scrollY = clamp(top);
      window.dispatchEvent(new Event("scroll"));
    };
    const relayout = (nextScrollY: number) => {
      const previous = scrollY;
      scrollY = clamp(nextScrollY);
      if (scrollY !== previous) window.dispatchEvent(new Event("scroll"));
    };
    /** Resize the page as late content does; the browser may move the offset with it. */
    const setPageHeight = (nextHeight: number, nextScrollY = scrollY) => {
      height = nextHeight;
      relayout(nextScrollY);
    };
    /** Resize the viewport, as zooming the preview does; the browser clamps the offset to fit. */
    const setViewportHeight = (nextHeight: number) => {
      viewportHeight = nextHeight;
      relayout(scrollY);
    };
    return { scrollTo, userScrollTo, setPageHeight, setViewportHeight };
  }

  function sentScrollOffsets(port: { postMessage: ReturnType<typeof vi.fn> }) {
    return port.postMessage.mock.calls
      .map(([msg]) => msg as { type: string; top: number; nonce: string })
      .filter((msg) => msg.type === BRIDGE_MSG.scroll)
      .map((msg) => {
        expect(msg.nonce).toBe(RUNTIME_NONCE);
        return msg.top;
      });
  }

  const restoreTo = (top: number) => JSON.stringify({ top, budgetMs: BUDGET_MS });
  const restoredTo = (top: number) => ({ left: 0, top, behavior: "instant" });
  // jsdom's viewport is 768px tall, so a 1,000px page clamps the offset to 232.
  const CLAMPING_PAGE_HEIGHT = 1_000;

  it("reports the reader's offset once per frame", () => {
    const { userScrollTo } = fakeScrolling();
    const port = startRuntime();

    userScrollTo(120);
    userScrollTo(180);
    expect(sentScrollOffsets(port)).toEqual([]);
    vi.advanceTimersToNextFrame();
    expect(sentScrollOffsets(port)).toEqual([180]);

    userScrollTo(0);
    vi.advanceTimersToNextFrame();
    expect(sentScrollOffsets(port)).toEqual([180, 0]);
  });

  it("reports an offset the reader reached before the channel connected", () => {
    const { userScrollTo } = fakeScrolling();
    const port = startRuntime({
      beforeConnect: () => {
        userScrollTo(900);
        vi.advanceTimersToNextFrame();
      },
    });
    expect(sentScrollOffsets(port)).toEqual([]);
    vi.advanceTimersToNextFrame();
    expect(sentScrollOffsets(port)).toEqual([900]);
  });

  it("does not report a clamped offset when the channel connects mid-restore", () => {
    const { setPageHeight } = fakeScrolling({ pageHeight: CLAMPING_PAGE_HEIGHT });
    const port = startRuntime({
      restoreScroll: restoreTo(640),
      beforeConnect: () => {
        setPageHeight(900); // the page shrinks, pulling the offset back to 132
        vi.advanceTimersToNextFrame();
      },
    });
    vi.advanceTimersToNextFrame();
    expect(sentScrollOffsets(port)).toEqual([]);
  });

  it("re-asserts the saved offset each frame until the budget runs out", () => {
    const { scrollTo, userScrollTo, setPageHeight } = fakeScrolling({
      pageHeight: CLAMPING_PAGE_HEIGHT,
    });
    const hide = addRestoreHide();
    const port = startRuntime({ restoreScroll: restoreTo(640) });
    expect(scrollTo).toHaveBeenLastCalledWith(restoredTo(640));
    expect(window.scrollY).toBe(232);
    expect(hide.isConnected).toBe(true);

    // Late content grows the page; the next frame reaches the saved offset and shows it.
    setPageHeight(2_000);
    vi.advanceTimersToNextFrame();
    expect(scrollTo).toHaveBeenCalledTimes(2);
    expect(window.scrollY).toBe(640);
    expect(hide.isConnected).toBe(false);
    // The clamped offset is not reported, so it can't overwrite the saved one.
    expect(sentScrollOffsets(port)).toEqual([]);

    vi.advanceTimersByTime(BUDGET_MS);
    const writes = scrollTo.mock.calls.length;
    vi.advanceTimersToNextFrame();
    expect(scrollTo).toHaveBeenCalledTimes(writes);
    expect(sentScrollOffsets(port)).toEqual([]);

    // After the restore settles, the reader's own scrolling is reported again.
    userScrollTo(700);
    vi.advanceTimersToNextFrame();
    expect(sentScrollOffsets(port)).toEqual([700]);
  });

  it("shows the page short of the saved offset once the budget runs out, without reporting it", () => {
    const { scrollTo, setPageHeight } = fakeScrolling({ pageHeight: CLAMPING_PAGE_HEIGHT });
    const hide = addRestoreHide();
    const port = startRuntime({ restoreScroll: restoreTo(640) });
    vi.advanceTimersByTime(BUDGET_MS - 20);
    expect(hide.isConnected).toBe(true);
    expect(sentScrollOffsets(port)).toEqual([]);
    vi.advanceTimersByTime(40);
    expect(hide.isConnected).toBe(false);
    expect(window.scrollY).toBe(232);

    // Content that arrives after the budget leaves the clamp in place, and the parent keeps 640.
    const writes = scrollTo.mock.calls.length;
    setPageHeight(2_000);
    vi.advanceTimersToNextFrame();
    expect(scrollTo).toHaveBeenCalledTimes(writes);
    expect(window.scrollY).toBe(232);
    expect(sentScrollOffsets(port)).toEqual([]);
  });

  it.each([
    ["after the last write's scroll event arrives", false],
    ["before the last write's scroll event arrives", true],
  ])(
    "doesn't report where a spent budget's last write leaves a page that grew partway, but reports a reader scroll %s",
    (_when, readerScrollsFirst) => {
      const { userScrollTo, setPageHeight } = fakeScrolling({
        pageHeight: CLAMPING_PAGE_HEIGHT,
      });
      const hide = addRestoreHide();
      const deadline = performance.now() + BUDGET_MS;
      const port = startRuntime({ restoreScroll: restoreTo(640) });
      // Run frames until the next one finds the budget spent.
      while (performance.now() + 16 < deadline) vi.advanceTimersToNextFrame();
      expect(hide.isConnected).toBe(true);

      setPageHeight(1_287); // the page can now scroll to 519
      vi.advanceTimersToNextFrame();
      expect(window.scrollY).toBe(519);
      expect(hide.isConnected).toBe(false);

      if (!readerScrollsFirst) {
        vi.advanceTimersToNextFrame(); // delivers the write's scroll event
        vi.advanceTimersToNextFrame(); // where its report would go out
        expect(sentScrollOffsets(port)).toEqual([]);
      }
      userScrollTo(300);
      vi.advanceTimersToNextFrame();
      vi.advanceTimersToNextFrame();
      expect(sentScrollOffsets(port)).toEqual([300]);
    },
  );

  it.each([
    [2_000, 640],
    [1_287, 519],
  ])(
    "re-asserts the saved offset once when a throttled frame lands past the budget on a %ipx page",
    (grownHeight, landedAt) => {
      const { scrollTo, setPageHeight } = fakeScrolling({ pageHeight: CLAMPING_PAGE_HEIGHT });
      const hide = addRestoreHide();
      const port = startRuntime({ restoreScroll: restoreTo(640) });
      expect(window.scrollY).toBe(232);
      // A background tab runs no frames while the page grows and the budget passes.
      setPageHeight(grownHeight);
      vi.spyOn(performance, "now").mockReturnValue(performance.now() + BUDGET_MS);
      vi.advanceTimersToNextFrame();
      expect(scrollTo).toHaveBeenCalledTimes(2);
      expect(window.scrollY).toBe(landedAt);
      expect(hide.isConnected).toBe(false);
      // The write's scroll event arrives a frame later, and its report would go out the next.
      vi.advanceTimersToNextFrame();
      vi.advanceTimersToNextFrame();
      expect(scrollTo).toHaveBeenCalledTimes(2);
      expect(sentScrollOffsets(port)).toEqual([]);
    },
  );

  it("turns scroll anchoring off until the restore settles, then gives the page its own", () => {
    fakeScrolling({ pageHeight: CLAMPING_PAGE_HEIGHT });
    const rootStyle = document.documentElement.style;
    rootStyle.setProperty("overflow-anchor", "auto");
    startRuntime({ restoreScroll: restoreTo(640) });
    expect(rootStyle.getPropertyValue("overflow-anchor")).toBe("none");
    expect(rootStyle.getPropertyPriority("overflow-anchor")).toBe("important");

    vi.advanceTimersByTime(BUDGET_MS + 20);
    expect(rootStyle.getPropertyValue("overflow-anchor")).toBe("auto");
    expect(rootStyle.getPropertyPriority("overflow-anchor")).toBe("");
  });

  it("keeps restoring when content growing above the reader moves the offset", () => {
    const { scrollTo, setPageHeight } = fakeScrolling();
    const port = startRuntime({ restoreScroll: restoreTo(640) });
    // Scroll anchoring shifts the offset by the 300px an image above it adds.
    setPageHeight(100_300, 940);
    vi.advanceTimersToNextFrame();
    expect(scrollTo).toHaveBeenCalledTimes(2);
    expect(window.scrollY).toBe(640);
    expect(sentScrollOffsets(port)).toEqual([]);
  });

  it("keeps restoring when a taller viewport clamps the offset", () => {
    const { setPageHeight, setViewportHeight } = fakeScrolling({ pageHeight: 1_500 });
    const port = startRuntime({ restoreScroll: restoreTo(640) });
    expect(window.scrollY).toBe(640);
    // Zooming the preview out makes its viewport taller, so the page can scroll less.
    setViewportHeight(1_000);
    expect(window.scrollY).toBe(500);
    vi.advanceTimersToNextFrame();
    setPageHeight(3_000);
    vi.advanceTimersToNextFrame();
    expect(window.scrollY).toBe(640);
    expect(sentScrollOffsets(port)).toEqual([]);
  });

  const dispatchToPage = (event: Event) => document.body.dispatchEvent(event);

  /** A `touchmove` with `fingers` points of contact. */
  function touchMove(fingers: number) {
    const move = new Event("touchmove", { bubbles: true });
    Object.defineProperty(move, "touches", { value: Array.from({ length: fingers }, () => ({})) });
    return move;
  }

  /** Press `key` on a focused element of the page, as a reader would. */
  function pressIn(focused: HTMLElement, key: string) {
    document.body.append(focused);
    focused.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true }));
    focused.remove();
  }

  /** Press `key` in `focused` inside a web component, which `window` sees as its host. */
  function pressInShadowRoot(focused: HTMLElement, key: string) {
    const host = document.createElement("div");
    host.attachShadow({ mode: "open" }).append(focused);
    document.body.append(host);
    focused.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true, composed: true }));
    host.remove();
  }

  const cancelable = { bubbles: true, cancelable: true };

  /** Dispatch `event` to a widget that consumes it, as a slide deck does with its keys. */
  function handledByPage(event: Event) {
    const widget = document.createElement("div");
    widget.addEventListener(event.type, (e) => e.preventDefault());
    document.body.append(widget);
    widget.dispatchEvent(event);
    widget.remove();
  }

  function editableText() {
    const text = document.createElement("div");
    // jsdom doesn't implement isContentEditable.
    Object.defineProperty(text, "isContentEditable", { value: true });
    return text;
  }

  it.each<[string, (userScrollTo: (top: number) => void) => void]>([
    ["the wheel", () => dispatchToPage(new Event("wheel", { bubbles: true }))],
    ["a touch drag", () => dispatchToPage(touchMove(1))],
    [
      "a scroll key",
      () => dispatchToPage(new KeyboardEvent("keydown", { key: "PageDown", bubbles: true })),
    ],
    ["a space on a focused link", () => pressIn(document.createElement("a"), " ")],
    ["the scrollbar", (userScrollTo) => userScrollTo(100)],
  ])("stops restoring when the reader takes over with %s", (_label, takeOver) => {
    const { scrollTo, userScrollTo } = fakeScrolling({ pageHeight: CLAMPING_PAGE_HEIGHT });
    const hide = addRestoreHide();
    const port = startRuntime({ restoreScroll: restoreTo(640) });
    takeOver(userScrollTo);
    vi.advanceTimersToNextFrame();
    expect(hide.isConnected).toBe(false);
    expect(scrollTo).toHaveBeenCalledTimes(1);
    vi.advanceTimersToNextFrame();
    // The offset the page shows is saved even when the input didn't move it.
    expect(sentScrollOffsets(port)).toEqual([window.scrollY]);

    userScrollTo(200);
    vi.advanceTimersToNextFrame();
    expect(window.scrollY).toBe(200);
    expect(sentScrollOffsets(port).at(-1)).toBe(200);
  });

  it("lets a scroll by the artifact's own script end the restore", () => {
    const { scrollTo } = fakeScrolling({ pageHeight: CLAMPING_PAGE_HEIGHT });
    const hide = addRestoreHide();
    const port = startRuntime({ restoreScroll: restoreTo(640) });
    // A deferred scrollTo, focus or fragment jump moves the page without input, like the scrollbar.
    window.scrollTo({ top: 100 });
    vi.advanceTimersToNextFrame();
    expect(scrollTo).toHaveBeenCalledTimes(2);
    expect(window.scrollY).toBe(100);
    expect(hide.isConnected).toBe(false);
    vi.advanceTimersToNextFrame();
    expect(sentScrollOffsets(port)).toEqual([100]);
  });

  it.each<[string, () => void]>([
    ["a click", () => dispatchToPage(new Event("pointerdown", { bubbles: true }))],
    ["a tap", () => dispatchToPage(new Event("touchstart", { bubbles: true }))],
    [
      "a modifier key",
      () => dispatchToPage(new KeyboardEvent("keydown", { key: "Control", bubbles: true })),
    ],
    [
      "the preview zoom shortcut",
      () =>
        dispatchToPage(new KeyboardEvent("keydown", { key: "=", ctrlKey: true, bubbles: true })),
    ],
    ["a space typed into a form field", () => pressIn(document.createElement("input"), " ")],
    ["an arrow key in editable text", () => pressIn(editableText(), "ArrowDown")],
    ["a space on a focused button", () => pressIn(document.createElement("button"), " ")],
    ["a space on a <summary>", () => pressIn(document.createElement("summary"), " ")],
    [
      "a space typed into a form field in a shadow root",
      () => pressInShadowRoot(document.createElement("input"), " "),
    ],
    [
      "a space on a button in a shadow root",
      () => pressInShadowRoot(document.createElement("button"), " "),
    ],
    [
      "a trackpad pinch",
      () => dispatchToPage(new WheelEvent("wheel", { ctrlKey: true, bubbles: true })),
    ],
    ["a two-finger pinch", () => dispatchToPage(touchMove(2))],
    [
      "a scroll key the page handles",
      () => handledByPage(new KeyboardEvent("keydown", { key: "PageDown", ...cancelable })),
    ],
    ["a wheel the page handles", () => handledByPage(new WheelEvent("wheel", cancelable))],
  ])("keeps restoring through %s", (_label, interact) => {
    const { scrollTo } = fakeScrolling({ pageHeight: CLAMPING_PAGE_HEIGHT });
    const hide = addRestoreHide();
    startRuntime({ restoreScroll: restoreTo(640) });
    interact();
    vi.advanceTimersToNextFrame();
    expect(scrollTo).toHaveBeenCalledTimes(2);
    expect(scrollTo).toHaveBeenLastCalledWith(restoredTo(640));
    expect(hide.isConnected).toBe(true);
  });

  interface RuntimePort {
    onmessage: ((event: { data: unknown }) => void) | null;
    postMessage: ReturnType<typeof vi.fn>;
  }

  /** Restore to 640 in a page whose one comment's highlight starts `highlightTop` px down. */
  function restoreWithComment(
    highlightTop: number,
    run: (port: RuntimePort, hide: HTMLStyleElement) => void,
  ) {
    const rect = Object.getOwnPropertyDescriptor(Range.prototype, "getBoundingClientRect");
    try {
      document.body.innerHTML = "<p>Quarterly revenue</p>";
      // jsdom lacks the Custom Highlight API the runtime tracks active ranges with.
      vi.stubGlobal("CSS", { highlights: new Map() });
      vi.stubGlobal("Highlight", Set);
      // jsdom has no layout, so place the commented text explicitly.
      Object.defineProperty(Range.prototype, "getBoundingClientRect", {
        configurable: true,
        value: () => {
          const top = highlightTop - window.scrollY;
          return { top, bottom: top + 20, width: 60, height: 20 };
        },
      });
      const hide = addRestoreHide();
      const port = startRuntime({ restoreScroll: restoreTo(640) }) as unknown as RuntimePort;
      port.onmessage?.({
        data: {
          type: BRIDGE_MSG.setComments,
          comments: [{ id: "c1", anchor_content: "revenue", occ: 0 }],
        },
      });
      run(port, hide);
    } finally {
      if (rect) Object.defineProperty(Range.prototype, "getBoundingClientRect", rect);
      else delete (Range.prototype as Partial<Range>).getBoundingClientRect;
      vi.unstubAllGlobals();
      document.body.innerHTML = "";
    }
  }

  const activate = (port: RuntimePort, commentId: string | null) =>
    port.onmessage?.({
      data: {
        type: BRIDGE_MSG.setActive,
        active: commentId ? { comment_id: commentId, anchor_content: "revenue" } : null,
      },
    });
  const smoothScroll = expect.objectContaining({ behavior: "smooth" });
  // Centers the 20px highlight at 4,640 in jsdom's 768px viewport.
  const toOffscreenComment = { top: 4_266, behavior: "smooth" };

  it.each([
    ["an offscreen comment, scrolling to it", 4_640, true],
    ["an already visible comment, leaving the page still", 740, false],
  ])("stops restoring when the reader picks %s", (_label, highlightTop, scrollsToComment) => {
    const { scrollTo } = fakeScrolling({ pageHeight: CLAMPING_PAGE_HEIGHT });
    restoreWithComment(highlightTop, (port, hide) => {
      activate(port, null); // the parent's initial sync on connect
      expect(hide.isConnected).toBe(true);
      activate(port, "c1");
      expect(hide.isConnected).toBe(false);
      if (scrollsToComment) expect(scrollTo).toHaveBeenLastCalledWith(toOffscreenComment);
      else expect(scrollTo).not.toHaveBeenCalledWith(smoothScroll);

      const writes = scrollTo.mock.calls.length;
      vi.advanceTimersToNextFrame();
      expect(scrollTo).toHaveBeenCalledTimes(writes);
      expect(sentScrollOffsets(port)).toEqual([window.scrollY]);
    });
  });

  it.each([
    ["", undefined, false],
    [" while late content clamps the page", CLAMPING_PAGE_HEIGHT, true],
  ])(
    "keeps restoring when the initial sync restates a comment visible at the saved offset%s",
    (_label, pageHeight, stillHidden) => {
      const { scrollTo } = fakeScrolling({ pageHeight });
      // Clamped to 232, this highlight is below the viewport; at 640 it is in view.
      restoreWithComment(1_300, (port, hide) => {
        activate(port, "c1");
        expect(scrollTo).not.toHaveBeenCalledWith(smoothScroll);
        const writes = scrollTo.mock.calls.length;
        vi.advanceTimersToNextFrame();
        expect(scrollTo).toHaveBeenCalledTimes(writes + 1);
        expect(scrollTo).toHaveBeenLastCalledWith(restoredTo(640));
        expect(hide.isConnected).toBe(stillHidden);
      });
    },
  );

  // A linked comment reaches the frame in this same first sync, so it must win.
  it("scrolls to an active comment outside the saved view on the initial sync", () => {
    const { scrollTo } = fakeScrolling({ pageHeight: CLAMPING_PAGE_HEIGHT });
    restoreWithComment(4_640, (port, hide) => {
      activate(port, "c1");
      expect(scrollTo).toHaveBeenLastCalledWith(toOffscreenComment);
      expect(hide.isConnected).toBe(false);
      const writes = scrollTo.mock.calls.length;
      vi.advanceTimersToNextFrame();
      expect(scrollTo).toHaveBeenCalledTimes(writes);
    });
  });

  it.each([
    ["no saved offset", undefined],
    ["null", "null"],
    ["invalid JSON", "{"],
    ["a negative offset", JSON.stringify({ top: -10, budgetMs: BUDGET_MS })],
    ["a string offset", JSON.stringify({ top: "640", budgetMs: BUDGET_MS })],
    ["no budget", JSON.stringify({ top: 640 })],
    ["a zero budget", JSON.stringify({ top: 640, budgetMs: 0 })],
  ])("starts without restoring given %s", (_label, restoreScroll) => {
    const { scrollTo } = fakeScrolling();
    const hide = addRestoreHide();
    const artifactStyle = addRestoreHide("another-nonce");
    startRuntime({ restoreScroll });
    expect(hide.isConnected).toBe(false);
    expect(artifactStyle.isConnected).toBe(true);
    vi.advanceTimersToNextFrame();
    expect(scrollTo).not.toHaveBeenCalled();
    expect(runtimeListeners.some(([, type]) => type === "wheel")).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// parseBridgeMessage — inbound validation (guards against spoofed postMessage)
// ---------------------------------------------------------------------------

describe("parseBridgeMessage", () => {
  const NONCE = "n1";
  const base = { source: BRIDGE_SOURCE, nonce: NONCE };

  it("accepts a well-formed selection message with its occurrence index", () => {
    const msg = parseBridgeMessage(
      {
        ...base,
        type: BRIDGE_MSG.selection,
        text: "Design Goals",
        occ: 2,
        prefix: "before ",
        suffix: " after",
        rect: { left: 1, top: 2, right: 3, bottom: 4 },
      },
      NONCE,
    );
    expect(msg).toEqual({
      type: BRIDGE_MSG.selection,
      text: "Design Goals",
      occ: 2,
      prefix: "before ",
      suffix: " after",
      rect: { left: 1, top: 2, right: 3, bottom: 4 },
      reflow: false,
    });
  });

  it("marks a selection re-reported after the frame resized", () => {
    const rect = { left: 0, top: 0, right: 0, bottom: 0 };
    const parse = (reflow: unknown) =>
      parseBridgeMessage(
        {
          ...base,
          type: BRIDGE_MSG.selection,
          text: "x",
          occ: 0,
          prefix: "",
          suffix: "",
          rect,
          reflow,
        },
        NONCE,
      );
    expect(parse(true)).toMatchObject({ reflow: true });
    expect(parse("true")).toMatchObject({ reflow: false });
  });

  it("rejects a selection message missing disambiguating context", () => {
    const msg = parseBridgeMessage(
      {
        ...base,
        type: BRIDGE_MSG.selection,
        text: "x",
        rect: { left: 0, top: 0, right: 0, bottom: 0 },
      },
      NONCE,
    );
    expect(msg).toBeNull();
  });

  it("round-trips anchor text containing quotes and newlines", () => {
    const text = 'He said "hi"\nthen left';
    const msg = parseBridgeMessage(
      {
        ...base,
        type: BRIDGE_MSG.selection,
        text,
        occ: 0,
        prefix: "",
        suffix: "",
        rect: { left: 0, top: 0, right: 0, bottom: 0 },
      },
      NONCE,
    );
    expect(msg && "text" in msg && msg.text).toBe(text);
  });

  it("accepts commentClick, selectionCleared, and ready", () => {
    expect(parseBridgeMessage({ ...base, type: BRIDGE_MSG.commentClick, id: "c1" }, NONCE)).toEqual(
      {
        type: BRIDGE_MSG.commentClick,
        id: "c1",
      },
    );
    expect(parseBridgeMessage({ ...base, type: BRIDGE_MSG.selectionCleared }, NONCE)).toEqual({
      type: BRIDGE_MSG.selectionCleared,
    });
    expect(parseBridgeMessage({ ...base, type: BRIDGE_MSG.ready }, NONCE)).toEqual({
      type: BRIDGE_MSG.ready,
    });
  });

  it("passes a string pathname through on ready and ignores non-strings", () => {
    expect(
      parseBridgeMessage(
        { ...base, type: BRIDGE_MSG.ready, pathname: "/omni/v1/artifacts/a1.x.y/index.html" },
        NONCE,
      ),
    ).toEqual({
      type: BRIDGE_MSG.ready,
      pathname: "/omni/v1/artifacts/a1.x.y/index.html",
    });
    expect(parseBridgeMessage({ ...base, type: BRIDGE_MSG.ready, pathname: 7 }, NONCE)).toEqual({
      type: BRIDGE_MSG.ready,
    });
  });

  it("accepts an openPath message carrying string href and base", () => {
    expect(
      parseBridgeMessage(
        {
          ...base,
          type: BRIDGE_MSG.openPath,
          href: "../outside.html",
          base: "http://host/v1/artifacts/tok/index.html",
        },
        NONCE,
      ),
    ).toEqual({
      type: BRIDGE_MSG.openPath,
      href: "../outside.html",
      base: "http://host/v1/artifacts/tok/index.html",
    });
  });

  it("rejects an openPath message with non-string or oversized fields", () => {
    const msg = (href: unknown, baseValue: unknown = "http://host/") => ({
      ...base,
      type: BRIDGE_MSG.openPath,
      href,
      base: baseValue,
    });
    expect(parseBridgeMessage(msg(7), NONCE)).toBeNull();
    expect(parseBridgeMessage(msg("../x", 7), NONCE)).toBeNull();
    expect(parseBridgeMessage(msg("a".repeat(4097)), NONCE)).toBeNull();
    expect(parseBridgeMessage(msg("../x", "b".repeat(4097)), NONCE)).toBeNull();
    // Exactly at the cap is still accepted.
    expect(parseBridgeMessage(msg("a".repeat(4096)), NONCE)).not.toBeNull();
  });

  it("accepts a zoom request only with a known action", () => {
    for (const action of ["in", "out", "reset"]) {
      expect(parseBridgeMessage({ ...base, type: BRIDGE_MSG.zoom, action }, NONCE)).toEqual({
        type: BRIDGE_MSG.zoom,
        action,
      });
    }
    expect(parseBridgeMessage({ ...base, type: BRIDGE_MSG.zoom, action: "2" }, NONCE)).toBeNull();
    expect(parseBridgeMessage({ ...base, type: BRIDGE_MSG.zoom }, NONCE)).toBeNull();
  });

  it("accepts a scroll report only with a finite, non-negative offset", () => {
    expect(parseBridgeMessage({ ...base, type: BRIDGE_MSG.scroll, top: 0 }, NONCE)).toEqual({
      type: BRIDGE_MSG.scroll,
      top: 0,
    });
    expect(parseBridgeMessage({ ...base, type: BRIDGE_MSG.scroll, top: 812.5 }, NONCE)).toEqual({
      type: BRIDGE_MSG.scroll,
      top: 812.5,
    });
    for (const top of [-1, Number.NaN, Number.POSITIVE_INFINITY, "640", undefined]) {
      expect(parseBridgeMessage({ ...base, type: BRIDGE_MSG.scroll, top }, NONCE)).toBeNull();
    }
  });

  it("rejects a wrong nonce (spoof from artifact JS)", () => {
    expect(
      parseBridgeMessage({ ...base, nonce: "other", type: BRIDGE_MSG.selectionCleared }, NONCE),
    ).toBeNull();
  });

  it("rejects a wrong source tag", () => {
    expect(
      parseBridgeMessage(
        { source: "evil", nonce: NONCE, type: BRIDGE_MSG.selectionCleared },
        NONCE,
      ),
    ).toBeNull();
  });

  it("rejects an unknown type, a malformed selection, and non-objects", () => {
    expect(parseBridgeMessage({ ...base, type: "omni:bogus" }, NONCE)).toBeNull();
    // Empty text / missing rect must not produce a selection.
    expect(
      parseBridgeMessage({ ...base, type: BRIDGE_MSG.selection, text: "   " }, NONCE),
    ).toBeNull();
    expect(
      parseBridgeMessage({ ...base, type: BRIDGE_MSG.selection, text: "x" }, NONCE),
    ).toBeNull();
    expect(parseBridgeMessage("not-an-object", NONCE)).toBeNull();
    expect(parseBridgeMessage(null, NONCE)).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// anchorOccurrence — which copy of repeated anchor text a comment refers to
// ---------------------------------------------------------------------------

describe("anchorOccurrence", () => {
  // A title reused verbatim in the body — the exact case that highlighted both.
  const src = "<h1>Aurora Sync</h1><p>Aurora Sync keeps state.</p>";
  const first = src.indexOf("Aurora Sync");
  const second = src.indexOf("Aurora Sync", first + 1);

  it("returns 0 for the first occurrence (the title)", () => {
    expect(anchorOccurrence(src, "Aurora Sync", first)).toBe(0);
  });

  it("returns 1 for the second occurrence (the body)", () => {
    expect(anchorOccurrence(src, "Aurora Sync", second)).toBe(1);
  });

  it("counts whitespace-tolerantly so wrapped source occurrences still count", () => {
    // First occurrence wraps across lines; the second is the target.
    const wrapped = "<p>then\n   latency</p><p>then latency again</p>";
    const target = wrapped.indexOf("then latency again");
    expect(anchorOccurrence(wrapped, "then latency", target)).toBe(1);
  });

  it("returns 0 for empty anchor text", () => {
    expect(anchorOccurrence(src, "   ", 0)).toBe(0);
  });

  it("does not count matches in non-rendered regions (attributes/comments)", () => {
    // An attribute "Submit" precedes the rendered button text; the rendered copy
    // must still be occurrence 0 so its count aligns with the in-frame bridge.
    const withAttr = '<button title="Submit"><!-- Submit --><span>Submit</span></button>';
    const rendered = withAttr.lastIndexOf("Submit");
    expect(anchorOccurrence(withAttr, "Submit", rendered)).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// omni-html-bridge.js runtime — handshake and link handling in jsdom
// ---------------------------------------------------------------------------

describe("omni-html-bridge.js runtime", () => {
  const NONCE = "n1";
  const ORIGIN = "http://localhost:3000";
  const BUNDLE_URL = `${ORIGIN}/omni/v1/artifacts/a1.x.y/index.html`;

  type BridgeWindow = Window & typeof globalThis;

  const windows: BridgeWindow[] = [];

  // Evaluate the asset like the server's inline <script> (data-omni-nonce on
  // the element, currentScript stubbed since it is valid only mid-run) in a
  // window of its own, so no test observes another's listeners.
  function startBridge(url: string, nonce: string | null): BridgeWindow {
    const dom = new JSDOM("<!doctype html><html><body></body></html>", {
      url,
      runScripts: "outside-only",
    });
    const win = dom.window as BridgeWindow;
    windows.push(win);
    const script = win.document.createElement("script");
    if (nonce !== null) script.dataset.omniNonce = nonce;
    Object.defineProperty(win.document, "currentScript", { value: script, configurable: true });
    try {
      win.eval(bridgeSource);
    } finally {
      delete (win.document as { currentScript?: unknown }).currentScript;
    }
    return win;
  }

  function clickAnchor(
    win: BridgeWindow,
    href: string,
    target?: string,
    download?: boolean,
  ): MouseEvent {
    const anchor = win.document.createElement("a");
    anchor.href = href;
    if (target) anchor.setAttribute("target", target);
    if (download) anchor.setAttribute("download", "");
    win.document.body.appendChild(anchor);
    const ev = new win.MouseEvent("click", { bubbles: true, cancelable: true });
    anchor.dispatchEvent(ev);
    anchor.remove();
    return ev;
  }

  function stubOpen(win: BridgeWindow) {
    const open = vi.fn(() => null);
    win.open = open;
    return open;
  }

  /** Let queued MessagePort deliveries run. */
  function flushPorts() {
    return new Promise<void>((resolve) => {
      setTimeout(resolve, 0);
    });
  }

  afterEach(() => {
    for (const win of windows) win.close();
    windows.length = 0;
  });

  it("captures the selected rendered copy and paints it after reload; stale context stays orphaned", async () => {
    const win = startBridge(BUNDLE_URL, NONCE);
    win.document.body.innerHTML =
      "<p>Alpha &amp; <em>Code</em></p><p>Beta &amp; <strong>Code</strong></p>";
    const highlights = new Map<string, { ranges: Range[] }>();
    (win as unknown as { CSS: unknown }).CSS = {
      highlights: {
        set: (name: string, value: { ranges: Range[] }) => highlights.set(name, value),
      },
    };
    (win as unknown as { Highlight: unknown }).Highlight = class {
      ranges: Range[];
      constructor(...ranges: Range[]) {
        this.ranges = ranges;
      }
    };
    const channel = new MessageChannel();
    const messages: Record<string, unknown>[] = [];
    channel.port2.onmessage = (ev) => messages.push(ev.data as Record<string, unknown>);
    win.dispatchEvent(
      new win.MessageEvent("message", {
        data: { source: BRIDGE_SOURCE, nonce: NONCE, type: BRIDGE_MSG.init },
        ports: [channel.port1],
      }),
    );
    await flushPorts();

    const second = win.document.querySelector("strong")!.firstChild!;
    const range = win.document.createRange();
    range.setStart(second, 0);
    range.setEnd(second, 4);
    Object.defineProperty(range, "getClientRects", {
      value: () => [{ left: 0, top: 0, right: 20, bottom: 12 }],
    });
    win.getSelection()!.removeAllRanges();
    win.getSelection()!.addRange(range);
    second.parentElement!.dispatchEvent(new win.MouseEvent("mouseup", { bubbles: true }));
    await flushPorts();
    const captured = messages.find((m) => m.type === BRIDGE_MSG.selection)!;
    expect(captured).toMatchObject({ text: "Code", occ: 1 });
    expect(captured.prefix).toContain("Beta & ");
    const anchor = {
      v: 1,
      kind: "html",
      text: captured.text,
      occ: captured.occ,
      prefix: captured.prefix,
      suffix: captured.suffix,
      selector: captured.selector,
    };

    channel.port2.postMessage({
      type: BRIDGE_MSG.setComments,
      comments: [{ id: "c2", anchor_content: "Code", anchor }],
    });
    await flushPorts();
    expect(highlights.get("omni-comment")?.ranges.map((r) => r.toString())).toEqual(["Code"]);
    expect(messages.at(-1)).toMatchObject({ type: BRIDGE_MSG.orphans, ids: [] });

    const spanning = win.document.createRange();
    spanning.setStart(win.document.querySelectorAll("p")[1].firstChild!, 0);
    spanning.setEnd(second, 4);
    Object.defineProperty(spanning, "getClientRects", {
      value: () => [{ left: 0, top: 0, right: 80, bottom: 12 }],
    });
    win.getSelection()!.removeAllRanges();
    win.getSelection()!.addRange(spanning);
    second.parentElement!.dispatchEvent(new win.MouseEvent("mouseup", { bubbles: true }));
    await flushPorts();
    expect(messages.at(-1)).toMatchObject({ type: BRIDGE_MSG.selection, text: "Beta & Code" });

    win.document.body.innerHTML =
      "<p>Alpha &amp; <em>Code</em></p><p>Gamma &amp; <strong>Code</strong></p>";
    channel.port2.postMessage({
      type: BRIDGE_MSG.setComments,
      comments: [{ id: "c2", anchor_content: "Code", anchor }],
    });
    await flushPorts();
    expect(messages.at(-1)).toMatchObject({ type: BRIDGE_MSG.orphans, ids: ["c2"] });
    expect(highlights.get("omni-comment")?.ranges).toEqual([]);
    win.document.body.innerHTML = "<p>Code</p><p>Code</p>";
    channel.port2.postMessage({
      type: BRIDGE_MSG.setComments,
      comments: [
        {
          id: "ambiguous",
          anchor_content: "Code",
          anchor: {
            v: 1,
            kind: "html",
            text: "Code",
            occ: 1,
            prefix: "",
            suffix: "",
            selector: "p:nth-of-type(2)",
          },
        },
        { id: "legacy-zero", anchor_content: "", occ: -1 },
      ],
    });
    await flushPorts();
    expect(messages.at(-1)).toMatchObject({
      type: BRIDGE_MSG.orphans,
      ids: ["ambiguous", "legacy-zero"],
    });
    const before = "😀" + "a".repeat(31);
    const after = "b".repeat(31) + "😀";
    win.document.body.innerHTML = `<span>${before}Code${after}</span>`;
    const textNode = win.document.querySelector("span")!.firstChild!;
    const emojiRange = win.document.createRange();
    emojiRange.setStart(textNode, before.length);
    emojiRange.setEnd(textNode, before.length + 4);
    Object.defineProperty(emojiRange, "getClientRects", {
      value: () => [{ left: 0, top: 0, right: 20, bottom: 12 }],
    });
    win.getSelection()!.removeAllRanges();
    win.getSelection()!.addRange(emojiRange);
    textNode.parentElement!.dispatchEvent(new win.MouseEvent("mouseup", { bubbles: true }));
    await flushPorts();
    expect(messages.at(-1)).toMatchObject({
      type: BRIDGE_MSG.selection,
      text: "Code",
      prefix: "a".repeat(31),
      suffix: "b".repeat(31),
    });
    win.document.body.textContent =
      "A".repeat(32) +
      "Code" +
      "B".repeat(32) +
      ("X".repeat(32) + "Code" + "Y".repeat(32)).repeat(1002) +
      "A".repeat(32) +
      "Code" +
      "B".repeat(32);
    channel.port2.postMessage({
      type: BRIDGE_MSG.setComments,
      comments: [
        {
          id: "late-duplicate",
          anchor_content: "Code",
          anchor: {
            v: 1,
            kind: "html",
            text: "Code",
            occ: 0,
            prefix: "A".repeat(32),
            suffix: "B".repeat(32),
          },
        },
      ],
    });
    await flushPorts();
    expect(messages.at(-1)).toMatchObject({ type: BRIDGE_MSG.orphans, ids: ["late-duplicate"] });
    expect(highlights.get("omni-comment")?.ranges).toEqual([]);
    win.document.body.innerHTML = "<p>Alpha</p><p>Beta</p>";
    const blocks = win.document.querySelectorAll("p");
    const blockRange = win.document.createRange();
    blockRange.setStart(blocks[0].firstChild!, 0);
    blockRange.setEnd(blocks[1].firstChild!, 4);
    Object.defineProperty(blockRange, "getClientRects", {
      value: () => [{ left: 0, top: 0, right: 80, bottom: 12 }],
    });
    win.getSelection()!.removeAllRanges();
    win.getSelection()!.addRange(blockRange);
    // jsdom has no layout; Chromium inserts paragraph breaks in Selection text.
    Object.defineProperty(win.getSelection()!, "toString", {
      value: () => "Alpha\n\nBeta",
      configurable: true,
    });
    blocks[1].dispatchEvent(new win.MouseEvent("mouseup", { bubbles: true }));
    await flushPorts();
    const crossBlock = messages.at(-1)!;
    expect(crossBlock).toMatchObject({ type: BRIDGE_MSG.selection, text: "Alpha\n\nBeta" });
    channel.port2.postMessage({
      type: BRIDGE_MSG.setComments,
      comments: [
        {
          id: "blocks",
          anchor_content: crossBlock.text,
          anchor: {
            v: 1,
            kind: "html",
            text: crossBlock.text,
            occ: crossBlock.occ,
            prefix: crossBlock.prefix,
            suffix: crossBlock.suffix,
          },
        },
      ],
    });
    await flushPorts();
    expect(messages.at(-1)).toMatchObject({ type: BRIDGE_MSG.orphans, ids: [] });
    expect(highlights.get("omni-comment")?.ranges).toHaveLength(1);

    win.document.body.innerHTML = "<p>Al<em>pha</em></p>";
    Reflect.deleteProperty(win.getSelection()!, "toString");
    const inlineRange = win.document.createRange();
    inlineRange.setStart(win.document.querySelector("p")!.firstChild!, 0);
    inlineRange.setEnd(win.document.querySelector("em")!.firstChild!, 3);
    Object.defineProperty(inlineRange, "getClientRects", {
      value: () => [{ left: 0, top: 0, right: 50, bottom: 12 }],
    });
    win.getSelection()!.removeAllRanges();
    win.getSelection()!.addRange(inlineRange);
    win.document
      .querySelector("em")!
      .dispatchEvent(new win.MouseEvent("mouseup", { bubbles: true }));
    await flushPorts();
    expect(messages.at(-1)).toMatchObject({ type: BRIDGE_MSG.selection, text: "Alpha" });
    channel.port1.close();
    channel.port2.close();
  });

  it("registers nothing when the script carries no nonce", async () => {
    const win = startBridge(BUNDLE_URL, null);

    const channel = new MessageChannel();
    const onMessage = vi.fn();
    channel.port2.onmessage = onMessage;
    win.dispatchEvent(
      new win.MessageEvent("message", {
        data: { source: BRIDGE_SOURCE, nonce: NONCE, type: BRIDGE_MSG.init },
        ports: [channel.port1],
      }),
    );
    await new Promise((resolve) => {
      setTimeout(resolve, 0);
    });
    expect(onMessage).not.toHaveBeenCalled();

    // No click listener either: an external link is not hijacked.
    const open = stubOpen(win);
    const external = clickAnchor(win, "https://example.com/x");
    expect(open).not.toHaveBeenCalled();
    expect(external.defaultPrevented).toBe(false);

    channel.port1.close();
    channel.port2.close();
  });

  it("adopts the init port, reports its pathname, and routes links by origin", async () => {
    const win = startBridge(BUNDLE_URL, NONCE);

    const channel = new MessageChannel();
    const messages: Record<string, unknown>[] = [];
    const ready = new Promise<void>((resolve) => {
      channel.port2.onmessage = (ev) => {
        messages.push(ev.data as Record<string, unknown>);
        if ((ev.data as { type?: string }).type === BRIDGE_MSG.ready) resolve();
      };
    });
    win.dispatchEvent(
      new win.MessageEvent("message", {
        data: { source: BRIDGE_SOURCE, nonce: NONCE, type: BRIDGE_MSG.init },
        ports: [channel.port1],
      }),
    );

    await ready;
    expect(messages[0]).toMatchObject({
      source: BRIDGE_SOURCE,
      nonce: NONCE,
      type: BRIDGE_MSG.ready,
      pathname: "/omni/v1/artifacts/a1.x.y/index.html",
    });

    const open = stubOpen(win);

    // Another origin: intercepted into a new tab.
    const external = clickAnchor(win, "https://example.com/x");
    expect(open).toHaveBeenCalledWith("https://example.com/x", "_blank", "noopener,noreferrer");
    expect(external.defaultPrevented).toBe(true);

    // Same bundle without a target: left native, so the page's own handlers
    // keep control and the bridge neither posts it nor moves the frame.
    open.mockClear();
    const sibling = clickAnchor(win, "page2.html");
    expect(open).not.toHaveBeenCalled();
    expect(sibling.defaultPrevented).toBe(false);

    // Same origin but a different token: outside this bundle, so the parent
    // opens it in the file viewer instead of a 404 tab.
    messages.length = 0;
    const otherToken = clickAnchor(win, "/omni/v1/artifacts/OTHER/x.html");
    expect(open).not.toHaveBeenCalled();
    expect(otherToken.defaultPrevented).toBe(true);
    await flushPorts();
    expect(messages).toContainEqual({
      source: BRIDGE_SOURCE,
      nonce: NONCE,
      type: BRIDGE_MSG.openPath,
      href: "/omni/v1/artifacts/OTHER/x.html",
      base: BUNDLE_URL,
    });

    channel.port1.close();
    channel.port2.close();
  });

  it("treats a link inside the first v1/artifacts bundle as internal when the path nests", () => {
    const win = startBridge(
      `${ORIGIN}/omni/v1/artifacts/T/dir/v1/artifacts/folder/index.html`,
      NONCE,
    );

    const open = stubOpen(win);

    // Resolves to /omni/v1/artifacts/T/dir/sibling.html — inside bundle T even
    // though the page path carries a second `v1/artifacts` segment.
    const sibling = clickAnchor(win, "../../../sibling.html");
    expect(open).not.toHaveBeenCalled();
    // No target: left native. Being read as outside the bundle would have
    // prevented the click to post openPath instead.
    expect(sibling.defaultPrevented).toBe(false);
  });

  it("treats a same-path link under a different scheme as external", () => {
    const win = startBridge("https://localhost:3000/omni/v1/artifacts/T/index.html", NONCE);

    const open = stubOpen(win);

    const insecure = clickAnchor(win, "http://localhost:3000/omni/v1/artifacts/T/page.html");
    expect(open).toHaveBeenCalledWith(
      "http://localhost:3000/omni/v1/artifacts/T/page.html",
      "_blank",
      "noopener,noreferrer",
    );
    expect(insecure.defaultPrevented).toBe(true);
  });

  /** Adopt a port with an init carrying *visit*, and await the bridge's ready. */
  async function initBridge(win: BridgeWindow, visit: boolean | undefined) {
    const channel = new MessageChannel();
    const ready = new Promise<void>((resolve) => {
      channel.port2.onmessage = () => resolve();
    });
    win.dispatchEvent(
      new win.MessageEvent("message", {
        data: {
          source: BRIDGE_SOURCE,
          nonce: NONCE,
          type: BRIDGE_MSG.init,
          ...(visit === undefined ? {} : { visit }),
        },
        ports: [channel.port1],
      }),
    );
    await ready;
    return channel;
  }

  it("navigates the frame for in-bundle links with an escaping target", async () => {
    const win = startBridge(BUNDLE_URL, NONCE);
    const channel = await initBridge(win, undefined);
    const open = stubOpen(win);

    // `_top`/`_parent` would navigate the top page and `_blank` opens a tab;
    // the panel keeps the frame in charge of all three.
    const top = clickAnchor(win, "#top-sec", "_top");
    expect(top.defaultPrevented).toBe(true);
    expect(win.location.href).toBe(`${BUNDLE_URL}#top-sec`);

    const parent = clickAnchor(win, "#parent-sec", "_parent");
    expect(parent.defaultPrevented).toBe(true);
    expect(win.location.href).toBe(`${BUNDLE_URL}#parent-sec`);

    const blank = clickAnchor(win, "#blank-sec", "_blank");
    expect(blank.defaultPrevented).toBe(true);
    expect(win.location.href).toBe(`${BUNDLE_URL}#blank-sec`);
    expect(open).not.toHaveBeenCalled();

    channel.port1.close();
    channel.port2.close();
  });

  it("uses the base target for an in-bundle link with no target attribute", async () => {
    const win = startBridge(BUNDLE_URL, NONCE);
    const channel = await initBridge(win, undefined);
    const open = stubOpen(win);

    const base = win.document.createElement("base");
    base.setAttribute("target", "_blank");
    win.document.head.appendChild(base);
    const inherited = clickAnchor(win, "#inherited-sec");
    expect(inherited.defaultPrevented).toBe(true);
    expect(win.location.href).toBe(`${BUNDLE_URL}#inherited-sec`);
    expect(open).not.toHaveBeenCalled();

    channel.port1.close();
    channel.port2.close();
  });

  it("leaves an in-bundle link with no escaping target native", async () => {
    const win = startBridge(BUNDLE_URL, NONCE);
    const channel = await initBridge(win, undefined);
    const open = stubOpen(win);

    const sibling = clickAnchor(win, "page2.html");
    expect(sibling.defaultPrevented).toBe(false);
    expect(open).not.toHaveBeenCalled();
    expect(win.location.href).toBe(BUNDLE_URL);

    // A target-less fragment: had the bridge assigned it, the frame would have
    // moved to #tab1.
    const fragment = clickAnchor(win, "#tab1");
    expect(fragment.defaultPrevented).toBe(false);
    expect(win.location.href).toBe(BUNDLE_URL);

    channel.port1.close();
    channel.port2.close();
  });

  it("prefers the anchor's own target over the inherited base target", async () => {
    const win = startBridge(BUNDLE_URL, NONCE);
    const channel = await initBridge(win, undefined);
    const open = stubOpen(win);

    const base = win.document.createElement("base");
    base.setAttribute("target", "_blank");
    win.document.head.appendChild(base);
    const self = clickAnchor(win, "#self-sec", "_self");
    expect(self.defaultPrevented).toBe(false);
    expect(win.location.href).toBe(BUNDLE_URL);
    expect(open).not.toHaveBeenCalled();

    channel.port1.close();
    channel.port2.close();
  });

  it("lets the page's own handler cancel an in-bundle fragment link (scenario 30)", async () => {
    const win = startBridge(BUNDLE_URL, NONCE);
    const channel = await initBridge(win, undefined);
    const open = stubOpen(win);

    const anchor = win.document.createElement("a");
    anchor.setAttribute("href", "#tab2");
    const onClick = vi.fn((ev: Event) => ev.preventDefault());
    anchor.addEventListener("click", onClick);
    win.document.body.appendChild(anchor);
    const ev = new win.MouseEvent("click", { bubbles: true, cancelable: true });
    anchor.dispatchEvent(ev);

    expect(onClick).toHaveBeenCalledTimes(1);
    expect(ev.defaultPrevented).toBe(true);
    // The bridge's capture listener runs first: leaving the click alone is
    // what lets the handler cancel before any frame navigation happens.
    expect(win.location.href).toBe(BUNDLE_URL);
    expect(open).not.toHaveBeenCalled();

    channel.port1.close();
    channel.port2.close();
  });

  it("keeps an in-bundle download link native even with an escaping target", async () => {
    const win = startBridge(BUNDLE_URL, NONCE);
    const channel = await initBridge(win, undefined);
    const open = stubOpen(win);

    const download = clickAnchor(win, "report.pdf", "_blank", true);
    expect(download.defaultPrevented).toBe(false);
    expect(open).not.toHaveBeenCalled();
    expect(win.location.href).toBe(BUNDLE_URL);

    channel.port1.close();
    channel.port2.close();
  });

  it("posts openPath for a file: link or a link above the bundle", async () => {
    const win = startBridge(BUNDLE_URL, NONCE);
    const channel = new MessageChannel();
    const messages: Record<string, unknown>[] = [];
    const ready = new Promise<void>((resolve) => {
      channel.port2.onmessage = (ev) => {
        messages.push(ev.data as Record<string, unknown>);
        if ((ev.data as { type?: string }).type === BRIDGE_MSG.ready) resolve();
      };
    });
    win.dispatchEvent(
      new win.MessageEvent("message", {
        data: { source: BRIDGE_SOURCE, nonce: NONCE, type: BRIDGE_MSG.init },
        ports: [channel.port1],
      }),
    );
    await ready;
    messages.length = 0;

    const open = stubOpen(win);

    const file = clickAnchor(win, "file:///abs/ws/outside.html");
    expect(file.defaultPrevented).toBe(true);
    expect(open).not.toHaveBeenCalled();

    // `../..` from the entry reaches /omni/v1/outside.html: same origin but
    // outside the artifact prefix, so it belongs to the parent, not the frame.
    const escape = clickAnchor(win, "../../outside.html");
    expect(escape.defaultPrevented).toBe(true);
    expect(open).not.toHaveBeenCalled();

    await flushPorts();
    expect(messages).toEqual([
      {
        source: BRIDGE_SOURCE,
        nonce: NONCE,
        type: BRIDGE_MSG.openPath,
        href: "file:///abs/ws/outside.html",
        base: BUNDLE_URL,
      },
      {
        source: BRIDGE_SOURCE,
        nonce: NONCE,
        type: BRIDGE_MSG.openPath,
        href: "../../outside.html",
        base: BUNDLE_URL,
      },
    ]);

    channel.port1.close();
    channel.port2.close();
  });

  it("leaves non-http schemes native in panel mode", async () => {
    const win = startBridge(BUNDLE_URL, NONCE);
    const channel = await initBridge(win, undefined);
    const open = stubOpen(win);

    const mailto = clickAnchor(win, "mailto:someone@example.com");
    expect(mailto.defaultPrevented).toBe(false);
    expect(open).not.toHaveBeenCalled();

    channel.port1.close();
    channel.port2.close();
  });

  it("visit mode opens cross-origin links in a new tab and keeps _top/_parent same-origin links in the frame", async () => {
    const win = startBridge(BUNDLE_URL, NONCE);
    const channel = await initBridge(win, true);

    const open = stubOpen(win);

    // Visit mode routes cross-origin links out with a plain noopener tab.
    const external = clickAnchor(win, "https://example.com/x");
    expect(open).toHaveBeenCalledWith("https://example.com/x", "_blank", "noopener");
    expect(external.defaultPrevented).toBe(true);

    // Same origin targeting the top window: the frame follows the link itself
    // so the shell is never navigated away. jsdom runs the same-document
    // navigation, making the frame's new URL observable.
    open.mockClear();
    const top = clickAnchor(win, "#section", "_top");
    expect(top.defaultPrevented).toBe(true);
    expect(open).not.toHaveBeenCalled();
    expect(win.location.href).toBe(`${BUNDLE_URL}#section`);

    const parent = clickAnchor(win, "#other", "_parent");
    expect(parent.defaultPrevented).toBe(true);
    expect(win.location.href).toBe(`${BUNDLE_URL}#other`);

    // Same-origin links outside the bundle still leave the sandbox via a tab.
    const outside = clickAnchor(win, "/omni/elsewhere");
    expect(open).toHaveBeenCalledWith(`${ORIGIN}/omni/elsewhere`, "_blank", "noopener");
    expect(outside.defaultPrevented).toBe(true);

    // An in-bundle link without a top target keeps the native frame navigation.
    const sibling = clickAnchor(win, "page2.html");
    expect(sibling.defaultPrevented).toBe(false);

    channel.port1.close();
    channel.port2.close();
  });
});
