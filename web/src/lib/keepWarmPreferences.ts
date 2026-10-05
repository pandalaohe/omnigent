// Keep-warm preferences (`keep_warm` namespace).
//
// One row per agent plus the host-offline archive delay. The server reader
// (`omnigent/server/user_preferences_store.py`) owns the wire contract and the
// clamps, so the constants below mirror it in one place.

import {
  mirrorLegacyChildKeepWarm,
  readSessionCollabPreferences,
} from "./sessionCollabPreferences";
import { queueUserPreferencePatch } from "./userPreferencesSync";

export const KEEP_WARM_STORAGE_KEY = "omnigent:keep-warm";
export const KEEP_WARM_CHANGED_EVENT = "omnigent:keep-warm-changed";

export type KeepWarmFamily = "claude" | "codex";

/** Harnesses with a server keep-warm channel, mapped to their cache family. */
export const KEEP_WARM_HARNESS_FAMILIES: Record<string, KeepWarmFamily> = {
  "claude-native": "claude",
  "claude-sdk": "claude",
  "codex-native": "codex",
  codex: "codex",
};

export function keepWarmFamilyForHarness(
  harness: string | null | undefined,
): KeepWarmFamily | null {
  return (harness && KEEP_WARM_HARNESS_FAMILIES[harness]) || null;
}

/** The legacy migration and mirror only ever cover the native CLIs. */
export function isLegacyNativeKeepWarmHarness(harness: string | null | undefined): boolean {
  return harness === "claude-native" || harness === "codex-native";
}

export const KEEP_WARM_CLAUDE_DEFAULT_INTERVAL_SECONDS = 3300;
export const KEEP_WARM_CODEX_DEFAULT_INTERVAL_SECONDS = 1500;
export const KEEP_WARM_DEFAULT_MAX_SECONDS = 14400;
export const KEEP_WARM_DEFAULT_HOST_OFFLINE_ARCHIVE_SECONDS = 14400;

export const KEEP_WARM_CLAUDE_INTERVAL_BOUNDS_SECONDS = { min: 300, max: 3540 } as const;
export const KEEP_WARM_CODEX_INTERVAL_BOUNDS_SECONDS = { min: 300, max: 1740 } as const;
export const KEEP_WARM_MAX_BOUNDS_SECONDS = { min: 3600, max: 172800 } as const;
/** 0 disables auto-archive; every other value lives between these bounds. */
export const KEEP_WARM_HOST_OFFLINE_ARCHIVE_BOUNDS_SECONDS = {
  min: 3600,
  max: 172800,
} as const;

export function defaultKeepWarmIntervalSeconds(family: KeepWarmFamily): number {
  return family === "claude"
    ? KEEP_WARM_CLAUDE_DEFAULT_INTERVAL_SECONDS
    : KEEP_WARM_CODEX_DEFAULT_INTERVAL_SECONDS;
}

export function keepWarmIntervalBoundsSeconds(family: KeepWarmFamily): {
  min: number;
  max: number;
} {
  return family === "claude"
    ? KEEP_WARM_CLAUDE_INTERVAL_BOUNDS_SECONDS
    : KEEP_WARM_CODEX_INTERVAL_BOUNDS_SECONDS;
}

export function clampKeepWarmIntervalSeconds(family: KeepWarmFamily, seconds: number): number {
  const { min, max } = keepWarmIntervalBoundsSeconds(family);
  return Math.min(Math.max(seconds, min), max);
}

export function clampKeepWarmMaxSeconds(seconds: number): number {
  const { min, max } = KEEP_WARM_MAX_BOUNDS_SECONDS;
  return Math.min(Math.max(seconds, min), max);
}

export function clampHostOfflineArchiveSeconds(seconds: number): number {
  if (seconds <= 0) return 0;
  const { min, max } = KEEP_WARM_HOST_OFFLINE_ARCHIVE_BOUNDS_SECONDS;
  return Math.min(Math.max(seconds, min), max);
}

