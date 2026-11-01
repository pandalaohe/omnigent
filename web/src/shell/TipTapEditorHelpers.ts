// Text-content position helpers for TipTap / ProseMirror comment anchoring.
//
// Rendered Markdown anchors use ProseMirror text with block separators.
// Historical plain text comments still use their stored offset as a hint.

import type { Editor } from "@tiptap/core";
import type { Node as ProseMirrorNode } from "@tiptap/pm/model";
import { EditorState } from "@tiptap/pm/state";
import { Mapping, StepMap } from "@tiptap/pm/transform";
import type { MarkdownManager } from "@tiptap/markdown";
import { exactIndex, markdownSourceMap, type SourceMap } from "./markdownSourceMap";
import type { Comment } from "@/hooks/useComments";
import {
  captureRenderedTextAnchor,
  decodeRenderedTextAnchor,
  encodeRenderedTextAnchor,
  isRenderedTextAnchor,
  resolveRenderedTextAnchor,
} from "./renderedTextAnchor";

const SEP = "\n";
const sourceHashes = new WeakMap<SourceMap, string>();

// Distinguish document revisions before trusting a persisted source position.
function sourceContentHash(map: SourceMap, raw: string): string {
  const cached = sourceHashes.get(map);
  if (cached) return cached;
  let first = 0x811c9dc5;
  let second = 0x9e3779b9;
  for (let index = 0; index < raw.length; index++) {
    const code = raw.charCodeAt(index);
    first = Math.imul(first ^ code, 0x01000193);
    second = Math.imul(second ^ code, 0x85ebca6b);
  }
  const hash = `${raw.length.toString(16)}:${(first >>> 0).toString(16).padStart(8, "0")}${(second >>> 0).toString(16).padStart(8, "0")}`;
  sourceHashes.set(map, hash);
  return hash;
}

function exactRange(
  starts: number[],
  start: number,
  ends: number[],
  end: number,
): [number, number] | null {
  const first = exactIndex(starts, start);
  let last = exactIndex(ends, end);
  while (last !== -1 && ends[last + 1] === end) last++;
  return first !== -1 && last >= first ? [first, last] : null;
}

/**
 * Returns the occurrence of `needle` whose start is closest to `hint`.
 *
 * A fixed forward search window is not enough for short selections: another
 * copy of the same text can sit well within that window, and `indexOf`
 * returns the first one rather than the one the user selected.
 */
