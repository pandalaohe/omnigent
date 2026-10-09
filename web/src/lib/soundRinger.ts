// Decides whether THIS device plays an alert.
//
// Kept apart from edge detection so a later slice can deliver alerts from the
// server instead of from local detection. The ringer also collapses bursts:
// cues are rate-limited per class, and the two classes are spaced apart so
// overlapping "needs response" and done/error cues stay distinguishable.

import {
  isSoundLevelEnabled,
  type SoundAlertDevicePreferences,
  type SoundAlertPreferences,
  type SoundLevel,
} from "@/lib/soundAlertPreferences";
import { isQuietNow, type SoundAlert } from "@/lib/soundAlertTransitions";

export interface RingerContext {
  account: SoundAlertPreferences;
  device: SoundAlertDevicePreferences;
  windowFocused: boolean;
  activeConversationId: string | undefined;
  now: Date;
}

export interface SoundRingerDeps {
  play: (level: SoundLevel) => void;
  getContext: () => RingerContext;
  nowMs: () => number;
  /** Schedule `fn` after `ms`; returns a cancel function for `dispose`. */
  schedule: (fn: () => void, ms: number) => () => void;
}

export interface SoundRinger {
  ring: (alert: SoundAlert) => void;
  /** Cancel every scheduled cue (call on unmount). */
  dispose: () => void;
}

// One cue per class per window; a later alert of the same class is part of the
// same burst.
const BURST_WINDOW_MS = 2_000;
// Minimum start-to-start distance between a "needs response" cue and a
// done/error cue, applied to whichever one starts later.
const CLASS_GAP_MS = 400;
// Bounds the de-duplication memory for long-lived tabs.
const MAX_RUNG_IDS = 500;

type AlertClass = "needs_response" | "other";

function alertClass(level: SoundLevel): AlertClass {
  return level === "needs_response" ? "needs_response" : "other";
}

/**
 * Whether the current local context still allows this alert to play.
 *
 * Evaluated both when an alert arrives and again when a scheduled cue
 * fires, since the device switch, quiet hours, session mute, level
 * enablement, or the viewed session may all have changed in between.
 */
function passesFilters(alert: SoundAlert, context: RingerContext): boolean {
  if (!context.device.enabled) return false;
  if (isQuietNow(context.account.quietHours, context.now)) return false;
  if (context.account.mutedSessionIds.includes(alert.sessionId)) return false;
  if (!isSoundLevelEnabled(context.account, alert.level)) return false;
  // The user is looking at this session; only a pending prompt still needs
  // their attention.
  if (
    alert.level !== "needs_response" &&
    context.windowFocused &&
    context.activeConversationId === alert.sessionId
  ) {
    return false;
  }
  return true;
}

export function createSoundRinger(deps: SoundRingerDeps): SoundRinger {
  const rungIds: string[] = [];
  const rungSet = new Set<string>();
  const scheduled = new Set<() => void>();
  // Last cue start per class. A scheduled cue records its planned start, so a
  // later alert can neither stack on it nor start too close to it.
  const lastCueAt: Record<AlertClass, number | undefined> = {
    needs_response: undefined,
    other: undefined,
  };
  let lastOtherLevel: SoundLevel | undefined;

  function remember(alertId: string): void {
    rungIds.push(alertId);
    rungSet.add(alertId);
    if (rungIds.length > MAX_RUNG_IDS) {
      const oldest = rungIds.shift();
      if (oldest !== undefined) rungSet.delete(oldest);
    }
  }

  function ring(alert: SoundAlert): void {
    if (rungSet.has(alert.alertId)) return;
    if (!passesFilters(alert, deps.getContext())) return;

    const now = deps.nowMs();
    const cls = alertClass(alert.level);
    const sameLast = lastCueAt[cls];
    if (sameLast !== undefined && now - sameLast < BURST_WINDOW_MS) {
      // A failure is worth surfacing even when it lands during a completion
      // burst; completions after a failure stay silent.
      const errorAfterDone =
        cls === "other" && alert.level === "error" && lastOtherLevel === "done";
      if (!errorAfterDone) return;
    }

    const otherLast = lastCueAt[cls === "needs_response" ? "other" : "needs_response"];
    const start =
      otherLast !== undefined && now < otherLast + CLASS_GAP_MS ? otherLast + CLASS_GAP_MS : now;

    remember(alert.alertId);
    lastCueAt[cls] = start;
    if (cls === "other") lastOtherLevel = alert.level;
    if (start > now) {
      const level = alert.level;
      let cancel = () => {};
      cancel = deps.schedule(() => {
        scheduled.delete(cancel);
        // The context may have changed while the cue waited: re-check the
        // device switch, quiet hours, session mute, level enablement, and
        // the viewing rule, and drop the cue if any now fails.
        if (!passesFilters(alert, deps.getContext())) return;
        deps.play(level);
      }, start - now);
      scheduled.add(cancel);
    } else {
      deps.play(alert.level);
    }
  }

  function dispose(): void {
    for (const cancel of scheduled) cancel();
    scheduled.clear();
  }

  return { ring, dispose };
}
