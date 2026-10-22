import { sanitizeField } from "../../shared/evidence";
import type { AppView } from "./model";

export interface Rect {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface TargetV1 {
  tag: string;
  selector: string;
  quote: string;
  prefix: string;
  suffix: string;
  aria_label: string;
  role: string;
}

export interface PageV1 {
  url: string;
  title: string;
  include_query_fragment: boolean;
}

/** Wire keys/limits match server/annotation_batch.py; drafts cannot be sent. */
export interface ItemV1 {
  capture_id: string;
  captured_at: string;
  kind: "element" | "region" | "text";
  note: string;
  page: PageV1;
  target: TargetV1;
  geometry: {
    rect: Rect;
    viewport: { width: number; height: number };
    bitmap_scale: { x: number; y: number } | null;
  };
  screenshot?: { file_id: string; sha256: string };
}

export interface Metrics {
  width: number;
  height: number;
  clientWidth: number;
  clientHeight: number;
  scrollX: number;
  scrollY: number;
  dpr: number;
  visual: {
    x: number;
    y: number;
    width: number;
    height: number;
    scale: number;
  };
  revision: number;
  visible: boolean;
  url: string;
}

export interface Selection {
  kind: ItemV1["kind"];
  page: PageV1;
  target: TargetV1;
  rect: Rect;
  metrics: Metrics;
}

export interface Activation {
  tabId: number;
  windowId: number;
  documentId: string;
  generation: string;
  valid: boolean;
}

export interface CapturePreview {
  ownerKey?: string;
  urlConsentKey?: string;
  capture_id: string;
  captured_at: string;
  selection: Selection;
  geometry?: ItemV1["geometry"];
  image?: string;
  screenshotFailure?: string;
}

export interface PanelState {
  message: string;
  preview?: CapturePreview;
  app?: AppView;
}

export function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw new Error("Invalid object");
  return value as Record<string, unknown>;
}

export function exactKeys(value: Record<string, unknown>, keys: string[]): void {
  if (Object.keys(value).length !== keys.length || keys.some((key) => !Object.hasOwn(value, key))) {
    throw new Error("Unexpected evidence fields");
  }
}

function number(value: unknown, min: number, max: number, exclusive = false): number {
  if (
    typeof value !== "number" ||
    !Number.isFinite(value) ||
    value > max ||
    (exclusive ? value <= min : value < min)
  )
    throw new Error("Invalid geometry");
  return value;
}

function field(value: unknown, cap: number): string {
  if (typeof value !== "string" || value.length > cap) throw new Error("Invalid evidence field");
  return sanitizeField(value, cap);
}

export function pageUrl(value: string, includeQueryFragment = false): string {
  const url = new URL(value);
  if (!canPickUrl(url.href)) throw new Error("Cannot pick here. Open an HTTP(S) page.");
  url.username = "";
  url.password = "";
  if (!includeQueryFragment) {
    url.search = "";
    url.hash = "";
  }
  if (url.href.length > 2000) throw new Error("Page URL is too long");
  return url.href;
}

export function canPickUrl(value: string): boolean {
  try {
    const url = new URL(value);
    return (
      ["http:", "https:"].includes(url.protocol) &&
      url.hostname !== "chromewebstore.google.com" &&
      !(url.hostname === "chrome.google.com" && url.pathname.startsWith("/webstore")) &&
      !url.pathname.toLowerCase().endsWith(".pdf")
    );
  } catch {
    return false;
  }
}

export function parseRect(input: unknown): Rect {
  const value = record(input);
  exactKeys(value, ["x", "y", "width", "height"]);
  return {
    x: number(value.x, -100000, 100000),
    y: number(value.y, -100000, 100000),
    width: number(value.width, 0, 100000, true),
    height: number(value.height, 0, 100000, true),
  };
}

