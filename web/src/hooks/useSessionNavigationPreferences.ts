import { useEffect, useState } from "react";

import {
  readSessionNavigationPreferences,
  SESSION_NAVIGATION_CHANGED_EVENT,
  type SessionNavigationPreferences,
} from "@/lib/sessionNavigationPreferences";

export function useSessionNavigationPreferences(): SessionNavigationPreferences {
  const [preferences, setPreferences] = useState(readSessionNavigationPreferences);

  useEffect(() => {
    const refresh = () => setPreferences(readSessionNavigationPreferences());
    window.addEventListener(SESSION_NAVIGATION_CHANGED_EVENT, refresh);
    window.addEventListener("storage", refresh);
    return () => {
      window.removeEventListener(SESSION_NAVIGATION_CHANGED_EVENT, refresh);
      window.removeEventListener("storage", refresh);
    };
  }, []);

  return preferences;
}
