// Adapted from omnigent-ae-public (Copyright 2026 Analytic Endeavors LLC, licensed under Apache-2.0).

import { useEffect } from "react";
import { toast } from "sonner";

import { startBuildCheck } from "@/lib/buildCheck";
import { useIsEmbedded } from "@/lib/embedded";

const TOAST_ID = "reload-notice";

/**
 * Shows one persistent "Omnigent was updated." toast when the server serves a
 * newer build than this tab runs. Renders nothing itself and never reloads on
 * its own. "Later" dismisses it for that build; a different, newer build shows
 * it again.
 */
export function ReloadNotice() {
  const embedded = useIsEmbedded();
  useEffect(
    () =>
      embedded
        ? undefined
        : startBuildCheck(() => {
            toast("Omnigent was updated.", {
              id: TOAST_ID,
              description: "Reload to get the new version.",
              duration: Number.POSITIVE_INFINITY,
              action: { label: "Reload", onClick: () => window.location.reload() },
              cancel: { label: "Later", onClick: () => toast.dismiss(TOAST_ID) },
            });
          }),
    [embedded],
  );
  return null;
}
