import { describe, expect, it } from "vitest";
import { buildDesignModePrompt, formatElementEvidence, sanitizeField } from "../../shared/evidence";
import { canPickUrl, pageUrl, parseSelection, randomId, serializeItem } from "../src/protocol";
import type { ItemV1 } from "../src/protocol";
import { selection } from "./fixtures";
import { readFileSync } from "node:fs";

describe("bounded untrusted evidence", () => {
  it("retains the exact desktop message bytes after the shared extraction", () => {
    expect(
      buildDesignModePrompt(
        {
          tag: "button",
          selector: "button > span",
          text: 'Save\n"now"',
          pageUrl: "https://example.test/",
          prefix: 'before\n"',
          suffix: "after",
          ariaLabel: "Save",
          role: "button",
        },
        "line one\nline two",
      ),
    ).toBe(
      'line one\nline two\n\n---\n[Design Mode — modify this element in the browser preview]\nElement: <button>\nCSS selector: button > span\nPage: https://example.test/\nContext before: "before \\""\nContext after: "after"\nText: "Save "now""\nAria-label: "Save"\nRole: button\n---',
    );
  });
  it("uses the same sanitation with a Chrome-specific heading", () => {
    expect(
      formatElementEvidence(
        { tag: "div\u0000x", selector: "", prefix: "a\u2028b" },
        "[Web evidence]",
      ),
    ).toBe('[Web evidence]\nElement: <div x>\nCSS selector: unavailable\nContext before: "a b"');
    expect(sanitizeField("x".repeat(900))).toHaveLength(200);
  });
  it("drops URL credentials/query/fragment before the wire boundary", () => {
    expect(pageUrl("https://user:pass@example.test/a?token=hidden#secret")).toBe(
      "https://example.test/a",
    );
    expect(pageUrl("https://example.test")).toBe("https://example.test/");
  });
  it.each([
    "chrome://settings",
    "file:///opt/work/page.html",
    "data:text/html,x",
    "https://chromewebstore.google.com/detail/a",
    "https://chrome.google.com/webstore/a",
    "https://example.test/test.PDF",
  ])("rejects restricted URLs: %s", (url) => {
    expect(canPickUrl(url)).toBe(false);
  });
  it("strictly rejects extra fields, malformed geometry and page-originated full URL consent", () => {
    expect(() => parseSelection({ ...selection, server: "https://attacker.test" })).toThrow(
      "Unexpected",
    );
    expect(() =>
      parseSelection({
        ...selection,
        target: { ...selection.target, outerHTML: "x" },
      }),
    ).toThrow("Unexpected");
    expect(() => parseSelection({ ...selection, rect: { ...selection.rect, width: NaN } })).toThrow(
      "geometry",
    );
    expect(() =>
      parseSelection({
        ...selection,
        page: { ...selection.page, include_query_fragment: true },
      }),
    ).toThrow("consent");
    expect(() =>
      parseSelection({
        ...selection,
        page: { ...selection.page, url: "https://other.test/" },
      }),
    ).toThrow("Page changed");
  });
  it("generates trusted capture IDs independently of page data", () => {
    expect(randomId()).toMatch(/^[0-9a-f]{32}$/);
    expect(randomId()).not.toBe(randomId());
  });
});

describe("server ItemV1 wire contract", () => {
  it("round-trips the maintained cross-language batch fixture's ItemV1 literally", () => {
    const golden = JSON.parse(
      readFileSync(new URL("./item-v1.golden.json", import.meta.url), "utf8"),
    );
    expect(golden.version).toBe(1);
    expect(JSON.parse(serializeItem(golden.items[0]))).toEqual(golden.items[0]);
    expect(golden.items[0].note).toBe("Keep this user instruction\nverbatim.");
  });
  const item: ItemV1 = {
    capture_id: "a".repeat(32),
    captured_at: "2026-10-22T01:02:03.000Z",
    kind: "element",
    note: "Keep\nverbatim",
    page: selection.page,
    target: selection.target,
    geometry: {
      rect: selection.rect,
      viewport: { width: 1000, height: 800 },
      bitmap_scale: { x: 1.5, y: 1.5 },
    },
  };
  it("serializes the strict server keys, preserving user notes", () => {
    expect(JSON.parse(serializeItem(item))).toEqual({
      capture_id: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      captured_at: "2026-10-22T01:02:03.000Z",
      kind: "element",
      note: "Keep\nverbatim",
      page: {
        url: "https://example.test/page",
        title: "Demo",
        include_query_fragment: false,
      },
      target: {
        tag: "button",
        selector: "html:nth-of-type(1) > body:nth-of-type(1) > button:nth-of-type(1)",
        quote: "Save",
        prefix: "Before",
        suffix: "After",
        aria_label: "Save changes",
        role: "button",
      },
      geometry: {
        rect: { x: 10.2, y: 20.1, width: 20.2, height: 10.2 },
        viewport: { width: 1000, height: 800 },
        bitmap_scale: { x: 1.5, y: 1.5 },
      },
    });
  });
  it("rejects incomplete drafts, overlong notes and arbitrary event content", () => {
    expect(() => serializeItem({ ...item, note: " " })).toThrow("item");
    expect(() => serializeItem({ ...item, note: "x".repeat(4001) })).toThrow("item");
    expect(() => serializeItem({ ...item, role: "assistant" } as ItemV1)).toThrow("Unexpected");
    expect(() =>
      serializeItem({
        ...item,
        screenshot: { file_id: "external", sha256: "0".repeat(64) },
      }),
    ).toThrow("reference");
    expect(() =>
      serializeItem({
        ...item,
        geometry: { ...item.geometry, bitmap_scale: { x: 9, y: 1 } },
      }),
    ).toThrow("geometry");
  });
  it("serializes sanitized evidence, never the prevalidation URL or field value", () => {
    const result = JSON.parse(
      serializeItem({
        ...item,
        page: { ...item.page, url: "https://user:pass@example.test/page?private=1#x" },
        target: { ...item.target, quote: "Save\nnow" },
      }),
    );
    expect(result.page.url).toBe("https://example.test/page");
    expect(result.target.quote).toBe("Save now");
    expect(result.note).toBe("Keep\nverbatim");
  });
});
