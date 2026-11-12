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
  return `$${value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2, roundingMode: "halfEven" })}`;
}

const CURRENCY_SYMBOLS: Record<string, string> = {
  USD: "$",
  CNY: "¥",
  HKD: "HK$",
  $: "$",
  "¥": "¥",
};

const COUNT_STEPS: [number, string][] = [
  [1e12, "T"],
  [1e9, "B"],
  [1e6, "M"],
  [1e3, "K"],
];

/**
 * A number that is not money: at most three significant digits, scaled with
 * K/M/B/T from a thousand, so `12,345.678 requests` reads `12.3K requests`.
 */
export function formatCount(value: number, unit: string | null = null): string {
  if (!Number.isFinite(value)) return "—";
  const [step, suffix] = COUNT_STEPS.find(([size]) => Math.abs(value) >= size) ?? [1, ""];
  const figure = (value / step).toLocaleString("en-US", {
    maximumSignificantDigits: 3,
    roundingMode: "trunc",
  });
  return `${figure}${suffix}${unit ? ` ${unit}` : ""}`;
}

/** Money with its symbol and two decimals; any other unit through `formatCount`. */
export function formatUsageValue(value: number, unit: string | null): string {
  const symbol = unit ? CURRENCY_SYMBOLS[unit] : undefined;
  if (!symbol) return formatCount(value, unit);
  const figure = value.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
    roundingMode: "halfEven",
  });
  return `${symbol}${figure}`;
}

/**
 * A reading's percent: at most three significant digits and two decimals,
 * trailing zeros dropped. It never rounds up to 100 or down to 0.
 */
export function formatPercentReading(value: number): string {
  if (!Number.isFinite(value)) return "—";
  if (value >= 999.5) return ">999";
  if (value >= 100) return String(Math.round(value));
  if (value >= 99.95) return "99.9";
  if (value <= 0) return "0";
  if (value < 0.01) return "<0.01";
  const decimals = value >= 10 ? 1 : 2;
  return String(Number(value.toFixed(decimals)));
}

const COMPACT_STEPS: [number, string][] = [
  [1e12, "T"],
  [1e9, "B"],
  [1e6, "M"],
  [1e3, "K"],
];

/**
 * A money figure for a tile: two decimals, and from six integer digits a
 * scaled two-decimal form (`$123.46K`); the full value stays in its title.
 */
export function formatTileAmount(value: number, unit: string | null): string {
  const symbol = unit ? CURRENCY_SYMBOLS[unit] : undefined;
  if (!symbol || Math.abs(value) < 1e5) return formatUsageValue(value, unit);
  const [step, suffix] = COMPACT_STEPS.find(([size]) => Math.abs(value) >= size) ?? [1, ""];
  const scaled = (value / step).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
    roundingMode: "trunc",
  });
  return `${symbol}${scaled}${suffix}`;
}

// Advance widths in em for figures; symbols and units render at the reduced
// unit size, so they count at that scale.
const FIGURE_EMS: Record<string, number> = { ".": 0.3, ",": 0.3, " ": 0.28, "<": 0.6, ">": 0.6 };
const UNIT_SCALE = 0.64;
let digitAdvance: { family: string; em: number } | null = null;

/**
 * The widest digit advance of the font the page actually uses (the Appearance
 * setting may choose any family), never less than the system stack's 0.6 em.
 */
export function digitAdvanceEm(): number {
  if (typeof document === "undefined" || /jsdom/i.test(navigator.userAgent)) return 0.6;
  const family = getComputedStyle(document.documentElement).fontFamily;
  if (digitAdvance?.family === family) return digitAdvance.em;
  const context = document.createElement("canvas").getContext("2d");
  let em = 0.6;
  if (context) {
    context.font = `600 100px ${family}`;
    const widest = Math.max(...[..."0123456789"].map((digit) => context.measureText(digit).width));
    if (widest > 30 && widest < 120) em = Math.max(0.6, widest / 100);
  }
  digitAdvance = { family, em };
  return em;
}

/** Estimated width of a figure in em, with its unit symbols at unit size. */
export function figureEms(number: string, unit = ""): number {
  const digit = digitAdvanceEm();
  const width = (text: string) =>
    [...text].reduce(
      (sum, char) => sum + (FIGURE_EMS[char] ?? (/\d/.test(char) ? digit : digit + 0.08)),
      0,
    );
  return (width(number) + width(unit) * UNIT_SCALE) * 1.04;
}

/**
 * Largest figure size, in percent of the ring's width, whose line stays inside
 * the ring's clear inner circle (radius in the same units) under an optional
 * name block of the given height.
 */
export function ringFigureSize(
  ems: number,
  nameBlock: number,
  radius: number,
  cap = 22,
  floor = 8,
): number {
  // Corners of the figure line: (ems·F/2)² + ((nameBlock + 0.74·F)/2)² ≤ radius².
  const a = (ems / 2) ** 2 + (0.74 / 2) ** 2;
  const b = 2 * (nameBlock / 2) * (0.74 / 2);
  const c = (nameBlock / 2) ** 2 - radius ** 2;
  const size = (-b + Math.sqrt(b * b - 4 * a * c)) / (2 * a);
  return Math.max(floor, Math.min(cap, Math.floor(size * 10) / 10));
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
