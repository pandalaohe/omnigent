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
  schedule: (fn: () => void, ms: number) => void;
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

export function createSoundRinger(deps: SoundRingerDeps): { ring: (alert: SoundAlert) => void } {
  const rungIds: string[] = [];
  const rungSet = new Set<string>();
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
    const context = deps.getContext();
    if (!context.device.enabled) return;
    if (isQuietNow(context.account.quietHours, context.now)) return;
    if (context.account.mutedSessionIds.includes(alert.sessionId)) return;
    if (!isSoundLevelEnabled(context.account, alert.level)) return;
    // The user is looking at this session; only a pending prompt still needs
    // their attention.
    if (
      alert.level !== "needs_response" &&
      context.windowFocused &&
      context.activeConversationId === alert.sessionId
    ) {
      return;
    }

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
      deps.schedule(() => deps.play(level), start - now);
    } else {
      deps.play(alert.level);
    }
  }

  return { ring };
}
