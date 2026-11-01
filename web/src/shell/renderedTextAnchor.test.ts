import { describe, expect, it } from "vitest";
import {
  captureRenderedTextAnchor,
  decodeRenderedTextAnchor,
  encodeRenderedTextAnchor,
  resolveRenderedTextAnchor,
} from "./renderedTextAnchor";

describe("rendered text anchors", () => {
  it("keeps the second occurrence and full selected text", () => {
    const content = "Alpha Code | Beta Code";
    const anchor = captureRenderedTextAnchor("html", content, 18, 22);
    expect(anchor).toMatchObject({
      v: 1,
      kind: "html",
      text: "Code",
      occ: 1,
      prefix: "Alpha Code | Beta ",
    });
    expect(decodeRenderedTextAnchor(encodeRenderedTextAnchor(anchor!)!)).toEqual(anchor);
    expect(resolveRenderedTextAnchor(content, anchor!)).toEqual({ start: 18, end: 22 });
  });

  it("returns unresolved when context is duplicated or stale even with an occurrence", () => {
    const block = "x".repeat(40) + "Code" + "y".repeat(40);
    const content = block + block;
    const anchor = captureRenderedTextAnchor(
      "markdown",
      content,
      block.length + 40,
      block.length + 44,
    )!;
    expect(anchor.occ).toBe(1);
    expect(resolveRenderedTextAnchor(content, anchor)).toBeNull();
    expect(
      resolveRenderedTextAnchor("Alpha Code | Gamma Code", {
        v: 1,
        kind: "html",
        text: "Code",
        occ: 1,
        prefix: "Beta ",
        suffix: "",
      }),
    ).toBeNull();
  });

  it("rejects malformed typed data and excess payloads", () => {
    for (const contentHash of ["invalid", "0:0", 42, null]) {
      expect(
        decodeRenderedTextAnchor(
          "__text__" +
            JSON.stringify({
              v: 1,
              kind: "markdown",
              text: "Code",
              occ: 0,
              prefix: "",
              suffix: "",
              source: { start_index: 0, end_index: 4, contentHash },
            }),
        ),
      ).toBeNull();
    }
    expect(
      decodeRenderedTextAnchor(
        '__text__{"v":1,"kind":"html","text":"Code","occ":-1,"prefix":"","suffix":""}',
      ),
    ).toBeNull();
    expect(
      decodeRenderedTextAnchor(
        '__text__{"v":1,"kind":"html","text":"Code","occ":0,"prefix":"","suffix":"","source":{"start_index":2,"end_index":1}}',
      ),
    ).toBeNull();
    expect(
      encodeRenderedTextAnchor({
        v: 1,
        kind: "html",
        text: "界".repeat(11000),
        occ: 0,
        prefix: "",
        suffix: "",
      }),
    ).toBeNull();
  });

  it("cuts emoji context at scalar boundaries and rejects unpaired surrogates", () => {
    const before = "😀" + "a".repeat(31);
    const after = "b".repeat(31) + "😀";
    const content = before + "Code" + after;
    const anchor = captureRenderedTextAnchor(
      "markdown",
      content,
      before.length,
      before.length + 4,
    )!;
    expect(anchor.prefix).toBe("a".repeat(31));
    expect(anchor.suffix).toBe("b".repeat(31));
    expect(resolveRenderedTextAnchor(content, anchor)).toEqual({
      start: before.length,
      end: before.length + 4,
    });
    expect(
      decodeRenderedTextAnchor("__text__" + JSON.stringify({ ...anchor, prefix: "\ud800" })),
    ).toBeNull();
    expect(
      decodeRenderedTextAnchor("__text__" + JSON.stringify({ ...anchor, text: "Code\udc00" })),
    ).toBeNull();
  });
});
