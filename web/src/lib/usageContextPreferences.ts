import { queueUserPreferencePatch } from "./userPreferencesSync";
import {
  providerUsageLimitsFromStoredValue,
  type ProviderUsageLimitsSnapshot,
} from "./providerUsageLimits";

export const USAGE_CONTEXT_STORAGE_KEY = "omnigent:usage-context-preferences";
export const USAGE_CONTEXT_CHANGED_EVENT = "omnigent:usage-context-preferences-changed";
const MAX_PROVIDER_USAGE_SOURCES = 24;
/** Levels whose keys exist, so a key carrying any other tag stays stored and unread. */
const KNOWN_LEVELS: readonly string[] = ["agent-model", "cell", "column", "host", "all"];

export interface UsageContextOverride {
  contextWindowTokens: number | null;
  /** Tokens reserved before the context total; compact point = total - buffer. */
  autoCompactBufferTokens: number | null;
  autoCompactBufferPercent?: number | null;
  compactBufferMode?: "tokens" | "percent";
}

/** Only present fields are applied; `null` clears the field to Auto. */
export interface UsageContextOverridePatch {
  contextWindowTokens?: number | null;
  autoCompactBufferTokens?: number | null;
  autoCompactBufferPercent?: number | null;
  compactBufferMode?: "tokens" | "percent";
}

export interface UsageContextPreferences {
  version: 5 | 6;
  /** Show provider-reported usage windows beside the context ring. */
  showProviderUsageLimits: boolean;
  /** Model, computer × agent, and agent column display overrides. */
  overrides: Record<string, UsageContextOverride>;
  /** Last valid provider reading for the exact Host/agent/harness/model source. */
  lastProviderUsageLimits: Record<string, ProviderUsageLimitsSnapshot>;
  /** The Usage board's display preferences. */
  board: UsageBoardPreferences;
  /** Account display labels; these never participate in session selection. */
  shownModels?: Record<string, string>;
}

export interface UsageContextSource {
  hostId: string;
  agentName: string;
  harness: string;
  model: string;
}

/**
 * Usage board display preferences. All per user, in the existing
 * ``usage_context`` namespace (a new namespace would refuse a full-envelope
 * write on an older image), and never written back to a usage source.
 */
export interface UsageBoardBand {
  id: string;
  name: string;
  /** Account ids in hand order; an id absent here is shown after them. */
  accounts: string[];
  open: boolean;
}

export interface UsageBoardPreferences {
  range?: {
    key: "today" | "h24" | "d7" | "period" | "d30" | "d90" | "custom";
    from?: string;
    to?: string;
  };
  unit?: "money" | "tokens";
  /** The user's switch. The deployment gate sits on top of it. */
  enabled: boolean;
  density: "std" | "mini";
  /** Grouping and order; empty means one group in the server's order. */
  bands: UsageBoardBand[];
  /** Account id → window id, or ``auto`` (most urgent). */
  windows: Record<string, string>;
  /** Account id → renewal date, epoch ms, where the source gives none. */
  renewals: Record<string, number>;
  /** Accounts the user switched off in ``On board``: not on the board. */
  hidden: string[];
}

export const DEFAULT_USAGE_BOARD_PREFERENCES: UsageBoardPreferences = {
  enabled: false,
  density: "std",
  bands: [],
  windows: {},
  renewals: {},
  hidden: [],
};

export const DEFAULT_USAGE_CONTEXT_PREFERENCES: UsageContextPreferences = {
  version: 6,
  showProviderUsageLimits: true,
  overrides: {},
  lastProviderUsageLimits: {},
  board: DEFAULT_USAGE_BOARD_PREFERENCES,
};

function positiveInteger(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && value > 0
    ? Math.round(value)
    : null;
}

function nonNegativeInteger(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && value >= 0
    ? Math.round(value)
    : null;
}

function bufferPercent(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 && value < 100
    ? value
    : null;
}

