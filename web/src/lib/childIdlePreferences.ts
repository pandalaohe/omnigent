import { queueUserPreferencePatch } from "@/lib/userPreferencesSync";

export const CHILD_IDLE_STORAGE_KEY = "omnigent:child-idle";
export const CHILD_IDLE_CHANGED_EVENT = "omnigent:child-idle-changed";

export interface ChildIdlePreferences {
  enabled: boolean;
  idleThresholdSeconds: number;
  mergeWindowSeconds: number;
  repeatSeconds: number;
}

export const CHILD_IDLE_DEFAULTS: ChildIdlePreferences = {
  enabled: false,
  idleThresholdSeconds: 3600,
  mergeWindowSeconds: 1800,
  repeatSeconds: 14400,
};

function normalize(value: unknown): ChildIdlePreferences {
  if (!value || typeof value !== "object" || Array.isArray(value))
    return { ...CHILD_IDLE_DEFAULTS };
  const raw = value as Partial<ChildIdlePreferences>;
  const number = (input: unknown, fallback: number, min: number, max: number): number =>
    typeof input === "number" && Number.isInteger(input) && input >= min && input <= max
      ? input
      : fallback;
  const repeat = number(raw.repeatSeconds, 14400, 0, 604800);
  return {
    enabled: raw.enabled === true,
    idleThresholdSeconds: number(raw.idleThresholdSeconds, 3600, 60, 604800),
    mergeWindowSeconds: number(raw.mergeWindowSeconds, 1800, 0, 86400),
    repeatSeconds: repeat === 0 || repeat >= 60 ? repeat : 14400,
  };
}

export function readChildIdlePreferences(): ChildIdlePreferences {
  try {
    return normalize(JSON.parse(window.localStorage.getItem(CHILD_IDLE_STORAGE_KEY) ?? "null"));
  } catch {
    return { ...CHILD_IDLE_DEFAULTS };
  }
}

export function writeChildIdlePreferences(preferences: ChildIdlePreferences): void {
  const normalized = normalize(preferences);
  try {
    window.localStorage.setItem(CHILD_IDLE_STORAGE_KEY, JSON.stringify(normalized));
  } catch {
    // The server remains authoritative when local storage is unavailable.
  }
  window.dispatchEvent(new Event(CHILD_IDLE_CHANGED_EVENT));
  queueUserPreferencePatch("child_idle", normalized);
}
