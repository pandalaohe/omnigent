import { Keyboard } from "lucide-react";
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type MouseEvent as ReactMouseEvent,
  type PointerEvent as ReactPointerEvent,
  type RefObject,
} from "react";
import type { ShortcutChord } from "@/lib/keyboardShortcutPreferences";
import { softKeyForChord, terminalDataForChord } from "@/lib/mobileAssistantPreferences";
import { cn } from "@/lib/utils";
import type { TerminalSession } from "./TerminalSession";

export type TerminalKeyRowItem =
  | {
      id: string;
      label: string;
      kind: "key";
      chord: ShortcutChord;
      repeat?: boolean;
    }
  | {
      id: string;
      label: string;
      kind: "action";
      action: "keyboard";
    };

export const TERMINAL_KEY_ROW_COLS = 6;
export const TERMINAL_REPEAT_DELAY_MS = 400;
export const TERMINAL_REPEAT_INTERVAL_MS = 80;
export const TERMINAL_DRAG_SLOP_PX = 10;
export const TERMINAL_SWIPE_THRESHOLD_PX = 36;

/**
 * Fixed default terminal key list for Slice S1 (Ctrl and Alt sticky modifier
 * slots land in S5). First 6 keys populate Row 1; remaining keys fill 6-slot
 * pages in Row 2.
 */
export const DEFAULT_TERMINAL_KEY_ITEMS: readonly TerminalKeyRowItem[] = [
  // Row 1 (fixed)
  { id: "esc", label: "Esc", kind: "key", chord: { code: "Escape", modifiers: [] } },
  {
    id: "up",
    label: "↑",
    kind: "key",
    chord: { code: "ArrowUp", modifiers: [] },
    repeat: true,
  },
  {
    id: "down",
    label: "↓",
    kind: "key",
    chord: { code: "ArrowDown", modifiers: [] },
    repeat: true,
  },
  { id: "enter", label: "↵", kind: "key", chord: { code: "Enter", modifiers: [] } },
  { id: "ctrl-c", label: "^C", kind: "key", chord: { code: "KeyC", modifiers: ["control"] } },
  { id: "keyboard", label: "Keyboard", kind: "action", action: "keyboard" },
  // Row 2, Page 1
  { id: "tab", label: "Tab", kind: "key", chord: { code: "Tab", modifiers: [] } },
  { id: "shift-tab", label: "⇧Tab", kind: "key", chord: { code: "Tab", modifiers: ["shift"] } },
  {
    id: "left",
    label: "←",
    kind: "key",
    chord: { code: "ArrowLeft", modifiers: [] },
    repeat: true,
  },
  {
    id: "right",
    label: "→",
    kind: "key",
    chord: { code: "ArrowRight", modifiers: [] },
    repeat: true,
  },
  { id: "slash", label: "/", kind: "key", chord: { code: "Slash", modifiers: [] } },
  { id: "pgup", label: "PgUp", kind: "key", chord: { code: "PageUp", modifiers: [] } },
  // Row 2, Page 2
  { id: "pgdn", label: "PgDn", kind: "key", chord: { code: "PageDown", modifiers: [] } },
  { id: "home", label: "Home", kind: "key", chord: { code: "Home", modifiers: [] } },
  { id: "end", label: "End", kind: "key", chord: { code: "End", modifiers: [] } },
  { id: "ctrl-d", label: "^D", kind: "key", chord: { code: "KeyD", modifiers: ["control"] } },
];

export interface TerminalKeyRowProps {
  sessionRef: RefObject<TerminalSession | null>;
  keyboardUp: boolean;
  disabled?: boolean;
  items?: readonly TerminalKeyRowItem[];
}

interface ActiveKeyPress {
  item: Extract<TerminalKeyRowItem, { kind: "key" }>;
  pointerId: number;
  startX: number;
  startY: number;
  moved: boolean;
  repeated: boolean;
  timeoutId: number | null;
  intervalId: number | null;
}

interface PagerSwipeStart {
  pointerId: number;
  startX: number;
  startY: number;
}

export function sendTerminalKeyRowItem(
  session: TerminalSession | null,
  item: Extract<TerminalKeyRowItem, { kind: "key" }>,
): void {
  if (!session) return;
  const softKey = softKeyForChord(item.chord);
  if (softKey) {
    session.sendSoftKey(softKey, undefined, { focus: false });
    return;
  }
  const data = terminalDataForChord(item.chord);
  if (data !== null) {
    session.sendSoftKey("enter", data, { focus: false });
  }
}

