// Picking the reading a tile or a band shows. The server already decided the
// period routing uses (``deciding_window``); the board follows the same window
// unless the user pinned one, so a tile and the routing decision never disagree
// about which window is "the" window.

import type { UsageAccountView, UsageWindowView } from "./usageBoardData";
import { isPercentWindow, type UsageTone } from "./usageBoardFormat";

export const AUTO_WINDOW = "auto";

export interface UsageWindowGroup {
  key: string;
  /** A gateway's own grouping (a model pool, a bucket); absent for one group. */
  tag: string | null;
  windows: UsageWindowView[];
}

/** Windows grouped in report order; a group keeps the server's window order. */
export function groupWindows(windows: UsageWindowView[]): UsageWindowGroup[] {
  const groups: UsageWindowGroup[] = [];
  for (const window of windows) {
    const key = window.group ?? "";
    const existing = groups.find((group) => group.key === key);
    if (existing) existing.windows.push(window);
    else groups.push({ key, tag: window.group, windows: [window] });
  }
  return groups;
}

/** Lowest pace first: the window that runs out soonest. */
function mostUrgent(windows: UsageWindowView[]): UsageWindowView | null {
  const timed = windows.filter((window) => isPercentWindow(window));
  if (timed.length === 0) return windows[0] ?? null;
  return timed.reduce((worst, window) => {
    const windowPace = window.pace_pct ?? Number.POSITIVE_INFINITY;
    const worstPace = worst.pace_pct ?? Number.POSITIVE_INFINITY;
    return windowPace < worstPace ? window : worst;
  });
}

/**
 * The window a group shows: the pinned id when that window is still reported,
 * else the account's deciding window, else the most urgent one. A pin for a
 * window the last read no longer has stays stored and applies when it returns.
 */
export function pickWindow(
  group: UsageWindowGroup,
  deciding: string | null,
  pinnedId: string | undefined,
): { window: UsageWindowView | null; pinned: boolean } {
  if (pinnedId && pinnedId !== AUTO_WINDOW) {
    const pinned = group.windows.find((window) => window.id === pinnedId);
    if (pinned) return { window: pinned, pinned: true };
  }
  const decidingWindow = deciding
    ? group.windows.find((window) => window.id === deciding)
    : undefined;
  if (decidingWindow) return { window: decidingWindow, pinned: false };
  return { window: mostUrgent(group.windows), pinned: false };
}

/** The whole group set with its picks, in report order. */
export function pickGroups(account: UsageAccountView, pinnedId: string | undefined) {
  return groupWindows(account.windows).map((group) => ({
    group,
    ...pickWindow(group, account.deciding_window, pinnedId),
  }));
}

/** A reading past its hour (or its `fresh_until`) is old: dim, no pace claim. */
export function isStale(account: UsageAccountView): boolean {
  return account.freshness === "old";
}

export function neverRead(account: UsageAccountView): boolean {
  return account.freshness === "none";
}

/**
 * The account's colour: the worst state among the windows it shows. An account
 * with no reading, or an old one, takes no colour — it must not claim pace.
 */
export function accountTone(account: UsageAccountView, stale: boolean): UsageTone {
  if (stale || neverRead(account)) return "mute";
  const rank: Record<string, number> = { ok: 0, tight: 1, low: 2, exhausted: 3 };
  let worst = 0;
  for (const window of account.windows) {
    const value = rank[window.state] ?? 0;
    if (value > worst) worst = value;
  }
  return worst === 3 ? "crit" : worst === 2 ? "crit" : worst === 1 ? "warn" : "ok";
}

/** A renewal in whole days, for the tile's footer. */
export function daysUntil(epochMs: number | null | undefined, now = Date.now()): number | null {
  if (epochMs === null || epochMs === undefined || !Number.isFinite(epochMs)) return null;
  return Math.max(0, Math.ceil((epochMs - now) / 86_400_000));
}

/** `Ms` → `Mon Nov 7 2026` for a renewal stored as a calendar day. */
export function toDateInput(epochMs: number | null | undefined): string {
  if (epochMs === null || epochMs === undefined || !Number.isFinite(epochMs)) return "";
  const date = new Date(epochMs);
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${date.getFullYear()}-${month}-${day}`;
}

/** A `yyyy-mm-dd` input value → local noon of that day, epoch ms. */
export function fromDateInput(value: string): number | null {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return null;
  const date = new Date(`${value}T12:00:00`);
  return Number.isNaN(date.getTime()) ? null : date.getTime();
}
