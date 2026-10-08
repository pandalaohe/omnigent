// Parent-side parser for the HTML-preview annotation channel (design §2.5).
//
// The frame is opaque-origin and page JS can spoof postMessage traffic, so the
// hook only consumes messages that pass this parser: matching source + nonce, a
// known frame→parent type, and every field clamped to its cap. The anchor
// payload goes through the shared clampElementAnchor validator.
//
// Pure helpers (no React) so they unit-test in isolation.

import { clampElementAnchor, type ElementAnchorV1 } from "./annotationAnchor";

/** Tag stamped on every annotate-channel message so stray traffic is ignored. */
export const ANNOTATE_SOURCE = "omni-html-annotate";

/** Message type strings shared by the parent and the injected annotate scripts. */
export const ANNOTATE_MSG = {
  /** parent → iframe: hands over the annotate MessagePort (transferred). */
  init: "annotate:init",
  /** iframe → parent: port adopted. */
  ready: "annotate:ready",
  /** parent → iframe: ship one runtime part's sources. */
  loadRuntime: "annotate:loadRuntime",
  /** iframe → parent: a runtime part finished evaluating. */
  runtimeLoaded: "annotate:runtimeLoaded",
  /** parent → iframe: the current resolved shortcut chords. */
  setShortcut: "annotate:setShortcut",
  /** parent → iframe: annotation mode on/off. */
  setMode: "annotate:setMode",
  /** iframe → parent: mode changed inside the frame. */
  modeChanged: "annotate:modeChanged",
  /** iframe → parent: shortcut pressed before the runtime existed. */
  toggleRequested: "annotate:toggleRequested",
  /** parent → iframe: this page's annotations to re-resolve and mark. */
  setAnnotations: "annotate:setAnnotations",
  /** iframe → parent: per-annotation resolve result. */
  resolved: "annotate:resolved",
  /** iframe → parent: an element or region was picked; the note stays owner UI. */
  picked: "annotate:picked",
  /** parent → iframe: the pick is closed (saved, cancelled or mode off). */
  pickDone: "annotate:pickDone",
  /** iframe → parent: a marker was clicked. */
  markerClick: "annotate:markerClick",
} as const;

export interface AnnotateReady {
  type: typeof ANNOTATE_MSG.ready;
  /** The frame's `location.pathname`; absent on older stubs. */
  pathname?: string;
}

export interface AnnotateRuntimeLoaded {
  type: typeof ANNOTATE_MSG.runtimeLoaded;
  part: "core" | "pick";
  ok: boolean;
}

export interface AnnotateModeChanged {
  type: typeof ANNOTATE_MSG.modeChanged;
  on: boolean;
  reason: "user" | "escape" | "shortcut";
}

export interface AnnotateToggleRequested {
  type: typeof ANNOTATE_MSG.toggleRequested;
}

export interface AnnotateResolvedItem {
  id: string;
  found: boolean;
}

export interface AnnotateResolved {
  type: typeof ANNOTATE_MSG.resolved;
  items: AnnotateResolvedItem[];
}

export interface AnnotatePickedScreenshot {
  dataUrl: string;
  width: number;
  height: number;
}