export function TerminalKeyRow({
  sessionRef,
  keyboardUp,
  disabled = false,
  items = DEFAULT_TERMINAL_KEY_ITEMS,
}: TerminalKeyRowProps) {
  const pageCount = Math.max(
    1,
    Math.ceil(Math.max(0, items.length - TERMINAL_KEY_ROW_COLS) / TERMINAL_KEY_ROW_COLS),
  );
  const [page, setPage] = useState(0);
  const clampedPage = Math.max(0, Math.min(page, pageCount - 1));

  const pressRef = useRef<ActiveKeyPress | null>(null);
  const pagerRef = useRef<PagerSwipeStart | null>(null);
  const pointerHandledRef = useRef(false);
  const keyboardDraggedRef = useRef(false);
  const keyboardStartRef = useRef<{ x: number; y: number } | null>(null);

  const stopPress = useCallback(() => {
    const active = pressRef.current;
    if (!active) return;
    if (active.timeoutId !== null) {
      window.clearTimeout(active.timeoutId);
    }
    if (active.intervalId !== null) {
      window.clearInterval(active.intervalId);
    }
    pressRef.current = null;
  }, []);

  const cancelAll = useCallback(() => {
    stopPress();
    pagerRef.current = null;
    keyboardStartRef.current = null;
  }, [stopPress]);

  useEffect(() => {
    if (disabled) {
      cancelAll();
    }
  }, [disabled, cancelAll]);

  useEffect(() => {
    const onBlur = () => cancelAll();
    window.addEventListener("blur", onBlur);
    return () => {
      window.removeEventListener("blur", onBlur);
      cancelAll();
    };
  }, [cancelAll]);

  const fireKey = useCallback(
    (item: Extract<TerminalKeyRowItem, { kind: "key" }>) => {
      if (disabled) return;
      sendTerminalKeyRowItem(sessionRef.current, item);
    },
    [disabled, sessionRef],
  );

  const handlePointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return;
    // Prevent focus from leaving the terminal or calling up the software keyboard.
    event.preventDefault();

    const target = event.target as HTMLElement | null;
    if (!target) return;

    const buttonEl = target.closest<HTMLButtonElement>("button[data-key-id]");
    const keyId = buttonEl && !buttonEl.disabled ? buttonEl.dataset.keyId : undefined;
    const item = keyId ? items.find((candidate) => candidate.id === keyId) : undefined;

    try {
      if (item?.kind === "action" && buttonEl) {
        buttonEl.setPointerCapture?.(event.pointerId);
      } else {
        event.currentTarget.setPointerCapture?.(event.pointerId);
      }
    } catch {
      // Synthetic test events may not register an active pointer id.
    }

    if (target.closest("[data-pager]")) {
      pagerRef.current = {
        pointerId: event.pointerId,
        startX: event.clientX,
        startY: event.clientY,
      };
    } else {
      pagerRef.current = null;
    }

    if (disabled || !buttonEl || buttonEl.disabled || !item) return;

    if (item.kind === "action") {
      keyboardDraggedRef.current = false;
      keyboardStartRef.current = { x: event.clientX, y: event.clientY };
      return;
    }

    stopPress();
    pointerHandledRef.current = true;
    const press: ActiveKeyPress = {
      item,
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      moved: false,
      repeated: false,
      timeoutId: null,
      intervalId: null,
    };
    if (item.repeat) {
      press.timeoutId = window.setTimeout(() => {
        if (pressRef.current !== press || press.moved) return;
        press.repeated = true;
        fireKey(item);
        press.intervalId = window.setInterval(() => {
          if (pressRef.current !== press || press.moved) return;
          fireKey(item);
        }, TERMINAL_REPEAT_INTERVAL_MS);
      }, TERMINAL_REPEAT_DELAY_MS);
    }
    pressRef.current = press;
  };

  const handlePointerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    const kbStart = keyboardStartRef.current;
    if (
      kbStart &&
      Math.hypot(event.clientX - kbStart.x, event.clientY - kbStart.y) >= TERMINAL_DRAG_SLOP_PX
    ) {
      keyboardDraggedRef.current = true;
    }

    const press = pressRef.current;
    if (!press) return;
    if (
      Math.hypot(event.clientX - press.startX, event.clientY - press.startY) >=
      TERMINAL_DRAG_SLOP_PX
    ) {
      press.moved = true;
      if (press.timeoutId !== null) {
        window.clearTimeout(press.timeoutId);
        press.timeoutId = null;
      }
      if (press.intervalId !== null) {
        window.clearInterval(press.intervalId);
        press.intervalId = null;
      }
    }
  };

  const handlePointerUp = (event: ReactPointerEvent<HTMLDivElement>) => {
    keyboardStartRef.current = null;
    const pager = pagerRef.current;
    pagerRef.current = null;
    if (pager) {
      const dx = event.clientX - pager.startX;
      const dy = event.clientY - pager.startY;
      if (Math.abs(dx) >= TERMINAL_SWIPE_THRESHOLD_PX && Math.abs(dx) > Math.abs(dy)) {
        stopPress();
        setPage((current) => {
          const base = Math.max(0, Math.min(current, pageCount - 1));
          return Math.max(0, Math.min(pageCount - 1, base + (dx < 0 ? 1 : -1)));
        });
        return;
      }
    }

    const press = pressRef.current;
    if (!press) return;
    const { item, moved, repeated } = press;
    stopPress();
    if (!moved && !repeated) {
      fireKey(item);
    }
  };

  const handlePointerCancel = () => {
    cancelAll();
  };

  const handleButtonClick = (
    item: TerminalKeyRowItem,
    event: ReactMouseEvent<HTMLButtonElement>,
  ) => {
    if (disabled) return;
    if (item.kind === "action" && item.action === "keyboard") {
      if (keyboardDraggedRef.current) {
        keyboardDraggedRef.current = false;
        return;
      }
      const session = sessionRef.current;
      if (!session) return;
      const focused = session.isFocused?.() ?? Boolean(document.activeElement?.closest(".xterm"));
      if (focused && keyboardUp) {
        session.blur?.();
      } else {
        if (focused) {
          session.blur?.();
        }
        session.focus();
      }
      return;
    }

    if (pointerHandledRef.current) {
      pointerHandledRef.current = false;
      return;
    }
    if (event.detail === 0 && item.kind === "key") {
      fireKey(item);
    }
  };

  const firstRow = items.slice(0, TERMINAL_KEY_ROW_COLS);
  const secondRowPage = items.slice(
    TERMINAL_KEY_ROW_COLS + clampedPage * TERMINAL_KEY_ROW_COLS,
    TERMINAL_KEY_ROW_COLS + (clampedPage + 1) * TERMINAL_KEY_ROW_COLS,
  );

  const renderCells = (rowItems: readonly TerminalKeyRowItem[], prefix: string) =>
    Array.from({ length: TERMINAL_KEY_ROW_COLS }, (_, index) => {
      const item = rowItems[index];
      if (!item) {
        return (
          <span
            key={`${prefix}-gap-${index}`}
            data-testid="terminal-key-gap"
            aria-hidden="true"
            className="h-[44px]"
          />
        );
      }
      const isKeyboardAction = item.kind === "action" && item.action === "keyboard";
      const isActive = isKeyboardAction && keyboardUp;
      return (
        <button
          key={item.id}
          type="button"
          data-key-id={item.id}
          disabled={disabled}
          aria-label={item.label}
          aria-pressed={isKeyboardAction ? isActive : undefined}
          onClick={(event) => handleButtonClick(item, event)}
          className={cn(
            "flex h-[44px] min-w-[44px] items-center justify-center rounded-[7px] border border-border bg-background px-1 font-mono text-[12px] font-semibold text-foreground transition-colors select-none touch-manipulation",
            "active:bg-muted disabled:pointer-events-none disabled:opacity-45",
            isActive && "border-primary/50 bg-primary/15 text-primary",
          )}
        >
          {isKeyboardAction ? (
            <Keyboard className="size-3.5 shrink-0" aria-hidden="true" />
          ) : (
            <span className="truncate">{item.label}</span>
          )}
        </button>
      );
    });

  return (
    <div
      role="toolbar"
      aria-label="Terminal keys"
      aria-disabled={disabled ? "true" : "false"}
      data-testid="terminal-key-row"
      data-page={String(clampedPage)}
      data-page-count={String(pageCount)}
      onPointerDown={handlePointerDown}
      onPointerMove={handlePointerMove}
      onPointerUp={handlePointerUp}
      onPointerCancel={handlePointerCancel}
      style={{
        paddingLeft: "max(8px, var(--key-row-safe-l, var(--omnigent-safe-left, 0px)))",
        paddingRight: "max(8px, var(--key-row-safe-r, var(--omnigent-safe-right, 0px)))",
      }}
      className={cn(
        "shrink-0 border-t border-border bg-card pt-[2px] pb-0 select-none touch-manipulation",
        "[:is([data-ios-native],[data-android-native])_[data-testid=terminals-panel]_&]:[--key-row-safe-l:0px]",
        "[:is([data-ios-native],[data-android-native])_[data-testid=terminals-panel]_&]:[--key-row-safe-r:0px]",
      )}
    >
      <div className="grid grid-cols-1 gap-[2px] @min-[35rem]:grid-cols-12">
        <div
          data-testid="terminal-key-row-primary"
          className="grid grid-cols-6 gap-[2px] @min-[35rem]:col-span-6"
        >
          {renderCells(firstRow, "row1")}
        </div>
        <div
          data-testid="terminal-key-row-pager"
          data-pager
          role="group"
          aria-label={`More keys, page ${clampedPage + 1} of ${pageCount}. Swipe left or right.`}
          className="grid grid-cols-6 gap-[2px] @min-[35rem]:col-span-6"
        >
          {renderCells(secondRowPage, `page-${clampedPage}`)}
        </div>
      </div>
      <div
        data-testid="terminal-key-row-dots"
        aria-hidden="true"
        onClick={() => {
          if (disabled || pageCount <= 1) return;
          setPage((current) => (current + 1) % pageCount);
        }}
        className="flex h-[6px] items-center justify-center gap-1 @min-[35rem]:h-[8px]"
      >
        {Array.from({ length: pageCount }, (_, index) => (
          <i
            key={index}
            data-page-dot={String(index)}
            onClick={(event) => {
              event.stopPropagation();
              if (disabled) return;
              setPage(index);
            }}
            className={cn(
              "block h-[3px] rounded-full transition-all",
              index === clampedPage ? "w-[10px] bg-muted-foreground" : "w-[3px] bg-border",
            )}
          />
        ))}
      </div>
    </div>
  );
}
