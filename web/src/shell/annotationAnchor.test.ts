import { describe, expect, it } from "vitest";
import {
  ELEMENT_ANCHOR_PREFIX,
  clampElementAnchor,
  decodeElementAnchor,
  elementAnchorLabel,
  elementAnchorRange,
  encodeElementAnchor,
  isElementAnchor,
  type ElementAnchorV1,
} from "./annotationAnchor";

const PAGE_URL = "http://localhost:6767/v1/artifacts/tok/reports/q3.html";

function encodedBytes(anchor: ElementAnchorV1): number {
  return new TextEncoder().encode(ELEMENT_ANCHOR_PREFIX + JSON.stringify(anchor)).length;
}

function makeAnchor(overrides: Partial<ElementAnchorV1> = {}): ElementAnchorV1 {
  return {
    v: 1,
    kind: "element",
    page: { url: PAGE_URL, title: "Q3 report", vw: 1280, vh: 800, sx: 0, sy: 120, dpr: 2 },
    target: {
      label: "div.filter-menu > button.option",
      css: "#report > div.filter-menu > button.option",
      xpath: "/html/body/div[1]/button[2]",
      quote: { exact: "Filter", prefix: ">", suffix: "<" },
      fingerprint: "button.option|Filter",
      neighborText: "Apply",
      tag: "button",
      id: "filter",
      role: "button",
      ariaLabel: "Filter",
      text: "Filter",
    },
    rect: { x: 12.5, y: 300.25, w: 100, h: 32 },
    region: null,
    selectedText: "",
    console: [{ level: "error", message: "boom", ts: 1_700_000_000_000 }],
    network: [
      {
        method: "GET",
        url: "http://localhost:6767/v1/artifacts/tok/missing.png",
        status: 404,
        ts: 1_700_000_000_000,
      },
    ],
    screenshot: null,
    ...overrides,
  };
}

describe("decodeElementAnchor / encodeElementAnchor", () => {
  it("round-trips a valid v1 anchor", () => {
    const anchor = makeAnchor();
    const decoded = decodeElementAnchor(encodeElementAnchor(anchor));
    expect(decoded).toEqual(anchor);
    expect(decoded).not.toBe(anchor);
  });

  it("returns null for prefix-less content and non-strings", () => {
    expect(decodeElementAnchor(JSON.stringify(makeAnchor()))).toBeNull();
    expect(decodeElementAnchor(null)).toBeNull();
    expect(decodeElementAnchor(undefined)).toBeNull();
    expect(isElementAnchor(`${ELEMENT_ANCHOR_PREFIX}{}`)).toBe(true);
    expect(isElementAnchor("plain anchor")).toBe(false);
    expect(isElementAnchor(null)).toBe(false);
  });

  it("returns null for a version other than 1", () => {
    const encoded = ELEMENT_ANCHOR_PREFIX + JSON.stringify({ ...makeAnchor(), v: 2 });
    expect(decodeElementAnchor(encoded)).toBeNull();
  });

  it("returns null for a kind outside the enum", () => {
    const encoded = ELEMENT_ANCHOR_PREFIX + JSON.stringify({ ...makeAnchor(), kind: "bogus" });
    expect(decodeElementAnchor(encoded)).toBeNull();
  });

  it("returns null for malformed JSON and non-object payloads", () => {
    expect(decodeElementAnchor(`${ELEMENT_ANCHOR_PREFIX}{nope`)).toBeNull();
    expect(decodeElementAnchor(`${ELEMENT_ANCHOR_PREFIX}42`)).toBeNull();
  });
});

