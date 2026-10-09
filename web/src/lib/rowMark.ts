// Pure derivation of a sidebar row's trailing mark.
//
// Extracted from the conversation row so the sound-alert edge detector can
// diff the same precedence the user sees. `getSessionState` never reports
// "starting" (that reads the chat store) or "unseen" (that reads the read
// state), so callers fold those in through the context.

import type { Conversation } from "@/hooks/useConversations";
import { getSessionState } from "@/hooks/useSessionState";
import type { LatestSessionError } from "@/lib/sessionError";

export type MarkState =
  "awaiting" | "running" | "starting" | "error" | "disconnected" | "unseen" | "cold" | "none";

export interface RowMark {
  state: MarkState;
  awaitingCount: number;
  background: boolean;
  goal: "active" | "paused" | "none";
}

export interface RowMarkContext {
  unseen: boolean;
  latestError: LatestSessionError | null;
  showGoalMarkers: boolean;
  starting: boolean;
}

export function rowMark(
  conversation: Pick<
    Conversation,
    | "status"
    | "foreground_status"
    | "pending_elicitations_count"
    | "child_pending_elicitations_count"
    | "goal_state"
    | "warm_state"
    | "background_activity_count"
  >,
  ctx: RowMarkContext,
): RowMark {
  const goal =
    ctx.showGoalMarkers &&
    (conversation.goal_state === "active" || conversation.goal_state === "paused")
      ? conversation.goal_state
      : "none";
  // An active goal frame already advertises the goal instead of the dot.
  const hasUnseen = ctx.unseen && goal !== "active";
  const derived = getSessionState(conversation, ctx.latestError);
  const awaitingCount = derived?.kind === "awaiting" ? derived.count : 0;

  let state: MarkState;
  if (derived?.kind === "awaiting") state = "awaiting";
  else if (derived?.kind === "running") state = "running";
  else if (ctx.starting) state = "starting";
  else if (derived?.kind === "error" || derived?.kind === "disconnected") state = derived.kind;
  else if (hasUnseen) state = "unseen";
  else if (conversation.warm_state === "cold") state = "cold";
  else state = "none";

  return {
    state,
    awaitingCount,
    background: (conversation.background_activity_count ?? 0) > 0,
    goal,
  };
}
