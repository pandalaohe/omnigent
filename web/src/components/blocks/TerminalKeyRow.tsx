import { LockKeyhole } from "lucide-react";
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
  softKeyForChord,
  terminalDataForChord,
  type TerminalBar,
  type TerminalKeyRowItem,
  type TerminalStickyModifiers,
  type TerminalStickyState,
} from "@/lib/mobileAssistantPreferences";
import { TERMINAL_KEY_HEIGHT_PX } from "@/lib/terminalKeyBar";
import {
  isArmedModifier,
  availableAction,
  encodeChord,
  fromBar322,
  keySupportsRepeat,
  resolveSend,
  specialModifier,
  type SurfaceKeys,
  type TerminalKeyRowV2,
} from "@/lib/terminalKeys";
import { TerminalKeyContent } from "./TerminalKeyContent";
import { cn } from "@/lib/utils";
import { getIOSKeyboardViewportHeight } from "@/lib/nativeBridge";
import type { StickyModifiers, TerminalSession } from "./TerminalSession";

export {
  DEFAULT_TERMINAL_KEY_ITEMS,
  type TerminalKeyRowItem,
} from "@/lib/mobileAssistantPreferences";

export const TERMINAL_REPEAT_DELAY_MS = 400;
export const TERMINAL_REPEAT_INTERVAL_MS = 80;
export const TERMINAL_DRAG_SLOP_PX = 10;
export const TERMINAL_SWIPE_THRESHOLD_PX = 36;

export interface TerminalKeyRowProps {
  sessionRef: RefObject<TerminalSession | null>;
  keyboardUp: boolean;
  disabled?: boolean;
  /** Resolved bar. When omitted, legacy items are migrated for compatibility. */
  bar?: TerminalBar;
  surface?: SurfaceKeys;
  items?: readonly TerminalKeyRowItem[];
  secondRow?: boolean;
  /** Changes when the mounted terminal changes, so sticky state cannot leak across. */
  identityKey?: string;
}

type RenderKey = TerminalKeyRowV2["keys"][number];

