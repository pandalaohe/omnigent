import {
  ArchiveIcon,
  ArrowDownIcon,
  ArrowLeftIcon,
  ArrowRightIcon,
  ArrowUpIcon,
  ChevronsDownIcon,
  ChevronsUpIcon,
  CommandIcon,
  CornerDownLeftIcon,
  DeleteIcon,
  KeyboardIcon,
  LockKeyhole,
  RefreshCwIcon,
  SquareTerminalIcon,
  XIcon,
  type LucideIcon,
} from "lucide-react";
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
  EMPTY_STICKY_MODIFIERS,
  packRow,
  migrateLegacy,
  reduceSticky,
  softKeyForChord,
  terminalDataForChord,
  type TerminalBar,
  type TerminalBarKey,
  type TerminalBarRow,
  type TerminalKeyIcon,
  type TerminalKeyRowItem,
  type TerminalStickyModifiers,
  type TerminalStickyState,
} from "@/lib/mobileAssistantPreferences";
import { cn } from "@/lib/utils";
import { getIOSKeyboardViewportHeight } from "@/lib/nativeBridge";
import type { StickyModifiers, TerminalSession } from "./TerminalSession";

export {
  DEFAULT_TERMINAL_KEY_ITEMS,
  type TerminalKeyRowItem,
} from "@/lib/mobileAssistantPreferences";

export const TERMINAL_REPEAT_DELAY_MS = 400;
export const TERMINAL_REPEAT_INTERVAL_MS = 80;
export const TERMINAL_LONG_PRESS_MS = 500;
export const TERMINAL_DRAG_SLOP_PX = 10;
export const TERMINAL_SWIPE_THRESHOLD_PX = 36;

export interface TerminalKeyRowProps {
  sessionRef: RefObject<TerminalSession | null>;
  keyboardUp: boolean;
  disabled?: boolean;
  /** Resolved bar. When omitted, legacy items are migrated for compatibility. */
  bar?: TerminalBar;
  items?: readonly TerminalKeyRowItem[];
  secondRow?: boolean;
  /** Changes when the mounted terminal changes, so sticky state cannot leak across. */
  identityKey?: string;
}

interface ActiveKeyPress {
  key: TerminalBarKey;
  pointerId: number;
  startX: number;
  startY: number;
  moved: boolean;
  consumed: boolean;
  timeoutId: number | null;
  intervalId: number | null;
}

interface PagerSwipeStart {
  pointerId: number;
  row: "top" | "bottom";
  startX: number;
  startY: number;
}

const TERMINAL_KEY_ICON_COMPONENTS: Record<TerminalKeyIcon, LucideIcon> = {
  escape: XIcon,
  tab: ArrowRightIcon,
  "arrow-up": ArrowUpIcon,
  "arrow-down": ArrowDownIcon,
  "arrow-left": ArrowLeftIcon,
  "arrow-right": ArrowRightIcon,
  enter: CornerDownLeftIcon,
  backspace: DeleteIcon,
  delete: DeleteIcon,
  "page-up": ChevronsUpIcon,
  "page-down": ChevronsDownIcon,
  keyboard: KeyboardIcon,
  refresh: RefreshCwIcon,
  archive: ArchiveIcon,
  terminal: SquareTerminalIcon,
  command: CommandIcon,
};

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
  const hasSticky =
    sticky && (sticky.ctrl !== "off" || sticky.alt !== "off" || sticky.shift !== "off");
  const options = hasSticky ? { focus: false, chord: item.chord } : { focus: false };
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

/**
 * The single path that turns a bar key into bytes or a sticky change. The phone
 * bar and the floating wheel both call this; neither has its own reducer.
 */
export function pressBarKey(
  session: TerminalSession | null,
  key: TerminalBarKey,
  gesture: "tap" | "longPress" | "repeat",
  keyboardUp?: boolean,
): void {
  if (!session) return;
  if (key.kind === "modifier") {
    const next = reduceSticky(session.getStickyModifiers(), {
      type: gesture === "longPress" ? "longPress" : "tap",
      mod: key.modifier,
      tapLocks: key.tapLocks === true,
    });
    session.setStickyModifiers(next);
    return;
  }
  if (key.kind === "action") {
    toggleTerminalKeyboard(session, keyboardUp);
    return;
  }
  if (key.kind === "text") {
    // Explicit payload: modifiers neither change nor consume it.
    session.sendSoftKey("enter", `${key.text}${key.submit ? "\r" : ""}`, { focus: false });
    return;
  }
  sendTerminalKeyRowItem(
    session,
    { id: key.id, label: key.label, kind: "key", chord: key.chord, repeat: key.repeat },
    session.getStickyModifiers(),
  );
}

