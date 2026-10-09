import { useEffect, useMemo, useRef, useState } from "react";

import { useLoadedConversations } from "@/hooks/useSidebarData";
import { useSessionErrorStates } from "@/hooks/useSessionErrors";
import { useSessionNavigationPreferences } from "@/hooks/useSessionNavigationPreferences";
import { useSoundAlertPreferences } from "@/hooks/useSoundAlertPreferences";
import { getConversationForegroundStatus } from "@/hooks/useSessionState";
import {
  isConversationUnseen,
  isExplicitlyUnread,
  useUnseenTick,
} from "@/hooks/useUnseenConversations";
import { authenticatedFetch } from "@/lib/identity";
import {
  getLegacyNativeNotificationSound,
  isNativeShell,
  setNativeSoundAlertsActive,
} from "@/lib/nativeBridge";
import { sessionUpdatesSocket } from "@/lib/sessionUpdatesSocket";
import {
  SOUND_LEVELS,
  isSoundLevelEnabled,
  readSoundAlertDevicePreferences,
  writeSoundAlertDevicePreferences,
  type SoundAlertDevicePreferences,
  type SoundLevel,
} from "@/lib/soundAlertPreferences";
import { canRingOnThisDevice, getSoundDeviceId, soundDeviceLabel } from "@/lib/soundDevice";
import { createSoundRinger, type RingerContext } from "@/lib/soundRinger";
import { initAudio, isAudioLocked, playLevel, subscribeAudioLock } from "@/lib/soundPlayer";
import {
  alertId,
  buildRowSoundStates,
  detectEdges,
  isDoneCandidate,
  type RowSoundState,
  type SoundAlert,
} from "@/lib/soundAlertTransitions";

// Agents that work in steps emit a dot per step; only a dot still showing
// after the settle window is a real turn end.
const DONE_SETTLE_MS = 10_000;
// A reconnect re-baselines even if no data change follows.
const REBASELINE_FALLBACK_MS = 1_500;
// At most one activity frame per window; the server's 5-minute recency check
// is far coarser, so this only bounds frame traffic.
const ACTIVITY_THROTTLE_MS = 10_000;

/** True when the app window currently has focus (SSR-safe default true). */
function isWindowFocused(): boolean {
  if (typeof document === "undefined") return true;
  return typeof document.hasFocus === "function" ? document.hasFocus() : true;
}

/**
 * Fold the shell's legacy notification-sound setting into device-local alert
 * preferences once, then mark it migrated. No legacy values (or an older shell
 * without the bridge) still records the marker so this never runs again.
 */
async function migrateLegacySoundSetting(): Promise<void> {
  if (readSoundAlertDevicePreferences().legacySoundMigrated) return;
  const legacy = await getLegacyNativeNotificationSound();
  // Re-read: a concurrent mount may have completed the migration while the
  // bridge call was in flight.
  const device = readSoundAlertDevicePreferences();
  if (device.legacySoundMigrated) return;
  const next: SoundAlertDevicePreferences = { ...device, legacySoundMigrated: true };
  if (legacy && typeof legacy.enabled === "boolean") {
    next.enabled = legacy.enabled;
    if (legacy.enabled && legacy.name) {
      const systemSounds: Partial<Record<SoundLevel, string>> = {};
      for (const level of SOUND_LEVELS) systemSounds[level] = legacy.name;
      next.systemSounds = systemSounds;
    }
  }
  writeSoundAlertDevicePreferences(next);
}

/**
 * Play a local sound when a top-level session changes its row mark. Mount
 * once, app-wide; the first loaded snapshot is a baseline. After a socket
 * reconnect the snapshot is re-baselined and still-awaiting rows ring once,
 * so a prompt missed during the outage isn't silent.
 */