export function fixedBufferFitsCapacity(buffer: number | null, capacity: number | null): boolean {
  return buffer === null || capacity === null || buffer < capacity;
}

export function normalizeUsageContextPreferences(value: unknown): UsageContextPreferences {
  if (!value || typeof value !== "object") return { ...DEFAULT_USAGE_CONTEXT_PREFERENCES };
  const raw = value as {
    showProviderUsageLimits?: unknown;
    /** v2 compatibility. */
    showCodexRateLimits?: unknown;
    overrides?: unknown;
    lastProviderUsageLimits?: unknown;
    board?: unknown;
    shownModels?: unknown;
  };
  const overrides: Record<string, UsageContextOverride> = {};
  if (raw.overrides && typeof raw.overrides === "object") {
    for (const [key, candidate] of Object.entries(raw.overrides).slice(0, 100)) {
      if (key.length === 0 || key.length > 512 || !candidate || typeof candidate !== "object")
        continue;
      try {
        const parts: unknown = JSON.parse(key);
        if (
          Array.isArray(parts) &&
          parts.length === 5 &&
          parts.every((part) => typeof part === "string") &&
          !KNOWN_LEVELS.includes(parts[4] as string)
        ) {
          overrides[key] = candidate as UsageContextOverride;
          continue;
        }
      } catch {
        // Legacy opaque keys still use the value normalizer below.
      }
      const source = candidate as Partial<UsageContextOverride> & {
        autoCompactThresholdPercent?: unknown;
      };
      const contextWindowTokens = positiveInteger(source.contextWindowTokens);
      const storedBufferTokens = nonNegativeInteger(source.autoCompactBufferTokens);
      const legacyPercent = source.autoCompactThresholdPercent;
      const migratedBuffer =
        storedBufferTokens === null &&
        contextWindowTokens !== null &&
        typeof legacyPercent === "number" &&
        Number.isFinite(legacyPercent) &&
        legacyPercent >= 1 &&
        legacyPercent < 100
          ? Math.round((contextWindowTokens * (100 - legacyPercent)) / 100)
          : null;
      const autoCompactBufferTokens =
        storedBufferTokens ??
        (migratedBuffer !== null && migratedBuffer > 0 ? migratedBuffer : null);
      const autoCompactBufferPercent = bufferPercent(source.autoCompactBufferPercent);
      const compactBufferMode =
        source.compactBufferMode === "percent"
          ? "percent"
          : source.compactBufferMode === "tokens"
            ? "tokens"
            : undefined;
      const normalized: UsageContextOverride = {
        contextWindowTokens,
        autoCompactBufferTokens,
        ...(autoCompactBufferPercent !== null ? { autoCompactBufferPercent } : {}),
        ...(compactBufferMode ? { compactBufferMode } : {}),
      };
      if (
        contextWindowTokens !== null ||
        autoCompactBufferTokens !== null ||
        autoCompactBufferPercent !== null ||
        compactBufferMode === "percent"
      ) {
        overrides[key] = normalized;
      }
    }
  }
  const lastProviderUsageLimits: Record<string, ProviderUsageLimitsSnapshot> = {};
  if (raw.lastProviderUsageLimits && typeof raw.lastProviderUsageLimits === "object") {
    const entries = Object.entries(raw.lastProviderUsageLimits)
      .slice(0, 200)
      .flatMap(([key, candidate]) => {
        const snapshot = providerUsageLimitsFromStoredValue(candidate);
        return key.length > 0 && key.length <= 512 && snapshot ? [[key, snapshot] as const] : [];
      })
      .sort((a, b) => b[1].capturedAt - a[1].capturedAt)
      .slice(0, MAX_PROVIDER_USAGE_SOURCES);
    for (const [key, snapshot] of entries) lastProviderUsageLimits[key] = snapshot;
  }
  return {
    version: 6,
    showProviderUsageLimits:
      raw.showProviderUsageLimits !== undefined
        ? raw.showProviderUsageLimits !== false
        : raw.showCodexRateLimits !== false,
    overrides,
    lastProviderUsageLimits,
    board: normalizeUsageBoardPreferences(raw.board),
    ...(raw.shownModels && typeof raw.shownModels === "object"
      ? {
          shownModels: Object.fromEntries(
            Object.entries(raw.shownModels)
              .filter(
                ([id, label]) =>
                  id.length > 0 &&
                  id.length <= 128 &&
                  typeof label === "string" &&
                  label.length <= 128,
              )
              .slice(0, 64),
          ) as Record<string, string>,
        }
      : {}),
  };
}

