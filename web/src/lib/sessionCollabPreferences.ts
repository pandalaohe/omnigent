import { queueUserPreferencePatch } from "./userPreferencesSync";

export const SESSION_COLLAB_STORAGE_KEY = "omnigent:session-collab";
export const SESSION_COLLAB_CHANGED_EVENT = "omnigent:session-collab-changed";

export type SessionCollabInboundPolicy = "accept" | "hold" | "refuse";

export interface SessionCollabPreferences {
  /** Master switch: off hides the collaboration tools and refuses peer routes. */
  enabled: boolean;
  openRateCount: number;
  openRateWindowS: number;
  relayDepthMax: number;
  pairRateCount: number;
  pairRateWindowS: number;
  senderRateCount: number;
  senderRateWindowS: number;
  duplicateWindowS: number;
  undeliveredTtlS: number;
  defaultInbound: SessionCollabInboundPolicy;
  flowTimerEnabled: boolean;
}

/**
 * Stored JSON field names (camelCase, mirroring the server accessor's keys).
 * Kept in one object so a rename on either side is a one-place edit.
 */
export const SESSION_COLLAB_FIELD_KEYS = {
  enabled: "enabled",
  openRateCount: "openRateCount",
  openRateWindowS: "openRateWindowS",
  relayDepthMax: "relayDepthMax",
  pairRateCount: "pairRateCount",
  pairRateWindowS: "pairRateWindowS",
  senderRateCount: "senderRateCount",
  senderRateWindowS: "senderRateWindowS",
  duplicateWindowS: "duplicateWindowS",
  undeliveredTtlS: "undeliveredTtlS",
  defaultInbound: "defaultInbound",
  flowTimerEnabled: "flowTimerEnabled",
} as const;

export type SessionCollabNumericField =
  | "openRateCount"
  | "openRateWindowS"
  | "relayDepthMax"
  | "pairRateCount"
  | "pairRateWindowS"
  | "senderRateCount"
  | "senderRateWindowS"
  | "duplicateWindowS"
  | "undeliveredTtlS";

/** Accepted range per numeric field, in the stored unit (seconds for windows). */
export const SESSION_COLLAB_BOUNDS: Record<
  SessionCollabNumericField,
  { min: number; max: number }
> = {
  openRateCount: { min: 1, max: 100 },
  openRateWindowS: { min: 60, max: 86400 },
  relayDepthMax: { min: 1, max: 100 },
  pairRateCount: { min: 1, max: 1000 },
  pairRateWindowS: { min: 1, max: 3600 },
  senderRateCount: { min: 1, max: 10000 },
  senderRateWindowS: { min: 60, max: 86400 },
  duplicateWindowS: { min: 0, max: 86400 },
  undeliveredTtlS: { min: 3600, max: 604800 },
};

export const SESSION_COLLAB_DEFAULTS: SessionCollabPreferences = {
  enabled: true,
  openRateCount: 5,
  openRateWindowS: 60,
  relayDepthMax: 30,
  pairRateCount: 6,
  pairRateWindowS: 60,
  senderRateCount: 60,
  senderRateWindowS: 600,
  duplicateWindowS: 600,
  undeliveredTtlS: 86400,
  defaultInbound: "accept",
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

function normalizeInbound(value: unknown): SessionCollabInboundPolicy {
  return value === "hold" || value === "refuse" || value === "accept"
    ? value
    : SESSION_COLLAB_DEFAULTS.defaultInbound;
}

function normalizePreferences(value: unknown): SessionCollabPreferences {
  if (!value || typeof value !== "object") return { ...SESSION_COLLAB_DEFAULTS };
  const raw = value as Record<string, unknown>;
  return {
    enabled: normalizeBoolean(
      raw[SESSION_COLLAB_FIELD_KEYS.enabled],
      SESSION_COLLAB_DEFAULTS.enabled,
    ),
    openRateCount: normalizeInteger(raw[SESSION_COLLAB_FIELD_KEYS.openRateCount], "openRateCount"),
    openRateWindowS: normalizeInteger(
      raw[SESSION_COLLAB_FIELD_KEYS.openRateWindowS],
      "openRateWindowS",
    ),
    relayDepthMax: normalizeInteger(raw[SESSION_COLLAB_FIELD_KEYS.relayDepthMax], "relayDepthMax"),
    pairRateCount: normalizeInteger(raw[SESSION_COLLAB_FIELD_KEYS.pairRateCount], "pairRateCount"),
    pairRateWindowS: normalizeInteger(
      raw[SESSION_COLLAB_FIELD_KEYS.pairRateWindowS],
      "pairRateWindowS",
    ),
    senderRateCount: normalizeInteger(
      raw[SESSION_COLLAB_FIELD_KEYS.senderRateCount],
      "senderRateCount",
    ),
    senderRateWindowS: normalizeInteger(
      raw[SESSION_COLLAB_FIELD_KEYS.senderRateWindowS],
      "senderRateWindowS",
    ),
    duplicateWindowS: normalizeInteger(
      raw[SESSION_COLLAB_FIELD_KEYS.duplicateWindowS],
      "duplicateWindowS",
    ),
    undeliveredTtlS: normalizeInteger(
      raw[SESSION_COLLAB_FIELD_KEYS.undeliveredTtlS],
      "undeliveredTtlS",
    ),
    defaultInbound: normalizeInbound(raw[SESSION_COLLAB_FIELD_KEYS.defaultInbound]),
    flowTimerEnabled: normalizeBoolean(
      raw[SESSION_COLLAB_FIELD_KEYS.flowTimerEnabled],
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
