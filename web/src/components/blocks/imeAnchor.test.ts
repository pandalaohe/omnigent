// Adapted from Dinotty (21b19cf5), MIT. See NOTICE.
import { describe, it, expect, vi } from "vitest";
import type { Terminal } from "@xterm/xterm";
import {
  attachImeHeuristic,
  computeCellSize,
  findInverseCell,
  findRowContentEnd,
  resolveFallbackAnchor,
} from "./imeAnchor";

type CellMark = "inv" | "norm";

type FakeCell = string | { chars: string; width?: number; inverse?: boolean };

function fakeLine(cells: (CellMark | FakeCell)[]) {
  return {
    length: cells.length,
    getCell(x: number) {
      const c = cells[x];
      if (c === undefined) return undefined;
      if (c === "inv") {
        return { isInverse: () => 1, getChars: () => " ", getWidth: () => 1 };
      }
      if (c === "norm") {
        return { isInverse: () => 0, getChars: () => "", getWidth: () => 1 };
      }
      if (typeof c === "string") {
        return { isInverse: () => 0, getChars: () => c, getWidth: () => 1 };
      }
      return {
        isInverse: () => (c.inverse ? 1 : 0),
        getChars: () => c.chars,
        getWidth: () => c.width ?? 1,
      };
    },
  };
}

function fakeBuffer(lines: (CellMark | FakeCell)[][], viewportY = 0) {
  const rows = lines.map(fakeLine);
  return {
    viewportY,
    getLine: (y: number) => rows[y],
  };
}

describe("findInverseCell (scan order and isolation)", () => {
  it("returns the lone inverse cell with viewport-relative row", () => {
    const buf = fakeBuffer(
      [
        ["norm", "norm", "norm"],
        ["norm", "inv", "norm"],
        ["norm", "norm", "norm"],
      ],
      1,
    );
    expect(findInverseCell(buf, 3)).toEqual({ col: 1, row: 0 });
  });

  it("scans bottom-up: the lowest inverse cell wins", () => {
    const buf = fakeBuffer([
      ["inv", "norm", "norm"],
      ["norm", "norm", "norm"],
      ["norm", "inv", "norm"],
    ]);
    expect(findInverseCell(buf, 3)).toEqual({ col: 1, row: 2 });
  });

  it("scans right-to-left: the rightmost inverse cell wins on a row", () => {
    const buf = fakeBuffer([["norm", "inv", "norm", "inv", "norm"]]);
    expect(findInverseCell(buf, 1)).toEqual({ col: 3, row: 0 });
  });

  it("skips a cell whose BOTH neighbours are inverse (selection bar)", () => {
    const buf = fakeBuffer([
      ["norm", "norm", "inv", "norm", "norm"],
      ["inv", "inv", "inv", "inv", "inv"],
    ]);
    expect(findInverseCell(buf, 2)).toEqual({ col: 2, row: 0 });
  });

  it("skips a run at the line edge (non-isolated), returning null", () => {
    const buf = fakeBuffer([["norm", "inv", "inv"]]);
    expect(findInverseCell(buf, 1)).toBeNull();
  });

  it("keeps a caret at column 0 (no left neighbour, normal right neighbour)", () => {
    const buf = fakeBuffer([["inv", "norm", "norm"]]);
    expect(findInverseCell(buf, 1)).toEqual({ col: 0, row: 0 });
  });

  it("returns null when nothing is inverse", () => {
    const buf = fakeBuffer([
      ["norm", "norm", "norm"],
      ["norm", "norm", "norm"],
    ]);
    expect(findInverseCell(buf, 2)).toBeNull();
  });

  it("respects requireIsolatedCell=false", () => {
    const buf = fakeBuffer([["inv", "inv", "inv"]]);
    expect(findInverseCell(buf, 1, false)).toEqual({ col: 2, row: 0 });
  });
});

