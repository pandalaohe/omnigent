import {
  COMPOSER_ATTACHMENT_PLACEHOLDER,
  composerPartsFromProjection,
  composerPartsToText,
  type ComposerDraftPart,
} from "./composerContent";
import {
  restoreReplyDraft,
  serializeReplyDraft,
  snapshotReplyDraft,
  type ReplyDraft,
  type StoredReplyDraft,
} from "./replyDraft";

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

/** Legacy cleanup for a draft restored from before the token model: strip
 *  U+FFFC from the text AND its saved snapshot together. Stripping a field
 *  empty can change the separators `serializeReplyDraft` picks, so the text
 *  has to be re-derived from the cleaned snapshot afterwards (see
 *  `restoreRecordDraft`). */
export function stripPlaceholdersFromStoredDraft(
  saved: StoredReplyDraft | undefined,
): StoredReplyDraft | undefined {
  if (!saved) return undefined;
  return {
    ...saved,
    quotes: saved.quotes.map((quote) => ({
      ...quote,
      before: stripPlaceholders(quote.before),
      text: stripPlaceholders(quote.text),
    })),
    text: stripPlaceholders(saved.text),
  };
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

/** Replace `range` in a live textarea with `insert` (empty `insert` deletes).
 *  When the browser has `execCommand`, the edit runs through the DOM so the
 *  textarea's own undo stack keeps working; the caller's change handler sees
 *  the resulting input event. Otherwise returns the spliced text and caret for
 *  the caller to commit to state and restore in a layout effect. */
export function applyFieldEdit(
  field: HTMLTextAreaElement | null,
  text: string,
  range: { start: number; end: number },
  insert: string,
): { applied: boolean; text: string; caret: number } {
  const nextText = text.slice(0, range.start) + insert + text.slice(range.end);
  const caret = range.start + insert.length;
  if (
    field !== null &&
    field.value === text &&
    !field.disabled &&
    typeof document !== "undefined" &&
    typeof document.execCommand === "function"
  ) {
    field.focus({ preventScroll: true });
    field.setSelectionRange(range.start, range.end);
    if (document.execCommand(insert ? "insertText" : "delete", false, insert || undefined)) {
      return { applied: true, text: nextText, caret };
    }
  }
  return { applied: false, text: nextText, caret };
}

/** Insert already-labelled `files`' tokens into a field at its caret. A field
 *  the user has never focused has no meaningful caret, so the tokens go at its
 *  end. `applied` means the DOM edit already happened. */
export function insertTokenFiles(
  field: HTMLTextAreaElement | null,
  text: string,
  files: readonly File[],
  useSelection: boolean,
): { applied: boolean; text: string; caret: number } {
  const selection =
    useSelection && field !== null
      ? { start: field.selectionStart ?? text.length, end: field.selectionEnd ?? text.length }
      : { start: text.length, end: text.length };
  const { insert, replaceStart, replaceEnd } = tokenInsertion(
    text,
    selection.start,
    selection.end,
    files,
  );
  return applyFieldEdit(field, text, { start: replaceStart, end: replaceEnd }, insert);
}

/** The whole-token delete a collapsed Backspace/Delete should perform at
 *  `caret`, or null when the caret is not at a live token's edge. */
export function deleteTokenAt(
  field: HTMLTextAreaElement | null,
  text: string,
  caret: number,
  key: "Backspace" | "Delete",
  files: readonly File[],
): { applied: boolean; text: string; caret: number } | null {
  const range = wholeTokenRange(text, caret, key, files);
  if (range === null) return null;
  return applyFieldEdit(field, text, range, "");
}

/** Focus the authored field holding `token` and put the caret just after it.
 *  The tail textarea is passed in; quote `before` fields are reached by the
 *  stable aria-label `ReplyDraftBlocks` gives them. */
export function focusTokenInComposer(
  token: string,
  tail: HTMLTextAreaElement | null,
  quotes: readonly { before: string }[],
): boolean {
  const fields: (HTMLTextAreaElement | null)[] = [tail];
  if (typeof document !== "undefined") {
    quotes.forEach((_, index) => {
      fields.push(
        document.querySelector<HTMLTextAreaElement>(
          `[aria-label="Reply text before quote ${index + 1}"]`,
        ),
      );
    });
  }
  const field = fields.find((candidate) => candidate?.value.includes(token)) ?? null;
  if (field === null) return false;
  const at = field.value.indexOf(token);
  field.focus({ preventScroll: true });
  field.setSelectionRange(at + token.length, at + token.length);
  return true;
}

/** Rebuild a draft from a sent/queued record: legacy placeholders are
 *  stripped from the text and its snapshot together, quotes are restored the
 *  upstream way, then each attachment's token goes back at the offset its
 *  parts recorded. Records without parts (legacy rows) keep plain text. */
export function restoreRecordDraft(record: {
  text: string;
  files?: readonly File[];
  composerParts?: readonly ComposerDraftPart[];
  replyDraft?: StoredReplyDraft;
}): ReplyDraft {
  const cleaned = stripPlaceholdersFromStoredDraft(record.replyDraft);
  // The saved pair was consistent before stripping, so re-derive the text
  // from the cleaned snapshot: the strip can empty a field and shift
  // `serializeReplyDraft`'s separators, which would fail its own validation.
  // A pair that was already inconsistent keeps the old plain-text fallback.
  const text =
    record.replyDraft && cleaned && serializeReplyDraft(record.replyDraft) === record.text
      ? serializeReplyDraft(cleaned)
      : stripPlaceholders(record.text);
  const draft = restoreReplyDraft(text, cleaned);
  const { attachments } = attachmentOffsets(record.composerParts ?? []);
  return reconcileLabels(placeTokens(draft, serializeReplyDraft, attachments), record.files ?? []);
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

/** The authored fields whose text changes when `files[index]`'s token is
 *  removed, as the new text for each (per-field, so the caller keeps the
 *  active field and a side-chat draft intact). */
export function removeFileTokens(
  draft: ReplyDraft,
  index: number,
  files: readonly File[],
): { fieldId: string | null; text: string }[] {
  const file = files[index];
  if (!file) return [];
  const stripped = removeTokens(draft, file);
  const edits: { fieldId: string | null; text: string }[] = [];
  draft.quotes.forEach((quote, i) => {
    const next = stripped.quotes[i]?.before;
    if (next !== undefined && next !== quote.before) edits.push({ fieldId: quote.id, text: next });
  });
  if (stripped.text !== draft.text) edits.push({ fieldId: null, text: stripped.text });
  return edits;
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
 *  regardless of how `serialize` (e.g. `joinParagraphs`) picks separators.
 *  `options.generatedPrefixLength` marks that many leading characters of
 *  authored field 0 as generated, not authored: token matches starting inside
 *  them never bind (their offsets still count for the projection). Measured
 *  on the placeholder-stripped field, the coordinate space the matches use. */
export interface BindDraftOptions {
  generatedPrefixLength?: number;
}
export function bindDraft<D extends DraftShape>(
  draft: D,
  files: readonly File[],
  serialize: (d: D) => string,
  options?: BindDraftOptions,
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
  const generatedPrefixLength = Math.max(0, options?.generatedPrefixLength ?? 0);
  const snapshotFields = getFields(stripped).map((text, fieldIndex) => {
    let result = "";
    let cursor = 0;
    for (const match of allTokenMatches(text)) {
      // A generated prefix is not authored text: a lookalike token inside it
      // must never bind an attachment (F1). Anchored matches end exactly at
      // the boundary, so checking the start is enough.
      if (fieldIndex === 0 && match.start < generatedPrefixLength) continue;
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

/** One composer send decision. `bound === 0` keeps upstream's call — the
 *  plain text plus the files as an argument list — so legacy rows and
 *  attachment-only drafts send exactly as before; otherwise `parts` carries
 *  each attachment's position and `files` is the bound order. Text, parts and
 *  replyDraft all derive from the single preamble-prefixed draft: the
 *  generated `preamble` lands on authored field 0 (`quotes[0].before` when
 *  quoted, the tail otherwise) BEFORE binding, so e.g. `joinParagraphs`
 *  picks the same separators for the snapshot and the sent text; the first
 *  `stripPlaceholders(preamble).length` characters of that field are marked
 *  generated so a path in the preamble can never bind a token (F1). */
export function planComposerSend(
  draft: ReplyDraft,
  files: readonly File[],
  preamble: string,
  trimmedText: string,
): {
  sendFiles: File[] | undefined;
  text: string;
  parts: ComposerDraftPart[] | undefined;
  replyDraft: StoredReplyDraft | undefined;
} {
  const quoted = draft.quotes.length > 0;
  const withPreamble: ReplyDraft = quoted
    ? {
        ...draft,
        quotes: draft.quotes.map((quote, index) =>
          index === 0 ? { ...quote, before: preamble + quote.before } : quote,
        ),
      }
    : { ...draft, text: preamble + trimmedText };
  const bound = bindDraft(withPreamble, files, quoted ? serializeReplyDraft : (d) => d.text, {
    // The boundary must be in the same coordinate space as the matches, which
    // are found in the placeholder-stripped field: a U+FFFC inside the
    // generated path would otherwise make it one character too long (F1).
    generatedPrefixLength: stripPlaceholders(preamble).length,
  });
  if (bound.bound === 0) {
    return {
      sendFiles: files.length > 0 ? [...files] : undefined,
      text: quoted ? serializeReplyDraft(withPreamble) : preamble + trimmedText,
      parts: undefined,
      replyDraft: quoted ? snapshotReplyDraft(withPreamble) : undefined,
    };
  }
  const parts = composerPartsFromProjection(bound.projection, bound.files);
  return {
    sendFiles: bound.files,
    // `stripPlaceholders(projection) === serialize(snapshot)` is bindDraft's
    // own invariant, so both reproduce the exact text that was sent — and the
    // stored pair validates, so recalling the message keeps its quotes.
    text: composerPartsToText(parts),
    parts,
    replyDraft: quoted ? snapshotReplyDraft(bound.snapshot) : undefined,
  };
}

/** Recover text-only offsets and their attachments from restored parts. */ export function attachmentOffsets(
  parts: readonly ComposerDraftPart[],
): {
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
