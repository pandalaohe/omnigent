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
    });
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
