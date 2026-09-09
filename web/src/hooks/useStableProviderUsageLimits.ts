import { useEffect } from "react";

import {
  providerUsageLimitsMatchesSource,
  type ProviderUsageLimitsSnapshot,
} from "@/lib/providerUsageLimits";
import {
  providerUsageLimitsForSource,
  writeLastProviderUsageLimits,
  type UsageContextPreferences,
} from "@/lib/usageContextPreferences";

/**
 * Prefer a fresh provider reading, otherwise retain the last user-synced
 * reading for this exact Host/agent/harness/model source.
 */
export function useStableProviderUsageLimits({
  preferences,
  sourceKey,
  fresh,
  agentName,
  harness,
}: {
  preferences: UsageContextPreferences;
  sourceKey: string;
  fresh: ProviderUsageLimitsSnapshot | null | undefined;
  agentName: string | null | undefined;
  harness: string | null | undefined;
}): ProviderUsageLimitsSnapshot | null {
  const source = { agentName, harness };
  const matchingFresh = providerUsageLimitsMatchesSource(fresh, source) ? fresh : null;
  const cached = providerUsageLimitsForSource(preferences, sourceKey);
  const matchingCached = providerUsageLimitsMatchesSource(cached, source) ? cached : null;

  useEffect(() => {
    if (matchingFresh) writeLastProviderUsageLimits(preferences, sourceKey, matchingFresh);
  }, [matchingFresh, preferences, sourceKey]);

  return matchingFresh ?? matchingCached;
}
