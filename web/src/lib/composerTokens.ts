import { COMPOSER_ATTACHMENT_PLACEHOLDER, type ComposerDraftPart } from "./composerContent";

/** `[image N]` for image/* MIME types, `[file N]` for anything else. */
export type TokenKind = "image" | "file";
export interface TokenLabel {
  kind: TokenKind;
  n: number;
}

/** Upstream's reply-draft shape: every `before` plus the tail `text` are
 *  authored fields a token can live in; `text` on a quote is quoted source
 *  and is never searched for tokens. */
export interface DraftShape {
  quotes: readonly { before: string; text: string }[];
  text: string;
}

/** A quote index, or null for the tail. */
export type FieldId = number | null;

const TOKEN_RE = /\[(?:image|file) \d+\]/g;

// Labels survive remount, session switch, and failed-send restore because
// they key off the File object itself, not any draft state.
const registry = new WeakMap<File, TokenLabel>();

export function tokenText(label: TokenLabel): string {
  return `[${label.kind} ${label.n}]`;
}

export function labelOf(file: File): TokenLabel | undefined {
  return registry.get(file);
}

function kindOf(file: File): TokenKind {
  return file.type.startsWith("image/") ? "image" : "file";
}

function maxLabelN(files: readonly File[]): Record<TokenKind, number> {
  const max: Record<TokenKind, number> = { image: 0, file: 0 };
  for (const file of files) {
    const label = registry.get(file);
    if (label) max[label.kind] = Math.max(max[label.kind], label.n);
  }
  return max;
}

/** Assign a label to every `incoming` file that doesn't already have one,
 *  numbered from `max(live N of that kind among existing) + 1`, in order. */
export function assignLabels(incoming: readonly File[], existing: readonly File[]): void {
  const next = maxLabelN(existing);
  for (const file of incoming) {
    const already = registry.get(file);
    if (already) {
      next[already.kind] = Math.max(next[already.kind], already.n);
      continue;
    }
    const kind = kindOf(file);
    next[kind] += 1;
    registry.set(file, { kind, n: next[kind] });
  }
}

function allTokenMatches(text: string): { start: number; end: number; text: string }[] {
  const matches: { start: number; end: number; text: string }[] = [];
  for (const match of text.matchAll(TOKEN_RE)) {
    const start = match.index ?? 0;
    matches.push({ start, end: start + match[0].length, text: match[0] });
  }
  return matches;
}

function liveTokenSet(files: readonly File[]): Set<string> {
  const set = new Set<string>();
  for (const file of files) {
    const label = registry.get(file);
    if (label) set.add(tokenText(label));
  }
  return set;
}

/** Every authored field, in document order: `before_1 .. before_n`, tail. */
function getFields<D extends DraftShape>(draft: D): string[] {
  return [...draft.quotes.map((quote) => quote.before), draft.text];
}

function withFields<D extends DraftShape>(draft: D, fields: string[]): D {
  const quotes = draft.quotes.map((quote, i) => ({ ...quote, before: fields[i] }));
  return { ...draft, quotes, text: fields[fields.length - 1] } as D;
}

export function stripPlaceholders(text: string): string {
  return text.replaceAll(COMPOSER_ATTACHMENT_PLACEHOLDER, "");
}

/** Among files sharing a label, the k-th occurrence (document order) belongs
 *  to the k-th such file (`files` order); every file after the first gets a
 *  fresh label and its occurrence, if any, is rewritten. Idempotent. */
