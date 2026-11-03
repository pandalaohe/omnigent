// The cards behind the board's readings. Every tip that carries a figure is
// built here as a structured model, so the same numbers never arrive as one
// sentence.

import type { UsageAccountView, UsageWindowView } from "./usageBoardData";
import {
  formatAge,
  formatFullDate,
  formatShortDate,
  formatTimeLeft,
  formatWindowLength,
  paceChip,
  windowName,
} from "./usageBoardFormat";
import type { UsageTipModel } from "./UsageTip";

/** The reading a window ring's card shows. */
export function windowTip(
  account: UsageAccountView,
  window: UsageWindowView,
  groupTag: string | null,
  options: { pinned: boolean; stale: boolean; auto: boolean },
): UsageTipModel {
  const rows: UsageTipModel["rows"] = [];
  const elapsed =
    window.window_minutes && window.time_left_s !== null
      ? Math.max(0, Math.min(100, 100 - (100 * window.time_left_s) / (window.window_minutes * 60)))
      : null;

  if (window.percent_used !== null) {
    rows.push({
      label: "Used",
      value: `${window.percent_used.toFixed(2).replace(/\.00$/, "")}%`,
      bar: window.percent_used,
      tone: options.stale ? "mute" : undefined,
    });
    if (elapsed !== null) {
      rows.push({ label: "Elapsed", value: `${Math.round(elapsed)}%`, bar: elapsed, tone: "ok" });
    }
  }
  if (window.used_value !== null) {
    rows.push({
      label: "Now",
      value: `${window.used_value.toLocaleString("en-US")}${window.unit ? ` ${window.unit}` : ""}`,
    });
    if (window.limit_value !== null) {
      rows.push({
        label: "Limit",
        value: `${window.limit_value.toLocaleString("en-US")}${window.unit ? ` ${window.unit}` : ""}`,
      });
    }
  }
  const dividerBefore = rows.length;
  if (window.resets_at !== null) {
    rows.push({ label: "Resets", value: formatShortDate(window.resets_at) });
  } else if (window.window_minutes) {
    rows.push({ label: "Length", value: formatWindowLength(window.window_minutes) });
  }
  if (window.time_left_s !== null && window.time_left_s > 0) {
    rows.push({ label: "Left", value: formatTimeLeft(window.time_left_s) });
  }
  if (account.windows.filter((item) => item.percent_used !== null).length > 1) {
    rows.push({
      label: "On the board",
      value: options.pinned ? "You pinned it" : options.auto ? "Auto, most urgent" : "Auto",
    });
  }
  if (options.stale && account.age_s !== null) {
    rows.push({ label: "Last read", value: `${formatAge(account.age_s)} ago` });
  }

  return {
    title: windowName(window),
    note: groupTag ?? undefined,
    chip: paceChip(window, options.stale),
    rows,
    dividerBefore,
  };
}

/** The card behind the reading's state button. */
export function readingTip(account: UsageAccountView): UsageTipModel {
  const rows: UsageTipModel["rows"] = [];
  if (account.age_s !== null) {
    rows.push({ label: "Last read", value: `${formatAge(account.age_s)} ago` });
  }
  if (account.read_at !== null) {
    rows.push({ label: "Read at", value: formatFullDate(account.read_at) });
  }
  if (account.state === "unknown" || account.state === "stale") {
    rows.push({ label: "Reason", value: account.reason ?? "No newer read" });
  }
  return {
    title: "Reading",
    chip:
      account.freshness === "fresh"
        ? { label: "Current", tone: "ok" }
        : { label: "Not read", tone: "mute" },
    rows,
  };
}

/** The renewal card: the date and how long is left. */
export function renewalTip(epochMs: number, official: boolean): UsageTipModel {
  const days = Math.max(0, Math.ceil((epochMs - Date.now()) / 86_400_000));
  return {
    title: "Renewal",
    chip: official ? { label: "From provider", tone: "ok" } : { label: "Set by you", tone: "mute" },
    rows: [
      { label: "Date", value: formatShortDate(epochMs / 1000) },
      { label: "In", value: days === 1 ? "1 day" : `${days} days` },
    ],
  };
}
