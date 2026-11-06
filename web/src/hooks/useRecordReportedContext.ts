import { useEffect, useRef } from "react";

import { useChatStore } from "@/store/chatStore";
import {
  usageContextSourceFromKey,
  usageContextSourceKey,
  writeReportedContext,
  type UsageContextPreferences,
} from "@/lib/usageContextPreferences";

interface Observation {
  conversationId: string | null;
  sourceKey: string;
  reading: NonNullable<ReturnType<typeof useChatStore.getState>["reportedContextReading"]>;
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
  const reading = useChatStore((state) => state.reportedContextReading);
  const latestPreferences = useRef(preferences);
  const lastObservation = useRef<Observation | null>(null);
  useEffect(() => {
    latestPreferences.current = preferences;
  }, [preferences]);
  useEffect(() => {
    const source = usageContextSourceFromKey(sourceKey);
    if (!source?.hostId || !source.agentName || !source.harness || !source.model || !reading?.model)
      return;
    const readingKey = usageContextSourceKey({ ...source, model: reading.model });
    const last = lastObservation.current;
    lastObservation.current = { conversationId, sourceKey: readingKey, reading };
    if (
      last?.conversationId === conversationId &&
      last.sourceKey === readingKey &&
      last.reading === reading
    )
      return;
    writeReportedContext(latestPreferences.current, readingKey, {
      contextWindow: reading.contextWindow,
      autoCompactTokenLimit: reading.autoCompactTokenLimit,
    });
  }, [conversationId, sourceKey, reading]);
}
