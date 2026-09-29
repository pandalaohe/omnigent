import { useEffect, useState } from "react";

import {
  readSessionCollabPreferences,
  SESSION_COLLAB_CHANGED_EVENT,
  type SessionCollabPreferences,
} from "@/lib/sessionCollabPreferences";

export function useSessionCollabPreferences(): SessionCollabPreferences {
  const [preferences, setPreferences] = useState(readSessionCollabPreferences);

  useEffect(() => {
    const refresh = () => setPreferences(readSessionCollabPreferences());
    window.addEventListener(SESSION_COLLAB_CHANGED_EVENT, refresh);
    window.addEventListener("storage", refresh);
    return () => {
      window.removeEventListener(SESSION_COLLAB_CHANGED_EVENT, refresh);
      window.removeEventListener("storage", refresh);
    };
  }, []);

  return preferences;
}