interface ActiveKeyPress {
  key: RenderKey;
  pointerId: number;
  clipboardTarget?: HTMLButtonElement;
  touchMenuTarget?: HTMLDivElement;
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

function preventTouchContextMenu(event: MouseEvent): void {
  if (!("pointerType" in event) || event.pointerType === "touch") event.preventDefault();
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
  const hasSticky = sticky && Object.values(sticky).some((value) => value !== "off");
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

function pressBarKey(session: TerminalSession, key: RenderKey, keyboardUp: boolean): void {
  const action = availableAction(key);
  if (!action) return;
  const sticky = session.getStickyModifiers();
  switch (action.type) {
    case "special": {
      const modifier = specialModifier(key);
      if (modifier) {
        session.setStickyModifiers({
          ...sticky,
          [modifier]: isArmedModifier(sticky, modifier)
            ? "off"
            : action.keepHeld
              ? "locked"
              : "once",
        });
      } else session.sendBarSequence(action.special === "tab" ? "\\t" : "\\e", false);
      break;
    }
    case "keyboard":
      toggleTerminalKeyboard(session, keyboardUp);
      break;
    case "legacy": {
      const legacy = action.key;
      if (legacy.kind === "text") {
        session.sendSoftKey("enter", `${legacy.text}${legacy.submit ? "\r" : ""}`, {
          focus: false,
        });
      } else {
        sendTerminalKeyRowItem(session, { id: key.id, label: key.label, ...legacy }, sticky);
      }
      break;
    }
    case "combo":
      session.sendBarChord(action.chord);
      break;
    case "send":
      session.sendBarSequence(action.send, action.autoEnter === true);
      break;
    case "clipboard":
      session.runClipboardAction(action.op, { enter: action.autoEnter === true, keyId: key.id });
      break;
  }
}

function stickyLabel(state: TerminalStickyState): string {
  return state === "off" ? "" : `, ${state === "once" ? "next key" : "locked"}`;
}

function isClipboardPress(key: RenderKey, sticky: TerminalStickyModifiers): boolean {
  const action = availableAction(key);
  if (action?.type === "send") {
    return "clipboard" in resolveSend(action.send, action.autoEnter === true, sticky, false);
  }
  if (action?.type === "combo") return "clipboard" in encodeChord(action.chord, sticky, false);
  if (
    action?.type === "legacy" &&
    action.key.kind === "key" &&
    (isArmedModifier(sticky, "cmd") || isArmedModifier(sticky, "win"))
  )
    return "clipboard" in encodeChord(action.key.chord, sticky, false);
  return action?.type === "clipboard";
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
  surface,
  items = DEFAULT_TERMINAL_KEY_ITEMS,
  secondRow = true,
  identityKey,
}: TerminalKeyRowProps) {
  const session = sessionRef.current;
  const resolvedBar = surface ?? fromBar322(bar ?? migrateLegacy(items, secondRow));
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
    active.touchMenuTarget?.removeEventListener("contextmenu", preventTouchContextMenu);
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
    session?.cancelBarWrites();
  }, [session, stopPress]);

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
    (key: RenderKey, gesture: "tap" | "repeat") => {
      if (disabled) return;
      const targetSession = sessionRef.current;
      if (!targetSession) return;
      if (gesture === "repeat" && isClipboardPress(key, targetSession.getStickyModifiers())) return;
      targetSession.runBarKey(() => {
        pressBarKey(targetSession, key, keyboardUp);
        updateSticky(targetSession.getStickyModifiers());
      }, gesture === "repeat");
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
      if (key?.action.type === "keyboard" && buttonEl)
        buttonEl.setPointerCapture?.(event.pointerId);
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

    if (key.action.type === "keyboard") {
      keyboardDraggedRef.current = false;
      keyboardStartRef.current = {
        pointerId: event.pointerId,
        x: event.clientX,
        y: event.clientY,
      };
      return;
    }

    stopPress();
    const clipboardTarget =
      session && isClipboardPress(key, session.getStickyModifiers()) ? buttonEl : undefined;
    const press: ActiveKeyPress = {
      key,
      pointerId: event.pointerId,
      clipboardTarget,
      startX: event.clientX,
      startY: event.clientY,
      moved: false,
      consumed: false,
      timeoutId: null,
      intervalId: null,
    };
    if (clipboardTarget) {
      press.timeoutId = window.setTimeout(() => {
        press.consumed = true;
      }, TERMINAL_REPEAT_DELAY_MS);
    } else if (key.repeat && keySupportsRepeat(key)) {
      if (event.pointerType === "touch") {
        // Opening the browser's long-press menu can cancel the active pointer.
        press.touchMenuTarget = event.currentTarget;
        press.touchMenuTarget.addEventListener("contextmenu", preventTouchContextMenu);
      }
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
            return dx < 0 ? (base + 1) % topPageCount : (base - 1 + topPageCount) % topPageCount;
          });
        } else {
          setBottomPage((current) => {
            const base = Math.max(0, Math.min(current, bottomPageCount - 1));
            return dx < 0
              ? (base + 1) % bottomPageCount
              : (base - 1 + bottomPageCount) % bottomPageCount;
          });
        }
        return;
      }
    }

    const press = pressRef.current;
    if (!press || press.pointerId !== event.pointerId) return;
    const { key, moved, consumed, clipboardTarget } = press;
    stopPress();
    if (
      clipboardTarget &&
      document.elementFromPoint &&
      !clipboardTarget.contains(document.elementFromPoint(event.clientX, event.clientY))
    )
      return;
    if (!moved && !consumed) fireKey(key, "tap");
  };

  const handlePointerCancel = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.pointerId === activePointerIdRef.current) cancelAll();
  };

  const handleButtonClick = (key: RenderKey, event: ReactMouseEvent<HTMLButtonElement>) => {
    if (disabled) return;
    if (key.action.type === "keyboard") {
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

  const renderKey = (key: RenderKey, pinnedCopy = false) => {
    const isKeyboardAction = key.action.type === "keyboard";
    const modifier = specialModifier(key);
    const modifierState = (modifier && sticky[modifier]) || "off";
    const available = availableAction(key);
    const unavailableReason = !available
      ? key.action.type === "clipboard"
        ? "Not available"
        : "Needs a newer version"
      : undefined;
    const isActive = (isKeyboardAction && keyboardUp) || modifierState !== "off";
    let functionLabel = "";
    if (available?.type === "special" && modifier) {
      functionLabel =
        modifierState === "off"
          ? available.keepHeld
            ? ", holds until tapped again"
            : ", holds for next key"
          : stickyLabel(modifierState);
    } else if (available?.type === "send") functionLabel = ", sends a terminal sequence";
    else if (available?.type === "combo") functionLabel = ", sends a terminal shortcut";
    return (
      <button
        key={`${pinnedCopy ? "copy-" : ""}${key.id}`}
        type="button"
        data-key-id={key.id}
        data-pinned={pinnedCopy ? "true" : undefined}
        disabled={disabled || !!unavailableReason}
        aria-label={
          unavailableReason ? `${key.label}, ${unavailableReason}` : `${key.label}${functionLabel}`
        }
        aria-pressed={isKeyboardAction || modifier ? isActive : undefined}
        data-sticky-state={modifier ? modifierState : undefined}
        title={unavailableReason ?? (modifier ? `${key.label}: ${modifierState}` : undefined)}
        onClick={(event) => handleButtonClick(key, event)}
        style={{ gridColumn: `span ${key.width}`, height: TERMINAL_KEY_HEIGHT_PX }}
        className={cn(
          "flex min-w-0 items-center justify-center rounded-[7px] border border-border bg-background px-1 font-mono text-[12px] font-semibold text-foreground transition-colors select-none touch-none",
          "active:bg-muted disabled:pointer-events-none disabled:opacity-45",
          key.repeat && keySupportsRepeat(key) && "[-webkit-touch-callout:none]",
          isKeyboardAction && isActive && "border-primary/50 bg-primary/15 text-primary",
          modifierState !== "off" &&
            "border-primary-cta bg-primary-cta text-primary-cta-foreground",
          key.style === "danger" && "text-destructive",
          pinnedCopy && "border-dashed opacity-60",
        )}
      >
        <TerminalKeyContent button={unavailableReason ? { ...key, display: "text" } : key} />
        {modifierState === "locked" && (
          <LockKeyhole className="ml-0.5 size-3 shrink-0" aria-hidden="true" />
        )}
      </button>
    );
  };

  const renderRow = (
    row: TerminalKeyRowV2,
    pack: ReturnType<typeof packRow<RenderKey>>,
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
                  style={{ height: TERMINAL_KEY_HEIGHT_PX }}
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
                  style={{ height: TERMINAL_KEY_HEIGHT_PX }}
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
