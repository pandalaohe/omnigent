export const RENDERED_TEXT_PREFIX = "__text__";

export interface RenderedTextAnchor {
  v: 1;
  kind: "html" | "markdown";
  text: string;
  occ: number;
  prefix: string;
  suffix: string;
  selector?: string;
  source?: { start_index: number; end_index: number; contentHash?: string };
}

const utf8 = new TextEncoder();
const index = (n: unknown): n is number => Number.isSafeInteger(n) && (n as number) >= 0;
const high = (code: number) => code >= 0xd800 && code <= 0xdbff;
const low = (code: number) => code >= 0xdc00 && code <= 0xdfff;

function validScalars(value: string): boolean {
  for (let i = 0; i < value.length; i++) {
    const code = value.charCodeAt(i);
    if (high(code)) {
      if (i + 1 >= value.length || !low(value.charCodeAt(++i))) return false;
    } else if (low(code)) return false;
  }
  return true;
}

function contextBefore(value: string, end: number): string {
  let start = Math.max(0, end - 32);
  if (start > 0 && low(value.charCodeAt(start)) && high(value.charCodeAt(start - 1))) start++;
  return value.slice(start, end);
}

function contextAfter(value: string, start: number): string {
  let end = Math.min(value.length, start + 32);
  if (end < value.length && high(value.charCodeAt(end - 1)) && low(value.charCodeAt(end))) end--;
  return value.slice(start, end);
}

export function isRenderedTextAnchor(value: string | null | undefined): boolean {
  return !!value?.startsWith(RENDERED_TEXT_PREFIX);
}

export function decodeRenderedTextAnchor(
  value: string | null | undefined,
): RenderedTextAnchor | null {
  if (!isRenderedTextAnchor(value) || utf8.encode(value ?? "").length > 32768) return null;
  try {
    const a = JSON.parse(value!.slice(RENDERED_TEXT_PREFIX.length)) as Record<string, unknown>;
    if (
      !a ||
      typeof a !== "object" ||
      a.v !== 1 ||
      (a.kind !== "html" && a.kind !== "markdown") ||
      typeof a.text !== "string" ||
      !a.text.trim() ||
      a.text.length > 16000 ||
      !validScalars(a.text) ||
      !index(a.occ) ||
      typeof a.prefix !== "string" ||
      a.prefix.length > 32 ||
      !validScalars(a.prefix) ||
      typeof a.suffix !== "string" ||
      a.suffix.length > 32 ||
      !validScalars(a.suffix) ||
      (a.selector !== undefined &&
        (typeof a.selector !== "string" || a.selector.length > 700 || !validScalars(a.selector))) ||
      (a.source !== undefined &&
        (!a.source ||
          typeof a.source !== "object" ||
          !index((a.source as Record<string, unknown>).start_index) ||
          !index((a.source as Record<string, unknown>).end_index) ||
          ((a.source as Record<string, unknown>).contentHash !== undefined &&
            (typeof (a.source as Record<string, unknown>).contentHash !== "string" ||
              !/^[\da-f]+:[\da-f]{16}$/.test((a.source as { contentHash: string }).contentHash))) ||
          (a.source as { start_index: number; end_index: number }).end_index <=
            (a.source as { start_index: number; end_index: number }).start_index))
    )
      return null;
    return a as unknown as RenderedTextAnchor;
  } catch {
    return null;
  }
}

export function encodeRenderedTextAnchor(anchor: RenderedTextAnchor): string | null {
  const value = RENDERED_TEXT_PREFIX + JSON.stringify(anchor);
  return decodeRenderedTextAnchor(value) ? value : null;
}

/** Locate a saved quote only when its surrounding rendered text still identifies one copy. */
export function resolveRenderedTextAnchor(
  content: string,
  anchor: RenderedTextAnchor,
): { start: number; end: number } | null {
  const matches: { start: number; end: number }[] = [];
  let from = 0;
  while (from <= content.length) {
    const start = content.indexOf(anchor.text, from);
    if (start < 0) break;
    const end = start + anchor.text.length;
    if (
      content.slice(Math.max(0, start - anchor.prefix.length), start) === anchor.prefix &&
      content.slice(end, end + anchor.suffix.length) === anchor.suffix
    ) {
      matches.push({ start, end });
      if (matches.length > 1) return null;
    }
    from = start + 1;
  }
  return matches[0] ?? null;
}

export function captureRenderedTextAnchor(
  kind: RenderedTextAnchor["kind"],
  content: string,
  start: number,
  end: number,
  selector?: string,
): RenderedTextAnchor | null {
  if (!index(start) || !index(end) || end <= start || end > content.length) return null;
  const text = content.slice(start, end);
  if (!text.trim() || text.length > 16000) return null;
  let occ = 0;
  let from = 0;
  while (true) {
    const next = content.indexOf(text, from);
    if (next < 0 || next >= start) break;
    occ++;
    from = next + 1;
  }
  const anchor: RenderedTextAnchor = {
    v: 1,
    kind,
    text,
    occ,
    prefix: contextBefore(content, start),
    suffix: contextAfter(content, end),
    ...(selector ? { selector } : {}),
  };
  return encodeRenderedTextAnchor(anchor) ? anchor : null;
}
