// Element anchor codec for the HTML-preview annotation channel.
//
// The frame is opaque-origin, so its `annotate:picked` payload is untrusted:
// every field is coerced/clamped here before the parent stores or sends it.
// The caps mirror the server's parse_element_anchor contract (design §2.6);
// change them in lockstep with omnigent/entities/element_annotation.py.

export const ELEMENT_ANCHOR_PREFIX = "__element__";

/** An encoded anchor above this many UTF-8 bytes is rejected, not stored. */
const MAX_ANCHOR_BYTES = 32 * 1024;

/** Geometry and page-position magnitudes are clamped to this abs bound. */
const MAX_GEOMETRY = 1_000_000;

const MAX_CONSOLE_ENTRIES = 50;
const MAX_NETWORK_ENTRIES = 20;
const SCREENSHOT_FILE_ID = /^[A-Za-z0-9_-]{1,64}$/;

export interface ElementAnchorScreenshot {
  file_id: string;
  filename: string;
  width: number;
  height: number;
}

interface ElementAnchorPage {
  url: string;
  title: string;
  vw: number;
  vh: number;
  sx: number;
  sy: number;
  dpr: number;
}

interface ElementAnchorRect {
  x: number;
  y: number;
  w: number;
  h: number;
}

interface ElementAnchorQuote {
  exact: string;
  prefix: string;
  suffix: string;
}

interface ElementAnchorTarget {
  label: string;
  css: string;
  xpath: string;
  quote: ElementAnchorQuote;
  fingerprint: string;
  neighborText: string;
  tag: string;
  id: string;
  role: string;
  ariaLabel: string;
  text: string;
}

interface ElementAnchorConsoleEntry {
  level: "error" | "warn";
  message: string;
  ts: number;
}

interface ElementAnchorNetworkEntry {
  method: string;
  url: string;
  status: number;
  ts: number;
}

/** The v1 element-anchor payload; one line per field in design §2.6. */
export interface ElementAnchorV1 {
  v: 1;
  kind: "element" | "region";
  page: ElementAnchorPage;
  target: ElementAnchorTarget;
  rect: ElementAnchorRect;
  region: ElementAnchorRect | null;
  selectedText: string;
  console: ElementAnchorConsoleEntry[];
  network: ElementAnchorNetworkEntry[];
  screenshot: ElementAnchorScreenshot | null;
}

export function isElementAnchor(anchor: string | null | undefined): boolean {
  return !!anchor?.startsWith(ELEMENT_ANCHOR_PREFIX);
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null ? (value as Record<string, unknown>) : null;
}

/** U+FFFD for every lone surrogate, so the result is well-formed UTF-16. */
function replaceLoneSurrogates(value: string): string {
  return value.replace(/[\uD800-\uDBFF][\uDC00-\uDFFF]|[\uD800-\uDFFF]/g, (match) =>
    match.length === 2 ? match : "\uFFFD",
  );
}

/** Cut at `cap`; never leave a high surrogate dangling before the ellipsis. */
function capString(value: string, cap: number): string {
  if (value.length <= cap) return value;
  let cut = value.slice(0, cap - 1);
  const last = cut.charCodeAt(cut.length - 1);
  if (last >= 0xd800 && last <= 0xdbff) cut = cut.slice(0, -1);
  return cut + "…";
}

/**
 * One-line, sanitized, capped string with `…` on truncation. Non-strings
 * become `""` so a hostile frame cannot smuggle an object into a string slot.
 */
function clampString(value: unknown, cap: number): string {
  if (typeof value !== "string") return "";
  const oneLine = value.replace(/\s+/g, " ").trim();
  return capString(replaceLoneSurrogates(oneLine), cap);
}

function clampNumber(value: unknown, min: number, max: number, fallback = 0): number {
  if (typeof value !== "number" || !Number.isFinite(value)) return fallback;
  return Math.min(max, Math.max(min, value));
}

/** Integer ms; non-finite input collapses to 0. Epoch values exceed MAX_GEOMETRY. */
function clampTimestamp(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? Math.round(value) : 0;
}

function clampInteger(value: unknown, min: number, max: number, fallback = 0): number {
  if (typeof value !== "number" || !Number.isFinite(value)) return fallback;
  return Math.min(max, Math.max(min, Math.round(value)));
}

