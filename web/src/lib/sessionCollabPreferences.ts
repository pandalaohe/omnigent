import { queueUserPreferencePatch } from "./userPreferencesSync";
import { NATIVE_CODING_AGENTS } from "./nativeCodingAgents";

export const SESSION_COLLAB_STORAGE_KEY = "omnigent:session-collab";
export const SESSION_COLLAB_CHANGED_EVENT = "omnigent:session-collab-changed";
export const QUOTA_PRODUCTS = NATIVE_CODING_AGENTS.map((agent) => agent.key);
export type QuotaProduct = (typeof QUOTA_PRODUCTS)[number];
export const QUOTA_PERIOD_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$/;

/** Field names are the stored JSON keys the server accessor reads. */
export interface SessionCollabPreferences {
  /** Master switch: off hides the collaboration tools and refuses peer routes. */
  enabled: boolean;
  sessionRestrictions: "off" | "subagents" | "all";
  openRateCount: number;
  openRateWindowSeconds: number;
  relayDepthMax: number;
  pairRateCount: number;
  pairRateWindowSeconds: number;
  senderRateCount: number;
  senderRateWindowSeconds: number;
  duplicateWindowSeconds: number;
  undeliveredTtlSeconds: number;
  flowTimerEnabled: boolean;
  childKeepWarmEnabled: boolean;
  childKeepWarmClaudeIntervalSeconds: number;
  childKeepWarmCodexIntervalSeconds: number;
  childKeepWarmMaxSeconds: number;
  childCap: number | null;
  quotaTightBelowPct: number;
  quotaLowBelowPct: number;
  quotaFloorPct: number | null;
  quotaPeriods: Partial<Record<QuotaProduct, string>>;
}

export type SessionCollabNumericField =
  | "openRateCount"
  | "openRateWindowSeconds"
  | "relayDepthMax"
  | "pairRateCount"
  | "pairRateWindowSeconds"
  | "senderRateCount"
  | "senderRateWindowSeconds"
  | "duplicateWindowSeconds"
  | "undeliveredTtlSeconds"
  | "childKeepWarmClaudeIntervalSeconds"
  | "childKeepWarmCodexIntervalSeconds"
  | "childKeepWarmMaxSeconds";

/** Accepted range per numeric field, in the stored unit (seconds for windows). */
export const SESSION_COLLAB_BOUNDS: Record<
  SessionCollabNumericField,
  { min: number; max: number }
> = {
  openRateCount: { min: 1, max: 100 },
  openRateWindowSeconds: { min: 60, max: 86400 },
  relayDepthMax: { min: 1, max: 100 },
  pairRateCount: { min: 1, max: 1000 },
  pairRateWindowSeconds: { min: 1, max: 3600 },
  senderRateCount: { min: 1, max: 10000 },
  senderRateWindowSeconds: { min: 60, max: 86400 },
  duplicateWindowSeconds: { min: 1, max: 86400 },
  undeliveredTtlSeconds: { min: 60, max: 604800 },
  childKeepWarmClaudeIntervalSeconds: { min: 300, max: 3540 },
  childKeepWarmCodexIntervalSeconds: { min: 300, max: 1740 },
  childKeepWarmMaxSeconds: { min: 3600, max: 172800 },
};

export const SESSION_COLLAB_DEFAULTS: SessionCollabPreferences = {
  enabled: true,
  sessionRestrictions: "subagents",
  openRateCount: 10,
  openRateWindowSeconds: 60,
  relayDepthMax: 30,
  pairRateCount: 6,
  pairRateWindowSeconds: 60,
  senderRateCount: 60,
  senderRateWindowSeconds: 600,
  duplicateWindowSeconds: 60,
  undeliveredTtlSeconds: 86400,
  flowTimerEnabled: true,
  childKeepWarmEnabled: false,
  childKeepWarmClaudeIntervalSeconds: 3300,
  childKeepWarmCodexIntervalSeconds: 1500,
  childKeepWarmMaxSeconds: 28800,
  childCap: null,
  quotaTightBelowPct: 50,
  quotaLowBelowPct: 20,
  quotaFloorPct: null,
  quotaPeriods: {},
};

