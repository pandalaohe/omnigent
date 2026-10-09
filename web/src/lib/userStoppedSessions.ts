// Remembers sessions the user just stopped, so the alert ringer can skip the
// done/error cue their own interrupt or stop would otherwise cause. The
// settled rule: the user's own pause/cancel never sounds.

const STOP_WINDOW_MS = 60_000;
const MAX_ENTRIES = 200;

const stoppedAt = new Map<string, number>();

/** Record that the user stopped `sessionId` at `nowMs`. */
export function noteUserStopped(sessionId: string, nowMs = Date.now()): void {
  // Re-insert so the newest note is the last evicted when over the cap.
  stoppedAt.delete(sessionId);
  stoppedAt.set(sessionId, nowMs);
  while (stoppedAt.size > MAX_ENTRIES) {
    const oldest = stoppedAt.keys().next().value;
    if (oldest === undefined) break;
    stoppedAt.delete(oldest);
  }
}

/** Whether the user stopped `sessionId` within the last 60 s. */
export function wasUserStoppedRecently(sessionId: string, nowMs = Date.now()): boolean {
  for (const [id, at] of stoppedAt) {
    if (nowMs - at > STOP_WINDOW_MS) stoppedAt.delete(id);
  }
  return stoppedAt.has(sessionId);
}

export function resetUserStoppedSessionsForTests(): void {
  stoppedAt.clear();
}
