import { useEffect, useState } from "react";

import { HOST_COLORS_CHANGED_EVENT, readHostColorPreferences } from "@/lib/hostColorPreferences";
import type { HostColorPreferences } from "@/lib/hostColors";

export function useHostColorPreferences(): HostColorPreferences {
  const [preferences, setPreferences] = useState(readHostColorPreferences);

  useEffect(() => {
    const refresh = () => setPreferences(readHostColorPreferences());
    window.addEventListener(HOST_COLORS_CHANGED_EVENT, refresh);
    window.addEventListener("storage", refresh);
    return () => {
      window.removeEventListener(HOST_COLORS_CHANGED_EVENT, refresh);
      window.removeEventListener("storage", refresh);
    };
  }, []);

  return preferences;
}
