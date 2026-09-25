import { useEffect, useState } from "react";

import {
  APPROVAL_TIMEOUT_CHANGED_EVENT,
  readApprovalTimeoutPreferences,
  type ApprovalTimeoutPreferences,
} from "@/lib/approvalTimeoutPreferences";

export function useApprovalTimeoutPreferences(): ApprovalTimeoutPreferences {
  const [preferences, setPreferences] = useState(readApprovalTimeoutPreferences);

  useEffect(() => {
    const refresh = () => setPreferences(readApprovalTimeoutPreferences());
    window.addEventListener(APPROVAL_TIMEOUT_CHANGED_EVENT, refresh);
    window.addEventListener("storage", refresh);
    return () => {
      window.removeEventListener(APPROVAL_TIMEOUT_CHANGED_EVENT, refresh);
      window.removeEventListener("storage", refresh);
    };
  }, []);

  return preferences;
}