describe("findRowContentEnd (exclusive content-end column)", () => {
  it("returns the column after the last visible cell", () => {
    const line = fakeLine([">", " ", "l", "s", " ", " "]);
    expect(findRowContentEnd(line)).toBe(4);
  });

  it("treats a lone inverse space (a fake caret) as content", () => {
    const line = fakeLine([" ", " ", "inv", " ", " "]);
    expect(findRowContentEnd(line)).toBe(3);
  });

  it("accounts for double-width (CJK) glyphs", () => {
    const line = fakeLine(["│", " ", { chars: "你", width: 2 }, "", " ", " "]);
    expect(findRowContentEnd(line)).toBe(4);
  });

  it("returns null for an entirely blank row", () => {
    expect(findRowContentEnd(fakeLine(["norm", " ", " ", "norm"]))).toBeNull();
    expect(findRowContentEnd(undefined)).toBeNull();
  });
});

describe("resolveFallbackAnchor (ConPTY-parked cursor heuristic, swept caret)", () => {
  const ctxFor = (
    line: (CellMark | FakeCell)[] | undefined,
    opts: { cols?: number; rows?: number; col?: number; row?: number } = {},
  ) => ({
    cols: opts.cols ?? 80,
    rows: opts.rows ?? 10,
    hardwareCursor: { col: opts.col ?? 79, row: opts.row ?? 2 },
    lineAt: () => (line ? fakeLine(line) : undefined),
  });

  it("keeps the hardware anchor when the cursor parks at the content end (shell)", () => {
    const line = [">", " ", { chars: "你", width: 2 }, "", { chars: "好", width: 2 }, ""];
    expect(resolveFallbackAnchor(ctxFor(line, { col: 6 }))).toBeNull();
  });

  it("keeps the hardware anchor when the cursor is one cell past (space-terminated prompt)", () => {
    const line = [">", " "];
    expect(resolveFallbackAnchor(ctxFor(line, { col: 2 }))).toBeNull();
  });

  it("anchors at the content end when the cursor is parked at the right edge (pi on ConPTY)", () => {
    const line = [">", " ", { chars: "你", width: 2 }, "", { chars: "好", width: 2 }, ""];
    expect(resolveFallbackAnchor(ctxFor(line))).toEqual({
      source: "contentEnd",
      col: 6,
      row: 2,
    });
  });

  it("anchors at the row start when the parked row is blank (caret swept away)", () => {
    expect(resolveFallbackAnchor(ctxFor([" ", " ", " "]))).toEqual({
      source: "contentEnd",
      col: 0,
      row: 2,
    });
  });

  it("keeps the hardware anchor on a blank row when the cursor is at/near col 0", () => {
    expect(resolveFallbackAnchor(ctxFor([" ", " ", " "], { col: 0 }))).toBeNull();
    expect(resolveFallbackAnchor(ctxFor([" ", " ", " "], { col: 1 }))).toBeNull();
  });

  it("keeps the hardware anchor when the content fills the row to the edge", () => {
    const line = ["a", "b", { chars: "你", width: 2 }, ""];
    expect(resolveFallbackAnchor(ctxFor(line, { cols: 4, col: 3 }))).toBeNull();
  });

  it("ignores a cursor row outside the viewport", () => {
    expect(resolveFallbackAnchor(ctxFor(["a"], { row: 10 }))).toBeNull();
    expect(resolveFallbackAnchor(ctxFor(["a"], { row: -1 }))).toBeNull();
  });
});

describe("computeCellSize (render-service cell vs rect fallback)", () => {
  it("prefers the render-service CSS cell size (letter_spacing/line_height correct)", () => {
    const screen = { getBoundingClientRect: () => ({ width: 900, height: 300 }) };
    expect(computeCellSize(screen, 120, 30, { width: 8.5, height: 24 })).toEqual({ w: 8.5, h: 24 });
  });

  it("falls back to rect/cols division when the render cell is missing", () => {
    const screen = { getBoundingClientRect: () => ({ width: 900, height: 300 }) };
    expect(computeCellSize(screen, 120, 30)).toEqual({ w: 7.5, h: 10 });
  });

  it("guards against a zero render cell", () => {
    const screen = { getBoundingClientRect: () => ({ width: 900, height: 300 }) };
    expect(computeCellSize(screen, 120, 30, { width: 0, height: 0 })).toEqual({ w: 7.5, h: 10 });
  });
});