function nearestOccurrence(
  haystack: string,
  needle: string,
  hint: number,
  accept: (index: number) => boolean = () => true,
): number {
  if (!needle) return -1;
  let best = -1;
  let bestDistance = Number.POSITIVE_INFINITY;
  let from = 0;
  while (from <= haystack.length) {
    const found = haystack.indexOf(needle, from);
    if (found === -1) break;
    const distance = Math.abs(found - hint);
    if (found > hint && distance >= bestDistance) break;
    if (distance < bestDistance && accept(found)) {
      best = found;
      bestDistance = distance;
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

function mappedRange(
  doc: ProseMirrorNode,
  map: SourceMap,
  start: number,
  end: number,
  anchor: string,
): { from: number; to: number } | null {
  const indexes = exactRange(map.starts, start, map.ends, end);
  if (!indexes) return null;
  const range = { from: map.positions[indexes[0]], to: map.positions[indexes[1]] + 1 };
  return doc.textBetween(range.from, range.to, SEP) === anchor ? range : null;
}

/**
 * Finds the PM [from, to) range for a saved comment.
 *
 * Typed anchors use verified source positions while the source is unchanged,
 * then fall back to rendered quote and context. Historical plain text comments
 * retain their raw offset hint.
 *
 * Returns null when anchor_content is absent or not found in the document.
 */
export function findPmRangeForComment(
  doc: ProseMirrorNode,
  comment: Comment,
  rawContent: string,
  manager?: MarkdownManager,
): { from: number; to: number } | null {
  const { anchor_content, start_index, end_index } = comment;
  if (!anchor_content?.trim()) return null;

  const map = markdownSourceMap(doc, rawContent, manager);
  const mapped =
    !isRenderedTextAnchor(anchor_content) &&
    map &&
    mappedRange(doc, map, start_index, end_index, anchor_content);
  if (mapped) return mapped;
  const textContent = doc.textBetween(0, doc.content.size, SEP);
  if (!textContent) return null;

  const rendered = decodeRenderedTextAnchor(anchor_content);
  if (isRenderedTextAnchor(anchor_content) && rendered?.kind !== "markdown") return null;
  if (
    map &&
    rendered?.source &&
    rendered.source.contentHash === sourceContentHash(map, rawContent)
  ) {
    const range = mappedRange(
      doc,
      map,
      rendered.source.start_index,
      rendered.source.end_index,
      rendered.text,
    );
    if (range) return range;
  }
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
 * offsets are rendered positions for sorting; validated source spans live in the anchor.
 */
export function computeSelectionData(
  from: number,
  to: number,
  doc: ProseMirrorNode,
  rawContent: string,
  manager?: MarkdownManager,
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
  const map = markdownSourceMap(doc, rawContent, manager);
  if (map) {
    const first = map.positions.findIndex((position) => position >= from);
    const after = map.positions.findIndex((position) => position >= to);
    const last = (after === -1 ? map.positions.length : after) - 1;
    if (first !== -1 && last >= first) {
      const start_index = map.starts[first];
      const end_index = map.ends[last];
      const range = mappedRange(doc, map, start_index, end_index, selectedText);
      if (
        range &&
        range.from >= from &&
        range.to <= to &&
        !doc.textBetween(from, range.from, SEP) &&
        !doc.textBetween(range.to, to, SEP)
      ) {
        anchor.source = { start_index, end_index, contentHash: sourceContentHash(map, rawContent) };
      }
    }
  }
  const anchor_content = encodeRenderedTextAnchor(anchor);
  if (!anchor_content) return null;
  return { start_index: textFrom, end_index: textFrom + selectedText.length, anchor_content };
}

const renderedIndexes = new WeakMap<ProseMirrorNode, { text: string; positions: number[] }>();

function renderedIndex(doc: ProseMirrorNode) {
  const cached = renderedIndexes.get(doc);
  if (cached) return cached;
  let text = "";
  let firstBlock = true;
  const positions: number[] = [];
  doc.descendants((node, position) => {
    const value = node.isText
      ? node.text!
      : node.isLeaf
        ? (node.type.spec.leafText?.(node) ?? "")
        : "";
    // Match textBetween's separators, which have no selectable source character.
    if (node.isBlock && ((node.isLeaf && value) || node.isTextblock)) {
      if (!firstBlock) {
        text += SEP;
        positions.push(-1);
      }
      firstBlock = false;
    }
    text += value;
    for (let index = 0; index < value.length; index++)
      positions.push(position + (node.isText ? index : 0));
  });
  const result = { text, positions };
  renderedIndexes.set(doc, result);
  return result;
}

export function remapMarkdownComment(
  doc: ProseMirrorNode,
  comment: Comment,
  rawContent: string,
  manager: MarkdownManager,
): Comment {
  const anchor = comment.anchor_content;
  if (!anchor) return comment;
  const map = markdownSourceMap(doc, rawContent, manager);
  if (!map || mappedRange(doc, map, comment.start_index, comment.end_index, anchor)) return comment;
  const { text, positions } = renderedIndex(doc);
  let best = comment;
  let distance = Infinity;
  for (let index = text.indexOf(anchor); index !== -1; index = text.indexOf(anchor, index + 1)) {
    const first = exactIndex(map.positions, positions[index]);
    const last = exactIndex(map.positions, positions[index + anchor.length - 1]);
    if (first === -1 || last < first) continue;
    const start_index = map.starts[first];
    const end_index = map.ends[last];
    const candidateDistance = Math.abs(start_index - comment.start_index);
    if (start_index > comment.start_index && candidateDistance >= distance) break;
    if (candidateDistance >= distance) continue;
    // A decoded entity can map several characters to one indivisible source span.
    const bounds = exactRange(map.starts, start_index, map.ends, end_index);
    if (!bounds || bounds[0] !== first || bounds[1] !== last) continue;
    best = { ...comment, start_index, end_index };
    distance = candidateDistance;
  }
  return best !== comment && mappedRange(doc, map, best.start_index, best.end_index, anchor)
    ? best
    : comment;
}

/**
 * Replace the editor's document with Markdown another writer saved.
 *
 * The document is exactly what `setContent` produces. The selection keeps its
 * type and maps through the one range that changed, so a caret outside it stays
 * on its text. Undo history is cleared, as Monaco's `setValue` does: an older
 * step could otherwise revert the other writer's edit, and auto-save would
 * persist that.
 *
 * :param editor: A TipTap editor with the Markdown extension.
 * :param markdown: The file's new content, e.g. ``"# Title\n\nBody\n"``.
 */
export function applyExternalMarkdown(editor: Editor, markdown: string): void {
  const { doc: previous, selection } = editor.state;
  editor.commands.setContent(markdown, { emitUpdate: false, contentType: "markdown" });
  const { doc, plugins } = editor.state;
  editor.view.updateState(EditorState.create({ doc, plugins }));
  // Restore the selection with a dispatch so the toolbar and bubbles see it.
  editor.view.dispatch(
    editor.state.tr
      .setSelection(selection.map(doc, changedRangeMapping(previous, doc)))
      .setMeta("addToHistory", false),
  );
}

/** Map positions from `previous` to `next` around the single range where they differ. */
function changedRangeMapping(previous: ProseMirrorNode, next: ProseMirrorNode): Mapping {
  const start = previous.content.findDiffStart(next.content);
  if (start === null) return new Mapping();
  let { a: endA, b: endB } = previous.content.findDiffEnd(next.content)!;
  // Repeated content can make the backward scan pass `start`; widen both ends.
  const overlap = start - Math.min(endA, endB);
  if (overlap > 0) {
    endA += overlap;
    endB += overlap;
  }
  return new Mapping([new StepMap([start, endA - start, endB - start])]);
}