function normalizeInteger(value: unknown, field: SessionCollabNumericField): number {
  const fallback = SESSION_COLLAB_DEFAULTS[field];
  if (typeof value !== "number" || !Number.isInteger(value)) return fallback;
  const { min, max } = SESSION_COLLAB_BOUNDS[field];
  if (value < min || value > max) return fallback;
  return value;
}

function normalizeBoolean(value: unknown, fallback: boolean): boolean {
  return typeof value === "boolean" ? value : fallback;
}

function normalizePercentage(value: unknown, min: number, max: number): number | null {
  return typeof value === "number" && Number.isFinite(value) && value >= min && value <= max
    ? value
    : null;
}

function normalizeQuotaPeriods(value: unknown): SessionCollabPreferences["quotaPeriods"] {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  const raw = value as Record<string, unknown>;
  const periods: SessionCollabPreferences["quotaPeriods"] = {};
  for (const product of QUOTA_PRODUCTS) {
    const period = raw[product];
    if (typeof period === "string" && QUOTA_PERIOD_PATTERN.test(period)) periods[product] = period;
  }
  return periods;
}

function normalizePreferences(value: unknown): SessionCollabPreferences {
  if (!value || typeof value !== "object" || Array.isArray(value))
    return { ...SESSION_COLLAB_DEFAULTS, quotaPeriods: {} };
  const raw = value as Record<string, unknown>;
  const low = normalizePercentage(raw.quotaLowBelowPct, 0, 100);
  const tight = normalizePercentage(raw.quotaTightBelowPct, 0, 100);
  const validThresholds = low !== null && tight !== null && low > 0 && low < tight;
  return {
    enabled: normalizeBoolean(raw.enabled, SESSION_COLLAB_DEFAULTS.enabled),
    sessionRestrictions:
      raw.sessionRestrictions === "off" || raw.sessionRestrictions === "all"
        ? raw.sessionRestrictions
        : "subagents",
    openRateCount: normalizeInteger(raw.openRateCount, "openRateCount"),
    openRateWindowSeconds: normalizeInteger(raw.openRateWindowSeconds, "openRateWindowSeconds"),
    relayDepthMax: normalizeInteger(raw.relayDepthMax, "relayDepthMax"),
    pairRateCount: normalizeInteger(raw.pairRateCount, "pairRateCount"),
    pairRateWindowSeconds: normalizeInteger(raw.pairRateWindowSeconds, "pairRateWindowSeconds"),
    senderRateCount: normalizeInteger(raw.senderRateCount, "senderRateCount"),
    senderRateWindowSeconds: normalizeInteger(
      raw.senderRateWindowSeconds,
      "senderRateWindowSeconds",
    ),
    duplicateWindowSeconds: normalizeInteger(raw.duplicateWindowSeconds, "duplicateWindowSeconds"),
    undeliveredTtlSeconds: normalizeInteger(raw.undeliveredTtlSeconds, "undeliveredTtlSeconds"),
    flowTimerEnabled: normalizeBoolean(
      raw.flowTimerEnabled,
      SESSION_COLLAB_DEFAULTS.flowTimerEnabled,
    ),
    childKeepWarmEnabled: normalizeBoolean(
      raw.childKeepWarmEnabled,
      SESSION_COLLAB_DEFAULTS.childKeepWarmEnabled,
    ),
    childKeepWarmClaudeIntervalSeconds: normalizeInteger(
      raw.childKeepWarmClaudeIntervalSeconds,
      "childKeepWarmClaudeIntervalSeconds",
    ),
    childKeepWarmCodexIntervalSeconds: normalizeInteger(
      raw.childKeepWarmCodexIntervalSeconds,
      "childKeepWarmCodexIntervalSeconds",
    ),
    childKeepWarmMaxSeconds: normalizeInteger(
      raw.childKeepWarmMaxSeconds,
      "childKeepWarmMaxSeconds",
    ),
    childCap:
      typeof raw.childCap === "number" && Number.isSafeInteger(raw.childCap) && raw.childCap >= 1
        ? raw.childCap
        : null,
    quotaTightBelowPct: validThresholds ? tight : SESSION_COLLAB_DEFAULTS.quotaTightBelowPct,
    quotaLowBelowPct: validThresholds ? low : SESSION_COLLAB_DEFAULTS.quotaLowBelowPct,
    quotaFloorPct: normalizePercentage(raw.quotaFloorPct, 0, 100),
    quotaPeriods: normalizeQuotaPeriods(raw.quotaPeriods),
  };
}

