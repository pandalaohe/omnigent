import { queueUserPreferencePatch } from "./userPreferencesSync";
import {
  providerUsageLimitsFromStoredValue,
  type ProviderUsageLimitsSnapshot,
} from "./providerUsageLimits";

export const USAGE_CONTEXT_STORAGE_KEY = "omnigent:usage-context-preferences";
export const USAGE_CONTEXT_CHANGED_EVENT = "omnigent:usage-context-preferences-changed";
const MAX_PROVIDER_USAGE_SOURCES = 24;
const MAX_CONTEXT_LIST_ENTRIES = 200;
const MAX_REPORTED_CONTEXT_SOURCES = 64;
/** Levels whose keys exist, so a key carrying any other tag stays stored and unread. */
const KNOWN_LEVELS: readonly string[] = ["agent-model", "cell", "column", "host", "all"];

export interface UsageContextOverride {
  contextWindowTokens: number | null;
  /** Tokens reserved before the context total; compact point = total - buffer. */
  autoCompactBufferTokens: number | null;
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
  /** The rows of Context limits; absent until the list is seeded, once. */
  contextList?: UsageContextList;
  /** What each exact Host/agent/harness/model source last reported, for Auto. */
  reportedContext?: Record<string, UsageContextReport>;
}

/** One row the user keeps in Context limits; an empty `model` is the agent's own row. */
export interface UsageContextListEntry {
  agentName: string;
  harness: string;
  model: string;
}

export interface UsageContextList {
  entries: UsageContextListEntry[];
  /** Several agents may stay open at once; otherwise opening one closes the others. */
  severalOpen: boolean;
}

export interface UsageContextReport {
  contextWindow: number | null;
  autoCompactTokenLimit: number | null;
  capturedAt: number;
}

