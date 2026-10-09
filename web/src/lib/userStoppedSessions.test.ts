import { beforeEach, describe, expect, it } from "vitest";

import {
  noteUserStopped,
  resetUserStoppedSessionsForTests,
  wasUserStoppedRecently,
} from "./userStoppedSessions";

describe("userStoppedSessions", () => {
  beforeEach(() => {
    resetUserStoppedSessionsForTests();
  });

  it("reports a session stopped right after the note", () => {
    noteUserStopped("conv_a", 1_000);

    expect(wasUserStoppedRecently("conv_a", 1_000)).toBe(true);
  });

  it("reports false once the 60 s window has passed", () => {
    noteUserStopped("conv_a", 1_000);

    expect(wasUserStoppedRecently("conv_a", 61_001)).toBe(false);
  });

  it("reports false for a session that was never stopped", () => {
    expect(wasUserStoppedRecently("conv_a", 1_000)).toBe(false);
  });
});
