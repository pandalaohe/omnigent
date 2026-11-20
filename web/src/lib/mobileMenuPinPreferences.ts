// Phone session-menu pins: a preference namespace of its own, so a pin save and
// a terminal-key save (in `mobile_assistant`) can never rewrite each other.
// localStorage is the synchronous read path; userPreferencesSync hydrates and
// patches the server with the whole object.

import { useSyncExternalStore } from "react";

import {
  DEFAULT_MOBILE_MENU_PINS,
  normalizeMobileMenuPins,
  toggledMobileMenuPins,
  type MobileMenuPinId,
} from "@/lib/mobileMenuPins";
import { queueUserPreferencePatch } from "@/lib/userPreferencesSync";

export const MOBILE_MENU_PINS_NAMESPACE = "mobile_menu_pins";
export const MOBILE_MENU_PINS_STORAGE_KEY = "omnigent:mobile-menu-pins";
export const MOBILE_MENU_PINS_CHANGED_EVENT = "omnigent:mobile-menu-pins-changed";
export const MOBILE_MENU_PINS_VERSION = 1;

export interface MobileMenuPinsPreferences {
  version: 1;
  /** Pinned row ids, in pinned order. */
  pinnedMenuItems: MobileMenuPinId[];
}

/**
 * The storable value: a version-1 object holding the pinned ids.
 *
 * Anything unrecognisable — not an object, another version, a non-list — falls
 * back to the stock choice, so a value written by a newer or broken client can
 * never leave the menu without its rows. Inside a list, ids this build doesn't
 * know are dropped (an empty list is a real "pin nothing").
 */
export function canonicalMobileMenuPins(value: unknown): MobileMenuPinsPreferences {
  const stock: MobileMenuPinsPreferences = {
    version: MOBILE_MENU_PINS_VERSION,
    pinnedMenuItems: [...DEFAULT_MOBILE_MENU_PINS],
  };
  if (!value || typeof value !== "object" || Array.isArray(value)) return stock;
  const raw = value as { version?: unknown; pinnedMenuItems?: unknown };
  if (raw.version !== MOBILE_MENU_PINS_VERSION) return stock;
  const pins = normalizeMobileMenuPins(raw.pinnedMenuItems);
  return pins === undefined ? stock : { version: MOBILE_MENU_PINS_VERSION, pinnedMenuItems: pins };
}

/** The stored value, or the stock choice when nothing usable is stored. */
export function readMobileMenuPinsPreferences(): MobileMenuPinsPreferences {
  if (typeof window === "undefined") return canonicalMobileMenuPins(null);
  try {
    const raw = window.localStorage.getItem(MOBILE_MENU_PINS_STORAGE_KEY);
    if (raw === null) return canonicalMobileMenuPins(null);
    return canonicalMobileMenuPins(JSON.parse(raw) as unknown);
  } catch {
    return canonicalMobileMenuPins(null);
  }
}

function subscribeMobileMenuPins(listener: () => void): () => void {
  window.addEventListener(MOBILE_MENU_PINS_CHANGED_EVENT, listener);
  window.addEventListener("storage", listener);
  return () => {
    window.removeEventListener(MOBILE_MENU_PINS_CHANGED_EVENT, listener);
    window.removeEventListener("storage", listener);
  };
}

export function useMobileMenuPinsPreferences(): MobileMenuPinsPreferences {
  useSyncExternalStore(
    subscribeMobileMenuPins,
    () => {
      try {
        return window.localStorage.getItem(MOBILE_MENU_PINS_STORAGE_KEY);
      } catch {
        return null;
      }
    },
    () => null,
  );
  return readMobileMenuPinsPreferences();
}

/** Rows the phone session menu pins, in pinned order. */
export function useMobileMenuPins(): MobileMenuPinId[] {
  return useMobileMenuPinsPreferences().pinnedMenuItems;
}

/** Store the list and queue the namespace save (the whole object, as usual). */
export function writeMobileMenuPins(pinnedMenuItems: readonly MobileMenuPinId[]): void {
  if (typeof window === "undefined") return;
  const canonical = canonicalMobileMenuPins({
    version: MOBILE_MENU_PINS_VERSION,
    pinnedMenuItems: [...pinnedMenuItems],
  });
  try {
    window.localStorage.setItem(MOBILE_MENU_PINS_STORAGE_KEY, JSON.stringify(canonical));
  } catch {
    // Storage quota / access errors must not break pinning.
  }
  window.dispatchEvent(new Event(MOBILE_MENU_PINS_CHANGED_EVENT));
  queueUserPreferencePatch(MOBILE_MENU_PINS_NAMESPACE, canonical);
}

/** Pin or unpin one row, keeping the rest of the pinned order. */
export function toggleMobileMenuPin(id: MobileMenuPinId): void {
  writeMobileMenuPins(toggledMobileMenuPins(readMobileMenuPinsPreferences().pinnedMenuItems, id));
}