function idMap(value: unknown): Record<string, string> {
  const out: Record<string, string> = {};
  if (!value || typeof value !== "object") return out;
  for (const [key, candidate] of Object.entries(value).slice(0, 64)) {
    if (key.length === 0 || key.length > 64) continue;
    if (typeof candidate !== "string" || candidate.length === 0 || candidate.length > 32) continue;
    out[key] = candidate;
  }
  return out;
}

function dateMap(value: unknown): Record<string, number> {
  const out: Record<string, number> = {};
  if (!value || typeof value !== "object") return out;
  for (const [key, candidate] of Object.entries(value).slice(0, 64)) {
    if (key.length === 0 || key.length > 64) continue;
    if (typeof candidate !== "number" || !Number.isFinite(candidate) || candidate <= 0) continue;
    out[key] = Math.round(candidate);
  }
  return out;
}

export function normalizeUsageBoardPreferences(value: unknown): UsageBoardPreferences {
  if (!value || typeof value !== "object") return { ...DEFAULT_USAGE_BOARD_PREFERENCES };
  const raw = value as {
    enabled?: unknown;
    density?: unknown;
    bands?: unknown;
    windows?: unknown;
    renewals?: unknown;
    hidden?: unknown;
    range?: UsageBoardPreferences["range"];
    unit?: unknown;
  };
  const bands: UsageBoardBand[] = [];
  const seenBands = new Set<string>();
  if (Array.isArray(raw.bands)) {
    for (const candidate of raw.bands.slice(0, 24)) {
      if (!candidate || typeof candidate !== "object") continue;
      const band = candidate as {
        id?: unknown;
        name?: unknown;
        accounts?: unknown;
        open?: unknown;
      };
      const id = typeof band.id === "string" ? band.id.slice(0, 32) : "";
      if (id.length === 0 || seenBands.has(id)) continue;
      seenBands.add(id);
      bands.push({
        id,
        name: typeof band.name === "string" ? band.name.slice(0, 40) : id,
        accounts: Array.isArray(band.accounts)
          ? band.accounts.filter((item): item is string => typeof item === "string").slice(0, 64)
          : [],
        open: band.open !== false,
      });
    }
  }
  return {
    enabled: raw.enabled === true,
    density: raw.density === "mini" ? "mini" : "std",
    bands,
    windows: idMap(raw.windows),
    renewals: dateMap(raw.renewals),
    hidden: Array.isArray(raw.hidden)
      ? raw.hidden.filter((item): item is string => typeof item === "string").slice(0, 64)
      : [],
    ...(raw.range &&
    ["today", "h24", "d7", "period", "d30", "d90", "custom"].includes(raw.range.key)
      ? {
          range: {
            key: raw.range.key,
            ...(typeof raw.range.from === "string" ? { from: raw.range.from.slice(0, 10) } : {}),
            ...(typeof raw.range.to === "string" ? { to: raw.range.to.slice(0, 10) } : {}),
          },
        }
      : {}),
    ...(raw.unit === "tokens" || raw.unit === "money" ? { unit: raw.unit } : {}),
  };
}

export function isDefaultUsageBoardPreferences(board: UsageBoardPreferences): boolean {
  return (
    !board.enabled &&
    board.density === "std" &&
    board.bands.length === 0 &&
    Object.keys(board.windows).length === 0 &&
    Object.keys(board.renewals).length === 0 &&
    board.hidden.length === 0 &&
    (!board.range || board.range.key === "d7") &&
    board.unit !== "tokens"
  );
}

