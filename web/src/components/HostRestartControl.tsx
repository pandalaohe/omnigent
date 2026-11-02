import { useEffect, useRef, useState } from "react";
import { RotateCwIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { useHostRestart, type HostRestartPhase } from "@/hooks/useHostRestart";
import type { Host } from "@/hooks/useHosts";

const PHASE_LABELS: Record<HostRestartPhase, string> = {
  draining: "Waiting for running turns to finish",
  stopping: "Stopping the host",
  waiting_reconnect: "Waiting for the host to reconnect",
  done: "Host restarted",
  failed: "Host restart failed",
};

function errorMessage(error: Error | null) {
  return error?.message ?? "Unable to reach the server.";
}

export function HostRestartControl({
  host,
  sessionTitles,
}: {
  host: Host;
  sessionTitles: Map<string, string>;
}) {
  const [open, setOpen] = useState(false);
  const [requestId, setRequestId] = useState<string | null>(null);
  const [expiresAt, setExpiresAt] = useState(0);
  const [expired, setExpired] = useState(false);
  const starting = useRef(false);
  const { preview, start, status } = useHostRestart(host.host_id, requestId);
  const canRestart =
    host.status === "online" &&
    host.supervised === true &&
    host.capabilities?.includes("host_restart");
  const name = host.name || host.host_id;
  const phase = status.data?.phase ?? start.data?.phase;
  const finished = phase === "done" || phase === "failed";

  useEffect(() => {
    if (!expiresAt) return;
    const timer = setTimeout(() => setExpired(true), Math.max(0, expiresAt - Date.now()));
    return () => clearTimeout(timer);
  }, [expiresAt]);

  function loadPreview() {
    // Start the TTL before the round trip so a slow preview cannot extend it.
    const deadline = Date.now() + 120_000;
    setExpired(false);
    setExpiresAt(0);
    start.reset();
    preview.mutate(undefined, { onSuccess: () => setExpiresAt(deadline) });
  }

  function changeOpen(next: boolean) {
    if (starting.current || start.isPending) return;
    setOpen(next);
    if (next && requestId === null) loadPreview();
  }

  if (!canRestart && requestId === null && !open) return null;

  return (
    <Dialog open={open} onOpenChange={changeOpen}>
      <DialogTrigger asChild>
        <Button variant="outline" size="sm" className="ml-auto">
          <RotateCwIcon className="size-3.5" aria-hidden="true" />
          {requestId ? "View restart" : "Restart"}
        </Button>
      </DialogTrigger>
      <DialogContent
        className="max-h-[85vh] overflow-y-auto sm:max-w-lg"
        showCloseButton={!start.isPending}
      >
        <DialogHeader>
          <DialogTitle>{requestId ? `Restart · ${name}` : `Restart ${name}?`}</DialogTitle>
          <DialogDescription>
            Restarting ends every runner and closes all terminals on this host, including host
            shells. Interrupted turns can resume when you send the next message.
          </DialogDescription>
        </DialogHeader>
        {requestId === null ? (
          <>
            {preview.isPending && <p role="status">Loading restart preview…</p>}
            {preview.isError && <p role="alert">{errorMessage(preview.error)}</p>}
            {preview.isSuccess && (
              <div className="space-y-3">
                <dl className="grid grid-cols-2 gap-2 rounded-lg bg-muted/50 p-3">
                  <dt className="text-muted-foreground">Supervisor</dt>
                  <dd className="break-words">{preview.data.supervisor}</dd>
                  <dt className="text-muted-foreground">Runners that will stop</dt>
                  <dd className="tabular-nums">{preview.data.runner_count}</dd>
                </dl>
                {preview.data.caller_on_host && (
                  <p role="alert" className="rounded-lg bg-destructive/10 p-3 text-destructive">
                    Your calling session runs on this host and will disconnect during the restart.
                  </p>
                )}
                <p className="font-medium">Running sessions</p>
                {preview.data.running_sessions.length === 0 ? (
                  <p className="text-muted-foreground">No turns are running.</p>
                ) : (
                  <ul className="max-h-40 space-y-1 overflow-y-auto">
                    {preview.data.running_sessions.map((id) => (
                      <li key={id} className="break-words">
                        {sessionTitles.get(id) ?? id}
                      </li>
                    ))}
                  </ul>
                )}
                <p className="text-sm text-muted-foreground">
                  Waits up to 2 minutes for running turns to finish before stopping.
                </p>
              </div>
            )}
            {expired && <p role="alert">Preview expired. Refresh it before restarting.</p>}
            {!canRestart && <p role="alert">This host is no longer available to restart.</p>}
            {start.isError && (
              <p role="alert">
                {errorMessage(start.error)} Check the host status before trying again.
              </p>
            )}
            <DialogFooter>
              <Button variant="ghost" disabled={start.isPending} onClick={() => changeOpen(false)}>
                Cancel
              </Button>
              {(expired || preview.isError || start.isError) && (
                <Button
                  variant="outline"
                  onClick={loadPreview}
                  disabled={preview.isPending || !canRestart}
                >
                  Refresh preview
                </Button>
              )}
              <Button
                variant="destructive"
                loading={start.isPending}
                disabled={
                  !canRestart || !preview.isSuccess || expired || !expiresAt || start.isError
                }
                onClick={() => {
                  if (starting.current) return;
                  if (!preview.data || Date.now() >= expiresAt) {
                    setExpired(true);
                    return;
                  }
                  // Mutation observers update later; guard the click synchronously.
                  starting.current = true;
                  start.mutate(preview.data.preview_id, {
                    onSuccess: (result) => setRequestId(result.request_id),
                    onSettled: () => {
                      starting.current = false;
                    },
                  });
                }}
              >
                Restart host
              </Button>
            </DialogFooter>
          </>
        ) : (
          <>
            <div role="status" aria-live="polite" className="space-y-3">
              <p className="font-medium">
                {phase ? PHASE_LABELS[phase] : "Checking restart status…"}
              </p>
              {phase === "draining" && (
                <p className="text-muted-foreground">This may take up to 2 minutes.</p>
              )}
              {status.data?.reason && <p>{status.data.reason}</p>}
              {status.data?.offline_seconds != null && (
                <p>Time offline: {status.data.offline_seconds} seconds</p>
              )}
              {status.data?.version && (
                <p className="break-words">Version: {status.data.version}</p>
              )}
              {!!status.data?.cut_sessions.length && (
                <div>
                  <p className="font-medium">Interrupted sessions</p>
                  <ul className="max-h-40 space-y-1 overflow-y-auto">
                    {status.data.cut_sessions.map((id) => (
                      <li key={id} className="break-words">
                        {sessionTitles.get(id) ?? id}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              {phase === "failed" && (
                <p className="text-muted-foreground">
                  Check the host and its supervisor on the machine.
                </p>
              )}
            </div>
            {status.isError && (
              <p role="alert">
                Could not check restart status: {errorMessage(status.error)}. Checking again…
              </p>
            )}
            <DialogFooter>
              <Button variant="outline" onClick={() => setOpen(false)}>
                Close
              </Button>
              {finished && canRestart && (
                <Button
                  variant="ghost"
                  onClick={() => {
                    setRequestId(null);
                    loadPreview();
                  }}
                >
                  New preview
                </Button>
              )}
            </DialogFooter>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}
