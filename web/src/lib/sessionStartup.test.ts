import { afterEach, describe, expect, it, vi } from "vitest";
import { sessionStartupFromWire } from "./sessionStartup";
import { parseEvent } from "./sse";
import { cancelStartup, getStartupReleasePreview, releaseStartupSlots } from "./sessionsApi";

afterEach(() => vi.unstubAllGlobals());

const wire = {
  id: "startup",
  attempt_id: "attempt",
  input_id: "input",
  state: "verifying",
  reason: null,
  elapsed_seconds: 12,
  queue_position_estimate: null,
  can_cancel: true,
  evidence: { runtime_ready: true, tools_ready: false, input_accepted: true, work_started: false },
};

describe("sessionStartupFromWire", () => {
  it("returns the authoritative cancellation response even without a live event stream", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(JSON.stringify({ ...wire, state: "cancelling", can_cancel: false }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
      ),
    );
    const cancelled = await cancelStartup("session", "startup", "attempt");
    expect(cancelled?.state).toBe("cancelling");
  });
  it("preserves independent evidence and the server's elapsed duration", () => {
    expect(sessionStartupFromWire(wire)).toEqual({
      id: "startup",
      hostId: null,
      attemptId: "attempt",
      inputId: "input",
      state: "verifying",
      reason: null,
      elapsedSeconds: 12,
      admittedElapsedSeconds: null,
      inputElapsedSeconds: null,
      unconfirmedStartups: 0,
      queuePositionEstimate: null,
      canCancel: true,
      evidence: { runtimeReady: true, toolsReady: false, inputAccepted: true, workStarted: false },
    });
  });
  it("preserves fixed-origin clocks and host uncertainty from the wire", () => {
    expect(
      sessionStartupFromWire({
        ...wire,
        state: "waiting_input",
        input_id: null,
        admitted_elapsed_seconds: 180,
        input_elapsed_seconds: null,
        unconfirmed_startups: 2,
      }),
    ).toMatchObject({
      state: "waiting_input",
      admittedElapsedSeconds: 180,
      inputElapsedSeconds: null,
      unconfirmedStartups: 2,
    });
    expect(
      sessionStartupFromWire({
        ...wire,
        admitted_elapsed_seconds: 190,
        input_elapsed_seconds: 120,
      }),
    ).toMatchObject({ admittedElapsedSeconds: 190, inputElapsedSeconds: 120 });
  });
  it("rejects invalid optional clocks and host counts", () => {
    for (const changed of [
      { admitted_elapsed_seconds: -1 },
      { admitted_elapsed_seconds: 1.5 },
      { admitted_elapsed_seconds: Infinity },
      { input_elapsed_seconds: -1 },
      { input_elapsed_seconds: 2.5 },
      { input_elapsed_seconds: NaN },
      { unconfirmed_startups: -1 },
      { unconfirmed_startups: 1.5 },
      { unconfirmed_startups: Infinity },
      { unconfirmed_startups: null },
    ])
      expect(sessionStartupFromWire({ ...wire, ...changed })).toBeNull();
  });
  it("drops unsupported states or incomplete evidence instead of inventing readiness", () => {
    for (const changed of [
      { state: "running" },
      { evidence: {} },
      { elapsed_seconds: NaN },
      { elapsed_seconds: -1 },
      { queue_position_estimate: 0 },
      { can_cancel: "yes" },
    ])
      expect(sessionStartupFromWire({ ...wire, ...changed })).toBeNull();
    expect(sessionStartupFromWire(null)).toBeNull();
  });
  it("parses full startup replacements without allowing malformed events to clear them", () => {
    expect(parseEvent("session.startup", { conversation_id: "session", startup: wire })).toEqual({
      type: "session_startup",
      conversationId: "session",
      startup: sessionStartupFromWire(wire),
    });
    expect(parseEvent("session.startup", { conversation_id: "session", startup: {} })).toBeNull();
    expect(parseEvent("session.startup", { conversation_id: "session", startup: null })).toEqual({
      type: "session_startup",
      conversationId: "session",
      startup: null,
    });
  });
  it.each(["opened", "operator_released"])(
    "keeps %s in REST and SSE without inventing work",
    (state) => {
      const raw = { ...wire, state, host_id: "target-host", can_cancel: false };
      expect(sessionStartupFromWire(raw)).toMatchObject({
        state,
        hostId: "target-host",
        evidence: { workStarted: false },
      });
      expect(
        parseEvent("session.startup", { conversation_id: "session", startup: raw }),
      ).toMatchObject({
        type: "session_startup",
        startup: { state, hostId: "target-host" },
      });
    },
  );
  it("rejects an invalid host identity", () => {
    expect(sessionStartupFromWire({ ...wire, host_id: 1 })).toBeNull();
  });
  it("validates the preview and submits only its exact targets", async () => {
    const target = {
      startup_id: "a".repeat(32),
      session_id: "b".repeat(32),
      attempt_id: "c".repeat(32),
      input_id: null,
      runtime_id: null,
      host_generation: "old",
      runner_id: null,
    };
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            current_host_generation: "current",
            targets: [{ target, title: "Draft", work_hint: "unknown" }],
          }),
        ),
      )
      .mockResolvedValueOnce(new Response("{}"));
    vi.stubGlobal("fetch", fetch);
    const preview = await getStartupReleasePreview("host/id");
    await releaseStartupSlots("host/id", {
      current_host_generation: preview.current_host_generation!,
      targets: preview.targets.map((item) => item.target),
    });
    expect(fetch.mock.calls[0][0]).toContain("/v1/hosts/host%2Fid/startup-releases");
    expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual({
      current_host_generation: "current",
      targets: [target],
    });
    fetch.mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          current_host_generation: "current",
          targets: [
            { target: { ...target, attempt_id: null }, title: "Draft", work_hint: "unknown" },
          ],
        }),
      ),
    );
    await expect(getStartupReleasePreview("host")).rejects.toThrow();
  });
});
