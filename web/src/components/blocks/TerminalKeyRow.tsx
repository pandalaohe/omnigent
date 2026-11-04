import { Keyboard, LockKeyhole } from "lucide-react";
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type MouseEvent as ReactMouseEvent,
  type PointerEvent as ReactPointerEvent,
  type RefObject,
} from "react";
import {
  DEFAULT_TERMINAL_KEY_ITEMS,
  softKeyForChord,
  terminalDataForChord,
  type TerminalKeyRowItem,
} from "@/lib/mobileAssistantPreferences";
import { cn } from "@/lib/utils";
import { getIOSKeyboardViewportHeight } from "@/lib/nativeBridge";
import type { StickyModifiers, TerminalSession } from "./TerminalSession";

export {
  DEFAULT_TERMINAL_KEY_ITEMS,
  type TerminalKeyRowItem,
} from "@/lib/mobileAssistantPreferences";

type PressableItem = Extract<TerminalKeyRowItem, { kind: "key" | "modifier" }>;

export const TERMINAL_KEY_ROW_COLS = 6;
export const TERMINAL_REPEAT_DELAY_MS = 400;
export const TERMINAL_REPEAT_INTERVAL_MS = 80;
export const TERMINAL_DRAG_SLOP_PX = 10;
export const TERMINAL_SWIPE_THRESHOLD_PX = 36;

export interface TerminalKeyRowProps {
  sessionRef: RefObject<TerminalSession | null>;
  keyboardUp: boolean;
  disabled?: boolean;
  items?: readonly TerminalKeyRowItem[];
  secondRow?: boolean;
}

interface ActiveKeyPress {
  item: PressableItem;
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

export function toggleTerminalKeyboard(
  session: TerminalSession | null,
  keyboardUp?: boolean,
): void {
  if (!session) return;
  const nativeHeight = getIOSKeyboardViewportHeight();
  const visibleBottom =
    nativeHeight ??
    (window.visualViewport
      ? window.visualViewport.offsetTop + window.visualViewport.height
      : window.innerHeight);
  const isKeyboardUp =
    keyboardUp ?? window.innerHeight - visibleBottom > (nativeHeight === null ? 80 : 0);
  const focused = session.isFocused?.() ?? Boolean(document.activeElement?.closest(".xterm"));
  if (focused) session.blur?.();
  if (!focused || !isKeyboardUp) session.focus();
}

export function sendTerminalKeyRowItem(
  session: TerminalSession | null,
  item: Extract<TerminalKeyRowItem, { kind: "key" }>,
  sticky?: StickyModifiers,
): void {
  if (!session) return;
  const options =
    sticky && (sticky.ctrl !== "off" || sticky.alt !== "off")
      ? { focus: false, chord: item.chord }
      : { focus: false };
  const softKey = softKeyForChord(item.chord);
  if (softKey) {
    session.sendSoftKey(softKey, undefined, options);
    return;
  }
  const data = terminalDataForChord(item.chord);
  if (data !== null) {
    session.sendSoftKey("enter", data, options);
  }
}

export function TerminalKeyRow({
  sessionRef,
  keyboardUp,
  disabled = false,
  items = DEFAULT_TERMINAL_KEY_ITEMS,
  secondRow = true,
}: TerminalKeyRowProps) {
  const pageCount = Math.max(
    1,
    Math.ceil(Math.max(0, items.length - TERMINAL_KEY_ROW_COLS) / TERMINAL_KEY_ROW_COLS),
  );
  const [page, setPage] = useState(0);
  const [sticky, setSticky] = useState<StickyModifiers>({ ctrl: "off", alt: "off" });
  const stickyRef = useRef(sticky);
  const updateSticky = useCallback((next: StickyModifiers) => {
    stickyRef.current = next;
    setSticky(next);
  }, []);

  useEffect(() => {
    const session = sessionRef.current;
    if (disabled || !session) return;
    session.setStickyModifiers(stickyRef.current);
    const unsubscribe = session.onStickyConsumed(updateSticky);
    return () => {
      unsubscribe();
      session.setStickyModifiers({ ctrl: "off", alt: "off" });
    };
  }, [disabled, sessionRef, updateSticky]);
  const clampedPage = Math.max(0, Math.min(page, pageCount - 1));

  const pressRef = useRef<ActiveKeyPress | null>(null);
  const pagerRef = useRef<PagerSwipeStart | null>(null);
  const activePointerIdRef = useRef<number | null>(null);
  const keyboardDraggedRef = useRef(false);
  const keyboardStartRef = useRef<{ pointerId: number; x: number; y: number } | null>(null);

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
    activePointerIdRef.current = null;
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
    (item: PressableItem) => {
      if (disabled) return;
      const session = sessionRef.current;
      if (!session) return;
      if (item.kind === "modifier") {
        const state = stickyRef.current[item.modifier];
        const next: StickyModifiers = {
          ...stickyRef.current,
          [item.modifier]: state === "off" ? "once" : state === "once" ? "locked" : "off",
        };
        session.setStickyModifiers(next);
        updateSticky(next);
      } else {
        sendTerminalKeyRowItem(session, item, stickyRef.current);
      }
    },
    [disabled, sessionRef, updateSticky],
  );

  const handlePointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return;
    // Prevent focus from leaving the terminal or calling up the software keyboard.
    event.preventDefault();
    if (activePointerIdRef.current !== null && activePointerIdRef.current !== event.pointerId) {
      return;
    }

