import { queueUserPreferencePatch } from "./userPreferencesSync";

export const RUNNER_LOG_WARNINGS_STORAGE_KEY = "omnigent:runner-log-warnings";
export const RUNNER_LOG_WARNINGS_CHANGED_EVENT = "omnigent:runner-log-warnings-changed";

const MAX_DISMISSED_WARNINGS = 50;

/**
 * Sanitize persisted or imported dismissal values: non-string and empty
 * entries are dropped, duplicates collapse to their newest occurrence, and
 * only the 50 newest detection instants survive.
 */
export function normalizeRunnerLogWarningPreferences(value: unknown): string[] {
  if (!value || typeof value !== "object" || Array.isArray(value)) return [];
  const raw = (value as { dismissed?: unknown }).dismissed;
  if (!Array.isArray(raw)) return [];
  const seen = new Set<string>();
  const normalized: string[] = [];
  for (let index = raw.length - 1; index >= 0; index -= 1) {
    const entry = raw[index];
    if (typeof entry !== "string" || entry === "" || seen.has(entry)) continue;
    seen.add(entry);
    normalized.unshift(entry);
  }
  return normalized.slice(-MAX_DISMISSED_WARNINGS);
}

export function readDismissedRunnerLogWarnings(): string[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(RUNNER_LOG_WARNINGS_STORAGE_KEY);
    return raw ? normalizeRunnerLogWarningPreferences(JSON.parse(raw)) : [];
  } catch {
    return [];
  }
}

/**
 * Remember one runaway flag (detection instant) as dismissed.
 *
 * The Server sync patch carries the full local list — the localStorage
 * mirror, which server hydration replaces — so an account switch cannot
 * replay the previous account's dismissals.
 */
export function dismissRunnerLogWarning(flag: string): void {
  if (typeof window === "undefined" || !flag) return;
  const dismissed = readDismissedRunnerLogWarnings();
  if (dismissed.includes(flag)) return;
  const next = [...dismissed, flag].slice(-MAX_DISMISSED_WARNINGS);
  try {
    window.localStorage.setItem(
      RUNNER_LOG_WARNINGS_STORAGE_KEY,
      JSON.stringify({ dismissed: next }),
    );
  } catch {
    // Storage denial or quota exhaustion must not break the banner.
  }
  window.dispatchEvent(new Event(RUNNER_LOG_WARNINGS_CHANGED_EVENT));
  queueUserPreferencePatch("runner_log_warnings", { dismissed: next });
}
