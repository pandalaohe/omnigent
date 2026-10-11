import { describe, expect, it } from "vitest";
import { Schema, type Node as ProseMirrorNode } from "@tiptap/pm/model";
import { Editor } from "@tiptap/core";
import { StarterKit } from "@tiptap/starter-kit";
import { Markdown } from "@tiptap/markdown";
import { findPmRangeForComment, computeSelectionData } from "./TipTapEditorHelpers";
import type { Comment } from "@/hooks/useComments";
import { decodeRenderedTextAnchor } from "./renderedTextAnchor";

// ---------------------------------------------------------------------------
// Test helpers
// ---------------------------------------------------------------------------

/**
 * Build a minimal ProseMirror Node mock where textBetween(from, to, sep)
 * is just text.slice(from, to).
 *
 * This collapses PM positions to text offsets (1:1 mapping), which makes
 * expected values trivial to compute while fully exercising the string
 * matching and binary search logic in the helpers.
 */
function makeDoc(text: string): ProseMirrorNode {
  return {
    content: { size: text.length },
    textBetween: (from: number, to: number, _sep: string) => text.slice(from, to),
    descendants: (callback: (node: { isText: boolean; text: string }, pos: number) => void) => {
      if (text) callback({ isText: true, text }, 0);
    },
  } as unknown as ProseMirrorNode;
}

/** Build a minimal Comment with only the fields the helpers use. */
function makeComment(
  anchor_content: string,
  start_index: number,
  end_index: number = start_index + anchor_content.length,
): Comment {
  return {
    id: "test-comment",
    start_index,
    end_index,
    anchor_content,
    body: "",
    created_at: "",
    author: null,
  } as unknown as Comment;
}

// ---------------------------------------------------------------------------
// findPmRangeForComment
// ---------------------------------------------------------------------------

describe("findPmRangeForComment", () => {
  const raw = "Hello, world! Hello, universe!";

  it("returns null when anchor_content is absent (undefined)", () => {
    const doc = makeDoc("Hello, world!");
    const comment = makeComment("x", 0);
    (comment as unknown as Record<string, unknown>).anchor_content = undefined;
    expect(findPmRangeForComment(doc, comment, raw)).toBeNull();
  });

  it("returns null when anchor_content is empty string", () => {
    const doc = makeDoc("Hello, world!");
    expect(findPmRangeForComment(doc, makeComment("", 0), raw)).toBeNull();
  });

  it("returns null when anchor_content is whitespace only", () => {
    const doc = makeDoc("Hello, world!");
    expect(findPmRangeForComment(doc, makeComment("   ", 0), raw)).toBeNull();
  });

  it("returns null when anchor_content is not present in the document", () => {
    const doc = makeDoc("Hello, world!");
    expect(findPmRangeForComment(doc, makeComment("missing text", 0), raw)).toBeNull();
  });

  it("does not search malformed typed payload as literal text", () => {
    const text = "Here is __text__not-json in a paragraph";
    expect(
      findPmRangeForComment(makeDoc(text), makeComment("__text__not-json", 8), text),
    ).toBeNull();
  });

  it("finds anchor at the very beginning of the document", () => {
    const doc = makeDoc("Hello, world!");
    const result = findPmRangeForComment(doc, makeComment("Hello", 0), "Hello, world!");
    expect(result).toEqual({ from: 0, to: 5 });
  });

  it("finds anchor in the middle of the document", () => {
    const text = "Hello, world!";
    const doc = makeDoc(text);
    const result = findPmRangeForComment(doc, makeComment("world", 7), text);
    expect(result).toEqual({ from: 7, to: 12 });
  });

  it("finds anchor at the end of the document", () => {
    const text = "Hello, world!";
    const doc = makeDoc(text);
    const result = findPmRangeForComment(doc, makeComment("world!", 7), text);
    expect(result).toEqual({ from: 7, to: 13 });
  });

  it("uses start_index hint to skip the first occurrence when it is >500 chars away", () => {
    // "foo" appears at 0 and again at 600. With hint=600 the search window
    // starts at 100 (= 600-500), so indexOf("foo", 100) finds the second
    // occurrence at 600, not the first at 0.
    const prefix = "x".repeat(597);
    const text = "foo" + prefix + "foo"; // "foo" at 0 and 600
    const doc = makeDoc(text);
    const result = findPmRangeForComment(doc, makeComment("foo", 600), text);
    expect(result).toEqual({ from: 600, to: 603 });
  });

  it("uses the nearest occurrence when two identical strings are within the search window", () => {
    // A short selection can repeat nearby. The stored offset must win over the
    // first match so the comment re-attaches to the selected occurrence.
    const text = "Hello, world! Hello, universe!";
    const doc = makeDoc(text);
    const result = findPmRangeForComment(doc, makeComment("Hello", 14), text);
    expect(result).toEqual({ from: 14, to: 19 });
  });

  it("falls back to the first occurrence when hint window misses", () => {
    // start_index=999 is way past the end; global fallback finds index 0.
    const text = "Hello, world!";
    const doc = makeDoc(text);
    const result = findPmRangeForComment(doc, makeComment("Hello", 999), text);
    expect(result).toEqual({ from: 0, to: 5 });
  });

  it("handles an empty document", () => {
    const doc = makeDoc("");
    expect(findPmRangeForComment(doc, makeComment("Hello", 0), "Hello")).toBeNull();
  });

  it("scales raw-file offset to text-content offset when rawContent is longer than text", () => {
    // Raw file has extra markdown syntax not in the doc text.
    // rawContent: "# Title\n\nHello, world!"  (len 22)
    // doc text:   "Title\nHello, world!"      (len 19)
    // anchor "Hello" at raw offset 10 → scaled hint ≈ 8 → finds at text offset 6.
    const rawContent = "# Title\n\nHello, world!";
    const docText = "Title\nHello, world!";
    const doc = makeDoc(docText);
    const result = findPmRangeForComment(doc, makeComment("Hello", 10), rawContent);
    expect(result).toEqual({ from: 6, to: 11 });
  });
});

