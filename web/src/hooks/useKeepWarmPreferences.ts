import { useEffect, useState } from "react";

import {
  KEEP_WARM_CHANGED_EVENT,
  readKeepWarmPreferences,
  type KeepWarmPreferences,
} from "@/lib/keepWarmPreferences";

export function useKeepWarmPreferences(): KeepWarmPreferences {
  const [preferences, setPreferences] = useState(readKeepWarmPreferences);

  useEffect(() => {
    const refresh = () => setPreferences(readKeepWarmPreferences());
    window.addEventListener(KEEP_WARM_CHANGED_EVENT, refresh);
    window.addEventListener("storage", refresh);
    return () => {
      window.removeEventListener(KEEP_WARM_CHANGED_EVENT, refresh);
      window.removeEventListener("storage", refresh);
    };
  }, []);

  return preferences;
}