export interface AgentKeepWarmPreferences {
  main: boolean;
  child: boolean;
  /** Stored ping interval in seconds; absent takes the family default. */
  intervalSeconds?: number;
  maxSeconds: number;
}

export interface KeepWarmPreferences {
  agents: Record<string, AgentKeepWarmPreferences>;
  hostOfflineArchiveSeconds: number;
  migratedFromLegacyAt?: number;
}

export interface ResolvedAgentKeepWarm {
  main: boolean;
  child: boolean;
  intervalSeconds: number;
  maxSeconds: number;
}

export interface AgentKeepWarmPatch {
  main?: boolean;
  child?: boolean;
  intervalSeconds?: number;
  maxSeconds?: number;
}

/** An absent agent row is off; the row is only written when it is first used. */
export const KEEP_WARM_DEFAULTS: KeepWarmPreferences = {
  agents: {},
  hostOfflineArchiveSeconds: KEEP_WARM_DEFAULT_HOST_OFFLINE_ARCHIVE_SECONDS,
};

function defaultKeepWarmPreferences(): KeepWarmPreferences {
  return { agents: {}, hostOfflineArchiveSeconds: KEEP_WARM_DEFAULT_HOST_OFFLINE_ARCHIVE_SECONDS };
}

function positiveInteger(value: unknown): number | undefined {
  if (typeof value !== "number" || !Number.isInteger(value) || value < 1) return undefined;
  return value;
}

function normalizeAgentRow(value: unknown): AgentKeepWarmPreferences | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const raw = value as Record<string, unknown>;
  const intervalSeconds = positiveInteger(raw.intervalSeconds);
  return {
    main: typeof raw.main === "boolean" ? raw.main : false,
    child: typeof raw.child === "boolean" ? raw.child : false,
    ...(intervalSeconds !== undefined ? { intervalSeconds } : {}),
    maxSeconds: positiveInteger(raw.maxSeconds) ?? KEEP_WARM_DEFAULT_MAX_SECONDS,
  };
}

/** Drop unknown/invalid fields; per-family clamps apply when a row is resolved. */
function normalizeKeepWarmPreferences(value: unknown): KeepWarmPreferences {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return defaultKeepWarmPreferences();
  }
  const raw = value as Record<string, unknown>;
  const agents: Record<string, AgentKeepWarmPreferences> = {};
  if (raw.agents && typeof raw.agents === "object" && !Array.isArray(raw.agents)) {
    for (const [agentId, row] of Object.entries(raw.agents as Record<string, unknown>)) {
      const normalized = normalizeAgentRow(row);
      if (normalized) agents[agentId] = normalized;
    }
  }
  const rawHost = raw.hostOfflineArchiveSeconds;
  const hostOfflineArchiveSeconds =
    typeof rawHost === "number" && Number.isInteger(rawHost) && rawHost >= 0
      ? clampHostOfflineArchiveSeconds(rawHost)
      : KEEP_WARM_DEFAULT_HOST_OFFLINE_ARCHIVE_SECONDS;
  const rawMigrated = raw.migratedFromLegacyAt;
  const migratedFromLegacyAt =
    typeof rawMigrated === "number" && Number.isInteger(rawMigrated) ? rawMigrated : undefined;
  return {
    agents,
    hostOfflineArchiveSeconds,
    ...(migratedFromLegacyAt !== undefined ? { migratedFromLegacyAt } : {}),
  };
}

function isKeepWarmPreferencesDefault(preferences: KeepWarmPreferences): boolean {
  return (
    Object.keys(preferences.agents).length === 0 &&
    preferences.hostOfflineArchiveSeconds === KEEP_WARM_DEFAULT_HOST_OFFLINE_ARCHIVE_SECONDS
  );
}

export function readKeepWarmPreferences(): KeepWarmPreferences {
  if (typeof window === "undefined") return defaultKeepWarmPreferences();
  try {
    const raw = window.localStorage.getItem(KEEP_WARM_STORAGE_KEY);
    return raw ? normalizeKeepWarmPreferences(JSON.parse(raw)) : defaultKeepWarmPreferences();
  } catch {
    return defaultKeepWarmPreferences();
  }
}