export function isSessionCollabPreferencesDefault(preferences: SessionCollabPreferences): boolean {
  return (Object.keys(SESSION_COLLAB_DEFAULTS) as (keyof SessionCollabPreferences)[]).every(
    (key) =>
      key === "quotaPeriods"
        ? Object.keys(preferences.quotaPeriods).length === 0
        : preferences[key] === SESSION_COLLAB_DEFAULTS[key],
  );
}

export function readSessionCollabPreferences(): SessionCollabPreferences {
  if (typeof window === "undefined") return { ...SESSION_COLLAB_DEFAULTS, quotaPeriods: {} };
  try {
    const raw = window.localStorage.getItem(SESSION_COLLAB_STORAGE_KEY);
    return raw
      ? normalizePreferences(JSON.parse(raw))
      : { ...SESSION_COLLAB_DEFAULTS, quotaPeriods: {} };
  } catch {
    return { ...SESSION_COLLAB_DEFAULTS, quotaPeriods: {} };
  }
}

/**
 * Mirror the keep-warm child switch into the legacy namespace.
 *
 * Used by the keep-warm settings, which own that switch now: a rolled-back
 * client still sees the same behaviour. The stored object is merged, never
 * replaced, so every legacy key survives; the namespace is patched even when
 * the merged value equals the defaults (a full write would delete it).
 */
export function mirrorLegacyChildKeepWarm(childKeepWarmEnabled: boolean): void {
  if (typeof window === "undefined") return;
  let merged: Record<string, unknown> = {};
  try {
    const raw = window.localStorage.getItem(SESSION_COLLAB_STORAGE_KEY);
    const parsed = raw ? (JSON.parse(raw) as unknown) : null;
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      merged = { ...(parsed as Record<string, unknown>) };
    }
  } catch {
    // A corrupt cache is simply replaced by the merged value below.
  }
  merged.childKeepWarmEnabled = childKeepWarmEnabled;
  try {
    window.localStorage.setItem(SESSION_COLLAB_STORAGE_KEY, JSON.stringify(merged));
  } catch {
    // Storage denial or quota exhaustion must not break the keep-warm save.
  }
  window.dispatchEvent(new Event(SESSION_COLLAB_CHANGED_EVENT));
  queueUserPreferencePatch("session_collab", merged);
}

export function writeSessionCollabPreferences(preferences: SessionCollabPreferences): void {
  if (typeof window === "undefined") return;
  const normalized = normalizePreferences(preferences);
  const isDefault = isSessionCollabPreferencesDefault(normalized);
  try {
    if (isDefault) window.localStorage.removeItem(SESSION_COLLAB_STORAGE_KEY);
    else window.localStorage.setItem(SESSION_COLLAB_STORAGE_KEY, JSON.stringify(normalized));
  } catch {
    // Storage denial or quota exhaustion must not break the settings page.
  }
  window.dispatchEvent(new Event(SESSION_COLLAB_CHANGED_EVENT));
  queueUserPreferencePatch("session_collab", isDefault ? null : normalized);
}
