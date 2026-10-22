// User-facing copy for the server's keep-warm status object
// (`KeepWarmStatus`). Pure, with the last-return label clock-injectable so
// tooltips and the Agent info panel render the same wording and tests can pin
// "now".
//
// The server sends counters, not prose: state is on/off/paused/stopped and
// stop_reason is a small vocabulary. This module turns those into the one
// line the sidebar tooltip shows (e.g. "Keep-warm stopped: cap reached ·
// 4 pings ≈$0.04") and the pieces the info panel lists.

import type { KeepWarmLastReturn, KeepWarmStatus } from "@/hooks/useConversations";
import { relativeTime } from "@/lib/relativeTime";

/** `$0.04`, prefixed with `≈` when any summed ping was an estimate. */
export function formatKeepWarmCost(costUsd: number, estimated: boolean): string {
  const amount = Number.isFinite(costUsd) ? costUsd : 0;
  return `${estimated ? "≈" : ""}$${amount.toFixed(2)}`;
}

/** `4 pings ≈$0.04` — one counter pair shared by tooltip and panel rows. */
export function formatKeepWarmCounters(pings: number, costUsd: number, estimated: boolean): string {
  const label = pings === 1 ? "ping" : "pings";
  return `${pings} ${label} ${formatKeepWarmCost(costUsd, estimated)}`;
}

/**
 * Human wording for a stop/pause reason, or `null` when none is stored.
 *
 * `cap` always reads "cap reached": `episode.started_at` predates any cold
 * time after the stop, so clocking the cap from now overstates the measured
 * warming duration. `failures` carries the raw skip/fail code (`last_reason`)
 * so the pause names its cause.
 */
export function keepWarmStopReason(status: KeepWarmStatus): string | null {
  if (status.stop_reason === null) return null;
  const reasons: Record<NonNullable<KeepWarmStatus["stop_reason"]>, string> = {
    cap: "cap reached",
    misses: "cache misses",
    failures: status.last_reason ? `failures (${status.last_reason})` : "failures",
    card: "pending card",
    host: "host offline",
    switch_off: "switched off",
    runner_version: "runner too old",
    expired: status.last_reason
      ? `window expired (last ping: ${status.last_reason})`
      : "window expired",
    archive_revision: "archive state changed",
    policy: "policy stopped warming",
    parent_inactive: "parent inactive",
    skipped: status.last_reason ? `ping skipped (${status.last_reason})` : "ping skipped",
  };
  const reason = reasons[status.stop_reason];
  if (!reason) return null;
  const blockers: Record<string, string> = {
    not_inspectable: "unsupported session",
    switch_off: "switched off",
    missing_running_since: "missing turn start",
    session_closed: "session closed",
    ancestor_archived: "ancestor archived",
    runner_offline: "runner offline",
    host_unavailable: "host unavailable",
    status_not_idle: "session not idle",
    pending_card: "pending card",
  };
  const blocker = status.blocked_by && (blockers[status.blocked_by] ?? status.blocked_by);
  return blocker ? `${reason} (blocked by ${blocker})` : reason;
}

/**
 * One-line tooltip for a cold keep-warm render. A null status means the
 * server could not read a keep-warm label at all, so the cache is likely
 * simply cold rather than stopped for a reason.
 */
export function keepWarmTooltipLine(status: KeepWarmStatus | null): string {
  if (status === null) return "Prompt cache likely cold";
  const reason = keepWarmStopReason(status);
  let line = `Keep-warm ${status.state}`;
  if (reason !== null) line += `: ${reason}`;
  // "off" is a terminal state with no episode worth counting.
  if (status.state !== "off") {
    line += ` · ${formatKeepWarmCounters(
      status.episode.pings,
      status.episode.cost_usd,
      status.episode.estimated,
    )}`;
  }
  return line;
}

/** `hit · 2h` — the measured first return after an absence. */
export function keepWarmLastReturnLabel(
  lastReturn: KeepWarmLastReturn,
  now: number = Date.now(),
): string {
  return `${lastReturn.result} · ${relativeTime(lastReturn.at * 1000, now)}`;
}