export function usageContextSourceKey(source: {
  hostId: string | null | undefined;
  agentName: string | null | undefined;
  harness: string | null | undefined;
  model: string | null | undefined;
}): string {
  return JSON.stringify([
    source.hostId ?? "",
    source.agentName ?? "",
    source.harness ?? "",
    source.model ?? "",
  ]);
}

/** Recover a displayable exact-source tuple from a persisted source key. */
export function usageContextSourceFromKey(sourceKey: string): UsageContextSource | null {
  try {
    const value: unknown = JSON.parse(sourceKey);
    if (
      !Array.isArray(value) ||
      value.length !== 4 ||
      value.some((part) => typeof part !== "string")
    ) {
      return null;
    }
    const [hostId, agentName, harness, model] = value as [string, string, string, string];
    return { hostId, agentName, harness, model };
  } catch {
    return null;
  }
}

export type UsageContextLevel = "model" | "agent-model" | "cell" | "column" | "host" | "all";
export type UsageContextValueLevel = UsageContextLevel | "reported";

export function usageContextLevelKey(source: UsageContextSource, level: UsageContextLevel): string {
  if (level === "model") return usageContextSourceKey(source);
  if (level === "agent-model")
    return JSON.stringify(["", source.agentName, source.harness, source.model, "agent-model"]);
  if (level === "host") return JSON.stringify([source.hostId, "", "", "", "host"]);
  if (level === "all") return JSON.stringify(["", "", "", "", "all"]);
  return JSON.stringify([
    level === "column" ? "" : source.hostId,
    source.agentName,
    source.harness,
    "",
    level,
  ]);
}

/** A level's own fields first, then the levels it inherits from, then the agent's report. */
export function usageContextLevelsAt(level: UsageContextLevel): UsageContextLevel[] {
  if (level === "model") return ["model", "agent-model", "cell", "column", "host", "all"];
  // A value for every host cannot inherit from one host's cell or column.
  if (level === "agent-model") return ["agent-model", "column", "all"];
  if (level === "cell") return ["cell", "column", "host", "all"];
  if (level === "column") return ["column", "all"];
  if (level === "host") return ["host", "all"];
  return ["all"];
}

/** Unknown levels remain stored but have no display or resolution semantics. */
export function usageContextRecordFromKey(
  key: string,
): { source: UsageContextSource; level: UsageContextLevel } | null {
  const model = usageContextSourceFromKey(key);
  if (model) return { source: model, level: "model" };
  try {
    const parts: unknown = JSON.parse(key);
    if (
      !Array.isArray(parts) ||
      parts.length !== 5 ||
      parts.some((part) => typeof part !== "string")
    )
      return null;
    const [hostId, agentName, harness, modelName, level] = parts as [
      string,
      string,
      string,
      string,
      string,
    ];
    if (level === "agent-model") {
      if (hostId !== "" || !modelName) return null;
      return { source: { hostId, agentName, harness, model: modelName }, level };
    }
    if (!KNOWN_LEVELS.includes(level) || modelName !== "") return null;
    if (level === "host" && (agentName !== "" || harness !== "")) return null;
    if (level === "all" && (hostId !== "" || agentName !== "" || harness !== "")) return null;
    if (level === "column" && hostId !== "") return null;
    return { source: { hostId, agentName, harness, model: "" }, level: level as UsageContextLevel };
  } catch {
    return null;
  }
}

export function usageContextLevelLabel(
  level: UsageContextValueLevel,
  labels: { hostLabel: string; agentLabel: string },
): string {
  if (level === "model") return "this model";
  if (level === "agent-model") return `${labels.agentLabel}, every host`;
  if (level === "cell") return `${labels.hostLabel} × ${labels.agentLabel}`;
  if (level === "column") return `${labels.agentLabel}, all hosts`;
  if (level === "host") return `all agents on ${labels.hostLabel}`;
  if (level === "all") return "all agents, all hosts";
  return "reported";
}

