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
  resolveField,
  type LayeredLevel,
  type LayeredOptions,
  type LayeredStore,
} from "./layeredValues";
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
  /** Layered records; present once the page has saved in the new shape. */
  layers?: KeepWarmLayers;
  migratedToLayersAt?: number;
}

export type KeepWarmCell = Partial<AgentKeepWarmPreferences>;
export type KeepWarmCells = Record<string, Record<string, KeepWarmCell>>;

export const KEEP_WARM_LAYER_FIELDS = [
  "main",
  "child",
  "intervalSeconds",
  "maxSeconds",
  "coldAfterSeconds",
] as const;
export type KeepWarmLayerField = (typeof KEEP_WARM_LAYER_FIELDS)[number];
export const KEEP_WARM_COLUMN_FIELDS: readonly KeepWarmLayerField[] = [
  "main",
  "child",
  "maxSeconds",
  "coldAfterSeconds",
];

/** Only the fields a level sets; an absent field inherits the next level. */
export interface KeepWarmLayerRecord {
  main?: boolean;
  child?: boolean;
  intervalSeconds?: number;
  maxSeconds?: number;
  /** ``null`` sets the platform rule at this level; absent inherits. */
  coldAfterSeconds?: number | null;
}

export interface KeepWarmLayers {
  corner: KeepWarmLayerRecord;
  hosts: Record<string, KeepWarmLayerRecord>;
  agents: Record<string, KeepWarmLayerRecord>;
  cells: Record<string, Record<string, KeepWarmLayerRecord>>;
  hostWins: Record<string, true>;
}

export interface KeepWarmRuntimeIdentity {
  templateId?: string | null;
  agentId?: string | null;
  harness?: string | null;
}

export interface ResolvedKeepWarmLayerRow {
  main: boolean;
  child: boolean;
  intervalSeconds: number | null;
  maxSeconds: number;
  coldAfterSeconds: number | null;
  sources: Record<KeepWarmLayerField, LayeredLevel | null>;
}

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
  const layers = raw.layers === undefined ? undefined : normalizeKeepWarmLayers(raw.layers);
  const migratedToLayersAt = Number.isInteger(raw.migratedToLayersAt)
    ? (raw.migratedToLayersAt as number)
    : undefined;
  return {
    default: normalizeDefaultRow(raw.default),
    agents,
    hostOfflineArchiveSeconds,
    ...(migratedFromLegacyAt !== undefined ? { migratedFromLegacyAt } : {}),
    ...(cells !== undefined ? { cells } : {}),
    ...(migratedToCellsAt !== undefined ? { migratedToCellsAt } : {}),
    ...(columns !== undefined ? { columns } : {}),
    ...(layers !== undefined && layers !== null ? { layers } : {}),
    ...(migratedToLayersAt !== undefined ? { migratedToLayersAt } : {}),
  };
}

