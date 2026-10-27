import { useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  ApiError,
  getStartupReleasePreview,
  releaseStartupSlots,
  type StartupReleasePreview,
} from "@/lib/sessionsApi";

/** The confirmation keeps the exact preview even if another opening starts meanwhile. */
export function StartupReleaseDialog({
  hostId,
  onReleased,
}: {
  hostId: string;
  onReleased: () => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [preview, setPreview] = useState<StartupReleasePreview | null>(null);
  const [loading, setLoading] = useState(false);
  const [releasing, setReleasing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<string | null>(null);
  const inFlight = useRef(false);

  async function load() {
    setLoading(true);
    setPreview(null);
    setError(null);
    try {
      setPreview(await getStartupReleasePreview(hostId));
    } catch (cause) {
      setError(
        cause instanceof ApiError && cause.status === 403
          ? "Only the host owner can release startup slots for sessions they own."
          : "Could not load stuck starts. Refresh the list to try again.",
      );
    } finally {
      setLoading(false);
    }
  }

  async function release() {
    if (inFlight.current || !preview?.current_host_generation || !preview.targets.length) return;
    inFlight.current = true;
    setReleasing(true);
    setError(null);
    try {
      await releaseStartupSlots(hostId, {
        current_host_generation: preview.current_host_generation,
        targets: preview.targets.map((item) => item.target),
      });
      setOpen(false);
      setResult(
        "Startup slots released. Old work may still be running. Saved input was not resent.",
      );
      try {
        await onReleased();
      } catch {
        setResult(
          "Startup slots released. Refresh the session to see queue progress. Saved input was not resent; old work may still be running.",
        );
      }
    } catch (cause) {
      setError(
        cause instanceof ApiError && (cause.status === 409 || cause.status === 403)
          ? "The host, session ownership, or startup changed. Refresh the list and review it again."
          : "Release could not be confirmed. Refresh the list before trying again.",
      );
      setPreview(null);
    } finally {
      inFlight.current = false;
      setReleasing(false);
    }
  }

  return (
    <>
      <Button
        variant="outline"
        size="sm"
        onClick={() => {
          setOpen(true);
          void load();
        }}
      >
        Review stuck starts
      </Button>
      {result && <p role="status">{result}</p>}
      <Dialog
        open={open}
        onOpenChange={(value) => {
          if (!releasing) setOpen(value);
        }}
      >
        <DialogContent
          className="max-h-[85vh] overflow-y-auto sm:max-w-lg"
          showCloseButton={!releasing}
        >
          <DialogHeader>
            <DialogTitle>Release stuck startup slots?</DialogTitle>
            <DialogDescription>
              Release lets other queued sessions start on this host. It does not stop old work, can
              allow more concurrent starts than the limit, and does not resend saved input.
            </DialogDescription>
          </DialogHeader>
          {loading && <p role="status">Checking stuck starts…</p>}
          {preview && (
            <>
              <p className="text-muted-foreground">Only these sessions you own will be released:</p>
              <ul className="space-y-3">
                {preview.targets.map(({ target, title, work_hint }) => (
                  <li key={target.startup_id} className="space-y-1 rounded-xl bg-muted/50 p-3">
                    <p className="break-words font-medium">{title || "Untitled session"}</p>
                    <p
                      title={target.session_id}
                      className="font-mono text-xs text-muted-foreground"
                    >
                      {target.session_id.slice(0, 8)}
                    </p>
                    <p className="text-muted-foreground">
                      {work_hint === "working"
                        ? "Work was observed; it may still be running."
                        : "Work status unknown; old work may still be running."}
                    </p>
                  </li>
                ))}
              </ul>
              {!preview.targets.length && <p>No stuck starts you can release right now.</p>}
              <p className="text-muted-foreground">
                Work observations are hints only. They do not decide whether a startup slot can be
                released.
              </p>
            </>
          )}
          {error && <p role="alert">{error}</p>}
          <DialogFooter>
            <Button variant="ghost" disabled={releasing} onClick={() => setOpen(false)}>
              Keep waiting
            </Button>
            <Button variant="outline" disabled={loading || releasing} onClick={() => void load()}>
              Refresh list
            </Button>
            <Button
              disabled={
                loading || releasing || !preview?.current_host_generation || !preview.targets.length
              }
              onClick={() => void release()}
            >
              {releasing ? "Releasing…" : "Release startup slots"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
