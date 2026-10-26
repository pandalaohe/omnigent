// Keep-warm preferences (`keep_warm` namespace).
//
// One default, agent/harness exceptions, and the host-offline archive delay. The server reader
// (`omnigent/server/user_preferences_store.py`) owns the wire contract and the
// clamps, so the constants below mirror it in one place.

import {
  mirrorLegacyChildKeepWarm,
  readSessionCollabPreferences,
} from "./sessionCollabPreferences";
import {
  queueAcknowledgedUserPreferencePatch,
  queueUserPreferencePatch,
} from "./userPreferencesSync";

export const KEEP_WARM_STORAGE_KEY = "omnigent:keep-warm";
export const KEEP_WARM_CHANGED_EVENT = "omnigent:keep-warm-changed";

export type KeepWarmFamily = "claude" | "codex" | "pi";

export function keepWarmFamilyForHarness(
  harness: string | null | undefined,
  capabilitiesByHarness: Record<string, { keep_warm_family?: string | null }>,
): KeepWarmFamily | null {
  const family = harness ? capabilitiesByHarness[harness]?.keep_warm_family : null;
  return family === "claude" || family === "codex" || family === "pi" ? family : null;
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
/** 0 means never cold; every other value lives between these bounds. */
export const KEEP_WARM_COLD_AFTER_BOUNDS_SECONDS = { min: 60, max: 172800 } as const;
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

export function clampKeepWarmColdAfterSeconds(seconds: number): number {
  if (seconds === 0) return 0;
  const { min, max } = KEEP_WARM_COLD_AFTER_BOUNDS_SECONDS;
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
  /** Idle seconds to cold; absent takes the platform rule, 0 never colds. */
  coldAfterSeconds?: number;
}

export interface KeepWarmPreferences {
  default: KeepWarmDefaultPreferences;
  agents: Record<string, AgentKeepWarmPreferences>;
  hostOfflineArchiveSeconds: number;
  migratedFromLegacyAt?: number;
  cells?: KeepWarmCells;
  migratedToCellsAt?: number;
  /** Display axes only, including columns whose cells are all off. */
  columns?: string[];
}

export type KeepWarmCell = Partial<AgentKeepWarmPreferences>;
export type KeepWarmCells = Record<string, Record<string, KeepWarmCell>>;

export interface KeepWarmDefaultPreferences {
  main: boolean;
  child: boolean;
  maxSeconds: number;
  coldAfterSeconds?: number;
}

export interface ResolvedAgentKeepWarm {
  main: boolean;
  child: boolean;
  intervalSeconds: number;
  maxSeconds: number;
  /** Effective idle seconds to cold, or null for the platform rule. */
  coldAfterSeconds: number | null;
}

export interface AgentKeepWarmPatch {
  main?: boolean;
  child?: boolean;
  intervalSeconds?: number;
  maxSeconds?: number;
  coldAfterSeconds?: number | null;
}

export type KeepWarmDefaultPatch = Omit<Partial<KeepWarmDefaultPreferences>, "coldAfterSeconds"> & {
  coldAfterSeconds?: number | null;
};

export const KEEP_WARM_DEFAULT_ROW: KeepWarmDefaultPreferences = {
  main: false,
  child: false,
  maxSeconds: KEEP_WARM_DEFAULT_MAX_SECONDS,
};

export const KEEP_WARM_DEFAULTS: KeepWarmPreferences = {
  default: KEEP_WARM_DEFAULT_ROW,
  agents: {},
  hostOfflineArchiveSeconds: KEEP_WARM_DEFAULT_HOST_OFFLINE_ARCHIVE_SECONDS,
};

function defaultKeepWarmPreferences(): KeepWarmPreferences {
  return {
    default: { ...KEEP_WARM_DEFAULT_ROW },
    agents: {},
    hostOfflineArchiveSeconds: KEEP_WARM_DEFAULT_HOST_OFFLINE_ARCHIVE_SECONDS,
  };
}

function positiveInteger(value: unknown): number | undefined {
  if (typeof value !== "number" || !Number.isInteger(value) || value < 1) return undefined;
  return value;
}

function normalizeAgentRow(value: unknown): AgentKeepWarmPreferences | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const raw = value as Record<string, unknown>;
  const intervalSeconds = positiveInteger(raw.intervalSeconds);
  const rawCold = raw.coldAfterSeconds;
  const coldAfterSeconds =
    typeof rawCold === "number" && Number.isInteger(rawCold) && rawCold >= 0
      ? clampKeepWarmColdAfterSeconds(rawCold)
      : undefined;
  return {
    main: typeof raw.main === "boolean" ? raw.main : false,
    child: typeof raw.child === "boolean" ? raw.child : false,
    ...(intervalSeconds !== undefined ? { intervalSeconds } : {}),
    maxSeconds: positiveInteger(raw.maxSeconds) ?? KEEP_WARM_DEFAULT_MAX_SECONDS,
    ...(coldAfterSeconds !== undefined ? { coldAfterSeconds } : {}),
  };
}