export function reconcileLabels<D extends DraftShape>(draft: D, files: readonly File[]): D {
  const byLabel = new Map<string, File[]>();
  for (const file of files) {
    const label = registry.get(file);
    if (!label) continue;
    const key = tokenText(label);
    const group = byLabel.get(key);
    if (group) group.push(file);
    else byLabel.set(key, [file]);
  }
  const duplicated = [...byLabel.entries()].filter(([, group]) => group.length > 1);
  if (duplicated.length === 0) return draft;

  const fields = getFields(draft);
  const nextN = maxLabelN(files);
  const rewrites = new Map<number, { start: number; from: string; to: string }[]>();

  for (const [tokenStr, group] of duplicated) {
    const occurrences: { fieldIndex: number; start: number }[] = [];
    fields.forEach((text, fieldIndex) => {
      let from = 0;
      for (;;) {
        const at = text.indexOf(tokenStr, from);
        if (at < 0) break;
        occurrences.push({ fieldIndex, start: at });
        from = at + tokenStr.length;
      }
    });
    for (let k = 1; k < group.length; k += 1) {
      const file = group[k];
      const kind = registry.get(file)!.kind;
      nextN[kind] += 1;
      const newLabel: TokenLabel = { kind, n: nextN[kind] };
      registry.set(file, newLabel);
      const occurrence = occurrences[k];
      if (!occurrence) continue;
      const list = rewrites.get(occurrence.fieldIndex) ?? [];
      list.push({ start: occurrence.start, from: tokenStr, to: tokenText(newLabel) });
      rewrites.set(occurrence.fieldIndex, list);
    }
  }

  const nextFields = fields.map((text, fieldIndex) => {
    const list = rewrites.get(fieldIndex);
    if (!list) return text;
    let result = text;
    for (const { start, from, to } of [...list].sort((a, b) => b.start - a.start)) {
      result = result.slice(0, start) + to + result.slice(start + from.length);
    }
    return result;
  });
  return withFields(draft, nextFields);
}

/** Text to insert for `files` (already labelled) at a collapsed or ranged
 *  selection, and the range the caller should replace — same result whether
 *  applied via `execCommand("insertText")` or by splicing. */
export function tokenInsertion(
  text: string,
  selStart: number,
  selEnd: number,
  files: readonly File[],
): { insert: string; replaceStart: number; replaceEnd: number } {
  const tokens = files
    .map((file) => registry.get(file))
    .filter((label): label is TokenLabel => label !== undefined)
    .map(tokenText)
    .join(" ");
  const precedingChar = selStart > 0 ? text[selStart - 1] : undefined;
  const leading = precedingChar !== undefined && !/\s/.test(precedingChar) ? " " : "";
  return { insert: `${leading}${tokens} `, replaceStart: selStart, replaceEnd: selEnd };
}

/** Remove every occurrence of `file`'s token (and the one trailing space an
 *  insertion added, if still there) from every authored field. The file
 *  itself is untouched — the caller drops it from `files` separately. */
export function removeTokens<D extends DraftShape>(draft: D, file: File): D {
  const label = registry.get(file);
  if (!label) return draft;
  const text = tokenText(label);
  const escaped = text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const re = new RegExp(`${escaped} ?`, "g");
  return withFields(
    draft,
    getFields(draft).map((field) => field.replace(re, "")),
  );
}

/** The live token (plus the one space `tokenInsertion` added) a collapsed
 *  caret's Backspace/Delete should remove as a unit, or null. */
export function wholeTokenRange(
  text: string,
  caret: number,
  key: "Backspace" | "Delete",
  files: readonly File[],
): { start: number; end: number } | null {
  const live = liveTokenSet(files);
  for (const match of allTokenMatches(text)) {
    if (!live.has(match.text)) continue;
    const followedBySpace = text[match.end] === " ";
    if (key === "Backspace") {
      if (caret !== match.end && caret !== match.end + 1) continue;
      // Caret one past the end is only a match when a space actually follows;
      // otherwise it's past unrelated text, not "the space this token added".
      if (caret === match.end + 1 && !followedBySpace) continue;
      return { start: match.start, end: followedBySpace ? match.end + 1 : match.end };
    } else if (caret === match.start) {
      return { start: match.start, end: followedBySpace ? match.end + 1 : match.end };
    }
  }
  return null;
}

/** The index into `files` of the live token the caret sits inside (or at an
 *  edge of), or null. */
