import { Clock3Icon } from "lucide-react";
import { useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import type { SessionStartupStatus } from "@/lib/types";
import { StartupReleaseDialog } from "./StartupReleaseDialog";

const LABELS: Partial<Record<SessionStartupStatus["state"], string>> = {
  queued: "Waiting to start on this host",
  starting: "Opening session",
  verifying: "Confirming session opened",
  uncertain: "Startup is stuck — opening not confirmed",
  blocked: "Startup needs attention",
  cancelling: "Stopping startup",
  ready_empty: "Session ready — waiting for your first message",
  waiting_input: "Waiting for your first message",
  opened: "Session opened",
  operator_released: "Startup slot released",
};

const REASONS: Record<string, string> = {
  needs_update: "Update this host to support queued startup.",
  host_offline: "Waiting for the host to reconnect.",
  parent_not_ready: "Waiting for the parent session to become ready.",
  resident_capacity: "Waiting for session capacity on this host.",
  sign_in_required: "Sign in to the provider to continue.",
  needs_attention: "This request has waited 24 hours and needs attention.",
  runtime_observation_pending: "Waiting to confirm that the host is ready.",
  input_observation_pending: "Waiting to confirm work after your first message.",
  runtime_readiness_unconfirmed: "Host readiness could not be confirmed.",
  input_start_unconfirmed: "Work after your first message could not be confirmed.",
  host_launch_unconfirmed: "Host startup could not be confirmed.",
  input_dispatch_unconfirmed: "Delivery of your first message could not be confirmed.",
  admission_origin_unknown: "Host startup time could not be confirmed.",
  opening_unconfirmed:
    "Opening has not been confirmed after 180 seconds. Nothing is released automatically.",
  delivery_unconfirmed:
    "Message delivery is unconfirmed. Saved input will not be sent again automatically.",
};

/** Keep an unconfirmed startup distinct from a positively observed working turn. */
export function SessionStartupIndicator({
  startup,
  onCancel,
  readOnly = false,
  hostId,
  onReleased,
}: {
  startup: SessionStartupStatus;
  onCancel: (id: string, attemptId: string | null) => Promise<void>;
  readOnly?: boolean;
  hostId?: string | null;
  onReleased?: () => Promise<void>;
}) {
  const [stopping, setStopping] = useState(false);
  const [stopFailed, setStopFailed] = useState(false);
  const inFlight = useRef(false);
  const label = LABELS[startup.state];
  if (label === undefined) return null;
  const admissionTakingLonger =
    (startup.state === "starting" || startup.state === "verifying") &&
    startup.admittedElapsedSeconds != null &&
    startup.admittedElapsedSeconds >= 30;

  async function stop() {
    if (inFlight.current) return;
    inFlight.current = true;
    setStopping(true);
    setStopFailed(false);
    try {
      await onCancel(startup.id, startup.attemptId);
    } catch {
      setStopFailed(true);
    } finally {
      inFlight.current = false;
      setStopping(false);
    }
  }

  return (
    <div role="status" aria-live="polite" className="space-y-2 py-2 text-ui text-muted-foreground">
      <div className="flex flex-wrap items-center gap-2">
        <Clock3Icon className="size-4 shrink-0" aria-hidden />
        <span>{label}</span>
        {startup.queuePositionEstimate !== null && startup.state === "queued" && (
          <span>About {startup.queuePositionEstimate} in queue</span>
        )}
        <span>
          {startup.state === "queued" ? "Waiting " : ""}
          {Math.floor(startup.elapsedSeconds)}s since request
        </span>
        {startup.admittedElapsedSeconds != null && (
          <span>{startup.admittedElapsedSeconds}s since opening began</span>
        )}
        {startup.inputElapsedSeconds != null && (
          <span>{startup.inputElapsedSeconds}s since first message</span>
        )}
        {!readOnly && startup.canCancel && startup.state !== "cancelling" && (
          <Button type="button" variant="ghost" size="sm" disabled={stopping} onClick={stop}>
            {stopping ? "Stopping…" : "Stop startup"}
          </Button>
        )}
      </div>
      {startup.inputId !== null && <p>Your message is saved.</p>}
      {admissionTakingLonger && (
        <p>
          Startup is taking longer than expected.
          {startup.inputId !== null && " Your message remains saved."}
        </p>
      )}
      {startup.state === "queued" && (startup.unconfirmedStartups ?? 0) >= 2 && (
        <p>
          Earlier starts are stuck. Keep waiting or review their startup slots to let queued
          sessions start.
        </p>
      )}
      {startup.state === "operator_released" && (
        <p>
          Other queued sessions can start. Old work may still be running; releasing did not stop it
          or resend input.
        </p>
      )}
      {startup.reason !== null && REASONS[startup.reason] && <p>{REASONS[startup.reason]}</p>}
      {!readOnly &&
        (startup.hostId || hostId) &&
        onReleased &&
        (startup.state === "uncertain" ||
          (startup.state === "queued" && (startup.unconfirmedStartups ?? 0) > 0)) && (
          <StartupReleaseDialog
            key={startup.hostId || hostId}
            hostId={(startup.hostId || hostId)!}
            onReleased={onReleased}
          />
        )}
      {stopFailed && <p>Stop could not be confirmed. Try again; startup remains tracked.</p>}
      <details>
        <summary className="cursor-pointer">Inspect startup</summary>
        <ul className="mt-1 space-y-1">
          <li>Runtime ready: {startup.evidence.runtimeReady ? "yes" : "no"}</li>
          <li>Required tools ready: {startup.evidence.toolsReady ? "yes" : "no"}</li>
          <li>First message recorded: {startup.evidence.inputAccepted ? "yes" : "no"}</li>
          <li>Work observed: {startup.evidence.workStarted ? "yes" : "no"}</li>
        </ul>
      </details>
    </div>
  );
}