describe("clampElementAnchor caps", () => {
  it("caps a 10,000-char label to 200 chars ending in …", () => {
    const anchor = makeAnchor({
      target: { ...makeAnchor().target, label: "x".repeat(10_000) },
    });
    const clamped = clampElementAnchor(anchor);
    expect(clamped?.target.label).toHaveLength(200);
    expect(clamped?.target.label.endsWith("…")).toBe(true);
    expect(clamped?.target.label.slice(0, 199)).toBe("x".repeat(199));
  });

  it("coerces newlines in a label to a single line", () => {
    const anchor = makeAnchor({
      target: { ...makeAnchor().target, label: "first\nsecond" },
    });
    const clamped = clampElementAnchor(anchor);
    expect(clamped?.target.label).toBe("first second");
  });

  it("keeps the newest 50 console entries", () => {
    const console = Array.from({ length: 60 }, (_, i) => ({
      level: "error" as const,
      message: `e${i}`,
      ts: i,
    }));
    const clamped = clampElementAnchor(makeAnchor({ console }));
    expect(clamped?.console).toHaveLength(50);
    expect(clamped?.console[0]!.message).toBe("e10");
    expect(clamped?.console[49]!.message).toBe("e59");
  });

  it("strips query, hash and credentials from page and network urls", () => {
    const anchor = makeAnchor({
      page: {
        ...makeAnchor().page,
        url: `http://user:pass@localhost:6767/v1/artifacts/tok/reports/q3.html?v=1#top`,
      },
      network: [
        {
          method: "GET",
          url: "https://bucket.example.com/data.json?token=secret#frag",
          status: 200,
          ts: 1,
        },
      ],
    });
    const clamped = clampElementAnchor(anchor);
    expect(clamped?.page.url).toBe(PAGE_URL);
    expect(clamped?.network[0]!.url).toBe("https://bucket.example.com/data.json");
  });

  it("drops a javascript: url to an empty string", () => {
    const anchor = makeAnchor({
      page: { ...makeAnchor().page, url: "javascript:alert(1)" },
    });
    expect(clampElementAnchor(anchor)?.page.url).toBe("");
  });

  it("rejects a NaN rect entirely", () => {
    const anchor = makeAnchor({ rect: { x: Number.NaN, y: 0, w: 1, h: 1 } });
    expect(clampElementAnchor(anchor)).toBeNull();
  });

  it("throws when the encoded anchor exceeds 32 KiB of UTF-8", () => {
    const anchor = makeAnchor({
      target: { ...makeAnchor().target, label: "x".repeat(40_000) },
    });
    expect(() => encodeElementAnchor(anchor)).toThrow("element anchor too large");
  });

  it("labels an anchor from its target path", () => {
    expect(elementAnchorLabel(makeAnchor())).toBe("div.filter-menu > button.option");
  });
});

