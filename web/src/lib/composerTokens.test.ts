import { describe, expect, it, vi } from "vitest";

import { composerPartsFromProjection, composerPartsToText } from "./composerContent";
import { restoreReplyDraft, serializeReplyDraft, type ReplyDraft } from "./replyDraft";
import {
  applyFieldEdit,
  assignLabels,
  attachmentBadges,
  attachmentOffsets,
  bindDraft,
  deleteTokenAt,
  focusTokenInComposer,
  insertTokenFiles,
  labelOf,
  placeTokens,
  planComposerSend,
  reconcileLabels,
  removeFileTokens,
  removeTokens,
  restoreRecordDraft,
  stripPlaceholders,
  stripPlaceholdersFromStoredDraft,
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

describe("stripPlaceholdersFromStoredDraft", () => {
  it("keeps a legacy quoted draft valid: both sides lose the placeholder together", () => {
    const legacy = {
      version: 1 as const,
      quotes: [{ before: "a￼", text: "q￼" }],
      text: "b￼",
    };
    const text = serializeReplyDraft(legacy);
    expect(restoreReplyDraft(text, legacy).quotes).toHaveLength(1);

    const stripped = stripPlaceholdersFromStoredDraft(legacy);
    const restored = restoreReplyDraft(stripPlaceholders(text), stripped);
    expect(restored.quotes).toHaveLength(1);
    expect(restored.quotes[0]!.before).toBe("a");
    expect(restored.text).toBe("b");
  });
});

describe("applyFieldEdit", () => {
  it("falls back to a splice + caret when no field is focused", () => {
    const file = img();
    assignLabels([file], []);
    const { insert, replaceStart, replaceEnd } = tokenInsertion("abcd", 2, 2, [file]);
    const result = applyFieldEdit(null, "abcd", { start: replaceStart, end: replaceEnd }, insert);
    expect(result.applied).toBe(false);
    expect(result.text).toBe("ab [image 1] cd");
    expect(result.caret).toBe("ab [image 1] ".length);
  });

  it("deletes a range by splicing when the browser has no execCommand", () => {
    const result = applyFieldEdit(null, "ab [image 1] cd", { start: 2, end: 13 }, "");
    expect(result.applied).toBe(false);
    expect(result.text).toBe("abcd");
    expect(result.caret).toBe(2);
  });

  it("uses execCommand on a live field so the textarea's undo stack keeps working", () => {
    const execCommand = vi.fn(() => true);
    document.execCommand = execCommand;
    const field = document.createElement("textarea");
    try {
      field.value = "abcd";
      document.body.append(field);
      const result = applyFieldEdit(field, "abcd", { start: 2, end: 2 }, " [image 1] ");
      expect(execCommand).toHaveBeenCalledWith("insertText", false, " [image 1] ");
      expect(field.selectionStart).toBe(2);
      expect(field.selectionEnd).toBe(2);
      expect(result.applied).toBe(true);
    } finally {
      field.remove();
      delete (document as { execCommand?: unknown }).execCommand;
    }
  });
});

describe("insertTokenFiles", () => {
  it("T1 — inserts at the field's saved caret", () => {
    const file = img();
    assignLabels([file], []);
    const field = document.createElement("textarea");
    field.value = "abcd";
    field.setSelectionRange(2, 2);
    const result = insertTokenFiles(field, "abcd", [file], true);
    expect(result.text).toBe("ab [image 1] cd");
    expect(result.applied).toBe(false);
  });

  it("appends at the end of a field the user never focused", () => {
    const file = img();
    assignLabels([file], []);
    const field = document.createElement("textarea");
    field.value = "abcd";
    field.setSelectionRange(0, 0);
    expect(insertTokenFiles(field, "abcd", [file], false).text).toBe("abcd [image 1] ");
    expect(insertTokenFiles(null, "abcd", [file], false).text).toBe("abcd [image 1] ");
  });
});

describe("deleteTokenAt", () => {
  it("T12 — a collapsed Backspace after a live token takes the token and its space", () => {
    const file = img();
    assignLabels([file], []);
    const text = "x [image 1] y";
    expect(deleteTokenAt(null, text, 11, "Backspace", [file])).toMatchObject({
      applied: false,
      text: "x y",
    });
  });

  it("is null away from every live token", () => {
    const file = img();
    assignLabels([file], []);
    expect(deleteTokenAt(null, "x [image 1] y", 2, "Backspace", [file])).toBeNull();
    expect(deleteTokenAt(null, "x [image 9]", 2, "Delete", [file])).toBeNull();
  });
});

describe("focusTokenInComposer", () => {
  it("T23 — focuses the tail and puts the caret just after the token", () => {
    const file = img();
    assignLabels([file], []);
    const tail = document.createElement("textarea");
    tail.value = "hi [image 1] there";
    document.body.append(tail);
    try {
      expect(focusTokenInComposer(tokenText(labelOf(file)!), tail, [])).toBe(true);
      expect(document.activeElement).toBe(tail);
      expect(tail.selectionStart).toBe("hi [image 1]".length);
    } finally {
      tail.remove();
    }
  });

  it("falls into a quote's before field when the token lives there", () => {
    const file = img();
    assignLabels([file], []);
    const tail = document.createElement("textarea");
    tail.value = "tail";
    const quoteField = document.createElement("textarea");
    quoteField.setAttribute("aria-label", "Reply text before quote 1");
    quoteField.value = "q [image 1] x";
    document.body.append(tail, quoteField);
    try {
      expect(
        focusTokenInComposer(tokenText(labelOf(file)!), tail, [{ before: quoteField.value }]),
      ).toBe(true);
      expect(document.activeElement).toBe(quoteField);
      expect(quoteField.selectionStart).toBe("q [image 1]".length);
    } finally {
      tail.remove();
      quoteField.remove();
    }
  });
});

describe("planComposerSend", () => {
  it("T5 — no token bound: upstream's text/files pair, no parts", () => {
    const i1 = img();
    assignLabels([i1], []);
    const plan = planComposerSend({ quotes: [], text: "hello" }, [i1], "", "hello");
    expect(plan).toMatchObject({ text: "hello", sendFiles: [i1], parts: undefined });
    expect(plan.replyDraft).toBeUndefined();
  });

  it("T24 — tokens bind in order: paste i1, text, paste i2", () => {
    const i1 = img("1.png");
    const i2 = img("2.png");
    assignLabels([i1, i2], []);
    const plan = planComposerSend(
      { quotes: [], text: "[image 1] between [image 2] " },
      [i1, i2],
      "",
      "[image 1] between [image 2]",
    );
    expect(plan.text).toBe(" between ");
    expect(plan.parts).toEqual([
      { type: "attachment", file: i1 },
      { type: "text", text: " between " },
      { type: "attachment", file: i2 },
    ]);
    expect(plan.sendFiles).toEqual([i1, i2]);
  });

  it("T6 — an unreferenced file is still sent, appended at the end", () => {
    const i1 = img("1.png");
    const f1 = pdf();
    assignLabels([i1, f1], []);
    const plan = planComposerSend({ quotes: [], text: "a [image 1]" }, [i1, f1], "", "a [image 1]");
    expect(plan.text).toBe("a ");
    expect(composerPartsToText(plan.parts!)).toBe("a ");
    expect(plan.sendFiles).toEqual([i1, f1]);
  });

  it("keeps a quoted draft's reply snapshot and prepends the mention preamble", () => {
    const i1 = img();
    assignLabels([i1], []);
    const draft: ReplyDraft = {
      quotes: [{ id: "q1", before: "[image 1] ", text: "quoted" }],
      text: "tail",
    };
    const plan = planComposerSend(draft, [i1], "@x ", "tail");
    expect(plan.parts?.[0]).toEqual({ type: "text", text: "@x " });
    // The token's own trailing space stays in the authored text.
    expect(plan.replyDraft?.quotes[0]!.before).toBe("@x  ");
    // `restoreReplyDraft` accepts the snapshot only when it serializes to the
    // text that was sent.
    expect(serializeReplyDraft(plan.replyDraft!)).toBe(plan.text);
  });

  it("F1 — a token inside the generated mention preamble never binds", () => {
    const i1 = img("assets/[image 1].png");
    assignLabels([i1], []);
    const preamble = "[Attached file: assets/[image 1].png]\n\n";
    const plan = planComposerSend({ quotes: [], text: "inspect" }, [i1], preamble, "inspect");
    expect(plan.text).toBe("[Attached file: assets/[image 1].png]\n\ninspect");
    expect(plan.parts).toBeUndefined();
    expect(plan.sendFiles).toEqual([i1]);
  });

  it("F1 — the quote snapshot gets the preamble after binding, so it still serializes to the sent text", () => {
    const i1 = img("assets/[image 1].png");
    assignLabels([i1], []);
    const preamble = "[Attached file: assets/[image 1].png]\n\n";
    const draft: ReplyDraft = {
      quotes: [{ id: "q1", before: "[image 1] ", text: "quoted" }],
      text: "tail",
    };
    const plan = planComposerSend(draft, [i1], preamble, "tail");
    expect(plan.parts?.[0]).toEqual({ type: "text", text: preamble });
    // The authored token bound; the preamble's lookalike stayed literal.
    expect(plan.text).toBe(`${preamble} \n\n> quoted\n\ntail`);
    expect(plan.replyDraft?.quotes[0]!.before).toBe(`${preamble} `);
    expect(serializeReplyDraft(plan.replyDraft!)).toBe(plan.text);
  });
});

describe("planComposerSend preamble round-trip (F1 residual)", () => {
  // The preamble carries a literal `[image 1]` inside a generated file path.
  // `planComposerSend` must bind the authored token while leaving that
  // lookalike alone, AND derive text/parts/replyDraft from the single
  // preamble-prefixed draft: `joinParagraphs` picks each separator from the
  // joined-so-far trailing newlines, so gluing the preamble on after the bind
  // shifts the separators and the snapshot no longer serializes to the text
  // that was sent (recall then restores zero quotes).
  function preambleWithToken(file: File): string {
    return `[Attached file: assets/${tokenText(labelOf(file)!)}.png]\n\n`;
  }

  function expectSendRoundTrip(
    draft: ReplyDraft,
    files: File[],
    preamble: string,
    trimmed: string,
  ): void {
    const plan = planComposerSend(draft, files, preamble, trimmed);
    // The authored token bound (nothing left literal) and every file ships.
    expect(plan.parts).toBeDefined();
    expect(plan.sendFiles).toEqual(files);
    // The generated lookalike path survived verbatim in the sent text.
    if (preamble) expect(plan.text.startsWith(preamble)).toBe(true);
    // Snapshot and parts both reproduce the exact text that was sent.
    expect(serializeReplyDraft(plan.replyDraft!)).toBe(plan.text);
    expect(composerPartsToText(plan.parts!)).toBe(plan.text);
    // The stored pair validates, so recalling the message keeps its quotes.
    expect(restoreReplyDraft(plan.text, plan.replyDraft).quotes).toHaveLength(draft.quotes.length);
  }

  it.each(["with preamble", "without preamble"])(
    "token followed by a newline in quotes[0].before (%s)",
    (variant) => {
      const i1 = img("assets/[image 1].png");
      assignLabels([i1], []);
      const preamble = variant === "with preamble" ? preambleWithToken(i1) : "";
      expectSendRoundTrip(
        { quotes: [{ id: "q1", before: "[image 1]\n", text: "quote" }], text: "tail" },
        [i1],
        preamble,
        "tail",
      );
    },
  );

  it.each(["with preamble", "without preamble"])(
    "bare token in quotes[0].before, stripping to empty (%s)",
    (variant) => {
      const i1 = img("assets/[image 1].png");
      assignLabels([i1], []);
      const preamble = variant === "with preamble" ? preambleWithToken(i1) : "";
      expectSendRoundTrip(
        { quotes: [{ id: "q1", before: "[image 1]", text: "quote" }], text: "tail" },
        [i1],
        preamble,
        "tail",
      );
    },
  );

  it.each(["with preamble", "without preamble"])(
    "token followed by two newlines in quotes[0].before (%s)",
    (variant) => {
      const i1 = img("assets/[image 1].png");
      assignLabels([i1], []);
      const preamble = variant === "with preamble" ? preambleWithToken(i1) : "";
      expectSendRoundTrip(
        { quotes: [{ id: "q1", before: "[image 1]\n\n", text: "quote" }], text: "tail" },
        [i1],
        preamble,
        "tail",
      );
    },
  );

  it.each(["with preamble", "without preamble"])(
    "empty before with the token in the tail instead (%s)",
    (variant) => {
      const i1 = img("assets/[image 1].png");
      assignLabels([i1], []);
      const preamble = variant === "with preamble" ? preambleWithToken(i1) : "";
      expectSendRoundTrip(
        { quotes: [{ id: "q1", before: "", text: "quote" }], text: "tail [image 1]" },
        [i1],
        preamble,
        "tail [image 1]",
      );
    },
  );

  it.each(["with preamble", "without preamble"])(
    "multi-quote draft whose middle before strips to empty (%s)",
    (variant) => {
      const i1 = img("assets/[image 1].png");
      const i2 = img("b.png");
      assignLabels([i1, i2], []);
      const preamble = variant === "with preamble" ? preambleWithToken(i1) : "";
      expectSendRoundTrip(
        {
          quotes: [
            { id: "q1", before: "[image 1]\n", text: "one" },
            { id: "q2", before: "[image 2]", text: "two" },
            { id: "q3", before: "after ", text: "three" },
          ],
          text: "tail",
        },
        [i1, i2],
        preamble,
        "tail",
      );
    },
  );

  it("a preamble holding a literal token never binds when nothing authored does", () => {
    const i1 = img("assets/[image 1].png");
    assignLabels([i1], []);
    const preamble = preambleWithToken(i1);
    const draft: ReplyDraft = {
      quotes: [{ id: "q1", before: "hello ", text: "quoted" }],
      text: "tail",
    };
    const plan = planComposerSend(draft, [i1], preamble, "tail");
    expect(plan.parts).toBeUndefined();
    expect(plan.sendFiles).toEqual([i1]);
    expect(plan.text).toBe(serializeReplyDraft(plan.replyDraft!));
    expect(plan.text.startsWith(preamble)).toBe(true);
    expect(restoreReplyDraft(plan.text, plan.replyDraft).quotes).toHaveLength(1);
  });
});

describe("planComposerSend with a U+FFFC in the generated preamble (F1 off-by-N)", () => {
  // The mentioned workspace path is interpolated verbatim, so a real U+FFFC
  // can land in the preamble. `bindDraft` matches on the placeholder-stripped
  // field, so the generated boundary must be measured there too — passing the
  // raw `preamble.length` overshoots by one char per U+FFFC and swallows an
  // authored token sitting just after the boundary.
  const FFFC = "\uFFFC";
  const preamble = `[Attached file: src/${FFFC}note.ts]\n\n`;

  it("F1 — both authored tokens bind in order instead of the first being skipped", () => {
    const i1 = img("1.png");
    const i2 = img("2.png");
    assignLabels([i1, i2], []);
    // The trigger: one stripped char, so the raw boundary overshoots by one.
    expect(preamble.length - stripPlaceholders(preamble).length).toBe(1);

    const draft: ReplyDraft = {
      quotes: [{ id: "q1", before: "[image 1] then [image 2]", text: "quoted" }],
      text: "tail",
    };
    const plan = planComposerSend(draft, [i1, i2], preamble, "tail");

    expect(plan.parts).toBeDefined();
    expect(plan.sendFiles).toEqual([i1, i2]);
    const { attachments } = attachmentOffsets(plan.parts!);
    const boundLabels = attachments.map(({ file }) => tokenText(labelOf(file)!));
    expect(boundLabels).toEqual(["[image 1]", "[image 2]"]);
    expect(plan.text).not.toContain("[image 1]");
    expect(plan.text).not.toContain("[image 2]");
    // The generated path itself survives, minus the stripped U+FFFC.
    expect(plan.text.startsWith(stripPlaceholders(preamble))).toBe(true);
    expect(composerPartsToText(plan.parts!)).toBe(plan.text);
    expect(serializeReplyDraft(plan.replyDraft!)).toBe(plan.text);
  });

  it("F1 — a single authored token right at the boundary still binds", () => {
    const i1 = img("1.png");
    assignLabels([i1], []);
    const draft: ReplyDraft = {
      quotes: [{ id: "q1", before: "[image 1] then", text: "quoted" }],
      text: "tail",
    };
    const plan = planComposerSend(draft, [i1], preamble, "tail");
    expect(plan.parts).toBeDefined();
    expect(plan.text).not.toContain("[image 1]");
    expect(composerPartsToText(plan.parts!)).toBe(plan.text);
    expect(serializeReplyDraft(plan.replyDraft!)).toBe(plan.text);
  });
});

describe("removeFileTokens", () => {
  it("T22 — removing a tile drops its token from every authored field", () => {
    const f = img();
    assignLabels([f], []);
    const draft: ReplyDraft = {
      quotes: [{ id: "q1", before: "a [image 1] b", text: "quoted" }],
      text: "tail [image 1]",
    };
    expect(removeFileTokens(draft, 0, [f])).toEqual([
      { fieldId: "q1", text: "a b" },
      // The token's own trailing space is not there to remove.
      { fieldId: null, text: "tail " },
    ]);
  });

  it("reports nothing for a file with no token", () => {
    const f = img();
    assignLabels([f], []);
    expect(removeFileTokens({ quotes: [], text: "plain" }, 0, [f])).toEqual([]);
  });
});

describe("restoreRecordDraft", () => {
  it("T14 — failed quoted send restores the quote and puts the token back in its field", () => {
    const i1 = img();
    assignLabels([i1], []);
    const original: ReplyDraft = {
      quotes: [{ id: "q1", before: "hi [image 1] ", text: "quoted" }],
      text: "tail",
    };
    const bound = bindDraft(original, [i1], serializeReplyDraft);
    const record = {
      text: serializeReplyDraft(bound.snapshot),
      files: [i1],
      composerParts: composerPartsFromProjection(bound.projection, bound.files),
      replyDraft: {
        version: 1 as const,
        quotes: bound.snapshot.quotes.map(({ before, text }) => ({ before, text })),
        text: bound.snapshot.text,
      },
    };
    const restored = restoreRecordDraft(record);
    expect(restored.quotes).toHaveLength(1);
    expect(restored.quotes[0]!.before).toBe("hi [image 1] ");
    expect(restored.text).toBe("tail");
  });

  it("T15 — a legacy record without parts restores plain text", () => {
    const restored = restoreRecordDraft({ text: "hello", files: [] });
    expect(restored).toEqual({ quotes: [], text: "hello" });
  });

  it("T16 — colliding labels in a recovered draft relabel the second file", () => {
    const first = img("a.png");
    const second = img("b.png");
    assignLabels([first], []);
    assignLabels([second], []);
    const restored = restoreRecordDraft({
      text: "[image 1] and [image 1]",
      files: [first, second],
    });
    expect(tokenText(labelOf(second)!)).toBe("[image 2]");
    expect(restored.text).toBe("[image 1] and [image 2]");
  });

  it("F3 — a quote whose placeholder-only field is stripped keeps its quotes", () => {
    const saved = {
      version: 1 as const,
      quotes: [{ before: "￼", text: "quote" }],
      text: "tail",
    };
    // The pair was consistent before stripping; stripping `before` to "" shifts
    // joinParagraphs' separator, so the text must be re-derived from the
    // cleaned snapshot or restoreReplyDraft rejects it.
    const restored = restoreRecordDraft({
      text: serializeReplyDraft(saved),
      files: [],
      replyDraft: saved,
    });
    expect(restored.quotes).toHaveLength(1);
    expect(restored.quotes[0]!.before).toBe("");
    expect(restored.quotes[0]!.text).toBe("quote");
    expect(restored.text).toBe("tail");
  });

  it("F3 — a saved pair that was already invalid stays plain text", () => {
    const saved = {
      version: 1 as const,
      quotes: [{ before: "￼", text: "quote" }],
      text: "tail",
    };
    const restored = restoreRecordDraft({
      text: "not the snapshot's text",
      files: [],
      replyDraft: saved,
    });
    expect(restored).toEqual({ quotes: [], text: "not the snapshot's text" });
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
