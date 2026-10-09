// Sound alert preferences.
//
// Account scope (`sound_alerts`) holds per-level switches and sounds, quiet
// hours, and cross-device state; it syncs through the server. Device scope
// (`omnigent:sound-alerts-device`) holds this device's master switch, volume,
// and OS-sound overrides, and never leaves the device.

import { queueUserPreferencePatch } from "./userPreferencesSync";

export const SOUND_ALERTS_STORAGE_KEY = "omnigent:sound-alerts";
export const SOUND_ALERTS_DEVICE_STORAGE_KEY = "omnigent:sound-alerts-device";
export const SOUND_ALERTS_CHANGED_EVENT = "omnigent:sound-alerts-changed";

export type SoundLevel = "done" | "error" | "needs_response";

export const SOUND_LEVELS: readonly SoundLevel[] = ["done", "error", "needs_response"];

export type BuiltinSoundId = "chime" | "ping" | "pop" | "alert";

export interface BuiltinSound {
  id: BuiltinSoundId;
  label: string;
}

export const BUILTIN_SOUNDS: readonly BuiltinSound[] = [
  { id: "chime", label: "Chime" },
  { id: "ping", label: "Ping" },
  { id: "pop", label: "Pop" },
  { id: "alert", label: "Alert" },
];

export interface SoundAlertLevelPreferences {
  enabled: boolean;
  sound: BuiltinSoundId;
}

export interface SoundAlertQuietHours {
  /** "HH:MM" 24-hour local time; enforcement arrives with a later slice. */
  enabled: boolean;
  start: string;
  end: string;
}

export interface SoundAlertPreferences {
  levels: Record<SoundLevel, SoundAlertLevelPreferences>;
  quietHours: SoundAlertQuietHours;
  primaryDeviceId: string | null;
  mutedSessionIds: string[];
}

export const SOUND_ALERT_DEFAULT_LEVELS: Record<SoundLevel, SoundAlertLevelPreferences> = {
  done: { enabled: true, sound: "chime" },
  error: { enabled: true, sound: "alert" },
  needs_response: { enabled: true, sound: "ping" },
};

export const SOUND_ALERT_DEFAULT_QUIET_HOURS: SoundAlertQuietHours = {
  enabled: true,
  start: "23:00",
  end: "08:00",
};

export const SOUND_ALERT_DEFAULTS: SoundAlertPreferences = {
  levels: SOUND_ALERT_DEFAULT_LEVELS,
  quietHours: SOUND_ALERT_DEFAULT_QUIET_HOURS,
  primaryDeviceId: null,
  mutedSessionIds: [],
};

/** Keep only the most recent mutes; the cap bounds the synced payload. */
const MAX_MUTED_SESSION_IDS = 200;

const TIME_OF_DAY_PATTERN = /^([01]\d|2[0-3]):[0-5]\d$/;

function defaultSoundAlertPreferences(): SoundAlertPreferences {
  return {
    levels: {
      done: { ...SOUND_ALERT_DEFAULT_LEVELS.done },
      error: { ...SOUND_ALERT_DEFAULT_LEVELS.error },
      needs_response: { ...SOUND_ALERT_DEFAULT_LEVELS.needs_response },
    },
    quietHours: { ...SOUND_ALERT_DEFAULT_QUIET_HOURS },
    primaryDeviceId: null,
    mutedSessionIds: [],
  };
}

function isBuiltinSoundId(value: unknown): value is BuiltinSoundId {
  return (
    typeof value === "string" && BUILTIN_SOUNDS.some((sound) => (sound.id as string) === value)
  );
}

function normalizeLevel(
  value: unknown,
  fallback: SoundAlertLevelPreferences,
): SoundAlertLevelPreferences {
  if (!value || typeof value !== "object" || Array.isArray(value)) return { ...fallback };
  const raw = value as Record<string, unknown>;
  return {
    enabled: typeof raw.enabled === "boolean" ? raw.enabled : fallback.enabled,
    sound: isBuiltinSoundId(raw.sound) ? raw.sound : fallback.sound,
  };
}

function normalizeQuietHours(value: unknown): SoundAlertQuietHours {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return { ...SOUND_ALERT_DEFAULT_QUIET_HOURS };
  }
  const raw = value as Record<string, unknown>;
  return {
    enabled:
      typeof raw.enabled === "boolean" ? raw.enabled : SOUND_ALERT_DEFAULT_QUIET_HOURS.enabled,
    start:
      typeof raw.start === "string" && TIME_OF_DAY_PATTERN.test(raw.start)
        ? raw.start
        : SOUND_ALERT_DEFAULT_QUIET_HOURS.start,
    end:
      typeof raw.end === "string" && TIME_OF_DAY_PATTERN.test(raw.end)
        ? raw.end
        : SOUND_ALERT_DEFAULT_QUIET_HOURS.end,
  };
}

function normalizeMutedSessionIds(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  const ids = new Set<string>();
  for (const entry of value) {
    if (typeof entry === "string" && entry.length > 0) ids.add(entry);
  }
  return [...ids].slice(-MAX_MUTED_SESSION_IDS);
}

