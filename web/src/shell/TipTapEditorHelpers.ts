// Text-content position helpers for TipTap / ProseMirror comment anchoring.
//
// Rendered Markdown anchors use ProseMirror text with block separators.
// Historical plain text comments still use their stored offset as a hint.

import type { Node as ProseMirrorNode } from "@tiptap/pm/model";
import type { Comment } from "@/hooks/useComments";
import {
  captureRenderedTextAnchor,
  decodeRenderedTextAnchor,
  encodeRenderedTextAnchor,
  isRenderedTextAnchor,
  resolveRenderedTextAnchor,
} from "./renderedTextAnchor";

const SEP = "\n";

/**
 * Returns the occurrence of `needle` whose start is closest to `hint`.
 *
 * A fixed forward search window is not enough for short selections: another
 * copy of the same text can sit well within that window, and `indexOf`
 * returns the first one rather than the one the user selected.
 */
function nearestOccurrence(haystack: string, needle: string, hint: number): number {
  if (!needle) return -1;
  let best = -1;
  let bestDistance = Number.POSITIVE_INFINITY;
  let from = 0;
  while (from <= haystack.length) {
    const found = haystack.indexOf(needle, from);
    if (found === -1) break;
    const distance = Math.abs(found - hint);
    if (distance < bestDistance) {
      best = found;
      bestDistance = distance;
    } else if (found > hint) {
      break;
    }
    from = found + 1;
  }
  return best;
}

function renderedRangeToPmRange(doc: ProseMirrorNode, start: number, end: number) {
  const parts: { start: number; end: number; pos: number }[] = [];
  let textOffset = 0;
  let sawBlock = false;
  doc.descendants((node, pos) => {
    const leafText =
      node.isLeaf && !node.isText ? doc.textBetween(pos, pos + node.nodeSize, SEP) : "";
    if (node.isBlock && (node.isTextblock || leafText)) {
      if (sawBlock) textOffset += SEP.length;
      sawBlock = true;
    }
    if (node.isText && node.text) {
      parts.push({ start: textOffset, end: textOffset + node.text.length, pos });
      textOffset += node.text.length;
    } else {
      textOffset += leafText.length;
    }
  });
  const first = parts.find((part) => start >= part.start && start < part.end);
  const last = parts.find((part) => end > part.start && end <= part.end);
  if (!first || !last) return null;
  return { from: first.pos + start - first.start, to: last.pos + end - last.start };
}

/**
 * Finds the PM [from, to) range for a saved comment.
 *
 * Typed anchors resolve by exact rendered quote and context. Historical plain
 * text comments retain their raw offset hint.
 *
 * Returns null when anchor_content is absent or not found in the document.
 */
export function findPmRangeForComment(
  doc: ProseMirrorNode,
  comment: Comment,
  rawContent: string,
): { from: number; to: number } | null {
  const { anchor_content, start_index } = comment;
  if (!anchor_content?.trim()) return null;

  const textContent = doc.textBetween(0, doc.content.size, SEP);
  if (!textContent) return null;

  const rendered = decodeRenderedTextAnchor(anchor_content);
  if (isRenderedTextAnchor(anchor_content) && rendered?.kind !== "markdown") return null;
  const match = rendered ? resolveRenderedTextAnchor(textContent, rendered) : null;
  // Historical plain-text comments retain their old source-position hint.
  const hint =
    rawContent.length > 0 ? Math.round((start_index * textContent.length) / rawContent.length) : 0;
  const textFrom = rendered
    ? (match?.start ?? -1)
    : nearestOccurrence(textContent, anchor_content, hint);
  if (textFrom === -1) return null;

  const range = renderedRangeToPmRange(
    doc,
    textFrom,
    rendered ? match!.end : textFrom + anchor_content.length,
  );
  if (!range || range.from >= range.to) return null;
  return range;
}

/**
 * Capture the selected text in the rendered ProseMirror document. Numeric
 * offsets are rendered positions for sorting; raw source is never inferred.
 */
export function computeSelectionData(
  from: number,
  to: number,
  doc: ProseMirrorNode,
  _rawContent: string,
): { start_index: number; end_index: number; anchor_content: string } | null {
  const selectedText = doc.textBetween(from, to, SEP);
  if (!selectedText.trim()) return null;

  const textContent = doc.textBetween(0, doc.content.size, SEP);
  const textFrom = doc.textBetween(0, from, SEP).length;

  const anchor = captureRenderedTextAnchor(
    "markdown",
    textContent,
    textFrom,
    textFrom + selectedText.length,
  );
  if (!anchor || anchor.text !== selectedText) return null;
  const anchor_content = encodeRenderedTextAnchor(anchor);
  if (!anchor_content) return null;
  return { start_index: textFrom, end_index: textFrom + selectedText.length, anchor_content };
}
