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
  autoCompactThresholdPercent: number | null;
}

export interface UsageContextPreferences {
  version: 4;
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
  version: 4,
  showProviderUsageLimits: true,
  overrides: {},
  lastProviderUsageLimits: {},
};

function positiveInteger(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && value > 0
    ? Math.round(value)
    : null;
}

function compactPercent(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && value >= 1 && value <= 100
    ? Math.round(value * 10) / 10
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
      const source = candidate as Partial<UsageContextOverride>;
      const normalized = {
        contextWindowTokens: positiveInteger(source.contextWindowTokens),
        autoCompactThresholdPercent: compactPercent(source.autoCompactThresholdPercent),
      };
      if (
        normalized.contextWindowTokens !== null ||
        normalized.autoCompactThresholdPercent !== null
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
    version: 4,
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
      autoCompactThresholdPercent: null,
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
    autoCompactThresholdPercent: compactPercent(override.autoCompactThresholdPercent),
  };
  const overrides = { ...preferences.overrides };
  if (
    normalizedOverride.contextWindowTokens === null &&
    normalizedOverride.autoCompactThresholdPercent === null
  ) {
    Reflect.deleteProperty(overrides, sourceKey);
  } else {
    overrides[sourceKey] = normalizedOverride;
  }
  writeUsageContextPreferences({ ...preferences, version: 4, overrides });
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
    version: 4,
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
  const manualCompactLimit =
    contextWindow != null && contextWindow > 0 && override.autoCompactThresholdPercent != null
      ? Math.round((contextWindow * override.autoCompactThresholdPercent) / 100)
      : null;
  return {
    contextWindow,
    autoCompactTokenLimit: manualCompactLimit ?? reportedAutoCompactTokenLimit,
  };
}
