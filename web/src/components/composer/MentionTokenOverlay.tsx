import { forwardRef, type ReactNode, type RefObject, useLayoutEffect } from "react";

import { findMentionTokens } from "@/lib/composerMentions";
import {
  isComposerTextarea,
  type ComposerTextTarget,
} from "@/components/composer/engine/composerInput";

/**
 * Pill look for an inline ``@`` token in the composer mirror. Background, ring
 * and color only: anything that changes glyph width (weight, padding) would
 * shift the mirror out of line with the textarea above it.
 */
export const MENTION_TOKEN_CLASS =
  "rounded-[4px] bg-brand-accent/10 text-brand-accent ring-2 ring-brand-accent/10";

/** ``text`` with each tagged token wrapped in a pill span. */
export function renderMentionTokens(text: string, tokens: readonly string[]): ReactNode {
  const ranges = findMentionTokens(text, tokens);
  if (ranges.length === 0) return text;
  const parts: ReactNode[] = [];
  let at = 0;
  for (const range of ranges) {
    parts.push(text.slice(at, range.start));
    parts.push(
      <span key={range.start} data-testid="composer-mention-token" className={MENTION_TOKEN_CLASS}>
        {range.token}
      </span>,
    );
    at = range.end;
  }
  parts.push(text.slice(at));
  return parts;
}

function alignMirror(overlay: HTMLDivElement, textarea: HTMLTextAreaElement) {
  const { offsetTop, offsetLeft, offsetHeight, scrollTop } = textarea;
  // A rounded width can move a glyph to a different line at a soft wrap.
  const { width } = getComputedStyle(textarea);
  overlay.style.top = `${offsetTop}px`;
  overlay.style.left = `${offsetLeft}px`;
  overlay.style.width = width;
  overlay.style.height = `${offsetHeight}px`;
  overlay.scrollTop = scrollTop;
}

/**
 * Pin a mirror overlay to its textarea's box and scroll position while
 * ``active``. The input area also holds the textarea's line-box descent, which
 * would otherwise cut the mirror's scroll range short. Only a textarea has a
 * mirror; other engines paint their own text.
 */
export function useComposerMirrorAlignment(
  overlayRef: RefObject<HTMLDivElement | null>,
  textareaRef: RefObject<ComposerTextTarget | null>,
  active: boolean,
  // The textarea node, passed when an engine switch can replace it so alignment re-binds.
  textareaNode: ComposerTextTarget | null = null,
) {
  // A draft update can move the textarea or change its inner scroll range
  // without resizing its outer box or firing a scroll event yet.
  useLayoutEffect(() => {
    const overlay = overlayRef.current;
    const textarea = textareaRef.current;
    if (!active || !overlay || !textarea || !isComposerTextarea(textarea)) return;
    alignMirror(overlay, textarea);
  });

  useLayoutEffect(() => {
    const overlay = overlayRef.current;
    const textarea = textareaRef.current;
    if (!active || !overlay || !textarea || !isComposerTextarea(textarea)) return;
    const syncScroll = () => {
      overlay.scrollTop = textarea.scrollTop;
    };
    textarea.addEventListener("scroll", syncScroll);
    textarea.addEventListener("compositionend", syncScroll);
    const observer =
      typeof ResizeObserver === "undefined"
        ? undefined
        : new ResizeObserver(() => alignMirror(overlay, textarea));
    observer?.observe(textarea);
    return () => {
      textarea.removeEventListener("scroll", syncScroll);
      textarea.removeEventListener("compositionend", syncScroll);
      observer?.disconnect();
    };
  }, [overlayRef, textareaRef, active, textareaNode]);
}

/** Classes that hand glyph painting to the mirror; the caret stays visible. */
export const MIRRORED_TEXTAREA_CLASS = "text-transparent caret-foreground";

/**
 * Mirror drawn behind a composer textarea, with the same typography so
 * wrapping matches. Mount it in ``ChatComposer``'s ``inputBackdrop`` slot.
 */
export const ComposerMirror = forwardRef<HTMLDivElement, { children: ReactNode }>(
  function ComposerMirror({ children }, ref) {
    return (
      <div
        ref={ref}
        aria-hidden
        data-testid="composer-highlight-overlay"
        className="composer-input-text pointer-events-none absolute inset-x-0 overflow-hidden whitespace-pre-wrap break-words px-3 text-ui text-foreground max-md:px-4"
      >
        {children}
      </div>
    );
  },
);