describe("design §2.6 parity cases", () => {
  it("missing rect is invalid", () => {
    expect(clampElementAnchor({ v: 1, kind: "element" })).toBeNull();
    expect(decodeElementAnchor(`${ELEMENT_ANCHOR_PREFIX}{"v":1,"kind":"element"}`)).toBeNull();
  });

  it("rect 1e400 is invalid", () => {
    const encoded = `${ELEMENT_ANCHOR_PREFIX}{"v":1,"kind":"element","rect":{"x":1e400,"y":0,"w":1,"h":1}}`;
    expect(decodeElementAnchor(encoded)).toBeNull();
  });

  it("screenshot without width keeps the anchor with a null screenshot", () => {
    const clamped = clampElementAnchor({ ...makeAnchor(), screenshot: { file_id: "file_a" } });
    expect(clamped).not.toBeNull();
    expect(clamped?.screenshot).toBeNull();
  });

  it("lone high surrogate in a label is replaced with U+FFFD", () => {
    const label = "a".repeat(198) + "\uD800b";
    const clamped = clampElementAnchor(makeAnchor({ target: { ...makeAnchor().target, label } }));
    expect(clamped?.target.label).toContain("\uFFFD");
    expect(/[\uD800-\uDFFF]/.test(clamped!.target.label)).toBe(false);
  });

  it("emoji at the cap edge is not split", () => {
    const label = "a".repeat(198) + "😀b";
    const clamped = clampElementAnchor(makeAnchor({ target: { ...makeAnchor().target, label } }));
    expect(clamped?.target.label).toBe("a".repeat(198) + "…");
    expect(/[\uD800-\uDFFF]/.test(clamped!.target.label)).toBe(false);
    expect(decodeElementAnchor(encodeElementAnchor(clamped!))).toEqual(clamped);
  });

  it("60 console and 20 long network entries trim to fit 32 KiB", () => {
    const console = Array.from({ length: 60 }, (_, i) => ({
      level: "warn" as const,
      message: String(i).padEnd(500, "x"),
      ts: i,
    }));
    const network = Array.from({ length: 20 }, (_, i) => ({
      method: "GET",
      url: `https://example.com/${i}/${"a".repeat(1950)}`,
      status: 500,
      ts: i,
    }));
    const clamped = clampElementAnchor(makeAnchor({ console, network }));
    expect(clamped).not.toBeNull();
    expect(clamped!.console).toHaveLength(50);
    expect(clamped!.network.length).toBeLessThan(20);
    expect(clamped!.network.at(-1)!.ts).toBe(19);
    expect(encodedBytes(clamped!)).toBeLessThanOrEqual(32 * 1024);
    expect(decodeElementAnchor(encodeElementAnchor(clamped!))).not.toBeNull();
  });

  it("rejects an untrimmed oversized stored anchor but trims the same frame payload", () => {
    const console = Array.from({ length: 50 }, (_, i) => ({
      level: "error" as const,
      message: String(i).padEnd(500, "x"),
      ts: 1_700_000_000_000 + i,
    }));
    const network = Array.from({ length: 20 }, (_, i) => ({
      method: "GET",
      url: `https://example.com/${"a".repeat(2000)}`,
      status: 500,
      ts: 1_700_000_000_000 + i,
    }));
    const serialized = ELEMENT_ANCHOR_PREFIX + JSON.stringify(makeAnchor({ console, network }));
    expect(new TextEncoder().encode(serialized).length).toBeGreaterThan(32 * 1024);

    expect(decodeElementAnchor(serialized)).toBeNull();

    const trimmed = clampElementAnchor(JSON.parse(serialized.slice(ELEMENT_ANCHOR_PREFIX.length)));
    expect(trimmed).not.toBeNull();
    expect(trimmed!.console.length + trimmed!.network.length).toBeLessThan(70);
    expect(encodedBytes(trimmed!)).toBeLessThanOrEqual(32 * 1024);
  });

  /** A canonical stored anchor whose payload (without the prefix) is exactly
   * `target` UTF-8 bytes: the padded urls stay inside the codec's caps. */
  function storedAnchorOfExactPayloadBytes(target: number): ElementAnchorV1 {
    const anchor = makeAnchor({
      console: Array.from({ length: 50 }, (_, i) => ({
        level: "warn" as const,
        message: "c".repeat(500),
        ts: i,
      })),
      network: Array.from({ length: 20 }, (_, i) => ({
        method: "GET",
        url: `https://example.com/${"a".repeat(1980)}`,
        status: 500,
        ts: i,
      })),
    });
    const excess = JSON.stringify(anchor).length - target;
    const per = Math.floor(excess / anchor.network.length);
    const remainder = excess % anchor.network.length;
    anchor.network.forEach((entry, index) => {
      entry.url = entry.url.slice(0, entry.url.length - (per + (index < remainder ? 1 : 0)));
    });
    expect(JSON.stringify(anchor).length).toBe(target);
    return anchor;
  }

  it("decodes a stored payload of exactly 32 KiB and rejects one byte more", () => {
    const atCap = storedAnchorOfExactPayloadBytes(32 * 1024);
    const stored = ELEMENT_ANCHOR_PREFIX + JSON.stringify(atCap);
    expect(new TextEncoder().encode(stored.slice(ELEMENT_ANCHOR_PREFIX.length)).length).toBe(
      32 * 1024,
    );
    expect(decodeElementAnchor(stored)).toEqual(atCap);

    atCap.network[0]!.url += "a";
    const overCap = ELEMENT_ANCHOR_PREFIX + JSON.stringify(atCap);
    expect(new TextEncoder().encode(overCap.slice(ELEMENT_ANCHOR_PREFIX.length)).length).toBe(
      32 * 1024 + 1,
    );
    expect(decodeElementAnchor(overCap)).toBeNull();
  });

  it("decodes the server's canonical form with omitted fields as defaults", () => {
    const canonical = `${ELEMENT_ANCHOR_PREFIX}{"v":1,"kind":"element","rect":{"x":1,"y":2,"w":3,"h":4},"screenshot":null}`;
    const decoded = decodeElementAnchor(canonical);
    expect(decoded).not.toBeNull();
    expect(decoded?.rect).toEqual({ x: 1, y: 2, w: 3, h: 4 });
    expect(decoded?.page).toEqual({ url: "", title: "", vw: 0, vh: 0, sx: 0, sy: 0, dpr: 0 });
    expect(decoded?.target.label).toBe("");
    expect(decoded?.target.quote).toEqual({ exact: "", prefix: "", suffix: "" });
    expect(decoded?.console).toEqual([]);
    expect(decoded?.network).toEqual([]);
    expect(decoded?.screenshot).toBeNull();
  });
});

describe("elementAnchorRange", () => {
  it("clamps a negative x to zero", () => {
    const range = elementAnchorRange(makeAnchor({ rect: { x: -10, y: 30_000, w: 1, h: 1 } }));
    expect(range).toEqual({ start_index: 300_000_000, end_index: 300_000_000 });
  });

  it("clamps y and x to their maxima", () => {
    const range = elementAnchorRange(makeAnchor({ rect: { x: 20_000, y: 200_000, w: 1, h: 1 } }));
    expect(range).toEqual({ start_index: 999_999_999, end_index: 999_999_999 });
  });

  it("rounds the rect corner to the nearest CSS pixel", () => {
    const range = elementAnchorRange(makeAnchor({ rect: { x: 3.4, y: 12.6, w: 1, h: 1 } }));
    expect(range).toEqual({ start_index: 130_003, end_index: 130_003 });
  });
});