export function useSoundAlerts(activeConversationId?: string): void {
  const { data } = useLoadedConversations();
  const { account, device } = useSoundAlertPreferences();
  const { showGoalSessionMarkers } = useSessionNavigationPreferences();
  // Re-run when the user marks a row read/unread, so the dot edge is seen
  // without waiting for the next list refresh.
  const unseenTick = useUnseenTick();

  // Refs keep the recompute effect keyed on the data snapshot alone, so a
  // settings or focus change never re-seeds the previous-snapshot map.
  const accountRef = useRef(account);
  accountRef.current = account;
  const deviceRef = useRef(device);
  deviceRef.current = device;
  const activeIdRef = useRef(activeConversationId);
  activeIdRef.current = activeConversationId;
  const windowFocusedRef = useRef(isWindowFocused());

  const topLevelRows = useMemo(
    () => (data?.pages.flatMap((page) => page.data) ?? []).filter((row) => !row.parent_session_id),
    [data],
  );
  const latestErrors = useSessionErrorStates(topLevelRows);
  const latestErrorsRef = useRef(latestErrors);
  latestErrorsRef.current = latestErrors;
  const errorsKey = latestErrors.join("|");

  const previousRows = useRef<Map<string, RowSoundState> | null>(null);
  const latestRows = useRef<Map<string, RowSoundState>>(new Map());
  const settleTimers = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map());
  const rebaselinePending = useRef(false);
  const rebaselineTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [recomputeTick, setRecomputeTick] = useState(0);
  // Re-announce to the server when the browser's audio unlocks, so a tab that
  // couldn't ring yet starts being chosen once the user interacts.
  const [audioLocked, setAudioLocked] = useState(isAudioLocked);

  const ringerRef = useRef<ReturnType<typeof createSoundRinger> | null>(null);
  if (ringerRef.current === null) {
    ringerRef.current = createSoundRinger({
      play: (level) => {
        void playLevel(level, accountRef.current, deviceRef.current);
      },
      getContext: (): RingerContext => ({
        account: accountRef.current,
        device: deviceRef.current,
        windowFocused: windowFocusedRef.current,
        activeConversationId: activeIdRef.current,
        now: new Date(),
      }),
      nowMs: () => Date.now(),
      schedule: (fn, ms) => {
        setTimeout(fn, ms);
      },
    });
  }

  // Announce this device to the server so a claimed alert can be routed to a
  // device that can actually play it. A locked browser tab can't ring yet, so
  // it advertises false and re-announces when the audio unlocks. Re-sent when
  // the device switch changes; the transport re-sends it on every reconnect.
  useEffect(() => {
    sessionUpdatesSocket.setHello({
      device_id: getSoundDeviceId(),
      device_label: soundDeviceLabel(),
      can_ring: canRingOnThisDevice(device) && !audioLocked,
    });
  }, [device, audioLocked]);

  useEffect(() => subscribeAudioLock(() => setAudioLocked(isAudioLocked())), []);

  // In a native shell the web app owns alert sounds, so tell the shell to mute
  // its legacy notification sound. The legacy setting is folded into device
  // preferences once on the same mount.
  useEffect(() => {
    const native = isNativeShell();
    initAudio({ native });
    if (!native) return;
    setNativeSoundAlertsActive(true);
    void migrateLegacySoundSetting();
    return () => setNativeSoundAlertsActive(false);
  }, []);

  // A delivered alert was claimed and routed to this one connection; the
  // ringer still applies this device's local filters before playing.
  useEffect(() => {
    return sessionUpdatesSocket.subscribe((frame) => {
      if (frame.type !== "sound_alert") return;
      ringerRef.current?.ring({
        sessionId: frame.session_id,
        level: frame.level,
        alertId: frame.alert_id,
      });
    });
  }, []);

  // Focus is tracked from the authoritative DOM events (and any pointer/key
  // interaction, which implies our window has focus) rather than a polled
  // `document.hasFocus()`, which can lie in the desktop shell. Focus and
  // interaction also tell the server this device is in use, throttled so a
  // burst of keystrokes is one frame.
  useEffect(() => {
    let lastActivitySentAt = 0;
    const sendActivity = () => {
      const now = Date.now();
      if (now - lastActivitySentAt < ACTIVITY_THROTTLE_MS) return;
      lastActivitySentAt = now;
      sessionUpdatesSocket.sendActivity();
    };
    const onFocus = () => {
      windowFocusedRef.current = true;
      sendActivity();
    };
    const onBlur = () => {
      windowFocusedRef.current = false;
    };
    const onInteract = () => {
      windowFocusedRef.current = true;
      sendActivity();
    };
    window.addEventListener("focus", onFocus);
    window.addEventListener("blur", onBlur);
    window.addEventListener("pointerdown", onInteract);
    window.addEventListener("keydown", onInteract);
    return () => {
      window.removeEventListener("focus", onFocus);
      window.removeEventListener("blur", onBlur);
      window.removeEventListener("pointerdown", onInteract);
      window.removeEventListener("keydown", onInteract);
    };
  }, []);

  // A (re)connect may have dropped edges while the stream was down; the next
  // recompute re-baselines and re-announces still-pending rows.
  useEffect(() => {
    const onStatusChange = () => {
      if (!sessionUpdatesSocket.isConnected()) return;
      rebaselinePending.current = true;
      if (rebaselineTimer.current !== null) return;
      rebaselineTimer.current = setTimeout(() => {
        rebaselineTimer.current = null;
        setRecomputeTick((tick) => tick + 1);
      }, REBASELINE_FALLBACK_MS);
    };
    const unsubscribe = sessionUpdatesSocket.subscribeStatus(onStatusChange);
    if (sessionUpdatesSocket.isConnected()) onStatusChange();
    return () => {
      unsubscribe();
      if (rebaselineTimer.current !== null) {
        clearTimeout(rebaselineTimer.current);
        rebaselineTimer.current = null;
      }
    };
  }, []);

  // Clear pending cues on unmount so none fires into a torn-down tree.
  useEffect(() => {
    const timers = settleTimers.current;
    return () => {
      for (const timer of timers.values()) clearTimeout(timer);
      timers.clear();
    };
  }, []);

  useEffect(() => {
    if (data === undefined) return;
    const errorById = new Map(
      topLevelRows.map((row, index) => [row.id, latestErrorsRef.current[index] ?? null]),
    );
    const next = buildRowSoundStates(topLevelRows, (conversation) => ({
      // Unlike the sidebar row, the open conversation is NOT suppressed here:
      // the ringer's viewing rule handles that, so a session finishing in a
      // minimized window still sounds.
      unseen:
        isConversationUnseen(
          conversation.id,
          conversation.updated_at,
          getConversationForegroundStatus(conversation),
        ) && !isExplicitlyUnread(conversation.id),
      latestError: errorById.get(conversation.id) ?? null,
      showGoalMarkers: showGoalSessionMarkers,
      starting: false,
    }));
    latestRows.current = next;

    // A row that left the list can't settle (deleted, archived, filtered):
    // drop its pending cue.
    for (const [sessionId, timer] of settleTimers.current) {
      if (!next.has(sessionId)) {
        clearTimeout(timer);
        settleTimers.current.delete(sessionId);
      }
    }

    const deliver = (alert: SoundAlert) => {
      const preferences = accountRef.current;
      // Skip locally so a disabled level or muted session never costs a
      // request; the server dedupes the rest across devices.
      if (!isSoundLevelEnabled(preferences, alert.level)) return;
      if (preferences.mutedSessionIds.includes(alert.sessionId)) return;
      void (async () => {
        try {
          const response = await authenticatedFetch("/v1/me/sound-alerts/claim", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              alert_id: alert.alertId,
              session_id: alert.sessionId,
              level: alert.level,
            }),
          });
          // A server without the claim route: ring locally so local
          // detection still sounds.
          if (response.status === 404 || response.status === 405) {
            ringerRef.current?.ring(alert);
          }
        } catch {
          // Alert delivery is best-effort; a network failure drops the cue.
        }
      })();
    };

    if (rebaselinePending.current) {
      rebaselinePending.current = false;
      if (rebaselineTimer.current !== null) {
        clearTimeout(rebaselineTimer.current);
        rebaselineTimer.current = null;
      }
      for (const timer of settleTimers.current.values()) clearTimeout(timer);
      settleTimers.current.clear();
      previousRows.current = next;
      for (const [sessionId, state] of next) {
        if (state.mark.awaitingCount > 0) {
          deliver({
            sessionId,
            level: "needs_response",
            alertId: alertId(sessionId, "needs_response", state),
          });
        }
      }
      return;
    }

    const edges = detectEdges(previousRows.current, next);
    previousRows.current = next;
    for (const alert of edges.immediate) deliver(alert);
    for (const sessionId of edges.settleStart) {
      const existing = settleTimers.current.get(sessionId);
      if (existing !== undefined) clearTimeout(existing);
      settleTimers.current.set(
        sessionId,
        setTimeout(() => {
          settleTimers.current.delete(sessionId);
          // Re-check at fire time: the row may have resumed or been read.
          const state = latestRows.current.get(sessionId);
          if (state === undefined || !isDoneCandidate(state)) return;
          deliver({
            sessionId,
            level: "done",
            alertId: alertId(sessionId, "done", state),
          });
        }, DONE_SETTLE_MS),
      );
    }
    for (const sessionId of edges.settleCancel) {
      const timer = settleTimers.current.get(sessionId);
      if (timer !== undefined) {
        clearTimeout(timer);
        settleTimers.current.delete(sessionId);
      }
    }
  }, [data, topLevelRows, unseenTick, showGoalSessionMarkers, errorsKey, recomputeTick]);
}
