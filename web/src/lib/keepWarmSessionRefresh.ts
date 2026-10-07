// Session-list refresh for the `keep_warm` namespace.
//
// A saved cold-after changes which sessions read warm, but child-session
// queries use `refetchOnMount: false`, so marking cached lists stale is not
// enough — `refetchType: "all"` forces even an inactive cached list.

import type { QueryClient } from "@tanstack/react-query";

import { USER_PREFERENCES_PATCH_ACKNOWLEDGED_EVENT } from "./userPreferencesSync";

/**
 * Refresh every session list after the Server acknowledges a keep-warm
 * patch. Called once at app scope, since the settings section may be
 * unmounted when the acknowledgement lands. Returns a remover for the
 * window listener, for tests.
 */
export function installKeepWarmSessionRefresh(queryClient: QueryClient): () => void {
  const onPatchAcknowledged = (event: Event) => {
    const detail = (event as CustomEvent<{ namespace?: string }>).detail;
    if (detail?.namespace !== "keep_warm") return;
    void queryClient.invalidateQueries({ queryKey: ["conversations"], refetchType: "all" });
    void queryClient.invalidateQueries({ queryKey: ["project-sessions"], refetchType: "all" });
    void queryClient.invalidateQueries({
      predicate: (query) =>
        query.queryKey[0] === "conversation" && query.queryKey[2] === "child_sessions",
      refetchType: "all",
    });
  };
  window.addEventListener(USER_PREFERENCES_PATCH_ACKNOWLEDGED_EVENT, onPatchAcknowledged);
  return () =>
    window.removeEventListener(USER_PREFERENCES_PATCH_ACKNOWLEDGED_EVENT, onPatchAcknowledged);
}