export function tokenAt(text: string, caret: number, files: readonly File[]): number | null {
  const byLabel = new Map<string, number>();
  files.forEach((file, i) => {
    const label = registry.get(file);
    if (label && !byLabel.has(tokenText(label))) byLabel.set(tokenText(label), i);
  });
  for (const match of allTokenMatches(text)) {
    if (caret < match.start || caret > match.end) continue;
    const index = byLabel.get(match.text);
    if (index !== undefined) return index;
  }
  return null;
}

/** Splice `marker` into `text` at each entry's offset. Descending offset
 *  first (an earlier, pending offset never shifts), then descending
 *  `ordinal`: same-offset entries land left-to-right in `ordinal` order. */
function spliceAtOffsets(
  text: string,
  entries: readonly { offset: number; ordinal: number; marker: string }[],
): string {
  const sorted = [...entries].sort((a, b) => b.offset - a.offset || b.ordinal - a.ordinal);
  let result = text;
  for (const { offset, marker } of sorted) {
    result = result.slice(0, offset) + marker + result.slice(offset);
  }
  return result;
}

/** Strip every literal U+FFFC and remove the first occurrence per file of a
 *  live token from its authored field (document order); an unreferenced file
 *  contributes nothing to `snapshot`. `projection` is `serialize(snapshot)`
 *  with one U+FFFC spliced back at each bound token's original position (in
 *  bound order) and one U+FFFC appended at the very end per unreferenced
 *  file — so `stripPlaceholders(projection) === serialize(snapshot)` always,
 *  regardless of how `serialize` (e.g. `joinParagraphs`) picks separators. */
export function bindDraft<D extends DraftShape>(
  draft: D,
  files: readonly File[],
  serialize: (d: D) => string,
): { projection: string; snapshot: D; files: File[]; bound: number } {
  const strippedFields = getFields(draft).map(stripPlaceholders);
  const strippedQuotes = draft.quotes.map((quote, i) => ({
    ...quote,
    before: strippedFields[i],
    text: stripPlaceholders(quote.text),
  }));
  const stripped = {
    ...draft,
    quotes: strippedQuotes,
    text: strippedFields[strippedFields.length - 1],
  } as D;

  const live = liveTokenSet(files);
  const fileByLabel = new Map<string, File>();
  for (const file of files) {
    const label = registry.get(file);
    if (label) fileByLabel.set(tokenText(label), file);
  }

  const seen = new Set<File>();
  const ordered: File[] = [];
  // Local offset (within the field's OWN token-free text) of each bound
  // file's removed token — the position `projection` must re-insert it at.
  const localOffsets = new Map<File, { fieldIndex: number; offset: number }>();
  const snapshotFields = getFields(stripped).map((text, fieldIndex) => {
    let result = "";
    let cursor = 0;
    for (const match of allTokenMatches(text)) {
      if (!live.has(match.text)) continue;
      const file = fileByLabel.get(match.text);
      if (!file || seen.has(file)) continue;
      result += text.slice(cursor, match.start);
      cursor = match.end;
      seen.add(file);
      ordered.push(file);
      localOffsets.set(file, { fieldIndex, offset: result.length });
    }
    result += text.slice(cursor);
    return result;
  });
  const bound = seen.size;
  for (const file of files) {
    if (!seen.has(file)) ordered.push(file);
  }

  const snapshot = withFields(stripped, snapshotFields);
  const base = serialize(snapshot);
  const ranges = fieldRanges(snapshot, serialize);
  const spliceEntries = ordered.map((file, ordinal) => {
    const local = localOffsets.get(file);
    const offset = local ? ranges[local.fieldIndex].start + local.offset : base.length;
    return { offset, ordinal, marker: COMPOSER_ATTACHMENT_PLACEHOLDER };
  });
  const projection = spliceAtOffsets(base, spliceEntries);

  return { projection, snapshot, files: ordered, bound };
}

