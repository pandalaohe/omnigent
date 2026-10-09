// Pure edge detection for sound alerts.
//
// The hook owns the previous-snapshot ref and the settle timers; this module
// only diffs two snapshots. The first snapshot after load and any session
// absent from the previous one are baselines, not edges.

import type { Conversation } from "@/hooks/useConversations";
import type { SoundAlertQuietHours, SoundLevel } from "@/lib/soundAlertPreferences";
import { rowMark, type RowMark, type RowMarkContext } from "@/lib/rowMark";

export interface RowSoundState {
  mark: RowMark;
  updatedAt: number;
  /**
   * Server-provided key identifying the row's outstanding prompts, or null
   * when the index holds none. A needs_response alert id keys off this so a
   * prompt cycle re-alerts even when the row's updated_at does not move.
   */
  pendingKey: string | null;
}

export interface SoundAlert {
  sessionId: string;
  level: SoundLevel;
  alertId: string;
}

export interface EdgeResult {
  immediate: SoundAlert[];
  settleStart: string[];
  settleCancel: string[];
}

/** A dot the user has not seen that is not covered by background work. */
export function isDoneCandidate(state: RowSoundState): boolean {
  return state.mark.state === "unseen" && !state.mark.background;
}

/** Stable identity for de-duplication across re-deliveries of one edge. */
export function alertId(sessionId: string, level: SoundLevel, s: RowSoundState): string {
  if (level === "needs_response") {
    // The prompt key identifies the specific prompt(s) awaiting; updated_at
    // does not move for a new prompt, so fall back to it only when the
    // server supplied no key (older server / no index entry).
    return s.pendingKey !== null
      ? `${sessionId}:needs_response:${s.pendingKey}`
      : `${sessionId}:needs_response:${s.updatedAt}:${s.mark.awaitingCount}`;
  }
  return `${sessionId}:${level}:${s.updatedAt}`;
}

/**
 * Alerts for sessions whose state changed between two snapshots.
 *
 * `previous === null` is the first snapshot after load and a session absent
 * from `previous` is newly loaded: both are baselines, not edges.
 */
export function detectEdges(
  previous: Map<string, RowSoundState> | null,
  next: Map<string, RowSoundState>,
): EdgeResult {
  const result: EdgeResult = { immediate: [], settleStart: [], settleCancel: [] };
  if (previous === null) return result;
  for (const [sessionId, state] of next) {
    const prior = previous.get(sessionId);
    if (prior === undefined) continue;
    if (state.mark.awaitingCount > prior.mark.awaitingCount) {
      result.immediate.push({
        sessionId,
        level: "needs_response",
        alertId: alertId(sessionId, "needs_response", state),
      });
    }
    const enteredFailure =
      (state.mark.state === "error" || state.mark.state === "disconnected") &&
      prior.mark.state !== "error" &&
      prior.mark.state !== "disconnected";
    if (enteredFailure) {
      result.immediate.push({
        sessionId,
        level: "error",
        alertId: alertId(sessionId, "error", state),
      });
    }
    const done = isDoneCandidate(state);
    if (done && !isDoneCandidate(prior)) result.settleStart.push(sessionId);
    else if (!done && isDoneCandidate(prior)) result.settleCancel.push(sessionId);
  }
  return result;
}

function parseTimeOfDay(value: string): number {
  const [hours, minutes] = value.split(":");
  return Number(hours) * 60 + Number(minutes);
}

/** Whether the account's quiet-hours window covers the given local time. */
export function isQuietNow(quietHours: SoundAlertQuietHours, now: Date): boolean {
  if (!quietHours.enabled) return false;
  const start = parseTimeOfDay(quietHours.start);
  const end = parseTimeOfDay(quietHours.end);
  if (start === end) return false;
  const current = now.getHours() * 60 + now.getMinutes();
  if (start < end) return current >= start && current < end;
  // The window wraps past midnight.
  return current >= start || current < end;
}

/** Top-level rows only: a child's prompts are its parent's to surface. */
export function buildRowSoundStates(
  conversations: readonly Conversation[],
  ctxFor: (conversation: Conversation) => RowMarkContext,
): Map<string, RowSoundState> {
  const states = new Map<string, RowSoundState>();
  for (const conversation of conversations) {
    if (conversation.parent_session_id) continue;
    states.set(conversation.id, {
      mark: rowMark(conversation, ctxFor(conversation)),
      updatedAt: conversation.updated_at,
      pendingKey: conversation.pending_elicitation_key ?? null,
    });
  }
  return states;
}
