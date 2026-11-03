// Adapted from Dinotty (21b19cf5), itself from msdshsk/xterm-ime-anchor (MIT).
// Copyright (c) 2026 ChenXi; Copyright (c) 2026 msd.shsk. See NOTICE.
// ConPTY can park the hardware cursor beyond a TUI input; pin both IME
// elements to its isolated inverse caret, or the content end of that row.

import type { Terminal } from "@xterm/xterm";

export type ImeAnchorSource = "heuristic" | "hardware" | "contentEnd";

export interface ImeAnchor {
  source: ImeAnchorSource;
  col: number;
  row: number;
}

export interface AttachOptions {
  onAnchor?: (a: ImeAnchor) => void;
  requireIsolatedCell?: boolean;
}

export interface Detached {
  detach: () => void;
}

export interface ImeBufferCellLike {
  isInverse: () => number;
  getChars?: () => string;
  getWidth?: () => number;
}

export interface ImeBufferLineLike {
  length: number;
  getCell: (x: number) => ImeBufferCellLike | undefined;
}

export interface ImeBufferScan {
  viewportY: number;
  getLine: (y: number) => ImeBufferLineLike | undefined;
}

export function findInverseCell(
  buffer: ImeBufferScan,
  rows: number,
  requireIsolatedCell = true,
): { col: number; row: number } | null {
  const startY = buffer.viewportY;
  for (let y = startY + rows - 1; y >= startY; y--) {
    const line = buffer.getLine(y);
    if (!line) continue;
    for (let x = line.length - 1; x >= 0; x--) {
      const cell = line.getCell(x);
      if (!cell || !cell.isInverse()) continue;

      if (requireIsolatedCell) {
        const left = x > 0 ? line.getCell(x - 1) : null;
        const right = x + 1 < line.length ? line.getCell(x + 1) : null;
        const leftInv = !!left && !!left.isInverse();
        const rightInv = !!right && !!right.isInverse();
        if (leftInv || rightInv) continue;
      }

      return { col: x, row: y - startY };
    }
  }
  return null;
}

export function resolveImeAnchor(
  hit: { col: number; row: number } | null,
  hardwareCursor: { col: number; row: number },
): ImeAnchor {
  if (!hit) return { source: "hardware", col: hardwareCursor.col, row: hardwareCursor.row };
  return { source: "heuristic", col: hit.col, row: hit.row };
}

export function findRowContentEnd(line: ImeBufferLineLike | undefined): number | null {
  if (!line) return null;
  for (let x = line.length - 1; x >= 0; x--) {
    const cell = line.getCell(x);
    if (!cell) continue;
    const width = cell.getWidth?.() ?? 1;
    if (width === 0) continue;
    const chars = cell.getChars?.() ?? "";
    const inverse = !!cell.isInverse();
    if (!inverse && chars.trim() === "") continue;
    return x + Math.max(width, 1);
  }
  return null;
}

export interface AnchorFallbackContext {
  cols: number;
  rows: number;
  viewportY: number;
  hardwareCursor: { col: number; row: number };
  lineAt: (viewportRow: number) => ImeBufferLineLike | undefined;
}

export function resolveFallbackAnchor(ctx: AnchorFallbackContext): ImeAnchor | null {
  const hw = ctx.hardwareCursor;
  if (hw.row < 0 || hw.row >= ctx.rows) return null;
  const end = findRowContentEnd(ctx.lineAt(hw.row));
  const suspect = end === null ? hw.col > 1 : hw.col > end + 1;
  if (!suspect) return null;
  if (end === null) {
    return { source: "contentEnd", col: 0, row: hw.row };
  }
  return { source: "contentEnd", col: Math.min(end, ctx.cols - 1), row: hw.row };
}

export function computeCellSize(
  screen: { getBoundingClientRect: () => { width: number; height: number } },
  cols: number,
  rows: number,
  renderCell?: { width: number; height: number },
): { w: number; h: number } {
  if (renderCell && renderCell.width > 0 && renderCell.height > 0) {
    return { w: renderCell.width, h: renderCell.height };
  }
  const rect = screen.getBoundingClientRect();
  return {
    w: rect.width / Math.max(cols, 1),
    h: rect.height / Math.max(rows, 1),
  };
}