/** Strict bounded integer: a non-integer or out-of-range value is rejected. */
function boundedInteger(value: unknown, min: number, max: number): number | null {
  if (typeof value !== "number" || !Number.isInteger(value)) return null;
  return value >= min && value <= max ? value : null;
}

/** Keep only http(s) scheme + host + path; drop query, hash and credentials. */
function clampUrl(value: unknown, cap: number): string {
  if (typeof value !== "string" || value === "") return "";
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    return "";
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") return "";
  return clampString(`${url.protocol}//${url.host}${url.pathname}`, cap);
}

/** Strict: all four sides must be finite numbers or the whole anchor is invalid. */
function clampRect(value: unknown): ElementAnchorRect | null {
  const o = asRecord(value);
  if (!o) return null;
  const { x, y, w, h } = o;
  if (
    typeof x !== "number" ||
    !Number.isFinite(x) ||
    typeof y !== "number" ||
    !Number.isFinite(y) ||
    typeof w !== "number" ||
    !Number.isFinite(w) ||
    typeof h !== "number" ||
    !Number.isFinite(h)
  ) {
    return null;
  }
  return {
    x: clampNumber(x, -MAX_GEOMETRY, MAX_GEOMETRY),
    y: clampNumber(y, -MAX_GEOMETRY, MAX_GEOMETRY),
    w: clampNumber(w, -MAX_GEOMETRY, MAX_GEOMETRY),
    h: clampNumber(h, -MAX_GEOMETRY, MAX_GEOMETRY),
  };
}

function clampPage(value: unknown): ElementAnchorPage {
  const o = asRecord(value) ?? {};
  return {
    url: clampUrl(o.url, 2000),
    title: clampString(o.title, 200),
    vw: clampNumber(o.vw, -MAX_GEOMETRY, MAX_GEOMETRY),
    vh: clampNumber(o.vh, -MAX_GEOMETRY, MAX_GEOMETRY),
    sx: clampNumber(o.sx, -MAX_GEOMETRY, MAX_GEOMETRY),
    sy: clampNumber(o.sy, -MAX_GEOMETRY, MAX_GEOMETRY),
    dpr: clampNumber(o.dpr, -MAX_GEOMETRY, MAX_GEOMETRY),
  };
}

function clampTarget(value: unknown): ElementAnchorTarget {
  const o = asRecord(value) ?? {};
  const quote = asRecord(o.quote) ?? {};
  return {
    label: clampString(o.label, 200),
    css: clampString(o.css, 700),
    xpath: clampString(o.xpath, 900),
    quote: {
      exact: clampString(quote.exact, 200),
      prefix: clampString(quote.prefix, 32),
      suffix: clampString(quote.suffix, 32),
    },
    fingerprint: clampString(o.fingerprint, 120),
    neighborText: clampString(o.neighborText, 80),
    tag: clampString(o.tag, 32),
    id: clampString(o.id, 191),
    role: clampString(o.role, 64),
    ariaLabel: clampString(o.ariaLabel, 200),
    text: clampString(o.text, 200),
  };
}

/** Keep the newest entries: a ring can exceed the cap between flushes. */
function clampConsole(value: unknown): ElementAnchorConsoleEntry[] {
  if (!Array.isArray(value)) return [];
  const entries: ElementAnchorConsoleEntry[] = [];
  for (const raw of value) {
    const o = asRecord(raw);
    if (!o) continue;
    if (o.level !== "error" && o.level !== "warn") continue;
    entries.push({
      level: o.level,
      message: clampString(o.message, 500),
      ts: clampTimestamp(o.ts),
    });
  }
  return entries.slice(-MAX_CONSOLE_ENTRIES);
}

function clampNetwork(value: unknown): ElementAnchorNetworkEntry[] {
  if (!Array.isArray(value)) return [];
  const entries: ElementAnchorNetworkEntry[] = [];
  for (const raw of value) {
    const o = asRecord(raw);
    if (!o) continue;
    entries.push({
      method: clampString(o.method, 20),
      url: clampUrl(o.url, 2000),
      status: clampInteger(o.status, 0, 599),
      ts: clampTimestamp(o.ts),
    });
  }
  return entries.slice(-MAX_NETWORK_ENTRIES);
}

