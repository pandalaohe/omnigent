import { queueUserPreferencePatch } from "./userPreferencesSync";
import {
  providerUsageLimitsFromStoredValue,
  type ProviderUsageLimitsSnapshot,
} from "./providerUsageLimits";

export const USAGE_CONTEXT_STORAGE_KEY = "omnigent:usage-context-preferences";
export const USAGE_CONTEXT_CHANGED_EVENT = "omnigent:usage-context-preferences-changed";
const MAX_PROVIDER_USAGE_SOURCES = 24;

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
}

export interface UsageContextSource {
  hostId: string;
  agentName: string;
  harness: string;
  model: string;
}

export const DEFAULT_USAGE_CONTEXT_PREFERENCES: UsageContextPreferences = {
  version: 6,
  showProviderUsageLimits: true,
  overrides: {},
  lastProviderUsageLimits: {},
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
          parts[4] !== "cell" &&
          parts[4] !== "column"
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
  };
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

export type UsageContextLevel = "model" | "cell" | "column";
export type UsageContextValueLevel = UsageContextLevel | "reported";

export function usageContextLevelKey(source: UsageContextSource, level: UsageContextLevel): string {
  if (level === "model") return usageContextSourceKey(source);
  return JSON.stringify([
    level === "column" ? "" : source.hostId,
    source.agentName,
    source.harness,
    "",
    level,
  ]);
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
    const [hostId, agentName, harness, modelName, level] = parts;
    if (
      modelName !== "" ||
      (level !== "cell" && level !== "column") ||
      (level === "column" && hostId !== "")
    )
      return null;
    return { source: { hostId, agentName, harness, model: "" }, level };
  } catch {
    return null;
  }
}

export function usageContextLevelLabel(
  level: UsageContextValueLevel,
  labels: { hostLabel: string; agentLabel: string },
): string {
  if (level === "model") return "model override";
  if (level === "cell") return `${labels.hostLabel} × ${labels.agentLabel}`;
  if (level === "column") return `${labels.agentLabel} · all hosts`;
  return "reported";
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
  const levels: UsageContextLevel[] =
    record?.level === "column"
      ? ["column"]
      : record?.level === "cell"
        ? ["cell", "column"]
        : ["model", "cell", "column"];
  const candidates = record
    ? levels.map((level) => ({
        level,
        override: preferences.overrides[usageContextLevelKey(record.source, level)],
      }))
    : [];
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
    Object.keys(preferences.lastProviderUsageLimits).length === 0
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
