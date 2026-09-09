import { useEffect, useState } from "react";

import {
  readUsageContextPreferences,
  USAGE_CONTEXT_CHANGED_EVENT,
  type UsageContextPreferences,
} from "@/lib/usageContextPreferences";

export function useUsageContextPreferences(): UsageContextPreferences {
  const [preferences, setPreferences] = useState(readUsageContextPreferences);

  useEffect(() => {
    const refresh = () => setPreferences(readUsageContextPreferences());
    window.addEventListener(USAGE_CONTEXT_CHANGED_EVENT, refresh);
    window.addEventListener("storage", refresh);
    return () => {
      window.removeEventListener(USAGE_CONTEXT_CHANGED_EVENT, refresh);
      window.removeEventListener("storage", refresh);
    };
  }, []);

  return preferences;
}
