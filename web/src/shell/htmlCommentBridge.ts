// Bridge between the host app and the sandboxed HTML-preview iframe so users
// can comment on *rendered* HTML the same way they comment on Markdown/code.
//
// Why a bridge at all:
//   The HTML preview iframe is deliberately sandboxed WITHOUT `allow-same-origin`
//   (see HTML_PREVIEW_SANDBOX in codeViewerHelpers.ts) so untrusted, agent-
//   generated HTML runs in an opaque origin and cannot reach the host app. That
//   same isolation means the parent CANNOT read the iframe's selection or DOM.
//   So we inject a small, app-authored script into the iframe that reads the
//   selection *inside* the frame and relays it over a private MessageChannel,
//   and paints highlights *inside* the frame on command. The sandbox flags are
//   unchanged — postMessage works fine across the opaque-origin boundary.
//
// Trust model:
//   Post-handshake messages travel over a MessagePort that only the parent and
//   the injected script hold, so ordinary page content can't read them. The
//   initial init message (which transfers the port) is delivered to *every*
//   `message` listener in the frame, so in principle artifact JS could grab the
//   port and post spoofed selections. That is a bounded, low-severity nuisance
//   confined to the review UI: it can never reach host-app data (the opaque
//   origin still applies), which is exactly the property the sandbox guarantees.
//   We still gate on a per-mount nonce + a source tag to reject stray messages.
//
// The script bodies themselves are the shared assets web/public/omni-html-bridge.js
// (text-selection comments) and web/public/omni-html-annotate.js (element
// annotation stub): the server inlines the same bytes into panel-view pages,
// and this module imports them ?raw for the srcdoc (embed) path and the unit
// tests.
//
// These helpers are pure (no React) so they unit-test in isolation.

import { escapeHtmlAttr } from "@/lib/html";
import annotateSource from "../../public/omni-html-annotate.js?raw";
import bridgeSource from "../../public/omni-html-bridge.js?raw";
import captureSource from "../../public/omni-html-capture.js?raw";
import { prepareHtmlPreviewDoc, startTagEnd } from "./codeViewerHelpers";

/** Protocol version — bump on any breaking change to the message shapes. */
export const BRIDGE_VERSION = 1;

/** Tag stamped on every message so we ignore unrelated postMessage traffic. */
export const BRIDGE_SOURCE = "omni-html-comment";

/** Message type strings shared by parent and the injected script. */
export const BRIDGE_MSG = {
  /** parent → iframe: hands over the MessagePort (transferred). */
  init: "omni:init",
  /** iframe → parent: port adopted, ready to receive state. */
  ready: "omni:ready",
  /** parent → iframe: full set of comments to highlight. */
  setComments: "omni:setComments",
  /** parent → iframe: the currently-active comment/selection (or null). */
  setActive: "omni:setActive",
  /** iframe → parent: the user made a non-empty text selection. */
  selection: "omni:selection",
  /** iframe → parent: the user clicked inside an existing comment range. */
  commentClick: "omni:commentClick",
  /** iframe → parent: the selection collapsed without hitting a comment. */
  selectionCleared: "omni:selectionCleared",
  /** iframe → parent: a panel-mode link that names a workspace file. */
  openPath: "omni:openPath",
  orphans: "omni:orphans",
} as const;

