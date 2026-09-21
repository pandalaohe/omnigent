import { describe, expect, it } from "vitest";

import { composerPartsFromProjection } from "./composerContent";
import { restoreReplyDraft, serializeReplyDraft, type ReplyDraft } from "./replyDraft";
import {
  assignLabels,
  attachmentBadges,
  attachmentOffsets,
  bindDraft,
  labelOf,
  placeTokens,
  reconcileLabels,
  removeTokens,
  stripPlaceholders,
  tokenAt,
  tokenInsertion,
  tokenText,
  wholeTokenRange,
  type DraftShape,
} from "./composerTokens";

function img(name = "a.png"): File {
  return new File([new Uint8Array(1)], name, { type: "image/png" });
}
function pdf(name = "a.pdf"): File {
  return new File([new Uint8Array(1)], name, { type: "application/pdf" });
}
const plainSerialize = (d: DraftShape) => d.text;

describe("tokenInsertion", () => {
  it("T1 — inserts a token at the caret, spaced from adjacent text", () => {
    const file = img();
    assignLabels([file], []);
    const text = "abcd";
    const { insert, replaceStart, replaceEnd } = tokenInsertion(text, 2, 2, [file]);
    expect(text.slice(0, replaceStart) + insert + text.slice(replaceEnd)).toBe("ab [image 1] cd");
  });

  it("T2 — inserts several tokens for mixed kinds in attach order", () => {
    const files = [img("a.png"), pdf("a.pdf"), img("b.png")];
    assignLabels(files, []);
    const { insert } = tokenInsertion("", 0, 0, files);
    expect(insert).toBe("[image 1] [file 1] [image 2] ");
  });
});

describe("assignLabels", () => {
  it("T3 — keeps numbers stable across removal; a new file takes max+1", () => {
    const f1 = img("1.png");
    const f2 = img("2.png");
    const f3 = img("3.png");
    assignLabels([f1, f2, f3], []);
    expect(tokenText(labelOf(f2)!)).toBe("[image 2]");

    const remaining = [f1, f3]; // f2's tile was removed
    const f4 = img("4.png");
    assignLabels([f4], remaining);
    expect(tokenText(labelOf(f4)!)).toBe("[image 4]");
    expect(tokenText(labelOf(f1)!)).toBe("[image 1]");
    expect(tokenText(labelOf(f3)!)).toBe("[image 3]");
  });
});