/** Effective row for display: absent is off and takes the family defaults. */
export function resolveKeepWarmAgent(
  preferences: KeepWarmPreferences,
  agentId: string,
  family: KeepWarmFamily,
): ResolvedAgentKeepWarm {
  const row = preferences.agents[agentId];
  return {
    main: row?.main ?? false,
    child: row?.child ?? false,
    intervalSeconds: clampKeepWarmIntervalSeconds(
      family,
      row?.intervalSeconds ?? defaultKeepWarmIntervalSeconds(family),
    ),
    maxSeconds: clampKeepWarmMaxSeconds(row?.maxSeconds ?? KEEP_WARM_DEFAULT_MAX_SECONDS),
  };
}

/** Merge one agent row; a first write materializes the family defaults. */
export function setKeepWarmAgent(
  preferences: KeepWarmPreferences,
  agentId: string,
  family: KeepWarmFamily,
  patch: AgentKeepWarmPatch,
): KeepWarmPreferences {
  const current = preferences.agents[agentId];
  const row: AgentKeepWarmPreferences = {
    main: patch.main ?? current?.main ?? false,
    child: patch.child ?? current?.child ?? false,
    intervalSeconds: clampKeepWarmIntervalSeconds(
      family,
      patch.intervalSeconds ?? current?.intervalSeconds ?? defaultKeepWarmIntervalSeconds(family),
    ),
    maxSeconds: clampKeepWarmMaxSeconds(
      patch.maxSeconds ?? current?.maxSeconds ?? KEEP_WARM_DEFAULT_MAX_SECONDS,
    ),
  };
  return { ...preferences, agents: { ...preferences.agents, [agentId]: row } };
}

export interface KeepWarmAgentIdentity {
  id: string;
  harness: string | null | undefined;
}

/**
 * Persist the namespace locally and to the server. The legacy SCC19 switch is
 * mirrored on every save so a rolled-back client keeps the same behaviour, and
 * unknown legacy keys are never dropped.
 */
export function writeKeepWarmPreferences(
  preferences: KeepWarmPreferences,
  agentIdentities: readonly KeepWarmAgentIdentity[] = [],
): void {
  if (typeof window === "undefined") return;
  const normalized = normalizeKeepWarmPreferences(preferences);
  const harnessById = new Map(agentIdentities.map((agent) => [agent.id, agent.harness]));
  let hasUnclassifiableChild = false;
  let hasNativeChild = false;
  for (const [agentId, row] of Object.entries(normalized.agents)) {
    if (!row.child) continue;
    if (!harnessById.has(agentId) || harnessById.get(agentId) == null) {
      hasUnclassifiableChild = true;
      continue;
    }
    if (isLegacyNativeKeepWarmHarness(harnessById.get(agentId))) hasNativeChild = true;
  }
  // A known native child proves the mirror on; otherwise a stored row whose
  // agent is absent or still unresolved cannot be proven non-native, so keep
  // the legacy value instead of mirroring a partial false.
  const legacyChildKeepWarmEnabled = hasNativeChild
    ? true
    : hasUnclassifiableChild
      ? readSessionCollabPreferences().childKeepWarmEnabled
      : false;
  mirrorLegacyChildKeepWarm(legacyChildKeepWarmEnabled);

  const isDefault = isKeepWarmPreferencesDefault(normalized);
  try {
    if (isDefault) window.localStorage.removeItem(KEEP_WARM_STORAGE_KEY);
    else window.localStorage.setItem(KEEP_WARM_STORAGE_KEY, JSON.stringify(normalized));
  } catch {
    // Storage denial or quota exhaustion must not break the settings page.
  }
  window.dispatchEvent(new Event(KEEP_WARM_CHANGED_EVENT));
  queueUserPreferencePatch("keep_warm", isDefault ? null : normalized);
}
