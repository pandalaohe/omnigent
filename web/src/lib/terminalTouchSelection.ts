import type { IBufferLine, IMarker, Terminal } from "@xterm/xterm";

const TOUCH_SELECT_HOLD_MS = 500;
const EDGE_ROWS = 1.5;
const EDGE_SCROLL_MS = 60;
const WORD = /[\w.:/@-]+|[一-鿿]+/g;

interface Point {
  x: number;
  y: number;
}
interface Screen {
  left: number;
  top: number;
  cellWidth: number;
  cellHeight: number;
}
interface Range {
  start: number;
  end: number;
}
export type SelectionHandle = "start" | "end";
export type SelectionHandles = Record<SelectionHandle, Point | null>;

export interface TerminalTouchSelectionOptions {
  copy: (text: string) => void;
  handles: (positions: SelectionHandles | null) => void;
}

type Drag = {
  identifier: number;
  point: Point;
} & (
  | { kind: "word"; anchor: Range }
  | { kind: SelectionHandle; offset: Point; listeners: AbortController }
);

function point(touch: Touch): Point {
  return { x: touch.clientX, y: touch.clientY };
}

function samePoint(a: Point | null, b: Point | null): boolean {
  return a?.x === b?.x && a?.y === b?.y;
}

function cellRange(line: IBufferLine | undefined, column: number): Range {
  const cell = line?.getCell(column);
  if (cell?.getWidth() === 0 && column > 0) return { start: column - 1, end: column + 1 };
  return { start: column, end: column + (cell?.getWidth() || 1) };
}

/** Map string indices back to cells, including wide and combined characters. */
function terminalTouchWord(line: IBufferLine, column: number): Range | undefined {
  let text = "";
  const cells: Range[] = [];
  for (let index = 0; index < line.length; index += 1) {
    const cell = line.getCell(index);
    const width = cell?.getWidth() ?? 1;
    if (width === 0) continue;
    const chars = cell?.getChars() || " ";
    const range = { start: index, end: index + width };
    for (let i = 0; i < chars.length; i += 1) cells.push(range);
    text += chars;
  }
  for (const match of text.matchAll(WORD)) {
    const start = cells[match.index].start;
    const end = cells[match.index + match[0].length - 1].end;
    if (column >= start && column < end) return { start, end };
  }
  // Punctuation and other scripts can still be selected one cell at a time.
  const cell = cellRange(line, column);
  return line.getCell(cell.start)?.getChars().trim() ? cell : undefined;
}

/** Owns the hold and selection branches. Ordinary terminal scrolling stays in TerminalSession. */
export class TerminalTouchSelection {
  private hold: ReturnType<typeof setTimeout> | undefined;
  private edgeScroll: ReturnType<typeof setInterval> | undefined;
  private drag: Drag | undefined;
  private selectionStart: { marker: IMarker; row: number } | undefined;
  private ownsSelection = false;
  private changingSelection = false;
  private positions: SelectionHandles | null = null;
  private readonly listeners = new AbortController();
  private readonly subscriptions;
  private readonly term: Terminal;
  private readonly container: HTMLElement;
  private readonly screen: () => Screen | null;
  private readonly stopScroll: () => void;
  private readonly options: TerminalTouchSelectionOptions;

  constructor(
    term: Terminal,
    container: HTMLElement,
    screen: () => Screen | null,
    stopScroll: () => void,
    options: TerminalTouchSelectionOptions,
  ) {
    this.term = term;
    this.container = container;
    this.screen = screen;
    this.stopScroll = stopScroll;
    this.options = options;
    const { signal } = this.listeners;
    // The second finger can land outside the xterm element.
    window.addEventListener("touchstart", this.onAdditionalTouch, { capture: true, signal });
    this.subscriptions = [
      term.onSelectionChange(() => {
        if (!this.changingSelection && this.ownsSelection) this.cancel(false);
      }),
      term.onRender(() => this.placeHandles()),
      term.onScroll(() => this.placeHandles()),
    ];
  }

  start(touch: Touch): void {
    this.cancelHold();
    const origin = point(touch);
    this.hold = setTimeout(() => {
      this.hold = undefined;
      const cell = this.cellAt(origin, false);
      const buffer = this.term.buffer.active;
      const line = cell && buffer.getLine(cell.row);
      if (!cell || !line) return;
      const word = terminalTouchWord(line, cell.column);
      if (!word) return;
      const offset = cell.row * this.term.cols;
      const anchor = { start: offset + word.start, end: offset + word.end };
      this.stopScroll();
      this.drag = { kind: "word", identifier: touch.identifier, point: origin, anchor };
      this.select(anchor);
    }, TOUCH_SELECT_HOLD_MS);
  }

  cancelHold(): void {
    clearTimeout(this.hold);
    this.hold = undefined;
  }