function makeDom() {
  document.body.innerHTML = "";
  const root = document.createElement("div");
  root.className = "xterm";
  root.innerHTML = [
    '<div class="xterm-screen"></div>',
    '<div class="xterm-helpers">',
    '  <textarea class="xterm-helper-textarea"></textarea>',
    '  <div class="composition-view"></div>',
    "</div>",
  ].join("");
  document.body.appendChild(root);
  const textarea = root.querySelector(".xterm-helper-textarea") as HTMLTextAreaElement;
  const compositionView = root.querySelector(".composition-view") as HTMLElement;
  return { root, textarea, compositionView };
}

function setup(opts: {
  lines: (CellMark | FakeCell)[][];
  rows?: number;
  cols?: number;
  viewportY?: number;
  cursorX?: number;
  cursorY?: number;
  cellW?: number;
  cellH?: number;
}) {
  const dom = makeDom();
  const rows = opts.rows ?? 3;
  const cols = opts.cols ?? 10;
  const lines = opts.lines.map(fakeLine);
  let renderCb: (() => void) | null = null;
  const buffer = {
    viewportY: opts.viewportY ?? 0,
    cursorX: opts.cursorX ?? 0,
    cursorY: opts.cursorY ?? 0,
    getLine: (y: number) => lines[y],
  };
  const terminal = {
    element: dom.root,
    cols,
    rows,
    buffer: { active: buffer },
    onRender(cb: () => void) {
      renderCb = cb;
      return {
        dispose() {
          renderCb = null;
        },
      };
    },
    _core: {
      _renderService: {
        dimensions: {
          css: { cell: { width: opts.cellW ?? 8, height: opts.cellH ?? 20 } },
        },
      },
    },
  };
  return {
    ...dom,
    terminal,
    buffer,
    setLine: (y: number, cells: (CellMark | FakeCell)[]) => {
      lines[y] = fakeLine(cells);
    },
    fireRender: () => renderCb?.(),
  };
}

const tick = () =>
  new Promise<void>((resolve) => {
    setTimeout(resolve, 0);
  });

