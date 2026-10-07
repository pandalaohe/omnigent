import { useEffect, useState } from "react";

import {
  readDismissedRunnerLogWarnings,
  RUNNER_LOG_WARNINGS_CHANGED_EVENT,
  type RunnerLogWarningDismissals,
} from "@/lib/runnerLogWarningPreferences";

export function useDismissedRunnerLogWarnings(): RunnerLogWarningDismissals {
  const [dismissed, setDismissed] = useState(readDismissedRunnerLogWarnings);

  useEffect(() => {
    const refresh = () => setDismissed(readDismissedRunnerLogWarnings());
    window.addEventListener(RUNNER_LOG_WARNINGS_CHANGED_EVENT, refresh);
    window.addEventListener("storage", refresh);
    return () => {
      window.removeEventListener(RUNNER_LOG_WARNINGS_CHANGED_EVENT, refresh);
      window.removeEventListener("storage", refresh);
    };
  }, []);

  return dismissed;
}
