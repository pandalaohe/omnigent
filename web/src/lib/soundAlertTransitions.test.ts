import { describe, expect, it } from "vitest";

import type { Conversation } from "@/hooks/useConversations";
import type { RowMark, RowMarkContext } from "./rowMark";
import {
  alertId,
  buildRowSoundStates,
  detectEdges,
  isDoneCandidate,
  isQuietNow,
  type RowSoundState,
} from "./soundAlertTransitions";

const CTX: RowMarkContext = {
  unseen: false,
  latestError: null,
  showGoalMarkers: true,
  starting: false,
};

function conv(id: string, partial: Partial<Conversation> = {}): Conversation {
  return {
    id,
    object: "conversation",
    title: id,
    created_at: 0,
    updated_at: 100,
    labels: {},
    permission_level: null,
    pending_elicitations_count: 0,
    ...partial,
  };
}

function rowState(
  partial: Partial<RowMark> = {},
  updatedAt = 100,
  pendingKey: string | null = null,
): RowSoundState {
  return {
    mark: { state: "none", awaitingCount: 0, background: false, goal: "none", ...partial },
    updatedAt,
    pendingKey,
  };
}

const IMMEDIATE_EMPTY = { immediate: [], settleStart: [], settleCancel: [] };

describe("sound alert transitions", () => {
  it("fires nothing for the first snapshot", () => {
    const next = buildRowSoundStates(
      [conv("conv_a", { pending_elicitations_count: 2 })],
      () => CTX,
    );
    expect(detectEdges(null, next)).toEqual(IMMEDIATE_EMPTY);
  });

  it("fires needs_response when a session goes from zero to awaiting", () => {
    const previous = new Map([["conv_a", rowState()]]);
    const next = buildRowSoundStates(
      [conv("conv_a", { pending_elicitations_count: 1, updated_at: 200 })],
      () => CTX,
    );
    expect(detectEdges(previous, next)).toEqual({
      immediate: [
        { sessionId: "conv_a", level: "needs_response", alertId: "conv_a:needs_response:200:1" },
      ],
      settleStart: [],
      settleCancel: [],
    });
  });

  it("fires when the awaiting count rises", () => {
    const previous = new Map([["conv_a", rowState({ awaitingCount: 1 })]]);
    const next = buildRowSoundStates(
      [conv("conv_a", { pending_elicitations_count: 2, updated_at: 300 })],
      () => CTX,
    );
    expect(detectEdges(previous, next).immediate).toEqual([
      { sessionId: "conv_a", level: "needs_response", alertId: "conv_a:needs_response:300:2" },
    ]);
  });

  it("stays quiet when the count is unchanged or lower", () => {
    const same = buildRowSoundStates(
      [conv("conv_a", { pending_elicitations_count: 2 })],
      () => CTX,
    );
    const lower = buildRowSoundStates(
      [conv("conv_a", { pending_elicitations_count: 1 })],
      () => CTX,
    );

    expect(detectEdges(new Map([["conv_a", rowState({ awaitingCount: 2 })]]), same)).toEqual(
      IMMEDIATE_EMPTY,
    );
    expect(detectEdges(new Map([["conv_a", rowState({ awaitingCount: 2 })]]), lower)).toEqual(
      IMMEDIATE_EMPTY,
    );
  });

  it("treats a newly loaded session already awaiting as a baseline", () => {
    const previous = new Map([["conv_a", rowState()]]);
    const next = buildRowSoundStates(
      [conv("conv_a"), conv("conv_new", { pending_elicitations_count: 1, updated_at: 500 })],
      () => CTX,
    );
    expect(detectEdges(previous, next)).toEqual(IMMEDIATE_EMPTY);
  });

  it("fires error when a row enters error or disconnected, once per entry", () => {
    const previous = new Map([
      ["conv_a", rowState()],
      ["conv_b", rowState({ state: "error" })],
      ["conv_c", rowState({ state: "error" })],
      ["conv_d", rowState()],
    ]);
    const next = buildRowSoundStates(
      [
        conv("conv_a", { status: "failed", updated_at: 200 }),
        conv("conv_b", { status: "failed", updated_at: 200 }),
        conv("conv_c", { updated_at: 200 }),
        conv("conv_d", { updated_at: 200 }),
      ],
      (conversation) =>
        conversation.id === "conv_c"
          ? { ...CTX, latestError: "disconnected" }
          : { ...CTX, latestError: conversation.id === "conv_d" ? "error" : null },
    );

    // conv_b already failed and conv_c moved error -> disconnected: both
    // stay quiet; only the new failure entrances ring.
    expect(detectEdges(previous, next).immediate).toEqual([
      { sessionId: "conv_a", level: "error", alertId: "conv_a:error:200" },
      { sessionId: "conv_d", level: "error", alertId: "conv_d:error:200" },
    ]);
  });

  it("starts the done settle when a row becomes a done candidate", () => {
    const previous = new Map([["conv_a", rowState()]]);
    const next = buildRowSoundStates([conv("conv_a")], () => ({ ...CTX, unseen: true }));

    expect(detectEdges(previous, next)).toEqual({
      immediate: [],
      settleStart: ["conv_a"],
      settleCancel: [],
    });
  });

  it("cancels the done settle when background work or a running turn covers the dot", () => {
    const previous = new Map([["conv_a", rowState({ state: "unseen" })]]);
    const background = buildRowSoundStates(
      [conv("conv_a", { background_activity_count: 1 })],
      () => ({ ...CTX, unseen: true }),
    );
    const running = buildRowSoundStates([conv("conv_a", { foreground_status: "running" })], () => ({
      ...CTX,
      unseen: true,
    }));

    expect(detectEdges(previous, background)).toEqual({
      immediate: [],
      settleStart: [],
      settleCancel: ["conv_a"],
    });
    expect(detectEdges(previous, running)).toEqual({
      immediate: [],
      settleStart: [],
      settleCancel: ["conv_a"],
    });
  });

  it("ignores done candidates covered by background activity", () => {
    const state = rowState({ state: "unseen", background: true });
    expect(isDoneCandidate(state)).toBe(false);
    expect(isDoneCandidate(rowState({ state: "unseen" }))).toBe(true);
  });

  it("omits the awaiting count from done and error alert ids", () => {
    expect(alertId("conv_a", "done", rowState({ state: "unseen" }, 42))).toBe("conv_a:done:42");
    expect(alertId("conv_a", "error", rowState({ state: "error" }, 42))).toBe("conv_a:error:42");
    expect(alertId("conv_a", "needs_response", rowState({ awaitingCount: 3 }, 42))).toBe(
      "conv_a:needs_response:42:3",
    );
  });

  it("keys a needs_response id off the prompt key, not updated_at", () => {
    const first = rowState({ state: "awaiting", awaitingCount: 1 }, 100, "key_a");
    const later = rowState({ state: "awaiting", awaitingCount: 1 }, 999, "key_a");

    expect(alertId("conv_a", "needs_response", first)).toBe(
      alertId("conv_a", "needs_response", later),
    );
  });

  it("distinguishes a new prompt by key even at the same count and updated_at", () => {
    const first = rowState({ state: "awaiting", awaitingCount: 1 }, 100, "key_a");
    const second = rowState({ state: "awaiting", awaitingCount: 1 }, 100, "key_b");

    expect(alertId("conv_a", "needs_response", first)).not.toBe(
      alertId("conv_a", "needs_response", second),
    );
  });

  it("falls back to the old id form when the server supplies no prompt key", () => {
    const state = rowState({ state: "awaiting", awaitingCount: 2 }, 42, null);

    expect(alertId("conv_a", "needs_response", state)).toBe("conv_a:needs_response:42:2");
  });

  it("ignores child sessions", () => {
    const states = buildRowSoundStates(
      [
        conv("conv_parent", { pending_elicitations_count: 1 }),
        conv("conv_child", { pending_elicitations_count: 1, parent_session_id: "conv_parent" }),
      ],
      () => CTX,
    );

    expect([...states.keys()]).toEqual(["conv_parent"]);
  });
});

describe("isQuietNow", () => {
  function at(hours: number, minutes: number): Date {
    return new Date(2026, 0, 15, hours, minutes, 0);
  }

  it("is never quiet when disabled or when start equals end", () => {
    expect(isQuietNow({ enabled: false, start: "23:00", end: "08:00" }, at(2, 30))).toBe(false);
    expect(isQuietNow({ enabled: true, start: "09:00", end: "09:00" }, at(9, 0))).toBe(false);
  });

  it("covers the evening-to-morning window across midnight", () => {
    const quiet = { enabled: true, start: "23:00", end: "08:00" };

    expect(isQuietNow(quiet, at(2, 30))).toBe(true);
    expect(isQuietNow(quiet, at(12, 0))).toBe(false);
    expect(isQuietNow(quiet, at(23, 0))).toBe(true);
    expect(isQuietNow(quiet, at(8, 0))).toBe(false);
  });

  it("covers a same-day window, end exclusive", () => {
    const quiet = { enabled: true, start: "09:00", end: "17:00" };

    expect(isQuietNow(quiet, at(12, 0))).toBe(true);
    expect(isQuietNow(quiet, at(8, 59))).toBe(false);
    expect(isQuietNow(quiet, at(17, 0))).toBe(false);
  });
});