export function parseMetrics(input: unknown): Metrics {
  const value = record(input);
  exactKeys(value, [
    "width",
    "height",
    "clientWidth",
    "clientHeight",
    "scrollX",
    "scrollY",
    "dpr",
    "visual",
    "revision",
    "visible",
    "url",
  ]);
  const visual = record(value.visual);
  exactKeys(visual, ["x", "y", "width", "height", "scale"]);
  if (typeof value.visible !== "boolean" || typeof value.url !== "string")
    throw new Error("Invalid metrics");
  return {
    width: number(value.width, 0, 100000, true),
    height: number(value.height, 0, 100000, true),
    clientWidth: number(value.clientWidth, 0, 100000, true),
    clientHeight: number(value.clientHeight, 0, 100000, true),
    scrollX: number(value.scrollX, -100000, 100000),
    scrollY: number(value.scrollY, -100000, 100000),
    dpr: number(value.dpr, 0, 8, true),
    revision: number(value.revision, 0, Number.MAX_SAFE_INTEGER),
    visible: value.visible,
    url: pageUrl(value.url),
    visual: {
      x: number(visual.x, -100000, 100000),
      y: number(visual.y, -100000, 100000),
      width: number(visual.width, 0, 100000, true),
      height: number(visual.height, 0, 100000, true),
      scale: number(visual.scale, 0, 8, true),
    },
  };
}

function parsePage(input: unknown, allowFull = false): PageV1 {
  const page = record(input);
  exactKeys(page, ["url", "title", "include_query_fragment"]);
  if (page.include_query_fragment !== false && !(allowFull && page.include_query_fragment === true))
    throw new Error("Full URL requires extension preview consent");
  return {
    url: pageUrl(field(page.url, 2000), page.include_query_fragment === true),
    title: field(page.title, 200),
    include_query_fragment: page.include_query_fragment === true,
  };
}

function parseTarget(input: unknown): TargetV1 {
  const target = record(input);
  exactKeys(target, ["tag", "selector", "quote", "prefix", "suffix", "aria_label", "role"]);
  return {
    tag: field(target.tag, 32),
    selector: field(target.selector, 700),
    quote: field(target.quote, 200),
    prefix: field(target.prefix, 32),
    suffix: field(target.suffix, 32),
    aria_label: field(target.aria_label, 200),
    role: field(target.role, 80),
  };
}

function parseKind(input: unknown): ItemV1["kind"] {
  if (input !== "element" && input !== "region" && input !== "text")
    throw new Error("Invalid capture kind");
  return input;
}

export function parseSelection(input: unknown): Selection {
  const value = record(input);
  exactKeys(value, ["kind", "page", "target", "rect", "metrics"]);
  const result: Selection = {
    kind: parseKind(value.kind),
    page: parsePage(value.page),
    target: parseTarget(value.target),
    rect: parseRect(value.rect),
    metrics: parseMetrics(value.metrics),
  };
  if (result.page.url !== result.metrics.url) throw new Error("Page changed during selection");
  return result;
}

export function randomId(): string {
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
}

export function serializeItem(item: ItemV1): string {
  const value = record(item);
  exactKeys(value, [
    "capture_id",
    "captured_at",
    "kind",
    "note",
    "page",
    "target",
    "geometry",
    ...(item.screenshot ? ["screenshot"] : []),
  ]);
  if (
    !/^[0-9a-f]{32}$/.test(item.capture_id) ||
    typeof item.captured_at !== "string" ||
    !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(
      item.captured_at,
    ) ||
    !Number.isFinite(Date.parse(item.captured_at)) ||
    typeof item.note !== "string" ||
    !item.note.trim() ||
    item.note.length > 4000
  )
    throw new Error("Invalid annotation item");
  const geometry = record(item.geometry);
  exactKeys(geometry, ["rect", "viewport", "bitmap_scale"]);
  parseRect(geometry.rect);
  for (const [key, cap] of [
    ["viewport", 100000],
    ["bitmap_scale", 8],
  ] as const) {
    if (key === "bitmap_scale" && geometry[key] === null && !item.screenshot) continue;
    const pair = record(geometry[key]);
    const keys = key === "viewport" ? ["width", "height"] : ["x", "y"];
    exactKeys(pair, keys);
    for (const axis of keys) number(pair[axis], 0, cap, true);
  }
  if (item.screenshot) {
    exactKeys(record(item.screenshot), ["file_id", "sha256"]);
    if (
      !/^[0-9a-f]{32}$/.test(item.screenshot.file_id) ||
      !/^[0-9a-f]{64}$/.test(item.screenshot.sha256)
    )
      throw new Error("Invalid screenshot reference");
  }
  return JSON.stringify({
    ...item,
    kind: parseKind(item.kind),
    page: parsePage(item.page, true),
    target: parseTarget(item.target),
  });
}