describe("bindDraft", () => {
  it("T4 — binds files in document order, not files order", () => {
    const i1 = img("1.png");
    const i2 = img("2.png");
    assignLabels([i1, i2], []);
    const { projection, files, bound } = bindDraft(
      { quotes: [], text: "x [image 2] y [image 1]" },
      [i1, i2],
      plainSerialize,
    );
    expect(projection).toBe("x ￼ y ￼");
    expect(files).toEqual([i2, i1]);
    expect(bound).toBe(2);
  });

  it("T5 — binds nothing when no token is present", () => {
    const i1 = img();
    assignLabels([i1], []);
    const { files, bound } = bindDraft({ quotes: [], text: "hello" }, [i1], plainSerialize);
    expect(bound).toBe(0);
    expect(files).toEqual([i1]);
  });

  it("T6 — binds a referenced file and appends the unreferenced one, in files order", () => {
    const i1 = img();
    const f1 = pdf();
    assignLabels([i1, f1], []);
    const { projection, files, bound } = bindDraft(
      { quotes: [], text: "a [image 1]" },
      [i1, f1],
      plainSerialize,
    );
    expect(projection).toBe("a ￼￼");
    expect(files).toEqual([i1, f1]);
    expect(bound).toBe(1);
  });

  it("T7 — an orphan token with no matching live file stays literal", () => {
    const i1 = img();
    assignLabels([i1], []);
    const { projection, bound } = bindDraft(
      { quotes: [], text: "see [image 9] [image 1]" },
      [i1],
      plainSerialize,
    );
    expect(projection).toBe("see [image 9] ￼");
    expect(bound).toBe(1);
  });

  it("T8 — a duplicated token binds only its first occurrence", () => {
    const i1 = img();
    assignLabels([i1], []);
    const { projection, bound } = bindDraft(
      { quotes: [], text: "[image 1] and [image 1]" },
      [i1],
      plainSerialize,
    );
    expect(projection).toBe("￼ and [image 1]");
    expect(bound).toBe(1);
  });

  it("T9 — a literal U+FFFC is stripped before binding", () => {
    const i1 = img();
    assignLabels([i1], []);
    const { projection, bound } = bindDraft(
      { quotes: [], text: "￼ then [image 1]" },
      [i1],
      plainSerialize,
    );
    expect(projection).toBe(" then ￼");
    expect(bound).toBe(1);
  });

  it("T10 — binds a token that lives in a quote's before field", () => {
    const i1 = img();
    assignLabels([i1], []);
    const draft: ReplyDraft = {
      quotes: [{ id: "q1", before: "see [image 1]", text: "quoted" }],
      text: "",
    };
    const result = bindDraft(draft, [i1], serializeReplyDraft);
    expect(result.snapshot.quotes[0].before).toBe("see ");
    expect(result.projection).toContain("see ￼");
    expect(result.files).toEqual([i1]);
    expect(result.bound).toBe(1);
  });

  it("T11 — never searches quoted source text for a token", () => {
    const i1 = img();
    assignLabels([i1], []);
    const draft: ReplyDraft = {
      quotes: [{ id: "q1", before: "", text: "quoted [image 1] text" }],
      text: "tail [image 1]",
    };
    const result = bindDraft(draft, [i1], serializeReplyDraft);
    expect(result.snapshot.quotes[0].text).toBe("quoted [image 1] text");
    expect(result.snapshot.text).toBe("tail ");
    expect(result.bound).toBe(1);
  });

  it("F2 — snapshot serialization always matches projection with placeholders stripped, even when a field's emptiness/newlines shift", () => {
    const i1 = img();
    assignLabels([i1], []);
    // codex repro: two quotes, the second's `before` holds the only token;
    // removing it makes that field empty, which changes joinParagraphs'
    // separator choice relative to a placeholder-laden intermediate string.
    const draft: ReplyDraft = {
      quotes: [
        { id: "q1", before: "", text: "one" },
        { id: "q2", before: "[image 1]", text: "two" },
      ],
      text: "tail",
    };
    const { projection, snapshot } = bindDraft(draft, [i1], serializeReplyDraft);
    const base = serializeReplyDraft(snapshot);
    expect(stripPlaceholders(projection)).toBe(base);
    expect(snapshot.quotes[1].before).toBe("");
    // The restore path must recover both quotes from this snapshot + text.
    const restored = restoreReplyDraft(base, {
      version: 1,
      quotes: snapshot.quotes.map(({ before, text }) => ({ before, text })),
      text: snapshot.text,
    });
    expect(restored.quotes).toHaveLength(2);
  });

  it("F2 — invariant holds for a field with a leading newline", () => {
    const i1 = img();
    assignLabels([i1], []);
    const draft: ReplyDraft = {
      quotes: [{ id: "q1", before: "", text: "q" }],
      text: "\n[image 1] abc",
    };
    const { projection, snapshot } = bindDraft(draft, [i1], serializeReplyDraft);
    const base = serializeReplyDraft(snapshot);
    expect(stripPlaceholders(projection)).toBe(base);
    // The token itself is removed; the space it was inserted with is not
    // bindDraft's concern (that's placeTokens' spacing rule on restore).
    expect(snapshot.text).toBe("\n abc");
  });

  it("F3 — attachments sharing an offset (all unreferenced) stay in files order", () => {
    const i1 = img("1.png");
    const i2 = img("2.png");
    const i3 = img("3.png");
    assignLabels([i1, i2, i3], []);
    const { projection, files, bound } = bindDraft(
      { quotes: [], text: "a [image 1]" },
      [i1, i2, i3],
      plainSerialize,
    );
    expect(bound).toBe(1);
    expect(files).toEqual([i1, i2, i3]);
    expect(projection).toBe("a ￼￼￼");
  });
});