function stickyLabel(state: TerminalStickyState): string {
  return state === "off" ? "" : `, ${state === "once" ? "next key" : "locked"}`;
}

function KeyContent({ item }: { item: TerminalBarKey }) {
  const Icon =
    item.display === "icon" && item.icon ? TERMINAL_KEY_ICON_COMPONENTS[item.icon] : null;
  return Icon ? (
    <Icon className="size-3.5 shrink-0" aria-hidden="true" />
  ) : (
    <span className={Array.from(item.label).length === 1 ? "shrink-0" : "truncate"}>
      {item.label}
    </span>
  );
}

function PageButton({
  row,
  page,
  pageCount,
  disabled,
  onAdvance,
}: {
  row: "top" | "bottom";
  page: number;
  pageCount: number;
  disabled?: boolean;
  onAdvance: () => void;
}) {
  const rowLabel = row === "top" ? "Top row" : "Bottom row";
  return (
    <button
      type="button"
      data-page-button={row}
      disabled={disabled}
      aria-label={`${rowLabel}: next page (${page + 1} of ${pageCount})`}
      onClick={onAdvance}
      className="flex h-6 min-w-14 items-center justify-center gap-1 rounded-full border border-border bg-background px-2 text-foreground transition-colors select-none touch-none hover:bg-muted focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-foreground"
    >
      {Array.from({ length: pageCount }, (_, index) => (
        <i
          key={index}
          data-page-dot={String(index)}
          aria-hidden="true"
          className={cn(
            "block rounded-full transition-all",
            index === page ? "h-1.5 w-3.5 bg-foreground" : "size-1.5 bg-foreground/35",
          )}
        />
      ))}
    </button>
  );
}

