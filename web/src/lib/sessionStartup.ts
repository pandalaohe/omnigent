import type { SessionStartupStatus } from "./types";

const STATES = new Set([
  "queued",
  "starting",
  "verifying",
  "uncertain",
  "blocked",
  "cancelling",
  "ready_empty",
  "waiting_input",
  "working",
  "opened",
  "operator_released",
  "cancelled",
  "stopped",
  "command_completed",
]);

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function nullableString(value: unknown): value is string | null {
  return value === null || (typeof value === "string" && value.length > 0);
}

function nonnegativeInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}

function optionalClock(value: unknown): value is number | null | undefined {
  return value === undefined || value === null || nonnegativeInteger(value);
}

/** Unknown protocol states and missing proof are not interpreted as a ready session. */
export function sessionStartupFromWire(value: unknown): SessionStartupStatus | null {
  const raw = record(value);
  const evidence = record(raw?.evidence);
  if (
    !raw ||
    !evidence ||
    typeof raw.id !== "string" ||
    !raw.id ||
    (raw.host_id !== undefined && !nullableString(raw.host_id)) ||
    typeof raw.state !== "string" ||
    !STATES.has(raw.state) ||
    !nullableString(raw.attempt_id) ||
    !nullableString(raw.input_id) ||
    !nullableString(raw.reason) ||
    typeof raw.can_cancel !== "boolean" ||
    typeof raw.elapsed_seconds !== "number" ||
    !Number.isFinite(raw.elapsed_seconds) ||
    raw.elapsed_seconds < 0 ||
    !optionalClock(raw.admitted_elapsed_seconds) ||
    !optionalClock(raw.input_elapsed_seconds) ||
    (raw.unconfirmed_startups !== undefined && !nonnegativeInteger(raw.unconfirmed_startups)) ||
    (raw.queue_position_estimate !== null &&
      (typeof raw.queue_position_estimate !== "number" ||
        !Number.isInteger(raw.queue_position_estimate) ||
        raw.queue_position_estimate < 1)) ||
    ["runtime_ready", "tools_ready", "input_accepted", "work_started"].some(
      (key) => typeof evidence[key] !== "boolean",
    )
  )
    return null;
  return {
    id: raw.id,
    hostId: raw.host_id ?? null,
    attemptId: raw.attempt_id,
    inputId: raw.input_id,
    state: raw.state as SessionStartupStatus["state"],
    reason: raw.reason,
    elapsedSeconds: raw.elapsed_seconds,
    admittedElapsedSeconds: raw.admitted_elapsed_seconds ?? null,
    inputElapsedSeconds: raw.input_elapsed_seconds ?? null,
    unconfirmedStartups: raw.unconfirmed_startups ?? 0,
    queuePositionEstimate: raw.queue_position_estimate as number | null,
    canCancel: raw.can_cancel,
    evidence: {
      runtimeReady: evidence.runtime_ready as boolean,
      toolsReady: evidence.tools_ready as boolean,
      inputAccepted: evidence.input_accepted as boolean,
      workStarted: evidence.work_started as boolean,
    },
  };
}