describe("wholeTokenRange", () => {
  it("T12 — Backspace matches the token plus its inserted trailing space, from either boundary", () => {
    const i1 = img();
    assignLabels([i1], []);
    const text = "[image 1] rest";
    // Caret right after the closing bracket must also take the following space.
    expect(wholeTokenRange(text, 9, "Backspace", [i1])).toEqual({ start: 0, end: 10 });
    // Caret one past that (after the space) gives the same range.
    expect(wholeTokenRange(text, 10, "Backspace", [i1])).toEqual({ start: 0, end: 10 });
  });

  it("Backspace with no following space removes only the token", () => {
    const i1 = img();
    assignLabels([i1], []);
    const text = "[image 1]cd";
    expect(wholeTokenRange(text, 9, "Backspace", [i1])).toEqual({ start: 0, end: 9 });
    expect(wholeTokenRange(text, 10, "Backspace", [i1])).toBeNull();
  });

  it("T13 — a half-deleted token is no longer a whole-token match", () => {
    const i1 = img();
    assignLabels([i1], []);
    const text = "[image 1";
    expect(wholeTokenRange(text, text.length, "Backspace", [i1])).toBeNull();
    const [badge] = attachmentBadges({ quotes: [], text }, [i1]);
    expect(badge.unreferenced).toBe(true);
  });

  it("Delete matches from the token's start, including one following space", () => {
    const i1 = img();
    assignLabels([i1], []);
    const text = "[image 1] rest";
    expect(wholeTokenRange(text, 0, "Delete", [i1])).toEqual({ start: 0, end: 10 });
  });
});

describe("tokenAt", () => {
  it("resolves the caret to the file index whose live token contains it", () => {
    const i1 = img();
    const f1 = pdf();
    assignLabels([i1, f1], []);
    const text = "a [image 1] b [file 1]";
    expect(tokenAt(text, 5, [i1, f1])).toBe(0);
    expect(tokenAt(text, text.length - 1, [i1, f1])).toBe(1);
    expect(tokenAt(text, 0, [i1, f1])).toBeNull();
  });
});

describe("removeTokens", () => {
  it("removes every occurrence of a file's token and its trailing space", () => {
    const i1 = img();
    assignLabels([i1], []);
    const draft = { quotes: [], text: "ab [image 1] cd" };
    expect(removeTokens(draft, i1).text).toBe("ab cd");
  });
});

describe("reconcileLabels", () => {
  it("T16 — relabels the second file when two drafts share a label after a merge", () => {
    const i1 = img("a.png");
    const i2 = img("b.png");
    assignLabels([i1], []);
    assignLabels([i2], []); // an independently-numbered draft also produced [image 1]
    expect(tokenText(labelOf(i2)!)).toBe("[image 1]");

    const draft = { quotes: [], text: "[image 1] [image 1]" };
    const reconciled = reconcileLabels(draft, [i1, i2]);
    expect(reconciled.text).toBe("[image 1] [image 2]");
    expect(tokenText(labelOf(i1)!)).toBe("[image 1]");
    expect(tokenText(labelOf(i2)!)).toBe("[image 2]");
  });

  it("is idempotent on an already-reconciled draft", () => {
    const i1 = img("a.png");
    const i2 = img("b.png");
    assignLabels([i1], []);
    assignLabels([i2], []);
    const draft = { quotes: [], text: "[image 1] [image 1]" };
    const once = reconcileLabels(draft, [i1, i2]);
    const twice = reconcileLabels(once, [i1, i2]);
    expect(twice).toEqual(once);
  });

  it("is a no-op on a clean draft with no shared labels", () => {
    const i1 = img("a.png");
    const f1 = pdf("a.pdf");
    assignLabels([i1, f1], []);
    const draft = { quotes: [], text: "[image 1] [file 1]" };
    expect(reconcileLabels(draft, [i1, f1])).toEqual(draft);
  });
});

describe("stripPlaceholders", () => {
  it("T17 — strips U+FFFC and nothing else", () => {
    expect(stripPlaceholders("x￼y")).toBe("xy");
  });
});

