import { useEffect, useState } from "react";

import {
  CONTEXT_INDICATOR_CHANGED_EVENT,
  readContextIndicatorMode,
  type ContextIndicatorMode,
} from "@/lib/contextIndicatorPreferences";

export function useContextIndicatorMode(): ContextIndicatorMode {
  const [mode, setMode] = useState(readContextIndicatorMode);

  useEffect(() => {
    const refresh = () => setMode(readContextIndicatorMode());
    window.addEventListener(CONTEXT_INDICATOR_CHANGED_EVENT, refresh);
    window.addEventListener("storage", refresh);
    return () => {
      window.removeEventListener(CONTEXT_INDICATOR_CHANGED_EVENT, refresh);
      window.removeEventListener("storage", refresh);
    };
  }, []);

  return mode;
}
