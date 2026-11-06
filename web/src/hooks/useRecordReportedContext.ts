import { useEffect, useRef } from "react";

import { useChatStore } from "@/store/chatStore";
import {
  usageContextSourceFromKey,
  writeReportedContext,
  type UsageContextPreferences,
} from "@/lib/usageContextPreferences";

interface Observation {
  conversationId: string | null;
  sourceKey: string;
  contextWindow: number | null;
  autoCompactTokenLimit: number | null;
}

/**
 * Keep what this session's agent reports, for its exact Host/agent/harness/model
 * source: Settings shows Auto from this record long after the session has gone.
 * A source without a host waits, since the session snapshot supplies the host.
 * Only a new reading is written: a preference refresh, such as another tab
 * recording its own session on the same source, is not one.
 */
export function useRecordReportedContext(
  preferences: UsageContextPreferences,
  sourceKey: string,
): void {
  const conversationId = useChatStore((state) => state.conversationId);
  const contextWindow = useChatStore((state) => state.contextWindow);
  const autoCompactTokenLimit = useChatStore((state) => state.autoCompactTokenLimit);
  const source = usageContextSourceFromKey(sourceKey);
  const complete = !!source?.hostId && !!source.agentName && !!source.harness && !!source.model;
  const latestPreferences = useRef(preferences);
  const lastObservation = useRef<Observation | null>(null);
  useEffect(() => {
    latestPreferences.current = preferences;
  }, [preferences]);
  useEffect(() => {
    if (!complete) return;
    const last = lastObservation.current;
    lastObservation.current = { conversationId, sourceKey, contextWindow, autoCompactTokenLimit };
    // A model switch inside one session arrives apart from the new model's reading:
    // until the figures change they are still the previous model's.
    if (
      last?.conversationId === conversationId &&
      last.contextWindow === contextWindow &&
      last.autoCompactTokenLimit === autoCompactTokenLimit
    )
      return;
    writeReportedContext(latestPreferences.current, sourceKey, {
      contextWindow,
      autoCompactTokenLimit,
    });
  }, [conversationId, sourceKey, complete, contextWindow, autoCompactTokenLimit]);
}
