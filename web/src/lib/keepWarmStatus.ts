// User-facing copy for the server's keep-warm status object
// (`KeepWarmStatus`). Pure and clock-injectable so tooltips and the Agent
// info panel render the same wording, and tests can pin "now".
//
// The server sends counters, not prose: state is on/off/paused/stopped and
// stop_reason is a small vocabulary. This module turns those into the one
// line the sidebar tooltip shows (e.g. "Keep-warm stopped: 4 h cap ·
// 4 pings ≈$0.04") and the pieces the info panel lists.

import type { KeepWarmLastReturn, KeepWarmStatus } from "@/hooks/useConversations";
import { relativeTime } from "@/lib/relativeTime";

const HOUR_MS = 3_600_000;

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
 * `cap` names the measured warming duration when the payload has an episode
 * start; without one it degrades to "cap reached" rather than inventing the
 * configured cap. `failures` carries the raw skip/fail code (`last_reason`)
 * so the pause names its cause.
 */
export function keepWarmStopReason(
  status: KeepWarmStatus,
  now: number = Date.now(),
): string | null {
  switch (status.stop_reason) {
    case "cap": {
      const startedAt = status.episode.started_at;
      if (startedAt == null) return "cap reached";
      const elapsedMs = now - startedAt * 1000;
      const hours = Math.round(elapsedMs / HOUR_MS);
      return elapsedMs > 0 && hours >= 1 ? `${hours} h cap` : "cap reached";
    }
    case "misses":
      return "cache misses";
    case "failures":
      return status.last_reason ? `failures (${status.last_reason})` : "failures";
    case "card":
      return "pending card";
    case "host":
      return "host offline";
    case "switch_off":
      return "switched off";
    case "runner_version":
      return "runner too old";
    default:
      return null;
  }
}

/**
 * One-line tooltip for a cold keep-warm render. A null status means the
 * server could not read a keep-warm label at all, so the cache is likely
 * simply cold rather than stopped for a reason.
 */
export function keepWarmTooltipLine(
  status: KeepWarmStatus | null,
  now: number = Date.now(),
): string {
  if (status === null) return "Prompt cache likely cold";
  const reason = keepWarmStopReason(status, now);
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
