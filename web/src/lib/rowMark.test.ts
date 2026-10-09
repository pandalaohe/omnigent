import { describe, expect, it } from "vitest";

import { rowMark, type RowMarkContext } from "./rowMark";

type ConversationSlice = Parameters<typeof rowMark>[0];

function conv(partial: Partial<ConversationSlice> = {}): ConversationSlice {
  return {
    status: "idle",
    foreground_status: "idle",
    pending_elicitations_count: 0,
    goal_state: null,
    warm_state: null,
    background_activity_count: 0,
    ...partial,
  };
}

function ctx(partial: Partial<RowMarkContext> = {}): RowMarkContext {
  return {
    unseen: false,
    latestError: null,
    showGoalMarkers: true,
    starting: false,
    ...partial,
  };
}

describe("rowMark", () => {
  it("reports awaiting with its count ahead of everything else", () => {
    const mark = rowMark(
      conv({ pending_elicitations_count: 2, foreground_status: "running", status: "failed" }),
      ctx({ starting: true, unseen: true }),
    );

    expect(mark).toEqual({ state: "awaiting", awaitingCount: 2, background: false, goal: "none" });
  });

  it("reports running ahead of starting and errors", () => {
    expect(rowMark(conv({ foreground_status: "running" }), ctx({ starting: true })).state).toBe(
      "running",
    );
    expect(rowMark(conv({ status: "failed", foreground_status: "running" }), ctx()).state).toBe(
      "running",
    );
  });

  it("reports starting ahead of an error while the launch is still in flight", () => {
    expect(rowMark(conv({ status: "failed" }), ctx({ starting: true })).state).toBe("starting");
    expect(rowMark(conv(), ctx({ starting: true })).state).toBe("starting");
  });

  it("reports error and disconnected as their own states", () => {
    expect(rowMark(conv({ status: "failed" }), ctx()).state).toBe("error");
    expect(rowMark(conv({ status: "idle" }), ctx({ latestError: "error" })).state).toBe("error");
    expect(rowMark(conv(), ctx({ latestError: "disconnected", unseen: true })).state).toBe(
      "disconnected",
    );
  });

  it("reports unseen when nothing more urgent applies", () => {
    expect(rowMark(conv(), ctx({ unseen: true })).state).toBe("unseen");
  });

  it("reports cold when idle and cold", () => {
    expect(rowMark(conv({ warm_state: "cold" }), ctx()).state).toBe("cold");
    expect(rowMark(conv({ warm_state: "cold" }), ctx({ unseen: true })).state).toBe("unseen");
  });

  it("reports none when idle and warm", () => {
    expect(rowMark(conv({ warm_state: "warm" }), ctx()).state).toBe("none");
  });

  it("hides the unseen dot behind an active goal marker", () => {
    const mark = rowMark(conv({ goal_state: "active" }), ctx({ unseen: true }));

    expect(mark).toEqual({ state: "none", awaitingCount: 0, background: false, goal: "active" });
  });

  it("keeps a paused goal and still shows the unseen dot", () => {
    const mark = rowMark(conv({ goal_state: "paused" }), ctx({ unseen: true }));

    expect(mark.state).toBe("unseen");
    expect(mark.goal).toBe("paused");
  });

  it("hides and drops goal state when goal markers are off", () => {
    const mark = rowMark(
      conv({ goal_state: "active" }),
      ctx({ showGoalMarkers: false, unseen: true }),
    );

    expect(mark.goal).toBe("none");
    expect(mark.state).toBe("unseen");
  });

  it("reads the background-activity flag from the count", () => {
    expect(rowMark(conv({ background_activity_count: 3 }), ctx()).background).toBe(true);
    expect(rowMark(conv({ background_activity_count: 0 }), ctx()).background).toBe(false);
    expect(rowMark(conv({ background_activity_count: undefined }), ctx()).background).toBe(false);
  });
});