/** The composer ring's one entry: this session's row in Settings, opened on its model. */
export function usageContextSettingsHref(sourceKey: string): string {
  const query = new URLSearchParams({ ctx: sourceKey });
  return `/settings/general?${query.toString()}#context-usage`;
}

export interface ResolvedUsageContext {
  contextWindow: { value: number | null; level: UsageContextValueLevel };
  buffer: { mode: "tokens" | "percent"; value: number | null; level: UsageContextValueLevel };
  autoCompactTokenLimit: number | null;
  ignored: { level: UsageContextLevel; reason: "buffer ≥ context" }[];
}

export function resolveUsageContext(
  preferences: UsageContextPreferences,
  sourceKey: string,
  reported: { contextWindow: number | null; autoCompactTokenLimit: number | null },
): ResolvedUsageContext {
  const record = usageContextRecordFromKey(sourceKey);
  return resolveUsageContextAt(
    preferences,
    record?.source ?? null,
    record ? usageContextLevelsAt(record.level) : [],
    reported,
  );
}

/** Resolve what one level would use, before anything below it is read. */
export function resolveUsageContextLevel(
  preferences: UsageContextPreferences,
  source: UsageContextSource,
  level: UsageContextLevel,
  reported: { contextWindow: number | null; autoCompactTokenLimit: number | null } = {
    contextWindow: null,
    autoCompactTokenLimit: null,
  },
): ResolvedUsageContext {
  return resolveUsageContextAt(preferences, source, usageContextLevelsAt(level), reported);
}

/** What a level would use if it held no value of its own. */
export function inheritedUsageContext(
  preferences: UsageContextPreferences,
  source: UsageContextSource,
  level: UsageContextLevel,
): ResolvedUsageContext {
  return resolveUsageContextAt(preferences, source, usageContextLevelsAt(level).slice(1), {
    contextWindow: null,
    autoCompactTokenLimit: null,
  });
}

function resolveUsageContextAt(
  preferences: UsageContextPreferences,
  source: UsageContextSource | null,
  levels: readonly UsageContextLevel[],
  reported: { contextWindow: number | null; autoCompactTokenLimit: number | null },
): ResolvedUsageContext {
  const candidates = !source
    ? []
    : levels.map((level) => ({
        level,
        override: preferences.overrides[usageContextLevelKey(source, level)],
      }));
  let contextWindow: ResolvedUsageContext["contextWindow"] = {
    value: reported.contextWindow,
    level: "reported",
  };
  for (const { level, override } of candidates) {
    const value = positiveInteger(override?.contextWindowTokens);
    if (value !== null) {
      contextWindow = { value, level };
      break;
    }
  }
  const reportedBuffer =
    reported.contextWindow != null &&
    reported.autoCompactTokenLimit != null &&
    reported.autoCompactTokenLimit <= reported.contextWindow
      ? reported.contextWindow - reported.autoCompactTokenLimit
      : null;
  const ignored: ResolvedUsageContext["ignored"] = [];
  for (const { level, override } of candidates) {
    const mode = override?.compactBufferMode ?? "tokens";
    const value =
      mode === "percent"
        ? bufferPercent(override?.autoCompactBufferPercent)
        : nonNegativeInteger(override?.autoCompactBufferTokens);
    if (value === null) continue;
    if (mode === "tokens" && !fixedBufferFitsCapacity(value, contextWindow.value)) {
      ignored.push({ level, reason: "buffer ≥ context" });
      continue;
    }
    const tokens =
      mode === "percent" && contextWindow.value != null
        ? (contextWindow.value * value) / 100
        : value;
    return {
      contextWindow,
      buffer: { mode, value, level },
      ignored,
      autoCompactTokenLimit:
        contextWindow.value != null ? contextWindow.value - tokens : reported.autoCompactTokenLimit,
    };
  }
  const effectiveReportedBuffer = fixedBufferFitsCapacity(reportedBuffer, contextWindow.value)
    ? reportedBuffer
    : null;
  return {
    contextWindow,
    buffer: { mode: "tokens", value: effectiveReportedBuffer, level: "reported" },
    ignored,
    autoCompactTokenLimit:
      contextWindow.value != null && effectiveReportedBuffer != null
        ? contextWindow.value - effectiveReportedBuffer
        : null,
  };
}

