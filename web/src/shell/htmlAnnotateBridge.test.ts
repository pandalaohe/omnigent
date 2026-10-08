import { describe, expect, it } from "vitest";
import { ANNOTATE_MSG, ANNOTATE_SOURCE, parseAnnotateMessage } from "./htmlAnnotateBridge";

const NONCE = "n1";
const base = { source: ANNOTATE_SOURCE, nonce: NONCE };

const ANCHOR = {
  v: 1,
  kind: "element",
  page: { url: "http://localhost:6767/v1/artifacts/tok/reports/q3.html" },
  target: { label: "div.filter-menu > button.option" },
  rect: { x: 1, y: 2, w: 3, h: 4 },
};

const JPEG_PREFIX = "data:image/jpeg;base64,";

const VIEWPORT_RECT = { x: 10, y: 20, w: 120, h: 32 };

function picked(overrides: Record<string, unknown> = {}) {
  return {
    ...base,
    type: ANNOTATE_MSG.picked,
    anchor: ANCHOR,
    screenshot: null,
    viewportRect: VIEWPORT_RECT,
    ...overrides,
  };
}

function parsePicked(data: unknown) {
  const msg = parseAnnotateMessage(data, NONCE);
  if (!msg || msg.type !== ANNOTATE_MSG.picked) throw new Error("expected a picked message");
  return msg;
}

describe("parseAnnotateMessage — gate", () => {
  it("rejects a wrong nonce, a wrong source tag, and non-objects", () => {
    expect(parseAnnotateMessage(picked({ nonce: "other" }), NONCE)).toBeNull();
    expect(parseAnnotateMessage(picked({ source: "evil" }), NONCE)).toBeNull();
    expect(parseAnnotateMessage("not-an-object", NONCE)).toBeNull();
    expect(parseAnnotateMessage(null, NONCE)).toBeNull();
  });

  it("rejects unknown types and parent→frame types", () => {
    expect(parseAnnotateMessage({ ...base, type: "annotate:bogus" }, NONCE)).toBeNull();
    expect(
      parseAnnotateMessage({ ...base, type: ANNOTATE_MSG.setMode, on: true }, NONCE),
    ).toBeNull();
    expect(parseAnnotateMessage({ ...base, type: ANNOTATE_MSG.init }, NONCE)).toBeNull();
    expect(parseAnnotateMessage({ ...base, type: ANNOTATE_MSG.pickDone }, NONCE)).toBeNull();
  });

  it("parses the other frame→parent types", () => {
    expect(parseAnnotateMessage({ ...base, type: ANNOTATE_MSG.ready }, NONCE)).toEqual({
      type: ANNOTATE_MSG.ready,
    });
    expect(
      parseAnnotateMessage(
        { ...base, type: ANNOTATE_MSG.ready, pathname: "/v1/artifacts/tok/reports/q3.html" },
        NONCE,
      ),
    ).toEqual({ type: ANNOTATE_MSG.ready, pathname: "/v1/artifacts/tok/reports/q3.html" });
    expect(
      parseAnnotateMessage(
        { ...base, type: ANNOTATE_MSG.runtimeLoaded, part: "pick", ok: true },
        NONCE,
      ),
    ).toEqual({ type: ANNOTATE_MSG.runtimeLoaded, part: "pick", ok: true });
    expect(
      parseAnnotateMessage(
        { ...base, type: ANNOTATE_MSG.runtimeLoaded, part: "snapdom", ok: true },
        NONCE,
      ),
    ).toBeNull();
    expect(
      parseAnnotateMessage(
        { ...base, type: ANNOTATE_MSG.modeChanged, on: false, reason: "escape" },
        NONCE,
      ),
    ).toEqual({ type: ANNOTATE_MSG.modeChanged, on: false, reason: "escape" });
    expect(
      parseAnnotateMessage(
        { ...base, type: ANNOTATE_MSG.modeChanged, on: true, reason: "bogus" },
        NONCE,
      ),
    ).toBeNull();
    expect(parseAnnotateMessage({ ...base, type: ANNOTATE_MSG.toggleRequested }, NONCE)).toEqual({
      type: ANNOTATE_MSG.toggleRequested,
    });
    expect(
      parseAnnotateMessage({ ...base, type: ANNOTATE_MSG.markerClick, id: "c1" }, NONCE),
    ).toEqual({ type: ANNOTATE_MSG.markerClick, id: "c1" });
    expect(parseAnnotateMessage({ ...base, type: ANNOTATE_MSG.markerClick }, NONCE)).toBeNull();
  });
});

