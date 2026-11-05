import { useEffect } from "react";

import { useChatStore } from "@/store/chatStore";
import {
  usageContextSourceFromKey,
  writeReportedContext,
  type UsageContextPreferences,
} from "@/lib/usageContextPreferences";

/**
 * Keep what this session's agent reports, for its exact Host/agent/harness/model
 * source: Settings shows Auto from this record long after the session has gone.
 * A source without a host waits, since the session snapshot supplies the host.
 */
export function useRecordReportedContext(
  preferences: UsageContextPreferences,
  sourceKey: string,
): void {
  const contextWindow = useChatStore((state) => state.contextWindow);
  const autoCompactTokenLimit = useChatStore((state) => state.autoCompactTokenLimit);
  const source = usageContextSourceFromKey(sourceKey);
  const complete = !!source?.hostId && !!source.agentName && !!source.harness && !!source.model;
  useEffect(() => {
    if (complete)
      writeReportedContext(preferences, sourceKey, { contextWindow, autoCompactTokenLimit });
  }, [preferences, sourceKey, complete, contextWindow, autoCompactTokenLimit]);
}