/** Pure proposal; only the manager's Merge action persists these replacements. */
export function proposeUsageContextCollapse(preferences: UsageContextPreferences): {
  removed: string[];
  added: Record<string, UsageContextOverride>;
} {
  const normalized = normalizeUsageContextPreferences(preferences);
  const groups = new Map<string, { key: string; override: UsageContextOverride }[]>();
  for (const [key, override] of Object.entries(normalized.overrides)) {
    const source = usageContextSourceFromKey(key);
    if (!source) continue;
    const cellKey = usageContextLevelKey(source, "cell");
    const group = groups.get(cellKey) ?? [];
    group.push({ key, override });
    groups.set(cellKey, group);
  }
  const removed: string[] = [];
  const added: Record<string, UsageContextOverride> = {};
  const signature = (override: UsageContextOverride) =>
    JSON.stringify([
      override.contextWindowTokens,
      override.compactBufferMode ?? "tokens",
      override.autoCompactBufferTokens,
      override.autoCompactBufferPercent ?? null,
    ]);
  for (const [cellKey, group] of groups) {
    const first = group[0]!;
    if (
      group.length < 2 ||
      Object.hasOwn(preferences.overrides, cellKey) ||
      group.some(({ override }) => signature(override) !== signature(first.override))
    )
      continue;
    const overrides = { ...normalized.overrides, [cellKey]: first.override };
    for (const { key } of group) Reflect.deleteProperty(overrides, key);
    const changed = group.some(({ key }) => {
      const reported = { contextWindow: null, autoCompactTokenLimit: null };
      const before = resolveUsageContext(normalized, key, reported);
      const after = resolveUsageContext({ ...normalized, overrides }, key, reported);
      return (
        before.contextWindow.value !== after.contextWindow.value ||
        before.buffer.value !== after.buffer.value ||
        before.buffer.mode !== after.buffer.mode
      );
    });
    if (changed) continue;
    removed.push(...group.map(({ key }) => key));
    added[cellKey] = first.override;
  }
  return { removed, added };
}

export function readUsageContextPreferences(): UsageContextPreferences {
  if (typeof window === "undefined") return { ...DEFAULT_USAGE_CONTEXT_PREFERENCES };
  try {
    const raw = window.localStorage.getItem(USAGE_CONTEXT_STORAGE_KEY);
    return raw
      ? normalizeUsageContextPreferences(JSON.parse(raw))
      : { ...DEFAULT_USAGE_CONTEXT_PREFERENCES };
  } catch {
    return { ...DEFAULT_USAGE_CONTEXT_PREFERENCES };
  }
}

function isDefault(preferences: UsageContextPreferences): boolean {
  return (
    preferences.showProviderUsageLimits &&
    Object.keys(preferences.overrides).length === 0 &&
    Object.keys(preferences.lastProviderUsageLimits).length === 0 &&
    Object.keys(preferences.shownModels ?? {}).length === 0 &&
    isDefaultUsageBoardPreferences(preferences.board)
  );
}

export function writeUsageContextPreferences(preferences: UsageContextPreferences): void {
  if (typeof window === "undefined") return;
  const normalized = normalizeUsageContextPreferences(preferences);
  try {
    if (isDefault(normalized)) {
      window.localStorage.removeItem(USAGE_CONTEXT_STORAGE_KEY);
    } else {
      window.localStorage.setItem(USAGE_CONTEXT_STORAGE_KEY, JSON.stringify(normalized));
    }
    window.dispatchEvent(new Event(USAGE_CONTEXT_CHANGED_EVENT));
    queueUserPreferencePatch("usage_context", isDefault(normalized) ? null : normalized);
  } catch {
    // Display preferences must never break the composer.
  }
}

