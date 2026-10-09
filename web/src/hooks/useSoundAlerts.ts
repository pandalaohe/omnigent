import { useEffect, useRef } from "react";

import { useLoadedConversations } from "@/hooks/useSidebarData";
import { useSoundAlertPreferences } from "@/hooks/useSoundAlertPreferences";
import { isNativeShell } from "@/lib/nativeBridge";
import { isSoundLevelEnabled } from "@/lib/soundAlertPreferences";
import { initAudio, playLevel } from "@/lib/soundPlayer";
import { buildRowSoundStates, detectEdges, type RowSoundState } from "@/lib/soundAlertTransitions";

/**
 * Play a local sound when a top-level session newly needs the user's
 * response. Mount once, app-wide; the first loaded snapshot is a baseline.
 */
export function useSoundAlerts(): void {
  const { data } = useLoadedConversations();
  const { account, device } = useSoundAlertPreferences();
  // Refs keep the edge effect keyed on the data snapshot alone, so a settings
  // change never re-seeds the previous-snapshot map.
  const accountRef = useRef(account);
  accountRef.current = account;
  const deviceRef = useRef(device);
  deviceRef.current = device;
  const previousRows = useRef<Map<string, RowSoundState> | null>(null);

  useEffect(() => {
    initAudio({ native: isNativeShell() });
  }, []);

  useEffect(() => {
    if (data === undefined) return;
    const conversations = data.pages.flatMap((page) => page.data);
    const next = buildRowSoundStates(conversations);
    const alerts = detectEdges(previousRows.current, next);
    previousRows.current = next;
    for (const alert of alerts) {
      const currentAccount = accountRef.current;
      const currentDevice = deviceRef.current;
      if (!currentDevice.enabled) continue;
      if (!isSoundLevelEnabled(currentAccount, alert.level)) continue;
      void playLevel(alert.level, currentAccount, currentDevice);
    }
  }, [data]);
}