function normalizeCell(value: unknown): KeepWarmCell | null {
  const row = normalizeAgentRow(value);
  if (!row) return null;
  const raw = value as Record<string, unknown>;
  return {
    ...(typeof raw.main === "boolean" ? { main: raw.main } : {}),
    ...(typeof raw.child === "boolean" ? { child: raw.child } : {}),
    ...(row.intervalSeconds !== undefined ? { intervalSeconds: row.intervalSeconds } : {}),
    ...(positiveInteger(raw.maxSeconds) !== undefined ? { maxSeconds: row.maxSeconds } : {}),
    ...(row.coldAfterSeconds !== undefined ? { coldAfterSeconds: row.coldAfterSeconds } : {}),
  };
}

function normalizeDefaultRow(value: unknown): KeepWarmDefaultPreferences {
  if (!value || typeof value !== "object" || Array.isArray(value))
    return { ...KEEP_WARM_DEFAULT_ROW };
  const raw = value as Record<string, unknown>;
  const rawCold = raw.coldAfterSeconds;
  const coldAfterSeconds =
    typeof rawCold === "number" && Number.isInteger(rawCold) && rawCold >= 0
      ? clampKeepWarmColdAfterSeconds(rawCold)
      : undefined;
  return {
    main: typeof raw.main === "boolean" ? raw.main : false,
    child: typeof raw.child === "boolean" ? raw.child : false,
    maxSeconds: clampKeepWarmMaxSeconds(
      positiveInteger(raw.maxSeconds) ?? KEEP_WARM_DEFAULT_MAX_SECONDS,
    ),
    ...(coldAfterSeconds !== undefined ? { coldAfterSeconds } : {}),
  };
}

/** Drop unknown/invalid fields; per-family clamps apply when a row is resolved. */
export function normalizeKeepWarmPreferences(value: unknown): KeepWarmPreferences {
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
  let cells: KeepWarmCells | undefined;
  if (raw.cells && typeof raw.cells === "object" && !Array.isArray(raw.cells)) {
    cells = {};
    for (const [host, rows] of Object.entries(raw.cells)) {
      if (!rows || typeof rows !== "object" || Array.isArray(rows)) continue;
      cells[host] = {};
      for (const [key, cellValue] of Object.entries(rows)) {
        const row = normalizeCell(cellValue);
        if (row) cells[host][key] = row;
      }
    }
  }
  const migratedToCellsAt = Number.isInteger(raw.migratedToCellsAt)
    ? (raw.migratedToCellsAt as number)
    : undefined;
  const columns = Array.isArray(raw.columns)
    ? [
        ...new Set(
          raw.columns.filter((key): key is string => typeof key === "string" && key.length > 0),
        ),
      ]
    : undefined;
  return {
    default: normalizeDefaultRow(raw.default),
    agents,
    hostOfflineArchiveSeconds,
    ...(migratedFromLegacyAt !== undefined ? { migratedFromLegacyAt } : {}),
    ...(cells !== undefined ? { cells } : {}),
    ...(migratedToCellsAt !== undefined ? { migratedToCellsAt } : {}),
    ...(columns !== undefined ? { columns } : {}),
  };
}