function writeUsageContextPreferencesLocally(preferences: UsageContextPreferences): void {
  if (typeof window === "undefined") return;
  const normalized = normalizeUsageContextPreferences(preferences);
  try {
    if (isDefault(normalized)) {
      window.localStorage.removeItem(USAGE_CONTEXT_STORAGE_KEY);
    } else {
      window.localStorage.setItem(USAGE_CONTEXT_STORAGE_KEY, JSON.stringify(normalized));
    }
    window.dispatchEvent(new Event(USAGE_CONTEXT_CHANGED_EVENT));
  } catch {
    // Provider telemetry must never break the composer.
  }
}

export function writeUsageContextOverride(
  preferences: UsageContextPreferences,
  sourceKey: string,
  override: UsageContextOverride,
): void {
  const normalizedOverride: UsageContextOverride = {
    contextWindowTokens: positiveInteger(override.contextWindowTokens),
    autoCompactBufferTokens: nonNegativeInteger(override.autoCompactBufferTokens),
    ...(override.autoCompactBufferPercent !== undefined
      ? { autoCompactBufferPercent: bufferPercent(override.autoCompactBufferPercent) }
      : {}),
    ...(override.compactBufferMode ? { compactBufferMode: override.compactBufferMode } : {}),
  };
  const overrides = { ...preferences.overrides };
  if (
    normalizedOverride.contextWindowTokens === null &&
    normalizedOverride.autoCompactBufferTokens === null &&
    normalizedOverride.autoCompactBufferPercent == null &&
    normalizedOverride.compactBufferMode !== "percent"
  ) {
    Reflect.deleteProperty(overrides, sourceKey);
  } else {
    overrides[sourceKey] = normalizedOverride;
  }
  writeUsageContextPreferences({ ...preferences, version: 6, overrides });
}

/** Whether a saved record holds any value at all. A buffer unit alone does not count. */
export function usageContextHasValue(override: UsageContextOverride | undefined): boolean {
  return (
    !!override &&
    (positiveInteger(override.contextWindowTokens) !== null ||
      nonNegativeInteger(override.autoCompactBufferTokens) !== null ||
      bufferPercent(override.autoCompactBufferPercent) !== null)
  );
}

/** Replace one level's record whole; `null` deletes it. */
export function setUsageContextLevel(
  preferences: UsageContextPreferences,
  sourceKey: string,
  override: UsageContextOverride | null,
): void {
  const overrides = { ...preferences.overrides };
  if (override) overrides[sourceKey] = override;
  else Reflect.deleteProperty(overrides, sourceKey);
  writeUsageContextPreferences({ ...preferences, version: 6, overrides });
}

/**
 * Apply one patch to many sources with a single sync write. Absent patch
 * fields keep each row's existing value, so batched edits never copy one row's
 * settings onto another.
 */
export function patchUsageContextOverrides(
  preferences: UsageContextPreferences,
  sourceKeys: string[],
  patch: UsageContextOverridePatch,
): void {
  if (sourceKeys.length === 0) return;
  const overrides = { ...preferences.overrides };
  for (const sourceKey of sourceKeys) {
    const existing = overrides[sourceKey] ?? {
      contextWindowTokens: null,
      autoCompactBufferTokens: null,
    };
    const compactBufferMode =
      patch.compactBufferMode === "tokens" && existing.compactBufferMode === undefined
        ? undefined
        : (patch.compactBufferMode ?? existing.compactBufferMode);
    const next: UsageContextOverride = {
      contextWindowTokens:
        patch.contextWindowTokens !== undefined
          ? positiveInteger(patch.contextWindowTokens)
          : positiveInteger(existing.contextWindowTokens),
      autoCompactBufferTokens:
        patch.autoCompactBufferTokens !== undefined
          ? nonNegativeInteger(patch.autoCompactBufferTokens)
          : nonNegativeInteger(existing.autoCompactBufferTokens),
      ...(patch.autoCompactBufferPercent !== undefined ||
      existing.autoCompactBufferPercent !== undefined
        ? {
            autoCompactBufferPercent: bufferPercent(
              patch.autoCompactBufferPercent !== undefined
                ? patch.autoCompactBufferPercent
                : existing.autoCompactBufferPercent,
            ),
          }
        : {}),
      ...(compactBufferMode ? { compactBufferMode } : {}),
    };
    if (
      next.contextWindowTokens === null &&
      next.autoCompactBufferTokens === null &&
      next.autoCompactBufferPercent == null &&
      next.compactBufferMode !== "percent"
    ) {
      Reflect.deleteProperty(overrides, sourceKey);
    } else {
      overrides[sourceKey] = next;
    }
  }
  writeUsageContextPreferences({ ...preferences, version: 6, overrides });
}