describe("placeTokens round-trip", () => {
  it("byte-exact for a plain draft: bind, serialize, and restore reproduce the same text", () => {
    const i1 = img();
    assignLabels([i1], []);
    const original = "ab [image 1] cd";
    const { projection, snapshot, files } = bindDraft(
      { quotes: [], text: original },
      [i1],
      plainSerialize,
    );
    const restored = { quotes: [], text: snapshot.text };
    const { attachments } = attachmentOffsets(composerPartsFromProjection(projection, files));
    const placed = placeTokens(restored, plainSerialize, attachments);
    expect(placed.text).toBe(original);
  });

  it("main-brain probe: plain 'ab [image 1] cd' round-trips without doubling the space", () => {
    const i1 = img();
    assignLabels([i1], []);
    const { projection, snapshot, files } = bindDraft(
      { quotes: [], text: "ab [image 1] cd" },
      [i1],
      plainSerialize,
    );
    const restored = { quotes: [], text: snapshot.text };
    const { attachments } = attachmentOffsets(composerPartsFromProjection(projection, files));
    const placed = placeTokens(restored, plainSerialize, attachments);
    expect(placed.text).toBe("ab [image 1] cd");
  });

  it("main-brain probe: a quote plus a tail starting with '\\n' restores the token in place, not one char early", () => {
    const i1 = img("1.png");
    assignLabels([i1], []); // the only new file → labelled [image 1]
    const original: ReplyDraft = {
      quotes: [{ id: "q1", before: "x", text: "q" }],
      text: "\nhi [image 1] yo",
    };
    const bound = bindDraft(original, [i1], serializeReplyDraft);
    const sentText = serializeReplyDraft(bound.snapshot);
    const storedReplyDraft = {
      version: 1 as const,
      quotes: bound.snapshot.quotes.map(({ before, text }) => ({ before, text })),
      text: bound.snapshot.text,
    };
    const restored = restoreReplyDraft(sentText, storedReplyDraft);
    const { attachments } = attachmentOffsets(
      composerPartsFromProjection(bound.projection, bound.files),
    );
    const placed = placeTokens(restored, serializeReplyDraft, attachments);
    expect(placed.text).toBe("\nhi [image 1] yo");
  });

  it("byte-exact for a quoted draft across bind, restore, and placeTokens", () => {
    const i1 = img("a.png");
    const f1 = pdf("a.pdf");
    assignLabels([i1, f1], []);

    const original: ReplyDraft = {
      quotes: [{ id: "q1", before: "before [image 1] ", text: "quoted source" }],
      text: "tail [file 1] ",
    };
    const bound = bindDraft(original, [i1, f1], serializeReplyDraft);
    const sentText = serializeReplyDraft(bound.snapshot);
    const storedReplyDraft = {
      version: 1 as const,
      quotes: bound.snapshot.quotes.map(({ before, text }) => ({ before, text })),
      text: bound.snapshot.text,
    };

    const restored = restoreReplyDraft(sentText, storedReplyDraft);
    const parts = composerPartsFromProjection(bound.projection, bound.files);
    const { attachments } = attachmentOffsets(parts);

    const placed = placeTokens(restored, serializeReplyDraft, attachments);
    expect(placed.quotes[0].before).toBe("before [image 1] ");
    expect(placed.text).toBe("tail [file 1] ");
    expect(placed.quotes[0].text).toBe("quoted source");
  });

  it("N1 — fallback attachments (offset inside quoted source) append in original order", () => {
    const i1 = img("1.png");
    const i2 = img("2.png");
    assignLabels([i1, i2], []);
    const restored: ReplyDraft = { quotes: [{ id: "q1", before: "", text: "q" }], text: "tail" };
    const placed = placeTokens(
      restored,
      serializeReplyDraft,
      [
        { offset: 1, file: i1 },
        { offset: 1, file: i2 },
      ], // offset 1 falls inside the quoted source "q", outside every field range
    );
    expect(placed.text).toBe("tail [image 1] [image 2] ");
  });
});
