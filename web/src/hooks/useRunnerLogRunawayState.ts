import { useEffect, useState } from "react";

import { useDismissedRunnerLogWarnings } from "@/hooks/useDismissedRunnerLogWarnings";
import {
  RUNNER_LOG_RUNAWAY_LABEL_KEY,
  RUNNER_LOG_RUNAWAY_SEEN_LABEL_KEY,
  runnerLogRunawayLeaseEnd,
  runnerLogRunawayNotice,
} from "@/lib/runnerLogRunaway";

export interface RunnerLogRunawayState {
  /** Detection instant, raw label value; also the dismissal key. */
  flag: string;
  notice: string;
  /** Server receive instant of the last confirmation, raw label value. */
  seen: string;
  dismissed: boolean;
}

/**
 * Live state of a session's runner-log runaway warning, or null while it must
 * not show: no flag, no parseable server receive instant (flags written before
 * the lease existed carry none), or the host stopped re-confirming — it
 * re-confirms every 5 minutes while the runner stays over the cap, so the
 * warning lapses 15 minutes after the last confirmation.
 */
export function useRunnerLogRunawayState(
  labels: Record<string, string> | undefined,
  fallbackLabels?: Record<string, string> | undefined,
): RunnerLogRunawayState | null {
  const effectiveLabels = labels ?? fallbackLabels;
  const notice = runnerLogRunawayNotice(effectiveLabels);
  const flag = effectiveLabels?.[RUNNER_LOG_RUNAWAY_LABEL_KEY];
  const seen = effectiveLabels?.[RUNNER_LOG_RUNAWAY_SEEN_LABEL_KEY];
  const leaseEnd = runnerLogRunawayLeaseEnd(effectiveLabels);
  const dismissedMap = useDismissedRunnerLogWarnings();
  const [now, setNow] = useState(() => Date.now());

  // One timer just past the lease end flips `now`, so the warning disappears on
  // its own when the host stops re-confirming the runaway report.
  useEffect(() => {
    if (leaseEnd === null) return;
    const timer = window.setTimeout(
      () => setNow(Date.now()),
      Math.max(0, leaseEnd - Date.now()) + 1,
    );
    return () => window.clearTimeout(timer);
  }, [leaseEnd]);

  if (notice === null || leaseEnd === null || !flag || !seen || leaseEnd <= now) return null;
  return { flag, notice, seen, dismissed: dismissedMap[flag] !== undefined };
}
