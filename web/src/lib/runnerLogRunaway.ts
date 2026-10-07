// The session flag a host's runaway-log report becomes, and the warning copy
// the banner and the browser notification share.
//
// The host samples each live runner's log size and reports a runner that
// writes abnormally fast (a likely error loop). The server flags every
// session bound to that runner; the flag value is the report instant, so a
// fresh detection changes the value and the notification diff can fire once
// per new value. The warning shows while the host keeps confirming the
// report; dismissing it hides the band until the flag value changes.

export const RUNNER_LOG_RUNAWAY_LABEL_KEY = "omnigent.runner_log_runaway";
export const RUNNER_LOG_RUNAWAY_MB_LABEL_KEY = "omnigent.runner_log_runaway_mb";
export const RUNNER_LOG_RUNAWAY_SEEN_LABEL_KEY = "omnigent.runner_log_runaway_seen";
// Three host probes; the host re-confirms every 5 minutes while the runner stays over the cap.
export const RUNNER_LOG_RUNAWAY_LEASE_MS = 15 * 60 * 1000;

/**
 * Epoch ms at which a session's runaway warning lapses, or null when the
 * warning must not show at all: no flag, or no parseable server receive
 * instant (flags written before the lease existed carry none).
 *
 * @param labels - The session's labels, e.g. `{"omnigent.runner_log_runaway": "2026-…"}`.
 */
export function runnerLogRunawayLeaseEnd(
  labels: Record<string, string> | undefined,
): number | null {
  if (!labels?.[RUNNER_LOG_RUNAWAY_LABEL_KEY]) return null;
  const seen = labels[RUNNER_LOG_RUNAWAY_SEEN_LABEL_KEY];
  if (!seen) return null;
  const seenAt = Date.parse(seen);
  if (Number.isNaN(seenAt)) return null;
  return seenAt + RUNNER_LOG_RUNAWAY_LEASE_MS;
}

/**
 * Build the warning for a session's labels, or null when it carries no
 * runaway flag.
 *
 * @param labels - The session's labels, e.g. `{"omnigent.runner_log_runaway": "2026-…"}`.
 */
export function runnerLogRunawayNotice(labels: Record<string, string> | undefined): string | null {
  if (!labels?.[RUNNER_LOG_RUNAWAY_LABEL_KEY]) return null;
  const raw = labels[RUNNER_LOG_RUNAWAY_MB_LABEL_KEY];
  const parsed = raw !== undefined && raw !== "" ? Number(raw) : Number.NaN;
  const mb = Number.isFinite(parsed) ? parsed : null;
  const amount = mb === null ? "" : ` (${mb} MB in the last hour)`;
  return (
    `This session's runner is writing logs unusually fast${amount}` +
    " — it may be stuck in an error loop."
  );
}