  move(event: TouchEvent): boolean {
    const drag = this.drag;
    if (drag?.kind !== "word" || event.touches.length !== 1) return false;
    const touch = event.touches[0];
    if (touch.identifier !== drag.identifier) return false;
    this.moveDrag(event, touch);
    return true;
  }

  end(event: TouchEvent, wasTap: boolean): void {
    this.cancelHold();
    if (this.drag?.kind === "word") {
      this.endDrag(event);
    } else if (wasTap && this.ownsSelection) {
      this.cancel();
    }
  }

  startHandle(handle: SelectionHandle, event: TouchEvent): (() => void) | undefined {
    const touch = event.touches[0];
    const position = this.positions?.[handle];
    const screen = this.screen();
    if (event.touches.length !== 1 || !position || !screen || !this.currentRange()) return;
    this.cancelHold();
    this.stopDrag();
    const bounds = this.container.getBoundingClientRect();
    const listeners = new AbortController();
    this.drag = {
      kind: handle,
      identifier: touch.identifier,
      point: point(touch),
      offset: {
        x:
          bounds.left +
          position.x -
          touch.clientX +
          (screen.cellWidth / 2) * (handle === "start" ? 1 : -1),
        y:
          bounds.top +
          position.y -
          touch.clientY +
          (screen.cellHeight / 2) * (handle === "start" ? 1 : -1),
      },
      listeners,
    };
    const { signal } = listeners;
    window.addEventListener(
      "touchmove",
      (move) => {
        if (move.touches.length === 1 && move.touches[0].identifier === touch.identifier) {
          this.moveDrag(move, move.touches[0]);
        }
      },
      { passive: false, signal },
    );
    window.addEventListener("touchend", (end) => this.endDrag(end), { passive: false, signal });
    window.addEventListener("touchcancel", () => this.cancel(), { signal });
    return () => {
      if (!signal.aborted) this.cancel();
    };
  }

  shouldSuppressContextMenu(): boolean {
    return this.hold !== undefined || this.drag?.kind === "word" || this.ownsSelection;
  }

  /** xterm clears selection on row resize, including when the phone keyboard appears. */
  refit(fit: () => void): void {
    const range = this.ownsSelection && this.currentRange();
    if (!range) {
      fit();
      return;
    }
    const { cols, rows } = this.term;
    const text = this.term.getSelection();
    this.changingSelection = true;
    try {
      fit();
    } finally {
      this.changingSelection = false;
      if (this.term.cols !== cols || range.end > this.term.buffer.active.length * cols) {
        this.cancel();
      } else if (this.term.rows !== rows) {
        this.select(range);
        if (this.term.getSelection() !== text) this.cancel();
      } else {
        this.placeHandles();
      }
    }
  }

  cancel(clear = true): void {
    this.cancelHold();
    this.stopDrag();
    const owned = this.ownsSelection;
    this.ownsSelection = false;
    this.selectionStart?.marker.dispose();
    this.selectionStart = undefined;
    if (clear && owned) this.term.clearSelection();
    this.publishHandles(null);
  }

  dispose(): void {
    this.listeners.abort();
    for (const subscription of this.subscriptions) subscription.dispose();
    this.cancel();
  }

  private readonly onAdditionalTouch = (event: TouchEvent) => {
    if (event.touches.length < 2 || (!this.drag && this.hold === undefined)) return;
    this.stopScroll();
    this.cancel();
  };

  private stopDrag(): void {
    if (this.drag && this.drag.kind !== "word") this.drag.listeners.abort();
    this.drag = undefined;
    this.stopEdgeScroll();
  }

  private stopEdgeScroll(): void {
    clearInterval(this.edgeScroll);
    this.edgeScroll = undefined;
  }

  private endDrag(event: TouchEvent): void {
    if (
      !this.drag ||
      !Array.from(event.changedTouches).some((touch) => touch.identifier === this.drag?.identifier)
    )
      return;
    if (event.cancelable) event.preventDefault();
    this.stopDrag();
    if (event.touches.length > 0 || !this.term.element?.isConnected || !this.currentRange()) {
      this.cancel();
      return;
    }
    const text = this.term.getSelection();
    if (this.ownsSelection && text) this.options.copy(text);
  }

  private cellAt(client: Point, clamp = true): { row: number; column: number } | undefined {
    const screen = this.screen();
    if (!screen || !this.term.element?.isConnected) return;
    const { cols, rows } = this.term;
    const column = Math.floor((client.x - screen.left) / screen.cellWidth);
    const row = Math.floor((client.y - screen.top) / screen.cellHeight);
    if (!clamp && (column < 0 || column >= cols || row < 0 || row >= rows)) return;
    return {
      row: this.term.buffer.active.viewportY + Math.max(0, Math.min(rows - 1, row)),
      column: Math.max(0, Math.min(cols - 1, column)),
    };
  }

