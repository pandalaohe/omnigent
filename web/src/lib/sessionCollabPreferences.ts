import { queueUserPreferencePatch } from "./userPreferencesSync";

export const SESSION_COLLAB_STORAGE_KEY = "omnigent:session-collab";
export const SESSION_COLLAB_CHANGED_EVENT = "omnigent:session-collab-changed";

/** Field names are the stored JSON keys the server accessor reads. */
export interface SessionCollabPreferences {
  /** Master switch: off hides the collaboration tools and refuses peer routes. */
  enabled: boolean;
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
  | "undeliveredTtlSeconds";

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
};

export const SESSION_COLLAB_DEFAULTS: SessionCollabPreferences = {
  enabled: true,
  openRateCount: 10,
  openRateWindowSeconds: 60,
  relayDepthMax: 30,
  pairRateCount: 6,
  pairRateWindowSeconds: 60,
  senderRateCount: 60,
  senderRateWindowSeconds: 600,
  duplicateWindowSeconds: 600,
  undeliveredTtlSeconds: 86400,
  flowTimerEnabled: true,
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

function normalizePreferences(value: unknown): SessionCollabPreferences {
  if (!value || typeof value !== "object") return { ...SESSION_COLLAB_DEFAULTS };
  const raw = value as Record<string, unknown>;
  return {
    enabled: normalizeBoolean(raw.enabled, SESSION_COLLAB_DEFAULTS.enabled),
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
  };
}

export function isSessionCollabPreferencesDefault(preferences: SessionCollabPreferences): boolean {
  return (Object.keys(SESSION_COLLAB_DEFAULTS) as (keyof SessionCollabPreferences)[]).every(
    (key) => preferences[key] === SESSION_COLLAB_DEFAULTS[key],
  );
}

export function readSessionCollabPreferences(): SessionCollabPreferences {
  if (typeof window === "undefined") return { ...SESSION_COLLAB_DEFAULTS };
  try {
    const raw = window.localStorage.getItem(SESSION_COLLAB_STORAGE_KEY);
    return raw ? normalizePreferences(JSON.parse(raw)) : { ...SESSION_COLLAB_DEFAULTS };
  } catch {
    return { ...SESSION_COLLAB_DEFAULTS };
  }
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
