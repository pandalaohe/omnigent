// Per-row state derivation for the sidebar badge.
// Priority: awaiting (web prompts, then terminal prompts) > running > disconnected
// > error > background work > no badge.
//
// Liveness (runner / host reachability) is no longer a sidebar state:
// it surfaces in the open-session view (see `useSessionLiveness`), so the
// sidebar no longer renders a "disconnected" badge and `getSessionState`
// no longer reads runner liveness.
//
// Errors are independent of read state. Native sessions can settle to idle
// after an error, so callers also inspect the latest conversation message.

import type { Conversation } from "@/hooks/useConversations";
import type { LatestSessionError } from "@/lib/sessionError";

export type SessionState =
  // `terminalReason`: no web card; the agent is parked on a prompt in its terminal.
  | { kind: "awaiting"; count: number; terminalReason?: string }
  // `background`: the turn ended but work outlives it (a background shell or a Codex goal).
  | { kind: "running"; background?: true }
  | { kind: "error" }
  | { kind: "disconnected" }
  | { kind: "unseen" }
  // The open session's launch/relaunch window — a send in flight or the PTY
  // being created before the server confirms `running`. Not derivable from a
  // conversation row (it reads the chat store), so `getSessionState` never
  // returns it; the sidebar row folds it in for the bound session only.
  | { kind: "starting" };

/** This session's own prompt/turn state, excluding background rollups. */
export function getConversationForegroundStatus(
  conversation: Pick<Conversation, "status" | "foreground_status"> | undefined | null,
): Conversation["status"] {
  return conversation?.foreground_status ?? conversation?.status;
}

export function getSessionState(
  conversation:
    | Pick<
        Conversation,
        | "status"
        | "foreground_status"
        | "pending_elicitations_count"
        | "child_pending_elicitations_count"
        | "background_work_running"
        | "blocked_on"
      >
    | undefined
    | null,
  latestError: LatestSessionError | null = null,
): SessionState | null {
  // Children have no sidebar row of their own, so their parked prompts roll
  // into the parent's badge: one click reaches the blocked sub-agent.
  const pending =
    (conversation?.pending_elicitations_count ?? 0) +
    (conversation?.child_pending_elicitations_count ?? 0);
  if (pending > 0) return { kind: "awaiting", count: pending };
  if (getConversationForegroundStatus(conversation) === "running" && conversation?.blocked_on) {
    return { kind: "awaiting", count: 0, terminalReason: conversation.blocked_on };
  }
  // Older servers omit foreground_status, so retain the aggregate status as a
  // compatibility fallback. New servers keep child-only work out of the spinner.
  if (getConversationForegroundStatus(conversation) === "running") {
    return { kind: "running" };
  }
  if (latestError === "disconnected") return { kind: "disconnected" };
  const failed = conversation?.status === "failed" || latestError === "error";
  if (failed && latestError !== "recovered_disconnect") return { kind: "error" };
  if (conversation?.background_work_running) return { kind: "running", background: true };
  return null;
}

/**
 * Whether archiving would interrupt the agent: a turn is running or waiting on
 * an approval, or background work outlives the last turn.
 */
export function isSessionWorking(
  conversation: Pick<
    Conversation,
    "status" | "pending_elicitations_count" | "background_work_running"
  >,
): boolean {
  return (
    conversation.status === "running" ||
    (conversation.pending_elicitations_count ?? 0) > 0 ||
    conversation.background_work_running === true
  );
}