/** The picked rect (or region box) in the frame's viewport CSS px at pick time. */
export interface AnnotateViewportRect {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface AnnotatePicked {
  type: typeof ANNOTATE_MSG.picked;
  anchor: ElementAnchorV1;
  /** `null` when the frame's capture failed or was rejected by the caps. */
  screenshot: AnnotatePickedScreenshot | null;
  viewportRect: AnnotateViewportRect;
}

export interface AnnotateMarkerClick {
  type: typeof ANNOTATE_MSG.markerClick;
  id: string;
}

/** Any message the frame can send to the parent (post-handshake). */
export type InboundAnnotateMessage =
  | AnnotateReady
  | AnnotateRuntimeLoaded
  | AnnotateModeChanged
  | AnnotateToggleRequested
  | AnnotateResolved
  | AnnotatePicked
  | AnnotateMarkerClick;

const MAX_RESOLVED_ITEMS = 200;
const MAX_ID_CHARS = 64;
const MAX_SCREENSHOT_BYTES = 2 * 1024 * 1024;
const MAX_SCREENSHOT_DIM = 4096;
const MAX_GEOMETRY = 1e6;

const JPEG_DATA_URL = "data:image/jpeg;base64,";
const PNG_DATA_URL = "data:image/png;base64,";

function isScreenshotDim(value: unknown): value is number {
  return (
    typeof value === "number" &&
    Number.isInteger(value) &&
    value >= 1 &&
    value <= MAX_SCREENSHOT_DIM
  );
}

/** A bad screenshot drops to `null`; the annotation itself survives (T10). */
function parsePickedScreenshot(raw: unknown): AnnotatePickedScreenshot | null {
  if (typeof raw !== "object" || raw === null) return null;
  const { dataUrl, width, height } = raw as Record<string, unknown>;
  if (typeof dataUrl !== "string") return null;
  if (!dataUrl.startsWith(JPEG_DATA_URL) && !dataUrl.startsWith(PNG_DATA_URL)) return null;
  // Estimate the decoded payload from the base64 length; no image decode here.
  const base64 = dataUrl.slice(dataUrl.indexOf(",") + 1);
  if (Math.floor((base64.length * 3) / 4) > MAX_SCREENSHOT_BYTES) return null;
  if (!isScreenshotDim(width) || !isScreenshotDim(height)) return null;
  return { dataUrl, width, height };
}

/** Four finite numbers within the geometry cap, or null (the pick is dropped). */
function parseViewportRect(raw: unknown): AnnotateViewportRect | null {
  if (typeof raw !== "object" || raw === null) return null;
  const { x, y, w, h } = raw as Record<string, unknown>;
  for (const value of [x, y, w, h]) {
    if (typeof value !== "number" || !Number.isFinite(value) || Math.abs(value) > MAX_GEOMETRY) {
      return null;
    }
  }
  return { x: x as number, y: y as number, w: w as number, h: h as number };
}

/**
 * Validate and narrow a raw annotate-channel message. Returns the typed message
 * on success, or `null` for anything that isn't a well-formed frame→parent
 * message carrying the expected `nonce` — guarding against arbitrary
 * postMessage traffic (including spoofs from artifact JS).
 *
 * @param data  The raw `MessageEvent.data`.
 * @param nonce The per-mount nonce the frame was initialised with.
 */
export function parseAnnotateMessage(data: unknown, nonce: string): InboundAnnotateMessage | null {
  if (typeof data !== "object" || data === null) return null;
  const d = data as Record<string, unknown>;
  if (d.source !== ANNOTATE_SOURCE || d.nonce !== nonce) return null;
  switch (d.type) {
    case ANNOTATE_MSG.ready:
      return typeof d.pathname === "string"
        ? { type: ANNOTATE_MSG.ready, pathname: d.pathname }
        : { type: ANNOTATE_MSG.ready };
    case ANNOTATE_MSG.runtimeLoaded:
      if ((d.part === "core" || d.part === "pick") && typeof d.ok === "boolean") {
        return { type: ANNOTATE_MSG.runtimeLoaded, part: d.part, ok: d.ok };
      }
      return null;
    case ANNOTATE_MSG.modeChanged:
      if (
        typeof d.on === "boolean" &&
        (d.reason === "user" || d.reason === "escape" || d.reason === "shortcut")
      ) {
        return { type: ANNOTATE_MSG.modeChanged, on: d.on, reason: d.reason };
      }
      return null;
    case ANNOTATE_MSG.toggleRequested:
      return { type: ANNOTATE_MSG.toggleRequested };
    case ANNOTATE_MSG.resolved: {
      if (!Array.isArray(d.items)) return null;
      const items: AnnotateResolvedItem[] = [];
      for (const raw of d.items) {
        if (items.length >= MAX_RESOLVED_ITEMS) break;
        if (typeof raw !== "object" || raw === null) continue;
        const o = raw as Record<string, unknown>;
        if (typeof o.id !== "string" || o.id === "") continue;
        items.push({ id: o.id.slice(0, MAX_ID_CHARS), found: o.found === true });
      }
      return { type: ANNOTATE_MSG.resolved, items };
    }
    case ANNOTATE_MSG.picked: {
      const anchor = clampElementAnchor(d.anchor);
      if (!anchor) return null;
      const viewportRect = parseViewportRect(d.viewportRect);
      if (!viewportRect) return null;
      return {
        type: ANNOTATE_MSG.picked,
        anchor,
        screenshot: parsePickedScreenshot(d.screenshot),
        viewportRect,
      };
    }
    case ANNOTATE_MSG.markerClick:
      if (typeof d.id === "string" && d.id !== "") {
        return { type: ANNOTATE_MSG.markerClick, id: d.id.slice(0, MAX_ID_CHARS) };
      }
      return null;
    default:
      return null;
  }
}
