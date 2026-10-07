import { queueUserPreferencePatch } from "./userPreferencesSync";

export const RUNNER_LOG_WARNINGS_STORAGE_KEY = "omnigent:runner-log-warnings";
export const RUNNER_LOG_WARNINGS_CHANGED_EVENT = "omnigent:runner-log-warnings-changed";

/** Dismissed detection instants mapped to the epoch-ms time of dismissal. */
export type RunnerLogWarningDismissals = Record<string, number>;

/**
 * Sanitize persisted or imported dismissals into the flat detection-instant →
 * dismissed-at map. Anything that is not a non-empty string key with a finite
 * number value is dropped. No count cap: the server retains entries for
 * 30 days instead, so an old dismissal cannot evict a recent episode.
 */
export function normalizeRunnerLogWarningPreferences(value: unknown): RunnerLogWarningDismissals {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  const normalized: RunnerLogWarningDismissals = {};
  for (const [flag, dismissedAt] of Object.entries(value)) {
    if (flag === "" || typeof dismissedAt !== "number" || !Number.isFinite(dismissedAt)) continue;
    normalized[flag] = dismissedAt;
  }
  return normalized;
}

export function readDismissedRunnerLogWarnings(): RunnerLogWarningDismissals {
  if (typeof window === "undefined") return {};
  try {
    const raw = window.localStorage.getItem(RUNNER_LOG_WARNINGS_STORAGE_KEY);
    return raw ? normalizeRunnerLogWarningPreferences(JSON.parse(raw)) : {};
  } catch {
    return {};
  }
}

/**
 * Remember one runaway flag (detection instant) as dismissed.
 *
 * The server merges a namespace patch per key, so every dismissal sends the
 * full local map: a device holding a stale snapshot only adds keys and can
 * never erase another device's dismissals.
 */
export function dismissRunnerLogWarning(flag: string): void {
  if (typeof window === "undefined" || !flag) return;
  const dismissed = readDismissedRunnerLogWarnings();
  if (dismissed[flag] !== undefined) return;
  const next = { ...dismissed, [flag]: Date.now() };
  try {
    window.localStorage.setItem(RUNNER_LOG_WARNINGS_STORAGE_KEY, JSON.stringify(next));
  } catch {
    // Storage denial or quota exhaustion must not break the banner.
  }
  window.dispatchEvent(new Event(RUNNER_LOG_WARNINGS_CHANGED_EVENT));
  queueUserPreferencePatch("runner_log_warnings", next);
}
