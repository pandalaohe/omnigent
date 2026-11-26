import { forwardRef, useMemo, type ReactNode } from "react";

import { labelOf, tokenText } from "@/lib/composerTokens";
import { findMentionTokens } from "@/lib/composerMentions";
import type { ComposerTextHighlight } from "@/components/composer/engine/composerInput";
import { MENTION_TOKEN_CLASS } from "@/components/composer/MentionTokenOverlay";

const NO_MENTION_TOKENS: readonly string[] = [];

const TOKEN_RE = /\[(?:image|file) \d+\]/g;
// Same pattern InlineComposerEditor's OmnigentReferenceHighlight matched, so
// a portable Archive Library reference keeps getting tinted after the
// editor's removal. The CLASSES are not reproduced verbatim: the editor's
// decoration could add padding, a border, and a different font (px-1 py-0.5
// border font-mono text-[0.78em]) because it lived inside ProseMirror's own
// layout; this overlay's text must stay pixel-aligned with the native
// textarea underneath, so its tint is colour/background/radius only.
const S26_RE = /⟦Omnigent reference \| [^⟧]+⟧/g;

const COMMAND_CLASS = "text-brand-accent";
const TOKEN_CLASS = "text-primary";
const ACTIVE_TOKEN_CLASS = "text-primary bg-primary/15 rounded-sm";
const S26_CLASS = "rounded-sm bg-primary/10 text-primary";

interface Span extends ComposerTextHighlight {
  testId?: string;
}

function commandSpan(value: string, command: string | null): Span | null {
  if (command === null) return null;
  const leading = /^\s*/.exec(value)?.[0] ?? "";
  const start = leading.length;
  if (value.slice(start, start + command.length) !== command) return null;
  return { start, end: start + command.length, className: COMMAND_CLASS };
}

function tokenSpans(value: string, files: readonly File[], activeIndex: number | null): Span[] {
  const byLabel = new Map<string, number>();
  files.forEach((file, i) => {
    const label = labelOf(file);
    if (label && !byLabel.has(tokenText(label))) byLabel.set(tokenText(label), i);
  });
  const spans: Span[] = [];
  for (const match of value.matchAll(TOKEN_RE)) {
    const fileIndex = byLabel.get(match[0]);
    if (fileIndex === undefined) continue;
    const start = match.index ?? 0;
    const active = activeIndex !== null && fileIndex === activeIndex;
    spans.push({
      start,
      end: start + match[0].length,
      className: active ? ACTIVE_TOKEN_CLASS : TOKEN_CLASS,
    });
  }
  return spans;
}

function s26Spans(value: string): Span[] {
  return [...value.matchAll(S26_RE)].map((match) => {
    const start = match.index ?? 0;
    return { start, end: start + match[0].length, className: S26_CLASS };
  });
}

/** Colour/background spans only — a leading command, live tokens, and S26
 *  reference markers. Command tint is opt-in per caller; tokens and S26
 *  references are what `hasTint` checks for the mount decision. */
function computeSpans(
  value: string,
  files: readonly File[],
  command: string | null,
  activeIndex: number | null,
  mentionTokens: readonly string[],
): Span[] {
  const spans: Span[] = composerAttachmentHighlights(value, files, activeIndex);
  spans.push(
    ...findMentionTokens(value, mentionTokens).map(({ start, end }) => ({
      start,
      end,
      className: MENTION_TOKEN_CLASS,
      testId: "composer-mention-token",
    })),
  );
  const commandTint = commandSpan(value, command);
  if (commandTint) spans.push(commandTint);
  return spans.sort((a, b) => a.start - b.start);
}

export function composerAttachmentHighlights(
  value: string,
  files: readonly File[],
  activeIndex: number | null,
): ComposerTextHighlight[] {
  return [...tokenSpans(value, files, activeIndex), ...s26Spans(value)];
}

/** Whether `value` has any live token or S26 reference — the backdrop's own
 *  mount condition is `composerIsCommand || hasTint(value, files)`. */
export function hasTint(value: string, files: readonly File[]): boolean {
  return tokenSpans(value, files, null).length > 0 || s26Spans(value).length > 0;
}

function renderSegments(value: string, spans: Span[]): ReactNode[] {
  const trailingNewline = value.endsWith("\n");
  const content = trailingNewline ? value.slice(0, -1) : value;
  const nodes: ReactNode[] = [];
  let cursor = 0;
  spans.forEach((span) => {
    // A span whose start lies inside an already-rendered (containing) span
    // — e.g. a live token inside an S26 reference — clamps to empty here:
    // the outer span already emitted that text once.
    const start = Math.max(cursor, Math.min(span.start, content.length));
    const end = Math.min(span.end, content.length);
    if (end <= start) return;
    if (start > cursor) nodes.push(content.slice(cursor, start));
    nodes.push(
      <span key={`${start}-${end}`} className={span.className} data-testid={span.testId}>
        {content.slice(start, end)}
      </span>,
    );
    cursor = end;
  });
  if (cursor < content.length) nodes.push(content.slice(cursor));
  // A final break needs content on its empty line to match the textarea's scroll range.
  if (trailingNewline) nodes.push(<br key="trailing-newline" />, "\u00a0");
  return nodes;
}

/** Renders the glyphs in the aligned composer backdrop. */
export const ComposerTokenBackdrop = forwardRef<
  HTMLDivElement,
  {
    value: string;
    files: readonly File[];
    command: string | null;
    activeIndex: number | null;
    mentionTokens?: readonly string[];
  }
>(function ComposerTokenBackdrop(
  { value, files, command, activeIndex, mentionTokens = NO_MENTION_TOKENS },
  ref,
) {
  const spans = useMemo(
    () => computeSpans(value, files, command, activeIndex, mentionTokens),
    [value, files, command, activeIndex, mentionTokens],
  );
  const nodes = useMemo(() => renderSegments(value, spans), [value, spans]);

  return (
    <div
      ref={ref}
      aria-hidden
      // Upstream's testid: this overlay replaces the slash-command
      // highlighter, and upstream's composer tests query it by that name.
      data-testid="composer-highlight-overlay"
      className="composer-input-text pointer-events-none absolute overflow-hidden whitespace-pre-wrap break-words p-0 text-ui text-foreground"
    >
      {nodes}
    </div>
  );
});
