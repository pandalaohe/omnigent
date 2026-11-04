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
  /** Bottom room this surface reserves inside its own box, in pixels. */
  paddingBottomPx: number;
  /** Width inside the key row's side safe-area padding, in pixels. */
  keyRowWidth: number;
}

const DISABLED_ROOM: TerminalKeyboardRoom = {
  overlap: 0,
  keyboardUp: false,
  roomPx: Number.POSITIVE_INFINITY,
  paddingBottomPx: 0,
  keyRowWidth: 0,
};

/**
 * Resolve a pixel length from the first custom property that is set. The callers
 * pass the same precedence the stylesheet would use, so the number measured here
 * is the number the layout applies.
 */
function resolveCssPx(
  style: CSSStyleDeclaration,
  probe: HTMLElement,
  names: readonly string[],
): number {
  for (const name of names) {
    const raw = style.getPropertyValue(name).trim();
    if (!raw) continue;
    const value = Number.parseFloat(raw);
    if (Number.isFinite(value)) return Math.max(0, value);
    probe.style.height = raw;
    const resolved = Number.parseFloat(window.getComputedStyle(probe).height);
    if (Number.isFinite(resolved)) return Math.max(0, resolved);
  }
  return 0;
}

/**
 * Measure the visible bottom edge of the viewport against a TerminalView root.
 *
 * Unifies the iOS native bridge height (`getIOSKeyboardViewportHeight`),
 * `window.visualViewport`, and `window.innerHeight` into a single bottom-edge
 * overlap measurement coalesced by `requestAnimationFrame`.
 *
 * `paddingBottomPx` is the whole reservation this surface owns — `max(overlap,
 * safe bottom when the keyboard is down)` — and `TerminalView` applies it as its
 * inline bottom padding, so the measured value and the layout cannot disagree.
 * `--term-safe-b: 0px`, set where an ancestor already reserves the inset, wins
 * over `--omnigent-inset-bottom`.
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
      setRoom((prev) => (prev === DISABLED_ROOM ? prev : DISABLED_ROOM));
      return;
    }

    const lengthProbe = document.createElement("div");
    lengthProbe.setAttribute("aria-hidden", "true");
    lengthProbe.style.cssText =
      "position:absolute;top:0;left:0;width:0;height:0;padding:0;border:0;visibility:hidden;pointer-events:none;";
    el.appendChild(lengthProbe);

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

      const style = window.getComputedStyle(current);
      const safeBottomPx = reservesBottomSafeArea
        ? resolveCssPx(style, lengthProbe, ["--term-safe-b", "--omnigent-inset-bottom"])
        : 0;
      const paddingBottomPx = Math.max(overlap, keyboardUp ? 0 : safeBottomPx);
      const rawHeight = rect.height > 0 || rect.bottom > 0 ? rect.height : window.innerHeight;
      const roomPx = Math.max(0, Math.round(rawHeight - paddingBottomPx));
      const measuredWidth = rect.width > 0 ? rect.width : window.innerWidth;
      const safeLeftPx = resolveCssPx(style, lengthProbe, [
        "--key-row-safe-l",
        "--omnigent-safe-left",
      ]);
      const safeRightPx = resolveCssPx(style, lengthProbe, [
        "--key-row-safe-r",
        "--omnigent-safe-right",
      ]);
      const keyRowWidth = Math.max(
        0,
        measuredWidth - Math.max(8, safeLeftPx) - Math.max(8, safeRightPx),
      );

      setRoom((prev) =>
        prev.overlap === overlap &&
        prev.keyboardUp === keyboardUp &&
        prev.roomPx === roomPx &&
        prev.paddingBottomPx === paddingBottomPx &&
        prev.keyRowWidth === keyRowWidth
          ? prev
          : { overlap, keyboardUp, roomPx, paddingBottomPx, keyRowWidth },
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
      lengthProbe.remove();
    };
  }, [enabled, reservesBottomSafeArea, root]);

  return enabled ? room : DISABLED_ROOM;
}
