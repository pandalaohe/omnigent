import { queueUserPreferencePatch } from "./userPreferencesSync";

export const APPROVAL_TIMEOUT_STORAGE_KEY = "omnigent:approval-timeout";
export const APPROVAL_TIMEOUT_CHANGED_EVENT = "omnigent:approval-timeout-changed";

/** Server-side defaults, mirrored here so an absent key means "server default". */
export const DEFAULT_APPROVAL_TIMEOUT_MINUTES = 50;
export const DEFAULT_STOP_TURN_ON_TIMEOUT = true;
export const MIN_APPROVAL_TIMEOUT_MINUTES = 1;
/** Kept under the host-side hook/bridge client budgets (24 h minus a margin). */
export const MAX_APPROVAL_TIMEOUT_MINUTES = 1380;

export interface ApprovalTimeoutPreferences {
  /** Minutes a harness prompt may wait before the deadline fires. */
  timeoutMinutes: number;
  /** ON: the deadline stops the turn; OFF: the native flow runs at the deadline. */
  stopTurn: boolean;
}

const DEFAULT_PREFERENCES: ApprovalTimeoutPreferences = {
  timeoutMinutes: DEFAULT_APPROVAL_TIMEOUT_MINUTES,
  stopTurn: DEFAULT_STOP_TURN_ON_TIMEOUT,
};

function normalizeTimeoutMinutes(value: unknown): number {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return DEFAULT_APPROVAL_TIMEOUT_MINUTES;
  }
  const minutes = Math.round(value);
  if (minutes < MIN_APPROVAL_TIMEOUT_MINUTES) return MIN_APPROVAL_TIMEOUT_MINUTES;
  return Math.min(minutes, MAX_APPROVAL_TIMEOUT_MINUTES);
}

function normalizePreferences(value: unknown): ApprovalTimeoutPreferences {
  if (!value || typeof value !== "object") return { ...DEFAULT_PREFERENCES };
  const candidate = value as Partial<ApprovalTimeoutPreferences>;
  return {
    timeoutMinutes: normalizeTimeoutMinutes(candidate.timeoutMinutes),
    stopTurn: candidate.stopTurn !== false,
  };
}

export function readApprovalTimeoutPreferences(): ApprovalTimeoutPreferences {
  if (typeof window === "undefined") return { ...DEFAULT_PREFERENCES };
  try {
    const raw = window.localStorage.getItem(APPROVAL_TIMEOUT_STORAGE_KEY);
    return raw ? normalizePreferences(JSON.parse(raw)) : { ...DEFAULT_PREFERENCES };
  } catch {
    return { ...DEFAULT_PREFERENCES };
  }
}

export function writeApprovalTimeoutPreferences(preferences: ApprovalTimeoutPreferences): void {
  if (typeof window === "undefined") return;
  const normalized = normalizePreferences(preferences);
  const isDefault =
    normalized.timeoutMinutes === DEFAULT_APPROVAL_TIMEOUT_MINUTES &&
    normalized.stopTurn === DEFAULT_STOP_TURN_ON_TIMEOUT;
  try {
    if (isDefault) window.localStorage.removeItem(APPROVAL_TIMEOUT_STORAGE_KEY);
    else window.localStorage.setItem(APPROVAL_TIMEOUT_STORAGE_KEY, JSON.stringify(normalized));
  } catch {
    // Storage denial or quota exhaustion must not break the settings page.
  }
  window.dispatchEvent(new Event(APPROVAL_TIMEOUT_CHANGED_EVENT));
  queueUserPreferencePatch("approval_timeout", isDefault ? null : normalized);
}