/** What the agent itself said, with nothing saved applied. */
export interface UsageContextReported {
  contextWindow: number | null;
  autoCompactTokenLimit: number | null;
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
  /** Monthly subscription fees, used only to compare against usage. */
  fees?: Record<string, number>;
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
    contextList?: unknown;
    reportedContext?: unknown;
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
  const contextList = normalizeContextList(raw.contextList);
  const reportedContext = normalizeReportedContext(raw.reportedContext);
  return {
    version: 6,
    showProviderUsageLimits:
      raw.showProviderUsageLimits !== undefined
        ? raw.showProviderUsageLimits !== false
        : raw.showCodexRateLimits !== false,
    overrides,
    lastProviderUsageLimits,
    board: normalizeUsageBoardPreferences(raw.board),
    ...(contextList ? { contextList } : {}),
    ...(reportedContext ? { reportedContext } : {}),
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

function normalizeContextList(value: unknown): UsageContextList | undefined {
  if (!value || typeof value !== "object") return undefined;
  const raw = value as { entries?: unknown; severalOpen?: unknown };
  const entries = new Map<string, UsageContextListEntry>();
  for (const candidate of Array.isArray(raw.entries) ? raw.entries : []) {
    const entry = candidate as Partial<UsageContextListEntry> | null;
    if (
      typeof entry?.agentName !== "string" ||
      typeof entry.harness !== "string" ||
      !entry.agentName ||
      !entry.harness ||
      entry.agentName.length > 128 ||
      entry.harness.length > 128 ||
      (typeof entry.model !== "string" && entry.model !== undefined) ||
      (entry.model?.length ?? 0) > 256
    )
      continue;
    const clean = { agentName: entry.agentName, harness: entry.harness, model: entry.model ?? "" };
    entries.set(usageContextEntryKey(clean), clean);
    if (entries.size === MAX_CONTEXT_LIST_ENTRIES) break;
  }
  return { entries: [...entries.values()], severalOpen: raw.severalOpen === true };
}

function normalizeReportedContext(value: unknown): Record<string, UsageContextReport> | undefined {
  if (!value || typeof value !== "object") return undefined;
  const entries = Object.entries(value)
    .flatMap(([key, candidate]) => {
      const report = candidate as Partial<UsageContextReport> | null;
      const contextWindow = positiveInteger(report?.contextWindow);
      const autoCompactTokenLimit = positiveInteger(report?.autoCompactTokenLimit);
      const capturedAt = positiveInteger(report?.capturedAt);
      if (key.length === 0 || key.length > 512 || capturedAt === null) return [];
      if (contextWindow === null && autoCompactTokenLimit === null) return [];
      return [[key, { contextWindow, autoCompactTokenLimit, capturedAt }] as const];
    })
    .sort((a, b) => b[1].capturedAt - a[1].capturedAt)
    .slice(0, MAX_REPORTED_CONTEXT_SOURCES);
  return entries.length > 0 ? Object.fromEntries(entries) : undefined;
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
    fees?: unknown;
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
    ...(raw.fees && typeof raw.fees === "object"
      ? {
          fees: Object.fromEntries(
            Object.entries(raw.fees)
              .slice(0, 64)
              .filter(
                ([key, fee]) =>
                  key.length > 0 &&
                  key.length <= 64 &&
                  typeof fee === "number" &&
                  Number.isFinite(fee) &&
                  fee >= 0 &&
                  fee <= 1e9,
              ),
          ) as Record<string, number>,
        }
      : {}),
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
    board.unit !== "tokens" &&
    Object.keys(board.fees ?? {}).length === 0
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
  if (level === "agent-model") return `${labels.agentLabel}, this model on all computers`;
  if (level === "cell") return `${labels.agentLabel} on ${labels.hostLabel}`;
  if (level === "column") return `${labels.agentLabel}, all computers`;
  if (level === "host") return `all agents on ${labels.hostLabel}`;
  if (level === "all") return "all agents, all computers";
  return "reported";
}

/** The composer ring's one entry: this session's model row in Settings, opened for editing. */
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
  reported: UsageContextReported = { contextWindow: null, autoCompactTokenLimit: null },
): ResolvedUsageContext {
  return resolveUsageContextAt(preferences, source, usageContextLevelsAt(level).slice(1), reported);
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
    preferences.contextList === undefined &&
    Object.keys(preferences.reportedContext ?? {}).length === 0 &&
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

export function usageContextEntryKey(entry: UsageContextListEntry): string {
  return JSON.stringify([entry.agentName, entry.harness, entry.model]);
}

/** Saved values always hold a row: the list can never hide a value that is in force. */
export function usageContextSavedEntries(
  preferences: UsageContextPreferences,
): UsageContextListEntry[] {
  return Object.entries(preferences.overrides).flatMap(([key, override]) => {
    const record = usageContextRecordFromKey(key);
    if (!record?.source.agentName || !usageContextHasValue(override)) return [];
    const { agentName, harness, model } = record.source;
    return [{ agentName, harness, model }];
  });
}

/** Agent and agent+model combinations this user's stored readings prove were used. */
export function usageContextRecordedEntries(
  preferences: UsageContextPreferences,
): UsageContextListEntry[] {
  const readings = [
    ...Object.keys(preferences.lastProviderUsageLimits),
    ...Object.keys(preferences.reportedContext ?? {}),
  ];
  return [
    ...usageContextSavedEntries(preferences),
    ...readings.flatMap((key) => {
      const source = usageContextSourceFromKey(key);
      if (!source?.agentName || !source.harness) return [];
      const { agentName, harness, model } = source;
      return [{ agentName, harness, model }];
    }),
  ];
}

function uniqueEntries(entries: readonly UsageContextListEntry[]): UsageContextListEntry[] {
  return [...new Map(entries.map((entry) => [usageContextEntryKey(entry), entry])).values()];
}

/** The rows the table shows: the user's list, plus any saved value it does not name. */
export function usageContextRows(preferences: UsageContextPreferences): UsageContextListEntry[] {
  return uniqueEntries([
    ...(preferences.contextList?.entries ?? []),
    ...usageContextSavedEntries(preferences),
  ]);
}

/** The one automatic write to the list. After it, only the user changes the list. */
export function seedUsageContextList(
  preferences: UsageContextPreferences,
  inUse: readonly UsageContextListEntry[],
): void {
  if (preferences.contextList) return;
  writeUsageContextPreferences({
    ...preferences,
    version: 6,
    contextList: {
      entries: uniqueEntries([...inUse, ...usageContextRecordedEntries(preferences)]),
      severalOpen: false,
    },
  });
}

export function addUsageContextEntry(
  preferences: UsageContextPreferences,
  entry: UsageContextListEntry,
): void {
  writeUsageContextPreferences({
    ...preferences,
    version: 6,
    contextList: {
      entries: uniqueEntries([...(preferences.contextList?.entries ?? []), entry]),
      severalOpen: preferences.contextList?.severalOpen ?? false,
    },
  });
}

/** Every saved level an entry owns; an agent's row owns its models too. */
export function usageContextKeysOfEntry(
  preferences: UsageContextPreferences,
  entry: UsageContextListEntry,
): string[] {
  return Object.keys(preferences.overrides).filter((key) => {
    const record = usageContextRecordFromKey(key);
    return (
      !!record &&
      record.source.agentName === entry.agentName &&
      record.source.harness === entry.harness &&
      (entry.model === "" || record.source.model === entry.model)
    );
  });
}

/**
 * What the agent reported across every source a row and column cover. A field
 * shows only when they all agree: an agent's row spans models of different sizes.
 */
export function reportedUsageContext(
  preferences: UsageContextPreferences,
  scope: { agentName: string; harness: string; model: string; hostId: string | null },
): UsageContextReported {
  const reports = Object.entries(preferences.reportedContext ?? {}).flatMap(([key, report]) => {
    const source = usageContextSourceFromKey(key);
    if (
      !source ||
      source.agentName !== scope.agentName ||
      source.harness !== scope.harness ||
      (scope.model !== "" && source.model !== scope.model) ||
      (scope.hostId !== null && source.hostId !== scope.hostId)
    )
      return [];
    return [report];
  });
  const agreed = (field: keyof UsageContextReported) => {
    const values = new Set(reports.map((report) => report[field]));
    return values.size === 1 ? ([...values][0] ?? null) : null;
  };
  return {
    contextWindow: agreed("contextWindow"),
    autoCompactTokenLimit: agreed("autoCompactTokenLimit"),
  };
}

/** Keep what a session reports; only a changed figure is written and synced. */
export function writeReportedContext(
  preferences: UsageContextPreferences,
  sourceKey: string,
  report: UsageContextReported,
  capturedAt = Date.now(),
): void {
  if (sourceKey.length === 0 || sourceKey.length > 512) return;
  const previous = preferences.reportedContext?.[sourceKey];
  if (report.contextWindow === null && report.autoCompactTokenLimit === null) {
    if (!previous) return;
    const reportedContext = { ...preferences.reportedContext };
    delete reportedContext[sourceKey];
    writeUsageContextPreferences({ ...preferences, version: 6, reportedContext });
    return;
  }
  if (
    previous?.contextWindow === report.contextWindow &&
    previous.autoCompactTokenLimit === report.autoCompactTokenLimit
  )
    return;
  writeUsageContextPreferences({
    ...preferences,
    version: 6,
    reportedContext: {
      ...preferences.reportedContext,
      [sourceKey]: { ...report, capturedAt },
    },
  });
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