function clampScreenshot(value: unknown): ElementAnchorScreenshot | null {
  const o = asRecord(value);
  if (!o) return null;
  const fileId = typeof o.file_id === "string" ? o.file_id : "";
  if (!SCREENSHOT_FILE_ID.test(fileId)) return null;
  const width = boundedInteger(o.width, 1, 4096);
  const height = boundedInteger(o.height, 1, 4096);
  if (width === null || height === null) return null;
  return { file_id: fileId, filename: clampString(o.filename, 128), width, height };
}

/** Encoded size in UTF-8 bytes, the unit the server's 32 KiB cap uses. */
function anchorByteLength(anchor: ElementAnchorV1): number {
  return new TextEncoder().encode(ELEMENT_ANCHOR_PREFIX + JSON.stringify(anchor)).length;
}

/**
 * Validate and clamp an untrusted anchor's fields, with no size handling.
 * Returns a fresh object with only the known v1 fields, or `null` when
 * `v`/`kind`/`rect` are missing or malformed.
 */
function clampAnchorFields(raw: unknown): ElementAnchorV1 | null {
  const o = asRecord(raw);
  if (!o) return null;
  if (o.v !== 1) return null;
  const kind = o.kind;
  if (kind !== "element" && kind !== "region") return null;
  const rect = clampRect(o.rect);
  if (!rect) return null;
  return {
    v: 1,
    kind,
    page: clampPage(o.page),
    target: clampTarget(o.target),
    rect,
    region: kind === "region" ? clampRect(o.region) : null,
    selectedText: kind === "region" ? clampString(o.selectedText, 500) : "",
    console: clampConsole(o.console),
    network: clampNetwork(o.network),
    screenshot: clampScreenshot(o.screenshot),
  };
}

/**
 * Clamp an untrusted frame `picked` payload before the parent stores it.
 * Over-cap diagnostics are dropped oldest-first; an anchor that still
 * exceeds 32 KiB is rejected.
 */
export function clampElementAnchor(raw: unknown): ElementAnchorV1 | null {
  const anchor = clampAnchorFields(raw);
  if (!anchor) return null;
  // Losing a log entry beats losing the annotation: a page that logged a lot
  // must still save. Network goes first, then console, oldest first.
  while (anchorByteLength(anchor) > MAX_ANCHOR_BYTES) {
    const dropped = anchor.network.length > 0 ? anchor.network.shift() : anchor.console.shift();
    if (dropped === undefined) return null;
  }
  return anchor;
}

export function encodeElementAnchor(anchor: ElementAnchorV1): string {
  const encoded = ELEMENT_ANCHOR_PREFIX + JSON.stringify(anchor);
  if (new TextEncoder().encode(encoded).length > MAX_ANCHOR_BYTES) {
    throw new Error("element anchor too large");
  }
  return encoded;
}

/**
 * Decode a stored anchor. Unlike the producer-side clamp, an oversized
 * anchor is rejected rather than trimmed, matching the server's
 * `parse_element_anchor` accept/reject decision (design §2.6). The size is
 * that of the stored payload without the prefix — the server's canonical
 * JSON — not a re-encode, which would add fields Python omits.
 */
export function decodeElementAnchor(anchor: string | null | undefined): ElementAnchorV1 | null {
  if (!isElementAnchor(anchor)) return null;
  const payload = anchor!.slice(ELEMENT_ANCHOR_PREFIX.length);
  if (new TextEncoder().encode(payload).length > MAX_ANCHOR_BYTES) return null;
  try {
    return clampAnchorFields(JSON.parse(payload));
  } catch {
    return null;
  }
}

/**
 * Synthetic range from the anchor's top-left corner (design §2.6), so element
 * rows sort top-to-bottom without a real source offset. 999,999,999 < 2³¹−1.
 */
export function elementAnchorRange(anchor: ElementAnchorV1): {
  start_index: number;
  end_index: number;
} {
  const y = Math.min(99_999, Math.max(0, Math.round(anchor.rect.y)));
  const x = Math.min(9_999, Math.max(0, Math.round(anchor.rect.x)));
  const index = y * 10_000 + x;
  return { start_index: index, end_index: index };
}

/** One-line display label for the comments panel and agent messages. */
export function elementAnchorLabel(anchor: ElementAnchorV1): string {
  return anchor.target.label || "Element annotation";
}
