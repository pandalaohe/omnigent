import { describe, expect, it } from "vitest";

import {
  RUNNER_LOG_RUNAWAY_LABEL_KEY,
  RUNNER_LOG_RUNAWAY_LEASE_MS,
  RUNNER_LOG_RUNAWAY_SEEN_LABEL_KEY,
  runnerLogRunawayLeaseEnd,
} from "./runnerLogRunaway";

const FLAG = "2026-09-23T09:25:00+00:00";
const SEEN = "2026-09-23T09:30:00+00:00";

describe("runnerLogRunawayLeaseEnd", () => {
  it("leases a confirmed warning to parse(seen) plus the lease window", () => {
    expect(
      runnerLogRunawayLeaseEnd({
        [RUNNER_LOG_RUNAWAY_LABEL_KEY]: FLAG,
        [RUNNER_LOG_RUNAWAY_SEEN_LABEL_KEY]: SEEN,
      }),
    ).toBe(Date.parse(SEEN) + RUNNER_LOG_RUNAWAY_LEASE_MS);
  });

  it("returns null without a runaway flag", () => {
    expect(runnerLogRunawayLeaseEnd(undefined)).toBeNull();
    expect(runnerLogRunawayLeaseEnd({})).toBeNull();
    expect(runnerLogRunawayLeaseEnd({ [RUNNER_LOG_RUNAWAY_LABEL_KEY]: "" })).toBeNull();
    expect(runnerLogRunawayLeaseEnd({ [RUNNER_LOG_RUNAWAY_SEEN_LABEL_KEY]: SEEN })).toBeNull();
  });

  it("returns null without a server receive instant", () => {
    // Flags written before the lease existed carry no `seen` and must not show.
    expect(runnerLogRunawayLeaseEnd({ [RUNNER_LOG_RUNAWAY_LABEL_KEY]: FLAG })).toBeNull();
    expect(
      runnerLogRunawayLeaseEnd({
        [RUNNER_LOG_RUNAWAY_LABEL_KEY]: FLAG,
        [RUNNER_LOG_RUNAWAY_SEEN_LABEL_KEY]: "",
      }),
    ).toBeNull();
  });

  it("returns null when the server receive instant is unparseable", () => {
    expect(
      runnerLogRunawayLeaseEnd({
        [RUNNER_LOG_RUNAWAY_LABEL_KEY]: FLAG,
        [RUNNER_LOG_RUNAWAY_SEEN_LABEL_KEY]: "not-a-date",
      }),
    ).toBeNull();
  });
});