export function TerminalKeyRow({
  sessionRef,
  keyboardUp,
  disabled = false,
  bar,
  items = DEFAULT_TERMINAL_KEY_ITEMS,
  secondRow = true,
  identityKey,
}: TerminalKeyRowProps) {
  const session = sessionRef.current;
  const resolvedBar = bar ?? migrateLegacy(items, secondRow);
  const topPack = packRow(resolvedBar.top);
  const bottomPack = packRow(resolvedBar.bottom);
  const topPageCount = topPack.pages.length;
  const bottomPageCount = bottomPack.pages.length;
  const [topPage, setTopPage] = useState(0);
  const [bottomPage, setBottomPage] = useState(0);
  const clampedTopPage = Math.max(0, Math.min(topPage, topPageCount - 1));
  const clampedBottomPage = Math.max(0, Math.min(bottomPage, bottomPageCount - 1));
  const topPagerRef = useRef<HTMLDivElement>(null);
  const bottomPagerRef = useRef<HTMLDivElement>(null);
  const previousPages = useRef([clampedTopPage, clampedBottomPage]);

  useEffect(() => {
    const next = [clampedTopPage, clampedBottomPage];
    const previous = previousPages.current;
    previousPages.current = next;
    if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) return;
    const animations = [topPagerRef, bottomPagerRef].flatMap((ref, index) => {
      if (previous[index] === next[index]) return [];
      const animation = ref.current?.animate?.(
        [
          { transform: `translateX(${next[index] > previous[index] ? 12 : -12}px)`, opacity: 0.5 },
          { transform: "translateX(0)", opacity: 1 },
        ],
        { duration: 150, easing: "ease-out" },
      );
      return animation ? [animation] : [];
    });
    return () => animations.forEach((animation) => animation.cancel());
  }, [clampedTopPage, clampedBottomPage]);

  useEffect(() => {
    setTopPage((current) => Math.min(current, topPageCount - 1));
  }, [topPageCount]);
  useEffect(() => {
    setBottomPage((current) => Math.min(current, bottomPageCount - 1));
  }, [bottomPageCount]);

  const [sticky, setSticky] = useState<TerminalStickyModifiers>({
    ...EMPTY_STICKY_MODIFIERS,
  });
  const stickyRef = useRef(sticky);
  const updateSticky = useCallback((next: TerminalStickyModifiers) => {
    stickyRef.current = next;
    setSticky(next);
  }, []);
  const resetSticky = useCallback(() => {
    const reset = { ...EMPTY_STICKY_MODIFIERS };
    stickyRef.current = reset;
    setSticky(reset);
    sessionRef.current?.setStickyModifiers(reset);
  }, [sessionRef]);

  useEffect(() => {
    if (disabled || !session) return;
    session.setStickyModifiers(stickyRef.current);
    const unsubscribe = session.onStickyConsumed(updateSticky);
    return () => {
      unsubscribe();
      const reset = { ...EMPTY_STICKY_MODIFIERS };
      stickyRef.current = reset;
      setSticky(reset);
      session.setStickyModifiers(reset);
    };
  }, [disabled, identityKey, session, updateSticky]);

  const pressRef = useRef<ActiveKeyPress | null>(null);
  const pagerRef = useRef<PagerSwipeStart | null>(null);
  const activePointerIdRef = useRef<number | null>(null);
  const keyboardDraggedRef = useRef(false);
  const keyboardStartRef = useRef<{ pointerId: number; x: number; y: number } | null>(null);

  const stopPress = useCallback(() => {
    const active = pressRef.current;
    if (!active) return;
    if (active.timeoutId !== null) window.clearTimeout(active.timeoutId);
    if (active.intervalId !== null) window.clearInterval(active.intervalId);
    pressRef.current = null;
  }, []);

  const cancelAll = useCallback(() => {
    stopPress();
    pagerRef.current = null;
    if (keyboardStartRef.current) keyboardDraggedRef.current = true;
    keyboardStartRef.current = null;
    activePointerIdRef.current = null;
  }, [stopPress]);

  useEffect(() => cancelAll, [identityKey, session, cancelAll]);

  useEffect(() => {
    if (disabled) {
      cancelAll();
      resetSticky();
    }
  }, [disabled, cancelAll, resetSticky]);

  useEffect(() => {
    const onBlur = () => cancelAll();
    const onVisibility = () => {
      if (document.visibilityState !== "hidden") return;
      cancelAll();
      resetSticky();
    };
    window.addEventListener("blur", onBlur);
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      window.removeEventListener("blur", onBlur);
      document.removeEventListener("visibilitychange", onVisibility);
      cancelAll();
    };
  }, [cancelAll, resetSticky]);

  const fireKey = useCallback(
    (key: TerminalBarKey, gesture: "tap" | "longPress" | "repeat") => {
      if (disabled) return;
      const targetSession = sessionRef.current;
      if (!targetSession) return;
      pressBarKey(targetSession, key, gesture, keyboardUp);
      updateSticky(targetSession.getStickyModifiers());
    },
    [disabled, sessionRef, updateSticky, keyboardUp],
  );

  const handlePointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return;
    // Prevent focus from leaving the terminal or calling up the software keyboard.
    if ((event.target as HTMLElement).closest("[data-page-button]")) return;
    event.preventDefault();
    if (activePointerIdRef.current !== null && activePointerIdRef.current !== event.pointerId) {
      return;
    }

    const target = event.target as HTMLElement | null;
    if (!target) return;

    const buttonEl = target.closest<HTMLButtonElement>("button[data-key-id]");
    const keyId = buttonEl && !buttonEl.disabled ? buttonEl.dataset.keyId : undefined;
    const key = keyId
      ? [...resolvedBar.top.keys, ...resolvedBar.bottom.keys].find(
          (candidate) => candidate.id === keyId,
        )
      : undefined;
    const pager = target.closest<HTMLElement>("[data-pager]");

    if (!pager && (disabled || !buttonEl || buttonEl.disabled || !key)) return;

    activePointerIdRef.current = event.pointerId;
    try {
      if (key?.kind === "action" && buttonEl) buttonEl.setPointerCapture?.(event.pointerId);
      else event.currentTarget.setPointerCapture?.(event.pointerId);
    } catch {
      // Synthetic test events may not register an active pointer id.
    }

    if (pager) {
      pagerRef.current = {
        pointerId: event.pointerId,
        row: pager.dataset.pagerRow === "bottom" ? "bottom" : "top",
        startX: event.clientX,
        startY: event.clientY,
      };
    } else {
      pagerRef.current = null;
    }

    if (disabled || !buttonEl || buttonEl.disabled || !key) return;

    if (key.kind === "action") {
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
      key,
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      moved: false,
      consumed: false,
      timeoutId: null,
      intervalId: null,
    };
    if (key.kind === "modifier") {
      press.timeoutId = window.setTimeout(() => {
        if (pressRef.current !== press || press.moved) return;
        press.consumed = true;
        fireKey(key, "longPress");
      }, TERMINAL_LONG_PRESS_MS);
    } else if (key.kind === "key" && key.repeat) {
      press.timeoutId = window.setTimeout(() => {
        if (pressRef.current !== press || press.moved) return;
        press.consumed = true;
        fireKey(key, "repeat");
        press.intervalId = window.setInterval(() => {
          if (pressRef.current !== press || press.moved) return;
          fireKey(key, "repeat");
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
    const swipe = pagerRef.current;
    pagerRef.current = null;
    if (swipe && swipe.pointerId === event.pointerId) {
      const dx = event.clientX - swipe.startX;
      const dy = event.clientY - swipe.startY;
      if (Math.abs(dx) >= TERMINAL_SWIPE_THRESHOLD_PX && Math.abs(dx) > Math.abs(dy)) {
        stopPress();
        if (swipe.row === "top") {
          setTopPage((current) => {
            const base = Math.max(0, Math.min(current, topPageCount - 1));
            return Math.max(0, Math.min(topPageCount - 1, base + (dx < 0 ? 1 : -1)));
          });
        } else {
          setBottomPage((current) => {
            const base = Math.max(0, Math.min(current, bottomPageCount - 1));
            return Math.max(0, Math.min(bottomPageCount - 1, base + (dx < 0 ? 1 : -1)));
          });
        }
        return;
      }
    }

    const press = pressRef.current;
    if (!press || press.pointerId !== event.pointerId) return;
    const { key, moved, consumed } = press;
    stopPress();
    if (!moved && !consumed) fireKey(key, "tap");
  };

  const handlePointerCancel = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (activePointerIdRef.current !== null && event.pointerId !== activePointerIdRef.current) {
      return;
    }
    cancelAll();
  };

  const handleButtonClick = (key: TerminalBarKey, event: ReactMouseEvent<HTMLButtonElement>) => {
    if (disabled) return;
    if (key.kind === "action") {
      if (event.detail !== 0 && keyboardDraggedRef.current) {
        keyboardDraggedRef.current = false;
        return;
      }
      keyboardDraggedRef.current = false;
      fireKey(key, "tap");
      return;
    }
    if (event.detail === 0) fireKey(key, "tap");
  };

  const renderKey = (key: TerminalBarKey, pinnedCopy = false) => {
    const isKeyboardAction = key.kind === "action" && key.action === "keyboard";
    const modifierState = key.kind === "modifier" ? sticky[key.modifier] : "off";
    const isActive = (isKeyboardAction && keyboardUp) || modifierState !== "off";
    return (
      <button
        key={`${pinnedCopy ? "copy-" : ""}${key.id}`}
        type="button"
        data-key-id={key.id}
        data-pinned={pinnedCopy ? "true" : undefined}
        disabled={disabled}
        aria-label={
          key.kind === "modifier" ? `${key.label}${stickyLabel(modifierState)}` : key.label
        }
        aria-pressed={isKeyboardAction || key.kind === "modifier" ? isActive : undefined}
        data-sticky-state={key.kind === "modifier" ? modifierState : undefined}
        title={key.kind === "modifier" ? `${key.label}: ${modifierState}` : undefined}
        onClick={(event) => handleButtonClick(key, event)}
        style={{ gridColumn: `span ${key.width}` }}
        className={cn(
          "flex h-[44px] min-w-0 items-center justify-center rounded-[7px] border border-border bg-background px-1 font-mono text-[12px] font-semibold text-foreground transition-colors select-none touch-none",
          "active:bg-muted disabled:pointer-events-none disabled:opacity-45",
          isKeyboardAction && isActive && "border-primary/50 bg-primary/15 text-primary",
          modifierState !== "off" &&
            "border-primary-cta bg-primary-cta text-primary-cta-foreground",
          pinnedCopy && "border-dashed opacity-60",
        )}
      >
        <KeyContent item={key} />
        {modifierState === "locked" && (
          <LockKeyhole className="ml-0.5 size-3 shrink-0" aria-hidden="true" />
        )}
      </button>
    );
  };

  const renderRow = (
    row: TerminalBarRow,
    pack: ReturnType<typeof packRow>,
    page: number,
    rowName: "top" | "bottom",
  ) => {
    const pageKeys = pack.pages[page] ?? [];
    const used = pageKeys.reduce((total, key) => total + key.width, 0);
    const gaps = Math.max(0, pack.pagerCells - used);
    const rowLabel = rowName === "top" ? "Top row" : "Bottom row";
    const pageCount = pack.pages.length;
    const pinnedCount = pack.pinned.length;
    return (
      <div
        data-testid={rowName === "top" ? "terminal-key-row-primary" : "terminal-key-row-pager"}
        data-row={rowName}
        data-page={String(page)}
        data-page-count={String(pageCount)}
      >
        <div className="grid grid-cols-12 gap-[2px]">
          {pack.pinned.map((key) => (
            <div key={key.id} className="contents">
              {renderKey(key)}
            </div>
          ))}
          {row.keys.length > pinnedCount ? (
            <div
              ref={rowName === "top" ? topPagerRef : bottomPagerRef}
              data-pager
              data-pager-row={rowName}
              role="group"
              aria-label={`${rowLabel}, page ${page + 1} of ${pageCount}. Swipe or use the page button.`}
              style={{
                gridColumn: `span ${pack.pagerCells}`,
                gridTemplateColumns: `repeat(${pack.pagerCells}, minmax(0, 1fr))`,
              }}
              className="grid touch-none gap-[2px] transition-opacity duration-150 motion-reduce:transition-none"
            >
              {pageKeys.map((key) => renderKey(key))}
              {Array.from({ length: gaps }, (_, index) => (
                <span
                  key={`${rowName}-gap-${page}-${index}`}
                  data-testid="terminal-key-gap"
                  aria-hidden="true"
                  className="h-[44px]"
                />
              ))}
            </div>
          ) : (
            Array.from(
              {
                length: Math.max(0, 12 - pack.pinned.reduce((total, key) => total + key.width, 0)),
              },
              (_, index) => (
                <span
                  key={`${rowName}-empty-${index}`}
                  data-testid="terminal-key-gap"
                  aria-hidden="true"
                  className="h-[44px]"
                />
              ),
            )
          )}
        </div>
      </div>
    );
  };

  const anyPages = topPageCount > 1 || (resolvedBar.bottom.enabled && bottomPageCount > 1);
  return (
    <div
      role="toolbar"
      aria-label="Terminal keys"
      aria-disabled={disabled ? "true" : "false"}
      data-testid="terminal-key-row"
      data-page={String(clampedTopPage)}
      data-page-count={String(topPageCount)}
      data-bottom-page={String(clampedBottomPage)}
      data-bottom-page-count={String(bottomPageCount)}
      onPointerDown={handlePointerDown}
      onPointerMove={handlePointerMove}
      onPointerUp={handlePointerUp}
      onPointerCancel={handlePointerCancel}
      onLostPointerCapture={handlePointerCancel}
      style={{
        paddingLeft: "max(8px, var(--key-row-safe-l, var(--omnigent-safe-left, 0px)))",
        paddingRight: "max(8px, var(--key-row-safe-r, var(--omnigent-safe-right, 0px)))",
      }}
      className="shrink-0 border-t border-border bg-card pt-[2px] pb-0 select-none touch-none"
    >
      <div className="@container">
        <div
          className={cn(
            "grid grid-cols-1 gap-[2px]",
            resolvedBar.bottom.enabled && "@min-[35rem]:grid-cols-12",
          )}
        >
          <div className={cn("min-w-0", resolvedBar.bottom.enabled && "@min-[35rem]:col-span-6")}>
            {renderRow(resolvedBar.top, topPack, clampedTopPage, "top")}
          </div>
          {resolvedBar.bottom.enabled && (
            <div className="min-w-0 @min-[35rem]:col-span-6">
              {renderRow(resolvedBar.bottom, bottomPack, clampedBottomPage, "bottom")}
            </div>
          )}
        </div>
        {anyPages && (
          <div
            data-testid="terminal-key-row-dots"
            className="mt-[2px] flex h-6 items-center justify-between"
          >
            <div>
              {topPageCount > 1 && (
                <PageButton
                  row="top"
                  page={clampedTopPage}
                  pageCount={topPageCount}
                  disabled={disabled}
                  onAdvance={() => setTopPage((current) => (current + 1) % topPageCount)}
                />
              )}
            </div>
            <div>
              {resolvedBar.bottom.enabled && bottomPageCount > 1 && (
                <PageButton
                  row="bottom"
                  page={clampedBottomPage}
                  pageCount={bottomPageCount}
                  disabled={disabled}
                  onAdvance={() => setBottomPage((current) => (current + 1) % bottomPageCount)}
                />
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
