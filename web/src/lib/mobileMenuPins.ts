/**
 * Pinned rows at the top of the phone session menu.
 *
 * The phone "…" menu is long, and the rows a user reaches for most sit near
 * the bottom (Shells above Archive). A pinned group sits right below the
 * Chat/Terminal switch and repeats those rows there; every other action row
 * carries a pin toggle so the group is the user's own.
 *
 * Ids are stable strings stored in the account's `mobile_assistant`
 * preferences, so they must outlive a row's label or position. An id whose row
 * isn't available in the current session (or that this build no longer knows)
 * is dropped on read and on render.
 */
import type { ReactNode } from "react";

/** Every row that can be pinned, in the order the menu declares them. */
export const MOBILE_MENU_PIN_IDS = [
  "share",
  "fork",
  "export",
  "instructions",
  "peer-messages",
  "agent-info",
  "worktree",
  "rename",
  "mark-unread",
  "copy-session-id",
  "sounds",
  "open-in-panel",
  "automation",
  "move",
  "files",
  "archived-sessions",
  "changes",
  "content-search",
  "pull-requests",
  "agents",
  "side-chats",
  "shells",
  "logs",
  "resume",
  "stop",
] as const;

export type MobileMenuPinId = (typeof MOBILE_MENU_PIN_IDS)[number];

const KNOWN_PIN_IDS: ReadonlySet<string> = new Set(MOBILE_MENU_PIN_IDS);

/**
 * Pinned before the user edits anything: Shells right under Terminal, the
 * arrangement the phone menu was asked for out of the box.
 */
export const DEFAULT_MOBILE_MENU_PINS: readonly MobileMenuPinId[] = ["shells"];

export function isMobileMenuPinId(value: unknown): value is MobileMenuPinId {
  return typeof value === "string" && KNOWN_PIN_IDS.has(value);
}

/**
 * Drop unknown ids and duplicates, keeping the pinned order. `undefined` means
 * the stored value says nothing (never edited), which keeps the default — an
 * empty array is a real choice and stays empty.
 */
export function normalizeMobileMenuPins(value: unknown): MobileMenuPinId[] | undefined {
  if (!Array.isArray(value)) return undefined;
  const seen = new Set<MobileMenuPinId>();
  for (const candidate of value) {
    if (isMobileMenuPinId(candidate)) seen.add(candidate);
  }
  return [...seen];
}

/** The pins a surface renders: the stored choice, else the stock one. */
export function resolveMobileMenuPins(value: unknown): MobileMenuPinId[] {
  return normalizeMobileMenuPins(value) ?? [...DEFAULT_MOBILE_MENU_PINS];
}

/** Pin order, then toggle `id` in or out of it. */
export function toggledMobileMenuPins(
  pins: readonly MobileMenuPinId[],
  id: MobileMenuPinId,
): MobileMenuPinId[] {
  return pins.includes(id) ? pins.filter((pinned) => pinned !== id) : [...pins, id];
}

/**
 * One pinnable menu row.
 *
 * The row never renders the control itself: the surface draws it at the row's
 * trailing edge, beside the item rather than inside it. `withPin` says such a
 * control is coming, so a row that ends in a badge reserves that width.
 */
export interface PinnableMenuRow {
  id: MobileMenuPinId;
  /** The row's label, for the toggle's "Pin Shells" / "Unpin Shells" name. */
  label: string;
  render: (withPin: boolean) => ReactNode;
}

export function menuRow(
  id: MobileMenuPinId,
  label: string,
  render: (withPin: boolean) => ReactNode,
): PinnableMenuRow {
  return { id, label, render };
}
