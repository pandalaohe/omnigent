import { queueUserPreferencePatch } from "./userPreferencesSync";

export const USAGE_CONTEXT_STORAGE_KEY = "omnigent:usage-context-preferences";
export const USAGE_CONTEXT_CHANGED_EVENT = "omnigent:usage-context-preferences-changed";

export interface UsageContextOverride {
  contextWindowTokens: number | null;
  autoCompactThresholdPercent: number | null;
}

export interface UsageContextPreferences {
  version: 3;
  /** Show provider-reported usage windows beside the context ring. */
  showProviderUsageLimits: boolean;
  /** Display overrides scoped to the exact Host, agent, harness, and model. */
  overrides: Record<string, UsageContextOverride>;
}

export const DEFAULT_USAGE_CONTEXT_PREFERENCES: UsageContextPreferences = {
  version: 3,
  showProviderUsageLimits: true,
  overrides: {},
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
  return {
    version: 3,
    showProviderUsageLimits:
      raw.showProviderUsageLimits !== undefined
        ? raw.showProviderUsageLimits !== false
        : raw.showCodexRateLimits !== false,
    overrides,
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
  return preferences.showProviderUsageLimits && Object.keys(preferences.overrides).length === 0;
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
  writeUsageContextPreferences({ ...preferences, version: 3, overrides });
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