export function isKeepWarmPreferencesDefault(preferences: KeepWarmPreferences): boolean {
  return (
    preferences.cells === undefined &&
    Object.keys(preferences.agents).length === 0 &&
    !preferences.default.main &&
    !preferences.default.child &&
    preferences.default.maxSeconds === KEEP_WARM_DEFAULT_MAX_SECONDS &&
    preferences.default.coldAfterSeconds === undefined &&
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

/** Effective row for a direct agent key, falling back to the shared default. */
export function resolveKeepWarmAgent(
  preferences: KeepWarmPreferences,
  agentId: string,
  family: KeepWarmFamily,
): ResolvedAgentKeepWarm {
  const row = preferences.agents[agentId];
  return {
    main: row?.main ?? preferences.default.main,
    child: row?.child ?? preferences.default.child,
    intervalSeconds: clampKeepWarmIntervalSeconds(
      family,
      row?.intervalSeconds ?? defaultKeepWarmIntervalSeconds(family),
    ),
    maxSeconds: clampKeepWarmMaxSeconds(row?.maxSeconds ?? preferences.default.maxSeconds),
    coldAfterSeconds: row
      ? (row.coldAfterSeconds ?? null)
      : (preferences.default.coldAfterSeconds ?? null),
  };
}

/** Merge one exception row; a first write materializes the effective defaults. */
export function setKeepWarmAgent(
  preferences: KeepWarmPreferences,
  agentId: string,
  family: KeepWarmFamily,
  patch: AgentKeepWarmPatch,
): KeepWarmPreferences {
  const current = preferences.agents[agentId];
  const coldAfterSeconds =
    patch.coldAfterSeconds === undefined
      ? current
        ? current.coldAfterSeconds
        : preferences.default.coldAfterSeconds
      : patch.coldAfterSeconds === null
        ? undefined
        : clampKeepWarmColdAfterSeconds(patch.coldAfterSeconds);
  const row: AgentKeepWarmPreferences = {
    main: patch.main ?? current?.main ?? preferences.default.main,
    child: patch.child ?? current?.child ?? preferences.default.child,
    intervalSeconds: clampKeepWarmIntervalSeconds(
      family,
      patch.intervalSeconds ?? current?.intervalSeconds ?? defaultKeepWarmIntervalSeconds(family),
    ),
    maxSeconds: clampKeepWarmMaxSeconds(
      patch.maxSeconds ?? current?.maxSeconds ?? preferences.default.maxSeconds,
    ),
    ...(coldAfterSeconds !== undefined ? { coldAfterSeconds } : {}),
  };
  return { ...preferences, agents: { ...preferences.agents, [agentId]: row } };
}

export function setKeepWarmDefault(
  preferences: KeepWarmPreferences,
  patch: KeepWarmDefaultPatch,
): KeepWarmPreferences {
  const current = preferences.default;
  const coldAfterSeconds =
    patch.coldAfterSeconds === undefined
      ? current.coldAfterSeconds
      : patch.coldAfterSeconds === null
        ? undefined
        : clampKeepWarmColdAfterSeconds(patch.coldAfterSeconds);
  return {
    ...preferences,
    default: {
      main: patch.main ?? current.main,
      child: patch.child ?? current.child,
      maxSeconds: clampKeepWarmMaxSeconds(patch.maxSeconds ?? current.maxSeconds),
      ...(coldAfterSeconds !== undefined ? { coldAfterSeconds } : {}),
    },
  };
}

export interface KeepWarmAgentIdentity {
  id: string;
  harness: string | null | undefined;
}

/** Preserve unresolved child-on identities when mirroring to older clients. */
function mirrorKeepWarmPreferences(
  normalized: KeepWarmPreferences,
  agentIdentities: readonly KeepWarmAgentIdentity[] = [],
): void {
  const harnessById = new Map(agentIdentities.map((agent) => [agent.id, agent.harness]));
  let hasUnclassifiableChild = false;
  let hasNativeChild =
    normalized.cells === undefined &&
    ["claude-native", "codex-native"].some(
      (harness) => normalized.agents[`harness:${harness}`]?.child ?? normalized.default.child,
    );
  const rows =
    normalized.cells === undefined
      ? Object.entries(normalized.agents)
      : Object.values(normalized.cells).flatMap((host) => Object.entries(host));
  for (const [agentId, row] of rows) {
    if (!row.child) continue;
    if (agentId.startsWith("harness:")) {
      if (isLegacyNativeKeepWarmHarness(agentId.slice("harness:".length))) hasNativeChild = true;
      continue;
    }
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
}

function persistKeepWarmPreferences(preferences: KeepWarmPreferences): KeepWarmPreferences {
  const normalized = normalizeKeepWarmPreferences(preferences);
  const isDefault = isKeepWarmPreferencesDefault(normalized);
  try {
    if (isDefault) window.localStorage.removeItem(KEEP_WARM_STORAGE_KEY);
    else window.localStorage.setItem(KEEP_WARM_STORAGE_KEY, JSON.stringify(normalized));
  } catch {
    // Storage denial or quota exhaustion must not break the settings page.
  }
  window.dispatchEvent(new Event(KEEP_WARM_CHANGED_EVENT));
  return normalized;
}

function namespaceValue(preferences: KeepWarmPreferences) {
  if (isKeepWarmPreferencesDefault(preferences)) return null;
  if (preferences.cells === undefined) return preferences;
  // Legacy rows remain on the server for rollback; cells saves never rewrite them.
  const { default: _default, agents: _agents, ...cellsNamespace } = preferences;
  return cellsNamespace;
}

export function writeKeepWarmPreferences(
  preferences: KeepWarmPreferences,
  agentIdentities: readonly KeepWarmAgentIdentity[] = [],
): void {
  if (typeof window === "undefined") return;
  const normalized = persistKeepWarmPreferences(preferences);
  mirrorKeepWarmPreferences(normalized, agentIdentities);
  queueUserPreferencePatch("keep_warm", namespaceValue(normalized));
}

export async function saveKeepWarmPreferences(
  preferences: KeepWarmPreferences,
  agentIdentities: readonly KeepWarmAgentIdentity[],
  signal?: AbortSignal,
): Promise<void> {
  if (signal?.aborted) throw new Error("Save cancelled.");
  const normalized = persistKeepWarmPreferences(preferences);
  await queueAcknowledgedUserPreferencePatch("keep_warm", namespaceValue(normalized), signal);
  mirrorKeepWarmPreferences(normalized, agentIdentities);
}

export function resolveKeepWarmCell(
  preferences: KeepWarmPreferences,
  host: string,
  key: string,
  harness?: string | null,
): AgentKeepWarmPreferences {
  const rows = preferences.cells?.[host];
  const row = rows?.[key] ?? (harness ? rows?.[`harness:${harness}`] : undefined);
  return {
    ...KEEP_WARM_DEFAULT_ROW,
    ...row,
    maxSeconds: clampKeepWarmMaxSeconds(row?.maxSeconds ?? KEEP_WARM_DEFAULT_MAX_SECONDS),
  };
}

export function turnOffKeepWarmCell(
  preferences: KeepWarmPreferences,
  host: string,
  key: string,
  harness?: string | null,
): KeepWarmPreferences {
  const rows = { ...preferences.cells?.[host] };
  Reflect.deleteProperty(rows, key);
  const fallback = !key.startsWith("harness:") && harness ? rows[`harness:${harness}`] : undefined;
  if (
    !key.startsWith("harness:") &&
    (!harness ||
      (fallback &&
        (fallback.main ||
          fallback.child ||
          fallback.coldAfterSeconds !== undefined ||
          fallback.intervalSeconds !== undefined ||
          (fallback.maxSeconds !== undefined &&
            fallback.maxSeconds !== KEEP_WARM_DEFAULT_MAX_SECONDS))))
  ) {
    rows[key] = { main: false, child: false };
  }
  return { ...preferences, cells: { ...preferences.cells, [host]: rows } };
}
