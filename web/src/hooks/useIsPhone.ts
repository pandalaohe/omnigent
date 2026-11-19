import { useSyncExternalStore } from "react";
import { isAndroidShell, isIOSShell } from "@/lib/nativeBridge";
import { KEY_BAR_GATE } from "@/lib/terminalKeys";

const MOBILE_MEDIA_QUERY = "(max-width: 767.98px)";
const COARSE_MEDIA_QUERY = "(pointer: coarse)";
const FINE_MEDIA_QUERY = "(any-pointer: fine)";

function subscribe(callback: () => void): () => void {
  if (typeof window === "undefined") return () => {};
  const maxMd =
    typeof window.matchMedia === "function" ? window.matchMedia(MOBILE_MEDIA_QUERY) : null;
  const coarse =
    typeof window.matchMedia === "function" ? window.matchMedia(COARSE_MEDIA_QUERY) : null;
  const fine = window.matchMedia?.(FINE_MEDIA_QUERY);
  maxMd?.addEventListener("change", callback);
  coarse?.addEventListener("change", callback);
  fine?.addEventListener("change", callback);
  window.addEventListener("resize", callback);
  window.addEventListener("orientationchange", callback);
  return () => {
    maxMd?.removeEventListener("change", callback);
    coarse?.removeEventListener("change", callback);
    fine?.removeEventListener("change", callback);
    window.removeEventListener("resize", callback);
    window.removeEventListener("orientationchange", callback);
  };
}

export function isPhoneViewport(): boolean {
  if (typeof window === "undefined") return false;
  if (isIOSShell() || isAndroidShell()) return true;
  const hasMatchMedia = typeof window.matchMedia === "function";
  const maxMd = hasMatchMedia
    ? window.matchMedia(MOBILE_MEDIA_QUERY).matches
    : window.innerWidth < 768;
  if (maxMd) return true;
  const coarse = hasMatchMedia ? window.matchMedia(COARSE_MEDIA_QUERY).matches : false;
  if (!coarse) return false;
  const shortSide =
    window.screen?.width > 0 && window.screen?.height > 0
      ? Math.min(window.screen.width, window.screen.height)
      : Math.min(window.innerWidth, window.innerHeight);
  return shortSide < 768;
}

function getServerSnapshot(): boolean {
  return false;
}

/**
 * Reactive phone layout gate.
 *
 * Keeps phone controls mounted when a phone rotates to landscape (width >= 768
 * with a coarse pointer and short screen side < 768) without showing them on
 * touch laptops or desktop windows.
 */
export function useIsPhone(): boolean {
  return useSyncExternalStore(subscribe, isPhoneViewport, getServerSnapshot);
}

export function terminalKeyBarGate(mode: typeof KEY_BAR_GATE = KEY_BAR_GATE): boolean {
  if (mode === "layout") return isPhoneViewport();
  return (
    typeof window !== "undefined" &&
    (isIOSShell() ||
      isAndroidShell() ||
      Boolean(
        window.matchMedia?.(COARSE_MEDIA_QUERY).matches &&
        !window.matchMedia?.(FINE_MEDIA_QUERY).matches,
      ))
  );
}

export function useTerminalKeyBarGate(): boolean {
  return useSyncExternalStore(subscribe, terminalKeyBarGate, getServerSnapshot);
}
