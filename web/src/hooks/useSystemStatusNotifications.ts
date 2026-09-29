// Surfaces NEW resource-monitor findings as OS notifications, riding the
// summary query the sidebar already keeps fresh (the server's
// `system_status_changed` nudge) — no extra poll.
//
// Only a finding id that was not in the previous summary notifies, so the
// first summary after mount merely seeds the baseline and a finding already
// present never re-fires. A condition replaced by a different one at the same
// level (CPU clears, disk appears) is a new id and does notify. The click
// target is the system-status page; the notification gate matches
// `useIdleNotifications` (native shell, or a granted Web Notifications
// permission).

import { useEffect, useRef } from "react";
import { getNotificationPermission, showNotification } from "@/lib/browserNotifications";
import { isNativeShell } from "@/lib/nativeBridge";
import { useNavigate } from "@/lib/routing";
import { useSystemStatusSummary } from "@/hooks/useSystemStatus";

export function useSystemStatusNotifications(): void {
  const { data } = useSystemStatusSummary();
  const navigate = useNavigate();
  // `null` until the first summary arrives: that load seeds the baseline.
  const previousIds = useRef<Set<string> | null>(null);

  useEffect(() => {
    if (data === undefined) return;
    const currentIds = new Set(data.findings.map((finding) => finding.id));
    const previous = previousIds.current;
    previousIds.current = currentIds;
    if (previous === null) return;
    if (!(isNativeShell() || getNotificationPermission() === "granted")) return;
    for (const finding of data.findings) {
      if (previous.has(finding.id)) continue;
      if (finding.level !== "amber" && finding.level !== "red") continue;
      showNotification({
        title: "System status",
        body: finding.detail,
        tag: `omnigent:system:${finding.id}`,
        // Browser path: run navigation directly. Desktop shell path: forward
        // `navigatePath` over IPC (the closure can't cross the boundary).
        onClick: () => navigate("/system"),
        navigatePath: "/system",
      });
    }
  }, [data, navigate]);
}