    const target = event.target as HTMLElement | null;
    if (!target) return;

    const buttonEl = target.closest<HTMLButtonElement>("button[data-key-id]");
    const keyId = buttonEl && !buttonEl.disabled ? buttonEl.dataset.keyId : undefined;
    const item = keyId ? items.find((candidate) => candidate.id === keyId) : undefined;
    const inPager = Boolean(target.closest("[data-pager]"));

    if (!inPager && (disabled || !buttonEl || buttonEl.disabled || !item)) {
      return;
    }

    activePointerIdRef.current = event.pointerId;

    try {
      if (item?.kind === "action" && buttonEl) {
        buttonEl.setPointerCapture?.(event.pointerId);
      } else {
        event.currentTarget.setPointerCapture?.(event.pointerId);
      }
    } catch {
      // Synthetic test events may not register an active pointer id.
    }

    if (inPager) {
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
      keyboardStartRef.current = {
        pointerId: event.pointerId,
        x: event.clientX,
        y: event.clientY,
      };
      return;
    }

    stopPress();
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
    if (activePointerIdRef.current !== null && event.pointerId !== activePointerIdRef.current) {
      return;
    }
    const kbStart = keyboardStartRef.current;
    if (
      kbStart &&
      kbStart.pointerId === event.pointerId &&
      Math.hypot(event.clientX - kbStart.x, event.clientY - kbStart.y) >= TERMINAL_DRAG_SLOP_PX
    ) {
      keyboardDraggedRef.current = true;
    }

    const press = pressRef.current;
    if (!press || press.pointerId !== event.pointerId) return;
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
    if (activePointerIdRef.current !== null && event.pointerId !== activePointerIdRef.current) {
      return;
    }
    activePointerIdRef.current = null;
    keyboardStartRef.current = null;
    const pager = pagerRef.current;
    pagerRef.current = null;
    if (pager && pager.pointerId === event.pointerId) {
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
    if (!press || press.pointerId !== event.pointerId) return;
    const { item, moved, repeated } = press;
    stopPress();
    if (!moved && !repeated) {
      fireKey(item);
    }
  };

  const handlePointerCancel = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (activePointerIdRef.current !== null && event.pointerId !== activePointerIdRef.current) {
      return;
    }
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
      toggleTerminalKeyboard(sessionRef.current, keyboardUp);
      return;
    }

    if (event.detail === 0 && item.kind !== "action") {
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
      const modifierState = item.kind === "modifier" ? sticky[item.modifier] : "off";
      const isActive = (isKeyboardAction && keyboardUp) || modifierState !== "off";
      return (
        <button
          key={item.id}
          type="button"
          data-key-id={item.id}
          disabled={disabled}
          aria-label={item.label}
          aria-pressed={isKeyboardAction || item.kind === "modifier" ? isActive : undefined}
          data-sticky-state={item.kind === "modifier" ? modifierState : undefined}
          title={item.kind === "modifier" ? `${item.label}: ${modifierState}` : undefined}
          onClick={(event) => handleButtonClick(item, event)}
          className={cn(
            "flex h-[44px] min-w-[44px] items-center justify-center rounded-[7px] border border-border bg-background px-1 font-mono text-[12px] font-semibold text-foreground transition-colors select-none touch-none",
            "active:bg-muted disabled:pointer-events-none disabled:opacity-45",
            isKeyboardAction && isActive && "border-primary/50 bg-primary/15 text-primary",
            modifierState !== "off" &&
              "border-primary-cta bg-primary-cta text-primary-cta-foreground",
          )}
        >
          {isKeyboardAction ? (
            <Keyboard className="size-3.5 shrink-0" aria-hidden="true" />
          ) : (
            <>
              <span className="truncate">{item.label}</span>
              {modifierState === "locked" && (
                <LockKeyhole className="ml-0.5 size-3 shrink-0" aria-hidden="true" />
              )}
            </>
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
      className="shrink-0 border-t border-border bg-card pt-[2px] pb-0 select-none touch-none"
    >
      <div className="@container">
        <div className={cn("grid grid-cols-1 gap-[2px]", secondRow && "@min-[35rem]:grid-cols-12")}>
          <div
            data-testid="terminal-key-row-primary"
            className={cn("grid grid-cols-6 gap-[2px]", secondRow && "@min-[35rem]:col-span-6")}
          >
            {renderCells(firstRow, "row1")}
          </div>
          {secondRow && (
            <div
              data-testid="terminal-key-row-pager"
              data-pager
              role="group"
              aria-label={`More keys, page ${clampedPage + 1} of ${pageCount}. Swipe left or right.`}
              className="grid grid-cols-6 gap-[2px] touch-none @min-[35rem]:col-span-6"
            >
              {renderCells(secondRowPage, `page-${clampedPage}`)}
            </div>
          )}
        </div>
        {secondRow && (
          <div
            data-testid="terminal-key-row-dots"
            aria-hidden="true"
            className="flex h-[6px] items-center justify-center gap-1 @min-[35rem]:h-[8px]"
          >
            {Array.from({ length: pageCount }, (_, index) => (
              <i
                key={index}
                data-page-dot={String(index)}
                className={cn(
                  "block h-[3px] rounded-full transition-all",
                  index === clampedPage ? "w-[10px] bg-muted-foreground" : "w-[3px] bg-border",
                )}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