function renderCellSize(terminal: Terminal): { width: number; height: number } | null {
  const core = (
    terminal as {
      _core?: {
        _renderService?: {
          dimensions?: { css?: { cell?: { width?: number; height?: number } } };
        };
      };
    }
  )["_core"];
  const cell = core?.["_renderService"]?.dimensions?.css?.cell;
  if (cell && (cell.width ?? 0) > 0 && (cell.height ?? 0) > 0) {
    return { width: cell.width as number, height: cell.height as number };
  }
  return null;
}

export function attachImeHeuristic(terminal: Terminal, options: AttachOptions = {}): Detached {
  const { onAnchor, requireIsolatedCell = true } = options;

  const root = terminal.element;
  if (!root) return { detach() {} };

  const textarea = root.querySelector(".xterm-helper-textarea") as HTMLTextAreaElement | null;
  const screen = root.querySelector(".xterm-screen") as HTMLElement | null;
  const compositionView = root.querySelector(".composition-view") as HTMLElement | null;

  if (!textarea || !screen || !compositionView) {
    return { detach() {} };
  }

  let composing = false;
  let pinned: { left: string; top: string } | null = null;
  let renderDisposable: { dispose: () => void } | null = null;

  const reapply = (el: HTMLElement) => {
    if (!composing || !pinned) return;
    if (el.style.left !== pinned.left || el.style.top !== pinned.top) {
      el.style.setProperty("left", pinned.left, "important");
      el.style.setProperty("top", pinned.top, "important");
    }
  };
  const moTa = new MutationObserver(() => reapply(textarea));
  const moCv = new MutationObserver(() => reapply(compositionView));

  const cellSize = () => {
    const rc = renderCellSize(terminal);
    return computeCellSize(screen, terminal.cols, terminal.rows, rc ?? undefined);
  };

  function pin(anchor: ImeAnchor) {
    const { w, h } = cellSize();
    const col = Math.min(Math.max(anchor.col, 0), terminal.cols - 1);
    const row = Math.min(Math.max(anchor.row, 0), terminal.rows - 1);
    const left = `${Math.round(col * w)}px`;
    const top = `${Math.round(row * h)}px`;
    if (pinned && pinned.left === left && pinned.top === top) return;

    pinned = { left, top };
    textarea!.style.setProperty("left", left, "important");
    textarea!.style.setProperty("top", top, "important");
    compositionView!.style.setProperty("left", left, "important");
    compositionView!.style.setProperty("top", top, "important");

    onAnchor?.({ ...anchor, col, row });
  }

  function recomputeAndPin() {
    if (!composing) return;

    const hit = findInverseCell(terminal.buffer.active, terminal.rows, requireIsolatedCell);
    if (!hit) {
      return;
    }

    pin({ source: "heuristic", col: hit.col, row: hit.row });
  }

  function resolveStartAnchor(): ImeAnchor {
    const buffer = terminal.buffer.active;
    const fallback = resolveFallbackAnchor({
      cols: terminal.cols,
      rows: terminal.rows,
      viewportY: buffer.viewportY,
      hardwareCursor: { col: buffer.cursorX, row: buffer.cursorY },
      lineAt: (row) => buffer.getLine(buffer.viewportY + row),
    });
    return fallback ?? { source: "hardware", col: buffer.cursorX, row: buffer.cursorY };
  }

  function onCompositionStart() {
    composing = true;

    const hit = findInverseCell(terminal.buffer.active, terminal.rows, requireIsolatedCell);
    if (hit) {
      recomputeAndPin();
    } else {
      const anchor = resolveStartAnchor();
      if (anchor.source === "hardware") {
        pinned = null;
        onAnchor?.(anchor);
      } else {
        pin(anchor);
      }
    }

    renderDisposable = terminal.onRender(() => recomputeAndPin());
  }

  function onCompositionEnd() {
    composing = false;
    if (pinned) {
      for (const element of [textarea!, compositionView!]) {
        element.style.removeProperty("left");
        element.style.removeProperty("top");
      }
    }
    pinned = null;
    renderDisposable?.dispose();
    renderDisposable = null;
  }

  textarea.addEventListener("compositionstart", onCompositionStart);
  textarea.addEventListener("compositionend", onCompositionEnd);
  moTa.observe(textarea, { attributes: true, attributeFilter: ["style"] });
  moCv.observe(compositionView, { attributes: true, attributeFilter: ["style"] });

  return {
    detach() {
      onCompositionEnd();
      textarea.removeEventListener("compositionstart", onCompositionStart);
      textarea.removeEventListener("compositionend", onCompositionEnd);
      moTa.disconnect();
      moCv.disconnect();
    },
  };
}
