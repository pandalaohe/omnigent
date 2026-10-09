import { describe, expect, it } from "vitest";

import type { Conversation } from "@/hooks/useConversations";
import { buildRowSoundStates, detectEdges, type RowSoundState } from "./soundAlertTransitions";

function conv(id: string, pendingElicitations = 0, parentSessionId?: string): Conversation {
  return {
    id,
    object: "conversation",
    title: id,
    created_at: 0,
    updated_at: 0,
    labels: {},
    permission_level: null,
    pending_elicitations_count: pendingElicitations,
    ...(parentSessionId ? { parent_session_id: parentSessionId } : {}),
  };
}

function state(awaitingCount: number): RowSoundState {
  return { awaitingCount };
}

describe("sound alert transitions", () => {
  it("fires nothing for the first snapshot", () => {
    const next = buildRowSoundStates([conv("conv_a", 2)]);
    expect(detectEdges(null, next)).toEqual([]);
  });

  it("fires needs_response when a session goes from zero to awaiting", () => {
    const previous = new Map([["conv_a", state(0)]]);
    const next = buildRowSoundStates([conv("conv_a", 1)]);
    expect(detectEdges(previous, next)).toEqual([{ sessionId: "conv_a", level: "needs_response" }]);
  });

  it("fires when the awaiting count rises", () => {
    const previous = new Map([["conv_a", state(1)]]);
    const next = buildRowSoundStates([conv("conv_a", 2)]);
    expect(detectEdges(previous, next)).toEqual([{ sessionId: "conv_a", level: "needs_response" }]);
  });

  it("stays quiet when the count is unchanged or lower", () => {
    const same = buildRowSoundStates([conv("conv_a", 2)]);
    const lower = buildRowSoundStates([conv("conv_a", 1)]);

    expect(detectEdges(new Map([["conv_a", state(2)]]), same)).toEqual([]);
    expect(detectEdges(new Map([["conv_a", state(2)]]), lower)).toEqual([]);
  });

  it("treats a newly loaded session already awaiting as a baseline", () => {
    const previous = new Map([["conv_a", state(0)]]);
    const next = buildRowSoundStates([conv("conv_a", 0), conv("conv_new", 1)]);
    expect(detectEdges(previous, next)).toEqual([]);
  });

  it("ignores child sessions", () => {
    const states = buildRowSoundStates([
      conv("conv_parent", 1),
      conv("conv_child", 1, "conv_parent"),
    ]);

    expect([...states.keys()]).toEqual(["conv_parent"]);
  });
});
