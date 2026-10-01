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
}

/** Only present fields are applied; `null` clears the field to Auto. */
export interface UsageContextOverridePatch {
  contextWindowTokens?: number | null;
  autoCompactBufferTokens?: number | null;
}

export interface UsageContextPreferences {
  version: 5;
  /** Show provider-reported usage windows beside the context ring. */
  showProviderUsageLimits: boolean;
  /** Display overrides scoped to the exact Host, agent, harness, and model. */
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
  version: 5,
  showProviderUsageLimits: true,
  overrides: {},
  lastProviderUsageLimits: {},
};

function positiveInteger(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && value > 0
    ? Math.round(value)
    : null;
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
      const source = candidate as Partial<UsageContextOverride> & {
        autoCompactThresholdPercent?: unknown;
      };
      const contextWindowTokens = positiveInteger(source.contextWindowTokens);
      const storedBufferTokens = positiveInteger(source.autoCompactBufferTokens);
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
      const normalized = { contextWindowTokens, autoCompactBufferTokens };
      if (normalized.contextWindowTokens !== null || normalized.autoCompactBufferTokens !== null) {
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
    version: 5,
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

export function usageContextOverrideFor(
  preferences: UsageContextPreferences,
  sourceKey: string,
): UsageContextOverride {
  return (
    preferences.overrides[sourceKey] ?? {
      contextWindowTokens: null,
      autoCompactBufferTokens: null,
    }
  );
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
  const normalizedOverride = {
    contextWindowTokens: positiveInteger(override.contextWindowTokens),
    autoCompactBufferTokens: positiveInteger(override.autoCompactBufferTokens),
  };
  const overrides = { ...preferences.overrides };
  if (
    normalizedOverride.contextWindowTokens === null &&
    normalizedOverride.autoCompactBufferTokens === null
  ) {
    Reflect.deleteProperty(overrides, sourceKey);
  } else {
    overrides[sourceKey] = normalizedOverride;
  }
  writeUsageContextPreferences({ ...preferences, version: 5, overrides });
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
    const next: UsageContextOverride = {
      contextWindowTokens:
        patch.contextWindowTokens !== undefined
          ? positiveInteger(patch.contextWindowTokens)
          : positiveInteger(existing.contextWindowTokens),
      autoCompactBufferTokens:
        patch.autoCompactBufferTokens !== undefined
          ? positiveInteger(patch.autoCompactBufferTokens)
          : positiveInteger(existing.autoCompactBufferTokens),
    };
    if (next.contextWindowTokens === null && next.autoCompactBufferTokens === null) {
      Reflect.deleteProperty(overrides, sourceKey);
    } else {
      overrides[sourceKey] = next;
    }
  }
  writeUsageContextPreferences({ ...preferences, version: 5, overrides });
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
  writeUsageContextPreferences({ ...preferences, version: 5, overrides });
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
    version: 5,
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

export function resolveUsageContextLimits(
  preferences: UsageContextPreferences,
  sourceKey: string,
  reportedContextWindow: number | null,
  reportedAutoCompactTokenLimit: number | null,
): { contextWindow: number | null; autoCompactTokenLimit: number | null } {
  const override = usageContextOverrideFor(preferences, sourceKey);
  const contextWindow = override.contextWindowTokens ?? reportedContextWindow;
  const buffer = override.autoCompactBufferTokens;
  const manualCompactLimit =
    contextWindow != null && buffer != null && contextWindow > buffer
      ? contextWindow - buffer
      : null;
  return {
    contextWindow,
    autoCompactTokenLimit: manualCompactLimit ?? reportedAutoCompactTokenLimit,
  };
}
