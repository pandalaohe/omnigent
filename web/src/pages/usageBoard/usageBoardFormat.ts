// Text and colour for the Usage board's readings. The server already decided
// pace and state (``omnigent/quota_pace.py``); nothing here recomputes them.

import type { UsageWindowView } from "./usageBoardData";

/** The one colour reading a ring, a tile or a chip may take. */
export type UsageTone = "ok" | "warn" | "crit" | "mute";

/** ``quota_pace`` state → tone. `tight` is faster than time, `low` is near empty. */
export function stateTone(state: string | null | undefined): UsageTone {
  switch (state) {
    case "ok":
      return "ok";
    case "tight":
      return "warn";
    case "low":
    case "exhausted":
      return "crit";
    default:
      return "mute";
  }
}

export const TONE_TEXT: Record<UsageTone, string> = {
  ok: "text-success",
  warn: "text-warning",
  crit: "text-destructive",
  mute: "text-muted-foreground",
};

export const TONE_STROKE: Record<UsageTone, string> = {
  ok: "var(--success)",
  warn: "var(--warning)",
  crit: "var(--destructive)",
  mute: "var(--muted-foreground)",
};

/**
 * Time left in two units, as the fixed look asks: `6d 2h`, `4h 51m`, and only
 * under an hour `45m`. An empty string when there is nothing to count down.
 */
export function formatTimeLeft(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds) || seconds <= 0) {
    return "";
  }
  const minutes = Math.floor(seconds / 60);
  if (minutes < 1) return "under 1m";
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ${minutes % 60}m`;
  const days = Math.floor(hours / 24);
  return `${days}d ${hours % 24}h`;
}

const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function clockOf(date: Date): string {
  return `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
}

/** `Nov 7, 16:07`, with `Today`/`Tomorrow` folded in. */
export function formatShortDate(epoch: number | null | undefined, now = Date.now()): string {
  if (epoch === null || epoch === undefined || !Number.isFinite(epoch)) return "";
  const date = new Date(epoch * (epoch < 1e12 ? 1000 : 1));
  const today = new Date(now);
  const dayStart = new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
  const todayStart = new Date(today.getFullYear(), today.getMonth(), today.getDate()).getTime();
  const dayDiff = Math.round((dayStart - todayStart) / 86_400_000);
  const tail = `${MONTHS[date.getMonth()]} ${date.getDate()}, ${clockOf(date)}`;
  if (dayDiff === 0) return `Today ${clockOf(date)}`;
  if (dayDiff === 1) return `Tomorrow ${clockOf(date)}`;
  return tail;
}

/** `Fri Nov 7, 16:07` — the full form the detail page writes. */
export function formatFullDate(epoch: number | null | undefined): string {
  if (epoch === null || epoch === undefined || !Number.isFinite(epoch)) return "";
  const date = new Date(epoch * (epoch < 1e12 ? 1000 : 1));
  return `${WEEKDAYS[date.getDay()]} ${MONTHS[date.getMonth()]} ${date.getDate()}, ${clockOf(date)}`;
}

/** `Nov 16` — a date with no time, for a renewal. */
export function formatDay(epochMs: number | null | undefined): string {
  if (epochMs === null || epochMs === undefined || !Number.isFinite(epochMs)) return "";
  const date = new Date(epochMs);
  return `${MONTHS[date.getMonth()]} ${date.getDate()}`;
}

/** `3 h`, `12 min`, `2 d` — how old a reading is. */
export function formatAge(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds) || seconds < 0) {
    return "";
  }
  if (seconds < 90) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours} h`;
  return `${Math.round(hours / 24)} d`;
}

/** Rounded percent; anything above 0 but under 1 reads `<1` rather than `0`. */
export function formatPercent(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  if (value > 0 && value < 1) return "<1";
  return String(Math.round(value));
}

export function formatMoney(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  if (value === 0) return "$0";
  if (value >= 100) return `$${Math.round(value).toLocaleString("en-US")}`;
  if (value < 0.01) return "<$0.01";
  return `$${value.toFixed(2)}`;
}

/** The window's own name: the report's label, else its id. */
export function windowName(window: UsageWindowView): string {
  return window.label ?? window.id;
}

export function isPercentWindow(window: UsageWindowView): boolean {
  return window.percent_used !== null;
}

/** How long the window runs, for a tip's row (`7 days`, `5 hours`). */
export function formatWindowLength(minutes: number | null | undefined): string {
  if (minutes === null || minutes === undefined || !Number.isFinite(minutes) || minutes <= 0) {
    return "";
  }
  if (minutes % 1440 === 0) {
    const days = minutes / 1440;
    return days === 1 ? "1 day" : `${days} days`;
  }
  if (minutes % 60 === 0) {
    const hours = minutes / 60;
    return hours === 1 ? "1 hour" : `${hours} hours`;
  }
  return `${minutes} min`;
}

/** The status chip a reading earns, as the design's ring tip states it. */
export function paceChip(
  window: UsageWindowView,
  stale: boolean,
): {
  label: string;
  tone: UsageTone;
} {
  if (stale) return { label: "Not read", tone: "mute" };
  if (window.state === "exhausted") return { label: "Used up", tone: "crit" };
  if (window.state === "low") return { label: "Nearly empty", tone: "crit" };
  if (window.state === "tight") return { label: "Faster than time", tone: "warn" };
  if (window.state === "ok") return { label: "On track", tone: "ok" };
  return { label: "Unknown", tone: "mute" };
}
