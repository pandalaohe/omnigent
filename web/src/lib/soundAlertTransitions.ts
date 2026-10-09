// Pure edge detection for sound alerts.
//
// The hook owns the previous-snapshot ref; this module only diffs two
// snapshots. The row state starts minimal (awaiting count) and grows with
// later slices that add the done/error marks.

import type { Conversation } from "@/hooks/useConversations";
import { getSessionState } from "@/hooks/useSessionState";
import type { SoundLevel } from "@/lib/soundAlertPreferences";

export interface RowSoundState {
  awaitingCount: number;
}

export interface SoundAlert {
  sessionId: string;
  level: SoundLevel;
}

/**
 * Alerts for sessions whose state changed between two snapshots.
 *
 * `previous === null` is the first snapshot after load and a session absent
 * from `previous` is newly loaded: both are baselines, not edges. Only a
 * rising awaiting count on a known session fires, once per transition.
 */
export function detectEdges(
  previous: Map<string, RowSoundState> | null,
  next: Map<string, RowSoundState>,
): SoundAlert[] {
  if (previous === null) return [];
  const alerts: SoundAlert[] = [];
  for (const [sessionId, state] of next) {
    const prior = previous.get(sessionId);
    if (prior === undefined) continue;
    if (state.awaitingCount > prior.awaitingCount) {
      alerts.push({ sessionId, level: "needs_response" });
    }
  }
  return alerts;
}

/** Top-level rows only: a child's prompts are its parent's to surface. */
export function buildRowSoundStates(conversations: Conversation[]): Map<string, RowSoundState> {
  const states = new Map<string, RowSoundState>();
  for (const conversation of conversations) {
    if (conversation.parent_session_id) continue;
    const state = getSessionState(conversation);
    states.set(conversation.id, {
      awaitingCount: state?.kind === "awaiting" ? state.count : 0,
    });
  }
  return states;
}
