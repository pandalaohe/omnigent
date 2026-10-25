import { queueUserPreferencePatch } from "@/lib/userPreferencesSync";

export const CHILD_IDLE_STORAGE_KEY = "omnigent:child-idle";
export const CHILD_IDLE_CHANGED_EVENT = "omnigent:child-idle-changed";

export interface ChildFreezePreferences {
  enabled: boolean;
  checkIntervalSeconds: number;
  freezeThresholdSeconds: number;
  resumeOnce: boolean;
}

export const CHILD_FREEZE_DEFAULTS: ChildFreezePreferences = {
  enabled: false,
  checkIntervalSeconds: 3600,
  freezeThresholdSeconds: 3600,
  resumeOnce: false,
};

export interface ChildIdlePreferences {
  enabled: boolean;
  idleThresholdSeconds: number;
  mergeWindowSeconds: number;
  minGapSeconds: number;
  coverage: "changed" | "summary";
  repeatSeconds: number;
  checkinInstructions: string | null;
  freezeDefault: ChildFreezePreferences;
  agents: Record<string, ChildFreezePreferences>;
}

export const CHILD_IDLE_DEFAULTS: ChildIdlePreferences = {
  enabled: false,
  idleThresholdSeconds: 3600,
  mergeWindowSeconds: 1800,
  minGapSeconds: 3600,
  coverage: "summary",
  repeatSeconds: 0,
  checkinInstructions: null,
  freezeDefault: CHILD_FREEZE_DEFAULTS,
  agents: {},
};

function normalize(value: unknown): ChildIdlePreferences {
  if (!value || typeof value !== "object" || Array.isArray(value))
    return { ...CHILD_IDLE_DEFAULTS };
  const raw = value as Partial<ChildIdlePreferences>;
  const number = (input: unknown, fallback: number, min: number, max: number): number =>
    typeof input === "number" && Number.isInteger(input) && input >= min && input <= max
      ? input
      : fallback;
  const repeat = number(raw.repeatSeconds, 0, 0, 604800);
  const agents: Record<string, ChildFreezePreferences> = {};
  if (raw.agents && typeof raw.agents === "object" && !Array.isArray(raw.agents)) {
    for (const [id, row] of Object.entries(raw.agents)) {
      if (!id || !row || typeof row !== "object" || Array.isArray(row)) continue;
      agents[id] = {
        enabled: row.enabled === true,
        checkIntervalSeconds: number(row.checkIntervalSeconds, 3600, 60, 604800),
        freezeThresholdSeconds: number(row.freezeThresholdSeconds, 3600, 60, 604800),
        resumeOnce: row.resumeOnce === true,
      };
    }
  }
  return {
    enabled: raw.enabled === true,
    idleThresholdSeconds: number(raw.idleThresholdSeconds, 3600, 60, 604800),
    mergeWindowSeconds: number(raw.mergeWindowSeconds, 1800, 0, 86400),
    minGapSeconds: number(raw.minGapSeconds, 3600, 0, 86400),
    coverage: raw.coverage === "changed" ? "changed" : "summary",
    repeatSeconds: repeat === 0 || repeat >= 60 ? repeat : 0,
    checkinInstructions:
      typeof raw.checkinInstructions === "string"
        ? raw.checkinInstructions.replaceAll("\0", "")
        : null,
    freezeDefault: {
      enabled: raw.freezeDefault?.enabled === true,
      checkIntervalSeconds: number(raw.freezeDefault?.checkIntervalSeconds, 3600, 60, 604800),
      freezeThresholdSeconds: number(raw.freezeDefault?.freezeThresholdSeconds, 3600, 60, 604800),
      resumeOnce: raw.freezeDefault?.resumeOnce === true,
    },
    agents,
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