describe("attachImeHeuristic (DOM integration)", () => {
  it("pins textarea + composition-view to the inverse cell with !important", () => {
    const { terminal, textarea, compositionView, buffer } = setup({
      lines: [
        ["norm", "norm", "norm"],
        ["norm", "norm", "inv", "norm"],
        ["norm", "norm", "norm"],
      ],
      cellW: 8,
      cellH: 20,
    });
    const onAnchor = vi.fn();
    attachImeHeuristic(terminal as unknown as Terminal, { onAnchor });
    textarea.dispatchEvent(new Event("compositionstart"));

    expect(textarea.style.getPropertyValue("left")).toBe("16px");
    expect(textarea.style.getPropertyPriority("left")).toBe("important");
    expect(textarea.style.getPropertyValue("top")).toBe("20px");
    expect(textarea.style.getPropertyPriority("top")).toBe("important");
    expect(compositionView.style.getPropertyValue("left")).toBe("16px");
    expect(compositionView.style.getPropertyValue("top")).toBe("20px");
    expect(onAnchor).toHaveBeenCalledWith({ source: "heuristic", col: 2, row: 1 });
    expect(buffer.cursorX).not.toBe(2);
  });

  it("re-applies the pin when xterm overwrites the style during composition", async () => {
    const { terminal, textarea } = setup({
      lines: [["norm", "inv", "norm", "norm"]],
      cellW: 10,
      cellH: 20,
    });
    attachImeHeuristic(terminal as unknown as Terminal);
    textarea.dispatchEvent(new Event("compositionstart"));
    expect(textarea.style.getPropertyValue("left")).toBe("10px");

    textarea.style.left = "0px";
    await tick();
    expect(textarea.style.getPropertyValue("left")).toBe("10px");
    expect(textarea.style.getPropertyPriority("left")).toBe("important");
  });

  it("falls back to the hardware cursor (no style pin) when it parks at the content end", () => {
    const { terminal, textarea, compositionView } = setup({
      lines: [
        ["norm", "norm", "norm", "norm"],
        ["P", "S", ">", " "],
        ["norm", "norm", "norm", "norm"],
      ],
      cursorX: 4,
      cursorY: 1,
    });
    const onAnchor = vi.fn();
    attachImeHeuristic(terminal as unknown as Terminal, { onAnchor });
    textarea.dispatchEvent(new Event("compositionstart"));

    expect(onAnchor).toHaveBeenCalledWith({ source: "hardware", col: 4, row: 1 });
    expect(textarea.style.getPropertyValue("left")).toBe("");
    expect(compositionView.style.getPropertyValue("top")).toBe("");
  });

  it("anchors at the content end when ConPTY parked the cursor at the right edge (swept caret)", () => {
    const { terminal, textarea, compositionView } = setup({
      lines: [
        ["norm", "norm", "norm", "norm", "norm", "norm", "norm", "norm", "norm", "norm"],
        [">", " ", { chars: "你", width: 2 }, "", { chars: "好", width: 2 }, "", " ", " ", " "],
        ["norm", "norm", "norm", "norm", "norm", "norm", "norm", "norm", "norm", "norm"],
      ],
      cols: 10,
      cursorX: 9,
      cursorY: 1,
      cellW: 8,
      cellH: 20,
    });
    const onAnchor = vi.fn();
    attachImeHeuristic(terminal as unknown as Terminal, { onAnchor });
    textarea.dispatchEvent(new Event("compositionstart"));

    expect(onAnchor).toHaveBeenCalledWith({ source: "contentEnd", col: 6, row: 1 });
    expect(textarea.style.getPropertyValue("left")).toBe("48px");
    expect(textarea.style.getPropertyPriority("left")).toBe("important");
    expect(compositionView.style.getPropertyValue("left")).toBe("48px");
    expect(compositionView.style.getPropertyValue("top")).toBe("20px");
  });

  it("follows content-end renders while ConPTY keeps sweeping away the caret", () => {
    const { terminal, textarea, compositionView, setLine, fireRender } = setup({
      lines: [[">", " ", "a", "norm", "norm", "norm", "norm", "norm", "norm", "norm"]],
      cursorX: 9,
      cursorY: 0,
      cellW: 8,
    });
    const attached = attachImeHeuristic(terminal as unknown as Terminal);
    try {
      textarea.dispatchEvent(new Event("compositionstart"));
      expect(textarea.style.left).toBe("24px");
      expect(compositionView.style.left).toBe("24px");
      setLine(0, [">", " ", "a", "b", "c", "norm", "norm", "norm", "norm", "norm"]);
      fireRender();
      expect(textarea.style.left).toBe("40px");
      expect(compositionView.style.left).toBe("40px");

      // A visible inverse caret now owns the same position, then briefly disappears.
      setLine(0, [">", " ", "a", "b", "c", "inv", "norm", "norm", "norm", "norm"]);
      fireRender();
      setLine(0, [">", "norm", "norm", "norm", "norm", "norm", "norm", "norm", "norm", "norm"]);
      fireRender();
      expect(textarea.style.left).toBe("40px");
      expect(compositionView.style.left).toBe("40px");
    } finally {
      attached.detach();
    }
  });

  it("releases the content-end pin when the hardware cursor returns to the input", () => {
    const { terminal, textarea, compositionView, buffer, fireRender } = setup({
      lines: [[">", " ", "a", "norm", "norm", "norm", "norm", "norm", "norm", "norm"]],
      cursorX: 9,
      cursorY: 0,
      cellW: 8,
    });
    const attached = attachImeHeuristic(terminal as unknown as Terminal);
    try {
      textarea.dispatchEvent(new Event("compositionstart"));
      expect(textarea.style.left).toBe("24px");
      buffer.cursorX = 3;
      fireRender();
      expect(textarea.style.left).toBe("");
      expect(compositionView.style.left).toBe("");
    } finally {
      attached.detach();
    }
  });

  it("anchors at the row start when the parked row is entirely blank (swept caret)", () => {
    const { terminal, textarea } = setup({
      lines: [
        ["norm", "norm", "norm", "norm"],
        [" ", " ", " ", " "],
        ["norm", "norm", "norm", "norm"],
      ],
      cols: 4,
      cursorX: 3,
      cursorY: 1,
    });
    const onAnchor = vi.fn();
    attachImeHeuristic(terminal as unknown as Terminal, { onAnchor });
    textarea.dispatchEvent(new Event("compositionstart"));

    expect(onAnchor).toHaveBeenCalledWith({ source: "contentEnd", col: 0, row: 1 });
    expect(textarea.style.getPropertyValue("left")).toBe("0px");
  });

  it("re-pins to the inverse caret once a blinking one comes back on", () => {
    const { terminal, textarea, setLine, fireRender } = setup({
      lines: [
        ["norm", "norm", "norm", "norm", "norm", "norm", "norm"],
        [">", " ", " ", " ", " ", " ", " "],
        ["norm", "norm", "norm", "norm", "norm", "norm", "norm"],
      ],
      cols: 7,
      cursorX: 6,
      cursorY: 1,
      cellW: 10,
      cellH: 20,
    });
    attachImeHeuristic(terminal as unknown as Terminal);
    textarea.dispatchEvent(new Event("compositionstart"));
    expect(textarea.style.getPropertyValue("left")).toBe("10px");

    setLine(1, [">", " ", " ", "inv", " ", " ", " "]);
    fireRender();
    expect(textarea.style.getPropertyValue("left")).toBe("30px");
  });

  it("follows the caret on render (IME partial commit) but keeps the last pin if it transiently disappears", () => {
    const { terminal, textarea, setLine, fireRender } = setup({
      lines: [
        ["norm", "norm", "norm"],
        ["norm", "norm", "norm", "norm", "norm", "inv", "norm"],
        ["norm", "norm", "norm"],
      ],
      cellW: 8,
      cellH: 20,
    });
    attachImeHeuristic(terminal as unknown as Terminal);
    textarea.dispatchEvent(new Event("compositionstart"));
    expect(textarea.style.getPropertyValue("left")).toBe("40px");
    setLine(1, ["norm", "norm", "norm", "norm", "norm", "norm", "inv", "norm"]);
    fireRender();
    expect(textarea.style.getPropertyValue("left")).toBe("48px");
    setLine(1, ["norm", "norm", "norm", "norm", "norm", "norm", "norm", "norm"]);
    fireRender();
    expect(textarea.style.getPropertyValue("left")).toBe("48px");
  });

  it("stops re-pinning after compositionend", async () => {
    const { terminal, textarea } = setup({
      lines: [["norm", "inv", "norm", "norm"]],
      cellW: 10,
      cellH: 20,
    });
    attachImeHeuristic(terminal as unknown as Terminal);
    textarea.dispatchEvent(new Event("compositionstart"));
    expect(textarea.style.getPropertyValue("left")).toBe("10px");

    textarea.dispatchEvent(new Event("compositionend"));
    expect(textarea.style.getPropertyPriority("left")).toBe("");
    expect(textarea.style.left).toBe("");
    textarea.style.left = "5px";
    await tick();
    expect(textarea.style.getPropertyValue("left")).toBe("5px");
  });

  it("detach removes listeners, observers, and the render subscription", () => {
    const { terminal, textarea, compositionView, fireRender } = setup({
      lines: [["norm", "inv", "norm", "norm"]],
      cellW: 10,
      cellH: 20,
    });
    const attached = attachImeHeuristic(terminal as unknown as Terminal);
    textarea.dispatchEvent(new Event("compositionstart"));
    expect(textarea.style.getPropertyValue("left")).toBe("10px");

    attached.detach();
    expect(textarea.style.left).toBe("");
    expect(compositionView.style.top).toBe("");
    textarea.style.removeProperty("left");
    textarea.dispatchEvent(new Event("compositionstart"));
    expect(textarea.style.getPropertyValue("left")).toBe("");

    fireRender();
    expect(textarea.style.getPropertyValue("left")).toBe("");
  });
});