/** Recover text-only offsets and their attachments from restored parts. */
export function attachmentOffsets(parts: readonly ComposerDraftPart[]): {
  text: string;
  attachments: { offset: number; file: File }[];
} {
  let text = "";
  const attachments: { offset: number; file: File }[] = [];
  for (const part of parts) {
    if (part.type === "text") text += part.text;
    else attachments.push({ offset: text.length, file: part.file });
  }
  return { text, attachments };
}

// Sentinel to locate a field's start in `serialize(draft)`. Appended at the
// field's END, not prepended: `joinParagraphs` picks separators from a
// field's OWN leading newline, and a prepended marker would corrupt that.
const FIELD_MARKER = "\u0000";

function fieldRanges<D extends DraftShape>(
  draft: D,
  serialize: (d: D) => string,
): { start: number; end: number }[] {
  const fields = getFields(draft);
  const total = serialize(draft).length;
  return fields.map((field, i) => {
    const marked = withFields(
      draft,
      fields.map((text, j) => (j === i ? text + FIELD_MARKER : text)),
    );
    const markerIndex = serialize(marked).indexOf(FIELD_MARKER);
    const start = Math.min(Math.max(0, markerIndex - field.length), total);
    return { start, end: start + field.length };
  });
}

/** Put each attachment's token back at its offset in `serialize(draft)`.
 *  An offset that lands outside every authored field (i.e. in quoted source)
 *  goes to the tail end instead of being dropped. */
export function placeTokens<D extends DraftShape>(
  draft: D,
  serialize: (d: D) => string,
  attachments: readonly { offset: number; file: File }[],
): D {
  if (attachments.length === 0) return draft;
  const unlabeled = attachments.map((a) => a.file).filter((file) => !registry.get(file));
  if (unlabeled.length > 0) assignLabels(unlabeled, []);

  const fields = getFields(draft);
  const ranges = fieldRanges(draft, serialize);
  const fieldTexts = [...fields];
  const tailIndex = fieldTexts.length - 1;

  function insertAt(fieldIndex: number, localOffset: number, file: File): void {
    const label = tokenText(registry.get(file)!);
    const current = fieldTexts[fieldIndex];
    const before = current.slice(0, localOffset);
    const after = current.slice(localOffset);
    const leading = before.length > 0 && !/\s$/.test(before) ? " " : "";
    const trailing = after.length === 0 || !/^\s/.test(after) ? " " : "";
    fieldTexts[fieldIndex] = `${before}${leading}${label}${trailing}${after}`;
  }

  const withField = attachments.map((attachment, index) => ({
    ...attachment,
    index,
    fieldIndex: ranges.findIndex(
      (range) => attachment.offset >= range.start && attachment.offset <= range.end,
    ),
  }));
  // In-range: descending offset (an earlier offset never shifts) then
  // descending index, so same-offset attachments land left-to-right in
  // original order. Fallback (quoted source, no containing field) has no
  // fixed offset to sort by, so it goes by original index instead.
  const inRange = withField
    .filter((a) => a.fieldIndex >= 0)
    .sort((a, b) => b.offset - a.offset || b.index - a.index);
  const fallback = withField.filter((a) => a.fieldIndex < 0).sort((a, b) => a.index - b.index);

  for (const { offset, file, fieldIndex } of inRange) {
    insertAt(fieldIndex, offset - ranges[fieldIndex].start, file);
  }
  for (const { file } of fallback) {
    insertAt(tailIndex, fieldTexts[tailIndex].length, file);
  }
  return withFields(draft, fieldTexts);
}

/** Per-file badge for the attachment legend: its current label, and whether
 *  that label appears (live) anywhere in an authored field. */
export function attachmentBadges(
  draft: DraftShape,
  files: readonly File[],
): { label: string; unreferenced: boolean }[] {
  const fields = getFields(draft);
  return files.map((file) => {
    const label = registry.get(file);
    if (!label) return { label: "", unreferenced: true };
    const text = tokenText(label);
    const referenced = fields.some((field) => field.includes(text));
    return { label: text, unreferenced: !referenced };
  });
}
