import { useLayoutEffect, useState, type RefObject } from "react";
import {
  getIOSKeyboardViewportHeight,
  isIOSShell,
  onNativeKeyboardViewportChanged,
} from "@/lib/nativeBridge";

export interface TerminalKeyboardRoom {
  overlap: number;
  keyboardUp: boolean;
  roomPx: number;
  rootWidth: number;
}

const DISABLED_ROOM: TerminalKeyboardRoom = {
  overlap: 0,
  keyboardUp: false,
  roomPx: Number.POSITIVE_INFINITY,
  rootWidth: 0,
};

/**
 * Measure the visible bottom edge of the viewport against a TerminalView root.
 *
 * Unifies the iOS native bridge height (`getIOSKeyboardViewportHeight`),
 * `window.visualViewport`, and `window.innerHeight` into a single bottom-edge
 * overlap measurement coalesced by `requestAnimationFrame`.
 */
export function useTerminalKeyboardRoom(
  root: RefObject<HTMLElement | null>,
  enabled: boolean,
  reservesBottomSafeArea = true,
): TerminalKeyboardRoom {
  const [room, setRoom] = useState<TerminalKeyboardRoom>(DISABLED_ROOM);

  useLayoutEffect(() => {
    const el = root.current;
    if (!enabled || !el || typeof window === "undefined") {
      if (el) {
        el.removeAttribute("data-keyboard-overlap");
        el.removeAttribute("data-keyboard-up");
      }
      setRoom((prev) => (prev === DISABLED_ROOM ? prev : DISABLED_ROOM));
      return;
    }

    let rafId: number | null = null;
    let scheduled = false;

    const measure = () => {
      const current = root.current;
      if (!current || typeof window === "undefined") return;
      const vv = window.visualViewport;
      const nativeHeight = getIOSKeyboardViewportHeight();
      const visibleBottom =
        nativeHeight !== null ? nativeHeight : vv ? vv.offsetTop + vv.height : window.innerHeight;
      const rect = current.getBoundingClientRect();
      const overlap = Math.max(0, Math.round(rect.bottom - visibleBottom));
      const keyboardUp =
        nativeHeight !== null
          ? window.innerHeight - nativeHeight > 0
          : window.innerHeight - visibleBottom > 80;

      if (!isIOSShell() && keyboardUp && ((vv && vv.offsetTop > 0) || window.scrollY > 0)) {
        window.scrollTo(0, 0);
      }

      current.setAttribute("data-keyboard-overlap", String(overlap));
      current.setAttribute("data-keyboard-up", String(keyboardUp));

      let safeBottomPx = 0;
      if (reservesBottomSafeArea && !keyboardUp) {
        const computed = window.getComputedStyle(current);
        if (computed.getPropertyValue("--term-safe-b").trim() !== "0px") {
          const prevInlinePadding = current.style.paddingBottom;
          if (prevInlinePadding) {
            current.style.paddingBottom =
              "var(--term-safe-b, var(--omnigent-safe-bottom, env(safe-area-inset-bottom, 0px)))";
          }
          const resolvedPadding =
            parseFloat(window.getComputedStyle(current).paddingBottom || "0") || 0;
          if (current.style.paddingBottom !== prevInlinePadding) {
            current.style.paddingBottom = prevInlinePadding;
          }
          const varPx =
            parseFloat(computed.getPropertyValue("--omnigent-inset-bottom") || "0") ||
            parseFloat(computed.getPropertyValue("--omnigent-safe-bottom") || "0") ||
            parseFloat(computed.getPropertyValue("--omnigent-android-safe-area-bottom") || "0") ||
            0;
          safeBottomPx = Math.max(resolvedPadding, varPx);
        }
      }
      const appliedPadding = Math.max(overlap, keyboardUp ? 0 : safeBottomPx);
      const rawHeight = rect.height > 0 || rect.bottom > 0 ? rect.height : window.innerHeight;
      const roomPx = Math.max(0, Math.round(rawHeight - appliedPadding));
      const rootWidth = rect.width > 0 ? Math.round(rect.width) : window.innerWidth;

      setRoom((prev) =>
        prev.overlap === overlap &&
        prev.keyboardUp === keyboardUp &&
        prev.roomPx === roomPx &&
        prev.rootWidth === rootWidth
          ? prev
          : { overlap, keyboardUp, roomPx, rootWidth },
      );
    };

    const schedule = () => {
      if (scheduled) return;
      scheduled = true;
      rafId = window.requestAnimationFrame(() => {
        scheduled = false;
        rafId = null;
        measure();
      });
    };

    measure();

    const offNative = onNativeKeyboardViewportChanged(schedule);
    const vv = window.visualViewport;
    vv?.addEventListener("resize", schedule);
    vv?.addEventListener("scroll", schedule);
    window.addEventListener("resize", schedule);
    window.addEventListener("orientationchange", schedule);

    let resizeObserver: ResizeObserver | null = null;
    if (typeof ResizeObserver !== "undefined") {
      resizeObserver = new ResizeObserver(schedule);
      resizeObserver.observe(el);
    }

    return () => {
      if (rafId !== null) {
        window.cancelAnimationFrame(rafId);
      }
      offNative();
      vv?.removeEventListener("resize", schedule);
      vv?.removeEventListener("scroll", schedule);
      window.removeEventListener("resize", schedule);
      window.removeEventListener("orientationchange", schedule);
      resizeObserver?.disconnect();
    };
  }, [enabled, reservesBottomSafeArea, root]);

  return enabled ? room : DISABLED_ROOM;
}