export function isKeepWarmPreferencesDefault(preferences: KeepWarmPreferences): boolean {
  return (
    preferences.layers === undefined &&
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
  if (preferences.layers !== undefined) {
    // Legacy keys stay on the server untouched for rollback; a layers save
    // never rewrites them from this browser's older copy.
    return {
      hostOfflineArchiveSeconds: preferences.hostOfflineArchiveSeconds,
      layers: preferences.layers,
      ...(preferences.migratedToLayersAt !== undefined
        ? { migratedToLayersAt: preferences.migratedToLayersAt }
        : {}),
    };
  }
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

function normalizeLayerRecord(value: unknown, allowInterval: boolean): KeepWarmLayerRecord {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  const raw = value as Record<string, unknown>;
  const record: KeepWarmLayerRecord = {};
  if (typeof raw.main === "boolean") record.main = raw.main;
  if (typeof raw.child === "boolean") record.child = raw.child;
  if (allowInterval && positiveInteger(raw.intervalSeconds) !== undefined) {
    record.intervalSeconds = raw.intervalSeconds as number;
  }
  if (positiveInteger(raw.maxSeconds) !== undefined) {
    record.maxSeconds = raw.maxSeconds as number;
  }
  if (Object.hasOwn(raw, "coldAfterSeconds")) {
    const cold = raw.coldAfterSeconds;
    if (cold === null) record.coldAfterSeconds = null;
    else if (typeof cold === "number" && Number.isInteger(cold) && cold >= 0) {
      record.coldAfterSeconds = clampKeepWarmColdAfterSeconds(cold);
    }
  }
  return record;
}

function normalizeLayerRecords(value: unknown, allowInterval: boolean) {
  const records: Record<string, KeepWarmLayerRecord> = {};
  if (!value || typeof value !== "object" || Array.isArray(value)) return records;
  for (const [key, record] of Object.entries(value as Record<string, unknown>)) {
    const normalized = normalizeLayerRecord(record, allowInterval);
    if (Object.keys(normalized).length > 0) records[key] = normalized;
  }
  return records;
}

/** Drop unknown fields; a level record keeps only what that level can hold. */
export function normalizeKeepWarmLayers(value: unknown): KeepWarmLayers | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const raw = value as Record<string, unknown>;
  const cells: Record<string, Record<string, KeepWarmLayerRecord>> = {};
  if (raw.cells && typeof raw.cells === "object" && !Array.isArray(raw.cells)) {
    for (const [host, rows] of Object.entries(raw.cells)) {
      if (!rows || typeof rows !== "object" || Array.isArray(rows)) continue;
      const records = normalizeLayerRecords(rows, true);
      if (Object.keys(records).length > 0) cells[host] = records;
    }
  }
  const hostWins: Record<string, true> = {};
  const rawWins = raw.hostWins;
  if (Array.isArray(rawWins)) {
    for (const host of rawWins) if (typeof host === "string" && host) hostWins[host] = true;
  } else if (rawWins && typeof rawWins === "object") {
    for (const [host, marked] of Object.entries(rawWins)) {
      if (marked) hostWins[host] = true;
    }
  }
  return {
    corner: normalizeLayerRecord(raw.corner, false),
    hosts: normalizeLayerRecords(raw.hosts, false),
    agents: normalizeLayerRecords(raw.agents, true),
    cells,
    hostWins,
  };
}

function normalizeKeepWarmLayerValue(
  field: KeepWarmLayerField,
  family: KeepWarmFamily | null,
  value: unknown,
): unknown {
  if (field === "main" || field === "child") return value === true;
  if (field === "intervalSeconds") {
    if (family === null || family === "pi") return undefined;
    const seconds =
      typeof value === "number" && Number.isFinite(value)
        ? value
        : defaultKeepWarmIntervalSeconds(family);
    return clampKeepWarmIntervalSeconds(family, seconds);
  }
  if (field === "maxSeconds") {
    const seconds =
      typeof value === "number" && Number.isFinite(value) ? value : KEEP_WARM_DEFAULT_MAX_SECONDS;
    const clamped = clampKeepWarmMaxSeconds(seconds);
    return family === "pi" ? Math.min(clamped, 1800) : clamped;
  }
  if (typeof value === "number" && Number.isFinite(value)) {
    return clampKeepWarmColdAfterSeconds(value);
  }
  return null;
}

/** The layered store shape the shared resolver reads. */
export function keepWarmLayeredStore(
  layers: KeepWarmLayers,
  identity?: KeepWarmRuntimeIdentity & { row: string },
): LayeredStore<KeepWarmLayerField> {
  if (!identity) {
    return {
      corner: layers.corner,
      rows: layers.agents,
      cols: layers.hosts,
      cells: layers.cells,
      hostWins: layers.hostWins,
    };
  }
  const harnessKey = identity.harness ? `harness:${identity.harness}` : null;
  const keys = [
    identity.templateId ?? (identity.row.startsWith("harness:") ? null : identity.row),
    identity.agentId,
    harnessKey,
  ].filter((key): key is string => !!key);
  const row = keys.map((key) => layers.agents[key]).find((record) => record !== undefined);
  const cells = Object.fromEntries(
    Object.entries(layers.cells).map(([host, rows]) => {
      const cell = keys.map((key) => rows[key]).find((record) => record !== undefined);
      return [host, cell ? { ...rows, [identity.row]: cell } : rows];
    }),
  );
  return {
    corner: layers.corner,
    rows: row ? { ...layers.agents, [identity.row]: row } : layers.agents,
    cols: layers.hosts,
    cells,
    hostWins: layers.hostWins,
  };
}

/** Resolver options for one row's keep-warm family; cross-agent levels take every column field. */
export function keepWarmLayeredOptions(
  family: KeepWarmFamily | null,
  { crossAgent = false }: { crossAgent?: boolean } = {},
): LayeredOptions<KeepWarmLayerField> {
  return {
    colFields: KEEP_WARM_COLUMN_FIELDS,
    canHold: (_row, field) =>
      crossAgent
        ? KEEP_WARM_COLUMN_FIELDS.includes(field)
        : family !== null &&
          (family !== "pi" || (field !== "intervalSeconds" && field !== "coldAfterSeconds")),
    normalize: (_row, field, value) => normalizeKeepWarmLayerValue(field, family, value),
  };
}

/** Resolve one cell, as the runtime will use it, with its per-field source. */
export function resolveKeepWarmRow(
  layers: KeepWarmLayers,
  row: string,
  host: string,
  family: KeepWarmFamily | null,
  identity?: KeepWarmRuntimeIdentity,
): ResolvedKeepWarmLayerRow {
  const store = keepWarmLayeredStore(layers, { row, ...identity });
  const options = keepWarmLayeredOptions(family);
  const main = resolveField(store, row, host, "main", options);
  const child = resolveField(store, row, host, "child", options);
  const interval = resolveField(store, row, host, "intervalSeconds", options);
  const max = resolveField(store, row, host, "maxSeconds", options);
  const cold = resolveField(store, row, host, "coldAfterSeconds", options);
  return {
    main: main.value === true,
    child: child.value === true,
    intervalSeconds:
      family === null
        ? null
        : family === "pi"
          ? 1800
          : typeof interval.value === "number"
            ? interval.value
            : defaultKeepWarmIntervalSeconds(family),
    maxSeconds: typeof max.value === "number" ? max.value : KEEP_WARM_DEFAULT_MAX_SECONDS,
    coldAfterSeconds: typeof cold.value === "number" ? cold.value : null,
    sources: {
      main: main.source,
      child: child.source,
      intervalSeconds: interval.source,
      maxSeconds: max.source,
      coldAfterSeconds: cold.source,
    },
  };
}

/** Set one level, dropping empty records so the store stays a set of values. */
export function setKeepWarmLayer(
  layers: KeepWarmLayers,
  at: LayeredLevel,
  record: KeepWarmLayerRecord,
): KeepWarmLayers {
  const next: KeepWarmLayers = structuredClone(layers);
  const empty = Object.keys(record).length === 0;
  if (at.level === "corner") {
    next.corner = { ...record };
  } else if (at.level === "col") {
    if (empty) Reflect.deleteProperty(next.hosts, at.col);
    else next.hosts[at.col] = { ...record };
  } else if (at.level === "row") {
    if (empty) Reflect.deleteProperty(next.agents, at.row);
    else next.agents[at.row] = { ...record };
  } else if (empty) {
    const hostCells = next.cells[at.col];
    if (hostCells) {
      Reflect.deleteProperty(hostCells, at.row);
      if (Object.keys(hostCells).length === 0) {
        Reflect.deleteProperty(next.cells, at.col);
      }
    }
  } else {
    next.cells[at.col] = { ...next.cells[at.col], [at.row]: { ...record } };
  }
  return next;
}

/** Drop a cell field only when the resulting identity lookup keeps the chosen value. */
export function dropKeepWarmCellEquals(
  record: KeepWarmLayerRecord,
  layers: KeepWarmLayers,
  row: string,
  host: string,
  family: KeepWarmFamily | null,
  identity?: KeepWarmRuntimeIdentity,
): KeepWarmLayerRecord {
  const at: LayeredLevel = { level: "cell", row, col: host };
  const options = keepWarmLayeredOptions(family);
  const fields: KeepWarmLayerField[] = ["main", "child"];
  if (family && family !== "pi") fields.push("intervalSeconds");
  fields.push("maxSeconds");
  if (family && family !== "pi") fields.push("coldAfterSeconds");
  let next = { ...record };
  for (const field of fields) {
    if (next[field] === undefined) continue;
    const candidate = { ...next };
    Reflect.deleteProperty(candidate, field);
    const without = setKeepWarmLayer(layers, at, candidate);
    const store = keepWarmLayeredStore(without, { row, ...identity });
    const unchanged = fields.every((chosen) => {
      const expected = record[chosen];
      if (expected === undefined) return true;
      const own = options.normalize ? options.normalize(row, chosen, expected) : expected;
      return Object.is(own, resolveField(store, row, host, chosen, options).value);
    });
    if (unchanged) next = candidate;
  }
  return next;
}

/** Remove a level's own values; a host column loses its mark with them. */
export function clearKeepWarmLevel(layers: KeepWarmLayers, at: LayeredLevel): KeepWarmLayers {
  const next = setKeepWarmLayer(layers, at, {});
  if (at.level === "col") Reflect.deleteProperty(next.hostWins, at.col);
  return next;
}

/** Remove every level below this one, including host-column marks. */
export function clearKeepWarmBelow(layers: KeepWarmLayers, at: LayeredLevel): KeepWarmLayers {
  const next: KeepWarmLayers = structuredClone(layers);
  if (at.level === "corner") {
    next.agents = {};
    next.hosts = {};
    next.cells = {};
    next.hostWins = {};
  } else if (at.level === "row") {
    for (const rows of Object.values(next.cells)) Reflect.deleteProperty(rows, at.row);
  } else if (at.level === "col") {
    Reflect.deleteProperty(next.cells, at.col);
  }
  return next;
}

/** Set or clear one host column's ``Host wins`` mark. */
export function setKeepWarmHostWins(
  layers: KeepWarmLayers,
  host: string,
  on: boolean,
): KeepWarmLayers {
  const next: KeepWarmLayers = structuredClone(layers);
  if (on) next.hostWins[host] = true;
  else Reflect.deleteProperty(next.hostWins, host);
  return next;
}

/** The key a table row writes at: template, then agent, then ``harness:x``. */
export function keepWarmRowKey(identity: {
  templateId?: string | null;
  agentId?: string | null;
  harness?: string | null;
}): string {
  return (
    identity.templateId ??
    identity.agentId ??
    (identity.harness ? `harness:${identity.harness}` : "")
  );
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