/** Remove many sources with a single sync write. */
export function deleteUsageContextOverrides(
  preferences: UsageContextPreferences,
  sourceKeys: string[],
): void {
  const overrides = { ...preferences.overrides };
  let removed = false;
  for (const sourceKey of sourceKeys) {
    if (Object.hasOwn(overrides, sourceKey)) {
      Reflect.deleteProperty(overrides, sourceKey);
      removed = true;
    }
  }
  if (!removed) return;
  writeUsageContextPreferences({ ...preferences, version: 6, overrides });
}

/** Return the last valid reading for one exact Host/agent/harness/model source. */
export function providerUsageLimitsForSource(
  preferences: UsageContextPreferences,
  sourceKey: string,
): ProviderUsageLimitsSnapshot | null {
  return preferences.lastProviderUsageLimits[sourceKey] ?? null;
}

function sameDisplayedProviderUsage(
  left: ProviderUsageLimitsSnapshot | null | undefined,
  right: ProviderUsageLimitsSnapshot,
): boolean {
  if (!left) return false;
  const displayWindows = (value: ProviderUsageLimitsSnapshot) =>
    value.windows.map((window) => ({
      label: window.label,
      ariaLabel: window.ariaLabel,
      usedPercent: Math.round(Math.min(window.usedPercent, 100)),
    }));
  return (
    left.provider === right.provider &&
    left.scope === right.scope &&
    JSON.stringify(displayWindows(left)) === JSON.stringify(displayWindows(right))
  );
}

/** Save a changed reading into the user-synced, exact-source cache. */
export function writeLastProviderUsageLimits(
  preferences: UsageContextPreferences,
  sourceKey: string,
  snapshot: ProviderUsageLimitsSnapshot,
): void {
  if (sourceKey.length === 0 || sourceKey.length > 512) return;
  const previous = preferences.lastProviderUsageLimits[sourceKey];
  const displayIsUnchanged = sameDisplayedProviderUsage(previous, snapshot);
  if (displayIsUnchanged && previous && snapshot.capturedAt <= previous.capturedAt) return;
  const entries = Object.entries({
    ...preferences.lastProviderUsageLimits,
    [sourceKey]: snapshot,
  })
    .sort((a, b) => b[1].capturedAt - a[1].capturedAt)
    .slice(0, MAX_PROVIDER_USAGE_SOURCES);
  const next = {
    ...preferences,
    version: 6,
    lastProviderUsageLimits: Object.fromEntries(entries),
  } satisfies UsageContextPreferences;
  if (displayIsUnchanged) {
    // Keep the local freshness clock alive without turning identical provider
    // telemetry into user-preference sync traffic on every poll.
    writeUsageContextPreferencesLocally(next);
  } else {
    writeUsageContextPreferences(next);
  }
}

/** Merge one change into the stored Usage board preferences. */
export function patchUsageBoardPreferences(patch: Partial<UsageBoardPreferences>): void {
  const current = readUsageContextPreferences();
  const next: UsageContextPreferences = {
    ...current,
    version: 6,
    board: normalizeUsageBoardPreferences({ ...current.board, ...patch }),
  };
  writeUsageContextPreferences(next);
}