/** Drop unknown/invalid fields field-by-field; a bad field keeps its default. */
export function normalizeSoundAlertPreferences(value: unknown): SoundAlertPreferences {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return defaultSoundAlertPreferences();
  }
  const raw = value as Record<string, unknown>;
  const rawLevels =
    raw.levels && typeof raw.levels === "object" && !Array.isArray(raw.levels)
      ? (raw.levels as Record<string, unknown>)
      : {};
  const levels = {} as Record<SoundLevel, SoundAlertLevelPreferences>;
  for (const level of SOUND_LEVELS) {
    levels[level] = normalizeLevel(rawLevels[level], SOUND_ALERT_DEFAULT_LEVELS[level]);
  }
  return {
    levels,
    quietHours: normalizeQuietHours(raw.quietHours),
    primaryDeviceId:
      typeof raw.primaryDeviceId === "string" && raw.primaryDeviceId.length > 0
        ? raw.primaryDeviceId
        : null,
    mutedSessionIds: normalizeMutedSessionIds(raw.mutedSessionIds),
  };
}

function isSoundAlertPreferencesDefault(preferences: SoundAlertPreferences): boolean {
  return JSON.stringify(preferences) === JSON.stringify(SOUND_ALERT_DEFAULTS);
}

export function readSoundAlertPreferences(): SoundAlertPreferences {
  if (typeof window === "undefined") return defaultSoundAlertPreferences();
  try {
    const raw = window.localStorage.getItem(SOUND_ALERTS_STORAGE_KEY);
    return raw ? normalizeSoundAlertPreferences(JSON.parse(raw)) : defaultSoundAlertPreferences();
  } catch {
    return defaultSoundAlertPreferences();
  }
}

export function writeSoundAlertPreferences(preferences: SoundAlertPreferences): void {
  if (typeof window === "undefined") return;
  const normalized = normalizeSoundAlertPreferences(preferences);
  const isDefault = isSoundAlertPreferencesDefault(normalized);
  try {
    if (isDefault) window.localStorage.removeItem(SOUND_ALERTS_STORAGE_KEY);
    else window.localStorage.setItem(SOUND_ALERTS_STORAGE_KEY, JSON.stringify(normalized));
  } catch {
    // Storage denial or quota exhaustion must not break the settings page.
  }
  window.dispatchEvent(new Event(SOUND_ALERTS_CHANGED_EVENT));
  queueUserPreferencePatch("sound_alerts", isDefault ? null : normalized);
}

export function isSoundLevelEnabled(
  preferences: SoundAlertPreferences,
  level: SoundLevel,
): boolean {
  return preferences.levels[level].enabled;
}

export interface SoundAlertDevicePreferences {
  enabled: boolean;
  volume: number;
  /** Overrides for OS-provided sounds; filled by a later slice. */
  systemSounds: Partial<Record<SoundLevel, string>>;
}

export const SOUND_ALERT_DEVICE_DEFAULTS: SoundAlertDevicePreferences = {
  enabled: true,
  volume: 0.7,
  systemSounds: {},
};

export function clampSoundVolume(volume: number): number {
  return Math.min(Math.max(volume, 0), 1);
}

function defaultSoundAlertDevicePreferences(): SoundAlertDevicePreferences {
  return { ...SOUND_ALERT_DEVICE_DEFAULTS, systemSounds: {} };
}

function normalizeSystemSounds(value: unknown): Partial<Record<SoundLevel, string>> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  const raw = value as Record<string, unknown>;
  const sounds: Partial<Record<SoundLevel, string>> = {};
  for (const level of SOUND_LEVELS) {
    if (typeof raw[level] === "string" && (raw[level] as string).length > 0) {
      sounds[level] = raw[level] as string;
    }
  }
  return sounds;
}

export function normalizeSoundAlertDevicePreferences(value: unknown): SoundAlertDevicePreferences {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return defaultSoundAlertDevicePreferences();
  }
  const raw = value as Record<string, unknown>;
  return {
    enabled: typeof raw.enabled === "boolean" ? raw.enabled : SOUND_ALERT_DEVICE_DEFAULTS.enabled,
    volume:
      typeof raw.volume === "number" && Number.isFinite(raw.volume)
        ? clampSoundVolume(raw.volume)
        : SOUND_ALERT_DEVICE_DEFAULTS.volume,
    systemSounds: normalizeSystemSounds(raw.systemSounds),
  };
}

export function readSoundAlertDevicePreferences(): SoundAlertDevicePreferences {
  if (typeof window === "undefined") return defaultSoundAlertDevicePreferences();
  try {
    const raw = window.localStorage.getItem(SOUND_ALERTS_DEVICE_STORAGE_KEY);
    return raw
      ? normalizeSoundAlertDevicePreferences(JSON.parse(raw))
      : defaultSoundAlertDevicePreferences();
  } catch {
    return defaultSoundAlertDevicePreferences();
  }
}

export function writeSoundAlertDevicePreferences(preferences: SoundAlertDevicePreferences): void {
  if (typeof window === "undefined") return;
  const normalized = normalizeSoundAlertDevicePreferences(preferences);
  try {
    window.localStorage.setItem(SOUND_ALERTS_DEVICE_STORAGE_KEY, JSON.stringify(normalized));
  } catch {
    // Storage denial or quota exhaustion must not break the settings page.
  }
  window.dispatchEvent(new Event(SOUND_ALERTS_CHANGED_EVENT));
}