// ---------------------------------------------------------------------------
// computeSelectionData
// ---------------------------------------------------------------------------

describe("computeSelectionData", () => {
  it("returns null when the selected PM range contains no text", () => {
    const text = "Hello, world!";
    const doc = makeDoc(text);
    expect(computeSelectionData(5, 5, doc, text)).toBeNull();
  });

  it("returns null when the selected text is whitespace only", () => {
    const text = "Hello   world";
    const doc = makeDoc(text);
    // Select the spaces at positions 5..8
    expect(computeSelectionData(5, 8, doc, text)).toBeNull();
  });

  it("returns correct indices when anchor is found verbatim in rawContent", () => {
    const text = "Hello, world!";
    const doc = makeDoc(text);
    const result = computeSelectionData(7, 12, doc, text);
    expect(result).toMatchObject({ start_index: 7, end_index: 12 });
    expect(decodeRenderedTextAnchor(result?.anchor_content)).toMatchObject({
      kind: "markdown",
      text: "world",
      occ: 0,
      prefix: "Hello, ",
      suffix: "!",
    });
  });

  it("uses anchor_content as-is (no trimming)", () => {
    // Selection includes a trailing space — should be stored verbatim.
    const text = "Hello, world!";
    const doc = makeDoc(text);
    const result = computeSelectionData(0, 7, doc, text); // "Hello, "
    expect(decodeRenderedTextAnchor(result?.anchor_content)?.text).toBe("Hello, ");
  });

  it("skips the first occurrence when hint places it >500 chars away", () => {
    // "foo" at raw offset 0 and again at 600. Selection covers the second
    // "foo" (text offset 600); hint = 600, searchFrom = 100, so indexOf
    // returns the match at 600.
    const padding = "x".repeat(597);
    const text = "foo" + padding + "foo"; // "foo" at 0 and 600
    const doc = makeDoc(text);
    const result = computeSelectionData(600, 603, doc, text);
    expect(result).toMatchObject({ start_index: 600, end_index: 603 });
    expect(decodeRenderedTextAnchor(result?.anchor_content)?.occ).toBe(1);
  });

  it("uses the nearest occurrence when duplicate strings are within the search window", () => {
    // Selecting the later short duplicate must not re-attach the comment to
    // the earlier identical text.
    const text = "foo bar foo baz";
    const doc = makeDoc(text);
    const result = computeSelectionData(8, 11, doc, text);
    expect(result).toMatchObject({ start_index: 8, end_index: 11 });
    expect(decodeRenderedTextAnchor(result?.anchor_content)?.occ).toBe(1);
  });

  it("falls back to proportional indices when anchor_content is not in rawContent verbatim", () => {
    // Simulate a multi-line selection where the doc joins with "\n" but
    // rawContent uses a different representation.
    const docText = "first\nsecond"; // doc has "\n" as separator
    const rawContent = "first\r\nsecond"; // raw file has "\r\n" (different)
    const doc = makeDoc(docText);
    // Select "first\nsecond" — the "\n" form won't be found in rawContent.
    const result = computeSelectionData(0, 12, doc, rawContent);
    expect(result).not.toBeNull();
    // Should use proportional fallback (hint-based) rather than returning null.
    expect(decodeRenderedTextAnchor(result!.anchor_content)?.text).toBe("first\nsecond");
    expect(decodeRenderedTextAnchor(result!.anchor_content)?.source).toBeUndefined();
    // start_index is proportional: hint = round(0 * 13 / 12) = 0
    expect(result!.start_index).toBe(0);
    // end_index is start_index + anchor_content.length = 0 + 12 = 12
    expect(result!.end_index).toBe(12);
  });

  it("falls back to proportional indices when rawContent is empty", () => {
    const text = "Hello";
    const doc = makeDoc(text);
    const result = computeSelectionData(0, 5, doc, "");
    expect(result).not.toBeNull();
    expect(decodeRenderedTextAnchor(result!.anchor_content)?.text).toBe("Hello");
    expect(result!.start_index).toBe(0);
    expect(result!.end_index).toBe(5);
  });

  it("handles selection of the full document", () => {
    const text = "Hello, world!";
    const doc = makeDoc(text);
    const result = computeSelectionData(0, text.length, doc, text);
    expect(result).toMatchObject({ start_index: 0, end_index: text.length });
    expect(decodeRenderedTextAnchor(result?.anchor_content)?.text).toBe(text);
  });
});