describe("parseAnnotateMessage — picked", () => {
  it("clamps the anchor and keeps a valid viewport rect", () => {
    const msg = parsePicked(picked());
    expect(msg.anchor.target.label).toBe("div.filter-menu > button.option");
    expect(msg.anchor.v).toBe(1);
    expect(msg.viewportRect).toEqual(VIEWPORT_RECT);
    expect(msg.screenshot).toBeNull();
  });

  it("rejects a picked message whose anchor does not validate", () => {
    expect(parseAnnotateMessage(picked({ anchor: null }), NONCE)).toBeNull();
    expect(parseAnnotateMessage(picked({ anchor: { ...ANCHOR, v: 2 } }), NONCE)).toBeNull();
    expect(
      parseAnnotateMessage(
        picked({ anchor: { ...ANCHOR, rect: { ...ANCHOR.rect, x: "1" } } }),
        NONCE,
      ),
    ).toBeNull();
    expect(
      parseAnnotateMessage(picked({ anchor: { ...ANCHOR, kind: "bogus" } }), NONCE),
    ).toBeNull();
  });

  it("drops a pick whose viewport rect is missing or not four finite numbers", () => {
    expect(parseAnnotateMessage(picked({ viewportRect: undefined }), NONCE)).toBeNull();
    expect(parseAnnotateMessage(picked({ viewportRect: null }), NONCE)).toBeNull();
    expect(parseAnnotateMessage(picked({ viewportRect: "10,20" }), NONCE)).toBeNull();
    for (const key of ["x", "y", "w", "h"]) {
      expect(
        parseAnnotateMessage(picked({ viewportRect: { ...VIEWPORT_RECT, [key]: "10" } }), NONCE),
      ).toBeNull();
      expect(
        parseAnnotateMessage(
          picked({ viewportRect: { ...VIEWPORT_RECT, [key]: Number.NaN } }),
          NONCE,
        ),
      ).toBeNull();
      expect(
        parseAnnotateMessage(
          picked({ viewportRect: { ...VIEWPORT_RECT, [key]: Number.POSITIVE_INFINITY } }),
          NONCE,
        ),
      ).toBeNull();
      expect(
        parseAnnotateMessage(picked({ viewportRect: { ...VIEWPORT_RECT, [key]: 1e6 + 1 } }), NONCE),
      ).toBeNull();
    }
  });

  it("keeps a viewport rect at the 1e6 geometry boundary", () => {
    const atLimit = { x: -1e6, y: 1e6, w: 0, h: 1e6 };
    expect(parsePicked(picked({ viewportRect: atLimit })).viewportRect).toEqual(atLimit);
  });

  it("drops an oversized screenshot but keeps the annotation", () => {
    const oversized = {
      dataUrl: JPEG_PREFIX + "A".repeat(4 * 1024 * 1024),
      width: 800,
      height: 600,
    };
    const msg = parsePicked(picked({ screenshot: oversized }));
    expect(msg.screenshot).toBeNull();
    expect(msg.anchor.target.label).toBe("div.filter-menu > button.option");
  });

  it("keeps a screenshot at the 2 MiB boundary and drops one byte over", () => {
    const base64AtLimit = Math.ceil((2 * 1024 * 1024 * 4) / 3);
    const atLimit = {
      dataUrl: JPEG_PREFIX + "A".repeat(base64AtLimit),
      width: 1,
      height: 1,
    };
    expect(parsePicked(picked({ screenshot: atLimit })).screenshot).toEqual(atLimit);

    const overLimit = { ...atLimit, dataUrl: atLimit.dataUrl + "A" };
    expect(parsePicked(picked({ screenshot: overLimit })).screenshot).toBeNull();
  });

  it("drops a non-image or malformed screenshot but keeps the annotation", () => {
    const bad = [
      { dataUrl: "data:image/gif;base64,AAAA", width: 1, height: 1 },
      { dataUrl: `data:image/png;base64,AAAA`, width: 0, height: 1 },
      { dataUrl: `data:image/png;base64,AAAA`, width: 1, height: 1.5 },
      { dataUrl: `data:image/png;base64,AAAA`, width: 1, height: 5000 },
      { dataUrl: "not-a-data-url", width: 1, height: 1 },
      { width: 1, height: 1 },
    ];
    for (const screenshot of bad) {
      expect(parsePicked(picked({ screenshot })).screenshot).toBeNull();
    }
  });
});

describe("parseAnnotateMessage — resolved", () => {
  it("rejects a non-array items payload", () => {
    expect(parseAnnotateMessage({ ...base, type: ANNOTATE_MSG.resolved }, NONCE)).toBeNull();
    expect(
      parseAnnotateMessage({ ...base, type: ANNOTATE_MSG.resolved, items: "nope" }, NONCE),
    ).toBeNull();
  });

  it("caps the list at 200 entries and drops malformed ones", () => {
    const items = Array.from({ length: 250 }, (_, i) => ({ id: `c${i}`, found: i % 2 === 0 }));
    const msg = parseAnnotateMessage(
      { ...base, type: ANNOTATE_MSG.resolved, items: [...items, null, { found: true }] },
      NONCE,
    );
    if (!msg || msg.type !== ANNOTATE_MSG.resolved) throw new Error("expected a resolved message");
    expect(msg.items).toHaveLength(200);
    expect(msg.items[0]).toEqual({ id: "c0", found: true });
    expect(msg.items[199]).toEqual({ id: "c199", found: false });
  });

  it("caps ids at 64 chars and coerces a non-boolean found to false", () => {
    const msg = parseAnnotateMessage(
      {
        ...base,
        type: ANNOTATE_MSG.resolved,
        items: [{ id: "x".repeat(100), found: "yes" }],
      },
      NONCE,
    );
    if (!msg || msg.type !== ANNOTATE_MSG.resolved) throw new Error("expected a resolved message");
    expect(msg.items).toEqual([{ id: "x".repeat(64), found: false }]);
  });
});