  private currentRange(): Range | undefined {
    const position = this.term.getSelectionPosition();
    const start = this.selectionStart;
    if (!position || start?.marker.isDisposed) return;
    const { cols } = this.term;
    if (start && start.row !== start.marker.line) {
      const shift = (start.marker.line - start.row) * cols;
      if (this.drag?.kind === "word") {
        this.drag.anchor.start += shift;
        this.drag.anchor.end += shift;
      }
      start.row = start.marker.line;
    }
    return {
      start: position.start.y * cols + position.start.x,
      end: position.end.y * cols + position.end.x,
    };
  }

  private select(range: Range): void {
    const { cols } = this.term;
    const row = Math.floor(range.start / cols);
    const buffer = this.term.buffer.active;
    // xterm can clamp a trimmed programmatic selection onto unrelated text.
    if (buffer.type === "normal" && this.selectionStart?.marker.line !== row) {
      this.selectionStart?.marker.dispose();
      const marker = this.term.registerMarker(row - buffer.baseY - buffer.cursorY);
      this.selectionStart = { marker, row };
    }
    this.ownsSelection = true;
    this.changingSelection = true;
    this.term.select(range.start % cols, row, range.end - range.start);
    this.changingSelection = false;
    this.placeHandles();
  }

  private moveDrag(event: TouchEvent, touch: Touch): void {
    if (!this.drag) return;
    if (event.cancelable) event.preventDefault();
    this.drag.point = point(touch);
    this.extendSelection();
    if (this.edgeDirection() === 0) {
      this.stopEdgeScroll();
    } else {
      this.edgeScroll ??= setInterval(() => {
        const direction = this.edgeDirection();
        if (!direction) {
          this.stopEdgeScroll();
          this.placeHandles();
          return;
        }
        this.term.scrollLines(direction);
        this.extendSelection();
      }, EDGE_SCROLL_MS);
    }
  }

  private edgeDirection(): number {
    const screen = this.screen();
    if (!screen || !this.drag || !this.term.element?.isConnected) return 0;
    const row = (this.drag.point.y - screen.top) / screen.cellHeight;
    if (row < EDGE_ROWS) return -1;
    if (row >= this.term.rows - EDGE_ROWS) return 1;
    return 0;
  }

  private extendSelection(): void {
    const drag = this.drag;
    const range = this.currentRange();
    if (!drag || !range) return;
    const client =
      drag.kind === "word"
        ? drag.point
        : {
            x: drag.point.x + drag.offset.x,
            y: drag.point.y + drag.offset.y,
          };
    const cell = this.cellAt(client);
    if (!cell) return;
    const rowOffset = cell.row * this.term.cols;
    const columns = cellRange(this.term.buffer.active.getLine(cell.row), cell.column);
    const start = rowOffset + columns.start;
    const end = rowOffset + columns.end;
    if (drag.kind === "word") {
      this.select({
        start: Math.min(start, drag.anchor.start),
        end: Math.max(end, drag.anchor.end),
      });
    } else if (drag.kind === "start" && start < range.end) {
      this.select({ start, end: range.end });
    } else if (drag.kind === "end" && end > range.start) {
      this.select({ start: range.start, end });
    }
  }

  private placeHandles(): void {
    if (!this.ownsSelection || this.changingSelection) return;
    const range = this.currentRange();
    if (!range || !this.term.element?.isConnected) {
      this.cancel();
      return;
    }
    const screen = this.screen();
    if (!screen) {
      this.publishHandles(null);
      return;
    }
    const bounds = this.container.getBoundingClientRect();
    const { cols, rows } = this.term;
    const viewport = this.term.buffer.active.viewportY;
    const handlePoint = (offset: number, end: boolean): Point | null => {
      const cell = end ? offset - 1 : offset;
      const row = Math.floor(cell / cols) - viewport;
      if (row < 0 || row >= rows) return null;
      return {
        x: screen.left - bounds.left + ((cell % cols) + Number(end)) * screen.cellWidth,
        y: screen.top - bounds.top + (row + Number(end)) * screen.cellHeight,
      };
    };
    this.publishHandles({
      start: handlePoint(range.start, false),
      end: handlePoint(range.end, true),
    });
  }

  private publishHandles(positions: SelectionHandles | null): void {
    const handle = this.drag?.kind;
    if (handle && handle !== "word" && !positions?.[handle]) {
      this.cancel();
      return;
    }
    if (
      samePoint(positions?.start ?? null, this.positions?.start ?? null) &&
      samePoint(positions?.end ?? null, this.positions?.end ?? null)
    )
      return;
    this.positions = positions;
    this.options.handles(positions);
  }
}
