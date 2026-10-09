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

// done/error alerts arriving within this window collapse into one cue; a
// needs_response cue is rate-limited to one per window.
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
  // done/error alerts gathered in the open collection window; their ids are
  // already remembered. Null when no window is open.
  let collecting: SoundAlert[] | null = null;

  function remember(alertId: string): void {
    rungIds.push(alertId);
    rungSet.add(alertId);
    if (rungIds.length > MAX_RUNG_IDS) {
      const oldest = rungIds.shift();
      if (oldest !== undefined) rungSet.delete(oldest);
    }
  }

  /** Push `start` out so the two classes stay CLASS_GAP_MS apart. */
  function spacedStart(now: number, earlier: number | undefined): number {
    return earlier !== undefined && now < earlier + CLASS_GAP_MS ? earlier + CLASS_GAP_MS : now;
  }

  /**
   * Play `level` at `start`, or now when already due. A scheduled cue for a
   * known alert re-checks its filters when it fires, since the device switch,
   * quiet hours, mute, level, or viewed session may have changed.
   */
  function playCue(level: SoundLevel, start: number, now: number, alert?: SoundAlert): void {
    if (start <= now) {
      deps.play(level);
      return;
    }
    let cancel = () => {};
    cancel = deps.schedule(() => {
      scheduled.delete(cancel);
      if (alert !== undefined && !passesFilters(alert, deps.getContext())) return;
      deps.play(level);
    }, start - now);
    scheduled.add(cancel);
  }

  function ringNeedsResponse(alert: SoundAlert): void {
    const now = deps.nowMs();
    const last = lastCueAt.needs_response;
    if (last !== undefined && now - last < BURST_WINDOW_MS) return;
    const start = spacedStart(now, lastCueAt.other);
    remember(alert.alertId);
    lastCueAt.needs_response = start;
    playCue(alert.level, start, now, alert);
  }

  function closeCollection(alerts: SoundAlert[]): void {
    const now = deps.nowMs();
    const start = spacedStart(now, lastCueAt.needs_response);
    lastCueAt.other = start;
    if (start <= now) {
      playCollected(alerts);
      return;
    }
    let cancel = () => {};
    cancel = deps.schedule(() => {
      scheduled.delete(cancel);
      playCollected(alerts);
    }, start - now);
    scheduled.add(cancel);
  }

  /** Re-filter the collected alerts and play the single surviving cue. */
  function playCollected(alerts: SoundAlert[]): void {
    const kept = alerts.filter((alert) => passesFilters(alert, deps.getContext()));
    if (kept.length === 0) return;
    deps.play(kept.some((alert) => alert.level === "error") ? "error" : "done");
  }

  function ringOther(alert: SoundAlert): void {
    if (collecting !== null) {
      collecting.push(alert);
      remember(alert.alertId);
      return;
    }
    const now = deps.nowMs();
    const last = lastCueAt.other;
    // A done/error right after an other cue is part of that burst, not a new one.
    if (last !== undefined && now - last < BURST_WINDOW_MS) return;
    remember(alert.alertId);
    const alerts = [alert];
    collecting = alerts;
    let cancel = () => {};
    cancel = deps.schedule(() => {
      scheduled.delete(cancel);
      collecting = null;
      closeCollection(alerts);
    }, BURST_WINDOW_MS);
    scheduled.add(cancel);
  }

  function ring(alert: SoundAlert): void {
    if (rungSet.has(alert.alertId)) return;
    if (!passesFilters(alert, deps.getContext())) return;
    if (alertClass(alert.level) === "needs_response") ringNeedsResponse(alert);
    else ringOther(alert);
  }

  function dispose(): void {
    for (const cancel of scheduled) cancel();
    scheduled.clear();
    collecting = null;
  }

  return { ring, dispose };
}