describe("rendered anchors in a real ProseMirror document", () => {
  const schema = new Schema({
    nodes: { doc: { content: "paragraph+" }, paragraph: { content: "text*" }, text: {} },
  });
  const paragraph = (text: string) =>
    schema.node("paragraph", null, text ? schema.text(text) : undefined);

  it("restores the second Code after long Markdown link syntax", () => {
    const href = `https://example.test/${"x".repeat(1007)}`;
    const raw = `[Code](${href})\n\nCode`;
    expect(raw.lastIndexOf("Code")).toBe(1038);
    const editor = new Editor({
      element: document.createElement("div"),
      extensions: [StarterKit, Markdown],
      content: raw,
      contentType: "markdown",
    });
    try {
      const doc = editor.state.doc;
      expect(doc.textBetween(0, doc.content.size, "\n")).toBe("Code\nCode");
      const selection = computeSelectionData(7, 11, doc, raw);
      expect(selection).not.toBeNull();
      expect(decodeRenderedTextAnchor(selection?.anchor_content)).toMatchObject({
        kind: "markdown",
        text: "Code",
        occ: 1,
        prefix: "Code\n",
        suffix: "",
      });
      expect(
        findPmRangeForComment(
          doc,
          makeComment(selection!.anchor_content, selection!.start_index),
          raw,
        ),
      ).toEqual({ from: 7, to: 11 });
    } finally {
      editor.destroy();
    }
  });

  it("keeps a selection spanning Markdown blocks after syntax is stripped", () => {
    const doc = schema.node("doc", null, [paragraph("Alpha"), paragraph("Beta")]);
    const raw = "**Alpha**\n\n_Beta_";
    const selection = computeSelectionData(1, 13, doc, raw);
    expect(decodeRenderedTextAnchor(selection?.anchor_content)?.text).toBe("Alpha\nBeta");
    expect(
      findPmRangeForComment(
        doc,
        makeComment(selection!.anchor_content, selection!.start_index),
        raw,
      ),
    ).toEqual({ from: 1, to: 12 });
  });

  it("counts an empty paragraph between repeated rendered words", () => {
    const doc = schema.node("doc", null, [paragraph("Code"), paragraph(""), paragraph("Code")]);
    const raw = "Code\n\n\n\nCode";
    const selection = computeSelectionData(9, 13, doc, raw);
    expect(decodeRenderedTextAnchor(selection?.anchor_content)).toMatchObject({
      text: "Code",
      occ: 1,
      prefix: "Code\n\n",
    });
    expect(
      findPmRangeForComment(
        doc,
        makeComment(selection!.anchor_content, selection!.start_index),
        raw,
      ),
    ).toEqual({ from: 9, to: 13 });
  });
});