/** Rect of a selection in the iframe's own viewport coordinates. */
export interface BridgeRect {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

/** A selection event relayed from inside the iframe. */
export interface BridgeSelection {
  type: typeof BRIDGE_MSG.selection;
  /** The selected rendered text, used as the comment anchor_content. */
  text: string;
  /** Which occurrence (0-based, document order) of `text` was selected, so the
   * parent anchors to the copy the user picked rather than the first match. */
  occ: number;
  prefix: string;
  suffix: string;
  selector?: string;
  rect: BridgeRect;
}

export interface BridgeCommentClick {
  type: typeof BRIDGE_MSG.commentClick;
  id: string;
}

export interface BridgeSelectionCleared {
  type: typeof BRIDGE_MSG.selectionCleared;
}

/**
 * A panel-mode link that leaves this artifact for another workspace file.
 * `href` is the anchor's RAW attribute — resolving it would clamp a `..` above
 * the token segment — and `base` the frame's `document.baseURI`, so the parent
 * resolves against the frame's own page rather than a possibly stale one.
 */
export interface BridgeOpenPath {
  type: typeof BRIDGE_MSG.openPath;
  href: string;
  base: string;
}

export interface BridgeReady {
  type: typeof BRIDGE_MSG.ready;
  /** The frame's `location.pathname`, so the parent can derive which bundle
   * file is displayed; absent on frames older than the artifact-relay asset. */
  pathname?: string;
}

export interface BridgeOrphans {
  type: typeof BRIDGE_MSG.orphans;
  ids: string[];
}

/** Any message the iframe can send to the parent (post-handshake). */
export type InboundBridgeMessage =
  | BridgeReady
  | BridgeSelection
  | BridgeCommentClick
  | BridgeSelectionCleared
  | BridgeOpenPath
  | BridgeOrphans;

// ---------------------------------------------------------------------------
// Inbound message validation
// ---------------------------------------------------------------------------

/** Shared href/base cap, so a hostile frame can't ship the parent a novel. */
const MAX_OPEN_PATH_CHARS = 4096;

function isRect(r: unknown): r is BridgeRect {
  if (typeof r !== "object" || r === null) return false;
  const o = r as Record<string, unknown>;
  return (
    typeof o.left === "number" &&
    Number.isFinite(o.left) &&
    typeof o.top === "number" &&
    Number.isFinite(o.top) &&
    typeof o.right === "number" &&
    Number.isFinite(o.right) &&
    typeof o.bottom === "number" &&
    Number.isFinite(o.bottom)
  );
}

/**
 * Validate and narrow a raw message received from the iframe. Returns the typed
 * message on success, or `null` for anything that isn't a well-formed bridge
 * message carrying the expected `nonce` — guarding against arbitrary
 * postMessage traffic (including spoofs from artifact JS).
 *
 * @param data  The raw `MessageEvent.data`.
 * @param nonce The per-mount nonce the iframe was initialised with.
 */
export function parseBridgeMessage(data: unknown, nonce: string): InboundBridgeMessage | null {
  if (typeof data !== "object" || data === null) return null;
  const d = data as Record<string, unknown>;
  if (d.source !== BRIDGE_SOURCE || d.nonce !== nonce) return null;
  switch (d.type) {
    case BRIDGE_MSG.ready:
      return typeof d.pathname === "string"
        ? { type: BRIDGE_MSG.ready, pathname: d.pathname }
        : { type: BRIDGE_MSG.ready };
    case BRIDGE_MSG.selection:
      if (
        typeof d.text === "string" &&
        !!d.text.trim() &&
        d.text.length <= 16000 &&
        Number.isSafeInteger(d.occ) &&
        (d.occ as number) >= 0 &&
        typeof d.prefix === "string" &&
        d.prefix.length <= 32 &&
        typeof d.suffix === "string" &&
        d.suffix.length <= 32 &&
        (d.selector === undefined ||
          (typeof d.selector === "string" && d.selector.length > 0 && d.selector.length <= 700)) &&
        isRect(d.rect)
      ) {
        return {
          type: BRIDGE_MSG.selection,
          text: d.text,
          occ: d.occ as number,
          prefix: d.prefix,
          suffix: d.suffix,
          ...(d.selector ? { selector: d.selector } : {}),
          rect: d.rect,
        };
      }
      return null;
    case BRIDGE_MSG.commentClick:
      if (typeof d.id === "string" && d.id !== "") {
        return { type: BRIDGE_MSG.commentClick, id: d.id };
      }
      return null;
    case BRIDGE_MSG.selectionCleared:
      return { type: BRIDGE_MSG.selectionCleared };
    case BRIDGE_MSG.openPath:
      if (
        typeof d.href === "string" &&
        typeof d.base === "string" &&
        d.href.length <= MAX_OPEN_PATH_CHARS &&
        d.base.length <= MAX_OPEN_PATH_CHARS
      ) {
        return { type: BRIDGE_MSG.openPath, href: d.href, base: d.base };
      }
      return null;
    case BRIDGE_MSG.orphans:
      if (
        Array.isArray(d.ids) &&
        d.ids.length <= 1000 &&
        d.ids.every((id) => typeof id === "string" && id.length > 0 && id.length <= 100)
      ) {
        return { type: BRIDGE_MSG.orphans, ids: d.ids };
      }
      return null;
    default:
      return null;
  }
}

// ---------------------------------------------------------------------------
// Historical source-offset occurrence recovery
// ---------------------------------------------------------------------------

function escapeRegExp(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

// Whitespace run matching the in-frame `normWs` (which treats every code point
// <= U+0020 as whitespace). Using `\s` here would additionally fold U+00A0 and
// other Unicode spaces, so the parent's occurrence count could diverge from the
// bridge's for text containing non-breaking spaces.
const WS_RUN = "[\\u0000-\\u0020]+";
const WS_SPLIT = new RegExp(WS_RUN);

/** Whitespace-tolerant regex source for `anchor` (already trimmed). */
function anchorPattern(trimmed: string): string {
  return trimmed.split(WS_SPLIT).map(escapeRegExp).join(WS_RUN);
}

/**
 * Half-open [start, end) ranges of `source` that are NOT rendered as visible
 * text: tag markup (so attribute values are excluded), HTML comments, and the
 * contents of `<script>`/`<style>`/`<title>`/`<noscript>`. Occurrence counting
 * skips these so the source's Nth match lines up with the Nth *rendered* match
 * the in-frame bridge counts (which walks only body text nodes).
 */
function nonRenderedRanges(source: string): [number, number][] {
  const ranges: [number, number][] = [];
  const collect = (re: RegExp) => {
    for (const m of source.matchAll(re)) {
      if (m.index !== undefined) ranges.push([m.index, m.index + m[0].length]);
    }
  };
  collect(/<!--[\s\S]*?-->/g);
  collect(/<(script|style|title|noscript)\b[\s\S]*?<\/\1\s*>/gi);
  collect(/<[^>]*>/g);
  ranges.sort((a, b) => a[0] - b[0]);
  return ranges;
}

/** Whether `index` falls inside any (sorted) non-rendered range. */
function inNonRendered(index: number, ranges: [number, number][]): boolean {
  for (const [start, end] of ranges) {
    if (index < start) break;
    if (index < end) return true;
  }
  return false;
}

/**
 * Which occurrence of `anchor` (0-based, document order) the comment at
 * `startIndex` refers to. Anchor text can repeat — e.g. a title and a body
 * paragraph both containing "Aurora Sync" — and the bridge highlights by text
 * match, so without this it would light up every copy. Counting the matches
 * before `startIndex` disambiguates to the one the user actually selected.
 *
 * Matches non-rendered source regions are skipped so the count aligns with the
 * in-frame bridge (which sees only rendered text). Returns 0 when the anchor is
 * empty or the pattern is pathological (the bridge then falls back to all copies).
 */
export function anchorOccurrence(source: string, anchor: string, startIndex: number): number {
  const trimmed = anchor.trim();
  if (!trimmed) return 0;
  let re: RegExp;
  try {
    re = new RegExp(anchorPattern(trimmed), "g");
  } catch {
    return 0;
  }
  const skip = nonRenderedRanges(source);
  let count = 0;
  for (const m of source.matchAll(re)) {
    if (m.index === undefined || m.index >= startIndex) break;
    if (inNonRendered(m.index, skip)) continue;
    count += 1;
  }
  return count;
}

// ---------------------------------------------------------------------------
// Injected bridge script
// ---------------------------------------------------------------------------

// The shared in-frame asset: inlined for the srcdoc path, loaded by URL when an
// embed's CSP blocks inline scripts.
export const HTML_COMMENT_BRIDGE_RUNTIME = bridgeSource;

/**
 * Wrap a shared in-frame asset in the nonce-carrying `<script>` tag the server
 * emits: the nonce reaches the script as `data-omni-nonce`, and without it the
 * script no-ops. The source is escaped so a literal `</script` in it can never
 * close the tag early.
 */
function buildAssetScript(source: string, nonce: string): string {
  const nonceAttr = ` data-omni-nonce="${escapeHtmlAttr(nonce)}"`;
  return `<script${nonceAttr}>` + source.replace(/<\/script/gi, "<\\/script") + "</script>";
}

/**
 * Wrap the shared in-frame bridge asset (web/public/omni-html-bridge.js). When
 * `runtimeUrl` is given the asset loads externally from that URL instead, for an
 * embed whose CSP blocks inline scripts.
 *
 * Exported for unit testing.
 */
export function buildBridgeScript(nonce: string, runtimeUrl?: string): string {
  if (!runtimeUrl) return buildAssetScript(bridgeSource, nonce);
  const nonceAttr = ` data-omni-nonce="${escapeHtmlAttr(nonce)}"`;
  return `<script src="${escapeHtmlAttr(runtimeUrl)}"${nonceAttr}></script>`;
}

/**
 * Wrap the shared in-frame annotate stub (web/public/omni-html-annotate.js),
 * which the srcdoc path places right after the bridge script.
 *
 * Exported for unit testing.
 */
export function buildAnnotateScript(nonce: string): string {
  return buildAssetScript(annotateSource, nonce);
}

/**
 * Wrap the shared early-capture asset (web/public/omni-html-capture.js), which
 * the srcdoc path places at the top of `<head>`.
 *
 * Exported for unit testing.
 */
export function buildCaptureScript(nonce: string): string {
  return buildAssetScript(captureSource, nonce);
}

/**
 * Prepare HTML artifact content for the comment-enabled preview iframe: first
 * run {@link prepareHtmlPreviewDoc} (so links still open in a new tab), then
 * inject the capture script at the top of `<head>` (so it runs before the
 * preview head markup and any page script) and the bridge + annotate stub
 * before `</body>`.
 *
 * Placement mirrors prepareHtmlPreviewDoc's deliberately-simple approach (NOT a
 * full HTML parse, which could subtly change how the artifact renders): the
 * body scripts go before `</body>` when present, else before `</html>`, else
 * append.
 *
 * @param html  Raw artifact HTML.
 * @param nonce Per-mount nonce shared with the parent for message validation.
 * @param runtimeUrl External runtime asset for embeds whose CSP blocks inline
 *   scripts. The annotation runtime is evaluated inline, so that mode carries
 *   the bridge only.
 */
export function injectCommentBridge(html: string, nonce: string, runtimeUrl?: string): string {
  const prepared = prepareHtmlPreviewDoc(html);
  if (runtimeUrl) return injectBeforeClose(prepared, buildBridgeScript(nonce, runtimeUrl));

  // prepareHtmlPreviewDoc leaves a real <head> unless the input is a bare
  // fragment, whose implicit head starts at offset 0.
  const headAt = Math.max(startTagEnd(prepared, "head"), 0);
  const withCapture =
    prepared.slice(0, headAt) + buildCaptureScript(nonce) + prepared.slice(headAt);
  return injectBeforeClose(withCapture, buildBridgeScript(nonce) + buildAnnotateScript(nonce));
}

function injectBeforeClose(doc: string, inject: string): string {
  const bodyClose = doc.search(/<\/body\s*>/i);
  if (bodyClose !== -1) return doc.slice(0, bodyClose) + inject + doc.slice(bodyClose);
  const htmlClose = doc.search(/<\/html\s*>/i);
  if (htmlClose !== -1) return doc.slice(0, htmlClose) + inject + doc.slice(htmlClose);
  return doc + inject;
}
