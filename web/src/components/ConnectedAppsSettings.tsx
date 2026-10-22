import { useCallback, useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  getConnectedApps,
  getOldAnnotationReceipt,
  revokeConnectedApp,
  type ConnectedApp,
  type OldAnnotationReceipt,
} from "@/lib/browserAnnotationsApi";
import { withBasePath } from "@/lib/basePath";
import { useViewerId } from "@/hooks/useViewerId";
import { getCurrentUserId } from "@/lib/identity";
import { getOmnigentHostGeneration, getOmnigentServerIdentity } from "@/lib/host";

function currentScope(): string {
  return JSON.stringify([
    getOmnigentServerIdentity(),
    getOmnigentHostGeneration(),
    getCurrentUserId(),
  ]);
}

function dateLabel(epoch: number | null): string {
  return epoch === null ? "—" : new Date(epoch * 1000).toLocaleDateString();
}

function receiptMessage(receipt: OldAnnotationReceipt): { title: string; detail: string } {
  if (receipt.state === "not_admitted") {
    return {
      title: "Not admitted",
      detail:
        "The original grant is closed and this batch was not recorded. You can explicitly confirm migration of this batch in the extension.",
    };
  }
  if (receipt.state === "unknown_record") {
    return {
      title: "Still unconfirmed",
      detail:
        "No record is available yet. The original authorization may still accept this batch. This result does not establish that migration or resending is safe.",
    };
  }
  if (
    receipt.state === "delivery_unknown" ||
    receipt.dispatch_phase === "dispatching" ||
    receipt.dispatch_phase === "uncertain"
  ) {
    return {
      title: "Delivery unknown",
      detail:
        "The batch was saved, but delivery could not be confirmed. Check the receiving session before retrying. This does not confirm that the agent read it.",
    };
  }
  if (receipt.dispatch_phase === "ready" && receipt.retry_required) {
    return {
      title: "Delivery paused",
      detail:
        "The batch was saved in the receiving session. Delivery is paused and requires an explicit retry in the extension.",
    };
  }
  return {
    title: "Batch accepted",
    detail:
      receipt.dispatch_phase === "confirmed"
        ? "The batch was saved and delivery was confirmed. This does not confirm that the agent read it."
        : "The batch was saved in the receiving session; delivery has not been confirmed.",
  };
}

export function ConnectedAppsSettings({
  pairingAvailable = false,
}: {
  pairingAvailable?: boolean;
}) {
  useViewerId();
  const scope = currentScope();
  const scopeRef = useRef(scope);
  scopeRef.current = scope;
  const lifecycle = useRef(0);
  const loadSequence = useRef(0);
  const receiptSequence = useRef(0);
  const [apps, setApps] = useState<ConnectedApp[] | null>(null);
  const [loadedScope, setLoadedScope] = useState<string | null>(null);
  const [code, setCode] = useState("");
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [receiptScope, setReceiptScope] = useState(scope);
  const [oldGrantId, setOldGrantId] = useState("");
  const [batchId, setBatchId] = useState("");
  const [checking, setChecking] = useState(false);
  const [receiptError, setReceiptError] = useState<string | null>(null);
  const [checkedBatch, setCheckedBatch] = useState<{
    grantId: string;
    batchId: string;
    receipt: OldAnnotationReceipt;
  } | null>(null);

  const refresh = useCallback(async () => {
    const generation = lifecycle.current;
    const current = () =>
      lifecycle.current === generation && scopeRef.current === scope && currentScope() === scope;
    if (!current() || getCurrentUserId() === null) return;
    const sequence = ++loadSequence.current;
    try {
      const data = await getConnectedApps();
      if (current() && sequence === loadSequence.current) {
        setApps(data);
        setLoadedScope(scope);
        setError(null);
      }
    } catch {
      if (current() && sequence === loadSequence.current) {
        setApps(null);
        setLoadedScope(null);
        setError("Connected apps could not be loaded.");
      }
    }
  }, [scope]);

  useEffect(() => {
    lifecycle.current += 1;
    setApps(null);
    setLoadedScope(null);
    setBusyId(null);
    setCode("");
    setError(null);
    receiptSequence.current += 1;
    setReceiptScope(scope);
    setOldGrantId("");
    setBatchId("");
    setChecking(false);
    setReceiptError(null);
    setCheckedBatch(null);
    void refresh();
    window.addEventListener("focus", refresh);
    return () => {
      lifecycle.current += 1;
      window.removeEventListener("focus", refresh);
    };
  }, [refresh, scope]);

  const revoke = async (grantId: string) => {
    const generation = lifecycle.current;
    const current = () =>
      lifecycle.current === generation && scopeRef.current === scope && currentScope() === scope;
    if (!current() || loadedScope !== scope || getCurrentUserId() === null) return;
    setBusyId(grantId);
    setError(null);
    try {
      await revokeConnectedApp(grantId);
      if (!current()) return;
      await refresh();
    } catch {
      if (current()) setError("Could not revoke this app. Try again.");
    } finally {
      if (current()) setBusyId(null);
    }
  };

  const visibleApps = loadedScope === scope ? apps : null;
  const visibleBatch = receiptScope === scope ? checkedBatch : null;
  const message = visibleBatch ? receiptMessage(visibleBatch.receipt) : null;

  const invalidateReceiptCheck = () => {
    receiptSequence.current += 1;
    setChecking(false);
    setReceiptError(null);
    setCheckedBatch(null);
  };

  const checkReceipt = async () => {
    const generation = lifecycle.current;
    const sequence = ++receiptSequence.current;
    const current = () =>
      lifecycle.current === generation &&
      scopeRef.current === scope &&
      currentScope() === scope &&
      receiptSequence.current === sequence;
    if (!current() || receiptScope !== scope || getCurrentUserId() === null) return;
    const grant = oldGrantId.trim();
    const batch = batchId.trim();
    setCheckedBatch(null);
    setReceiptError(null);
    if (!/^[A-Za-z0-9_-]{1,128}$/.test(grant) || !/^[0-9a-f]{32}$/.test(batch)) {
      setReceiptError("Enter a valid original grant ID and a 32-character lowercase batch ID.");
      setChecking(false);
      return;
    }
    setChecking(true);
    try {
      const receipt = await getOldAnnotationReceipt(grant, batch);
      if (current()) setCheckedBatch({ grantId: grant, batchId: batch, receipt });
    } catch {
      if (current()) {
        setReceiptError("This batch could not be checked. Verify the IDs and try again.");
      }
    } finally {
      if (current()) setChecking(false);
    }
  };

  return (
    <section aria-labelledby="connected-apps-heading" className="border-t border-border pt-5">
      <h2 id="connected-apps-heading" className="text-sm font-medium">
        Connected apps
      </h2>
      <p className="mt-1 text-sm text-muted-foreground">
        Review access you gave to browser extensions and other apps.
      </p>
      {pairingAvailable && (
        <form
          className="mt-4 flex flex-wrap items-end gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            const userCode = code.trim();
            if (userCode) {
              window.location.assign(
                withBasePath(`/oauth/device?user_code=${encodeURIComponent(userCode)}`),
              );
            }
          }}
        >
          <label htmlFor="browser-pairing-code" className="flex flex-col gap-1 text-sm">
            Browser extension code
            <Input
              id="browser-pairing-code"
              value={code}
              onChange={(event) => setCode(event.target.value)}
              maxLength={32}
              autoComplete="off"
              placeholder="Enter code from Chrome"
            />
          </label>
          <Button type="submit" variant="outline" disabled={!code.trim()}>
            Connect browser extension
          </Button>
        </form>
      )}
      {pairingAvailable && (
        <p className="mt-2 text-xs text-muted-foreground">
          Confirm the browser label and code on the next page before approving. Browser access can
          list editable sessions, add screenshots, and send annotation batches.
        </p>
      )}
      {error && (
        <p role="alert" className="mt-3 text-sm text-destructive">
          {error}
        </p>
      )}
      {visibleApps === null ? (
        <p className="mt-4 text-sm text-muted-foreground">Loading connected apps…</p>
      ) : visibleApps.length === 0 ? (
        <p className="mt-4 text-sm text-muted-foreground">No connected apps.</p>
      ) : (
        <ul className="mt-4 divide-y divide-border border-y border-border">
          {visibleApps.map((app) => (
            <li
              key={app.grant_id}
              className="flex flex-wrap items-center justify-between gap-3 py-3"
            >
              <div className="min-w-0 text-sm">
                <div className="font-medium">{app.label || app.client_name}</div>
                <div className="text-muted-foreground">
                  {app.scope === "annotations:write" ? "Web annotations" : app.scope} · Connected{" "}
                  {dateLabel(app.created_at)} · Expires {dateLabel(app.expires_at)}
                </div>
              </div>
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={busyId === app.grant_id}
                aria-label={`Revoke ${app.label || app.client_name}`}
                onClick={() => void revoke(app.grant_id)}
              >
                Revoke
              </Button>
            </li>
          ))}
        </ul>
      )}
      {pairingAvailable && (
        <div className="mt-5 border-t border-border pt-5">
          <h3 className="text-sm font-medium">Check an old annotation batch</h3>
          <p id="old-annotation-check-help" className="mt-1 text-sm text-muted-foreground">
            Enter the original grant ID and batch ID shown by the extension, including after access
            was revoked or expired. This checks delivery metadata only.
          </p>
          <form
            className="mt-3 flex flex-wrap items-end gap-2"
            onSubmit={(event) => {
              event.preventDefault();
              void checkReceipt();
            }}
          >
            <label htmlFor="old-annotation-grant" className="flex min-w-0 flex-col gap-1 text-sm">
              Original grant ID
              <Input
                id="old-annotation-grant"
                value={receiptScope === scope ? oldGrantId : ""}
                onChange={(event) => {
                  invalidateReceiptCheck();
                  setOldGrantId(event.target.value);
                }}
                maxLength={128}
                autoComplete="off"
                spellCheck={false}
                aria-describedby="old-annotation-check-help"
                className="font-mono"
              />
            </label>
            <label htmlFor="old-annotation-batch" className="flex min-w-0 flex-col gap-1 text-sm">
              Batch ID
              <Input
                id="old-annotation-batch"
                value={receiptScope === scope ? batchId : ""}
                onChange={(event) => {
                  invalidateReceiptCheck();
                  setBatchId(event.target.value);
                }}
                maxLength={32}
                autoComplete="off"
                spellCheck={false}
                aria-describedby="old-annotation-check-help"
                className="font-mono"
              />
            </label>
            <Button
              type="submit"
              variant="outline"
              disabled={(receiptScope === scope && checking) || getCurrentUserId() === null}
            >
              {receiptScope === scope && checking ? "Checking…" : "Check batch"}
            </Button>
          </form>
          {receiptScope === scope && receiptError && (
            <p role="alert" className="mt-3 text-sm text-destructive">
              {receiptError}
            </p>
          )}
          {visibleBatch && message && (
            <div role="status" className="mt-3 space-y-2 text-sm">
              <p className="font-medium">{message.title}</p>
              <p className="text-muted-foreground">{message.detail}</p>
              <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1">
                <dt>Grant ID</dt>
                <dd className="break-all font-mono">{visibleBatch.grantId}</dd>
                <dt>Batch ID</dt>
                <dd className="break-all font-mono">{visibleBatch.batchId}</dd>
                {"session_id" in visibleBatch.receipt && (
                  <>
                    <dt>Receiving session</dt>
                    <dd className="min-w-0">
                      <a
                        className="break-all text-primary underline underline-offset-4"
                        href={withBasePath(
                          `/c/${encodeURIComponent(visibleBatch.receipt.session_id)}`,
                        )}
                      >
                        {visibleBatch.receipt.session_id}
                      </a>
                    </dd>
                    <dt>Accepted</dt>
                    <dd>{new Date(visibleBatch.receipt.accepted_at * 1000).toLocaleString()}</dd>
                  </>
                )}
              </dl>
            </div>
          )}
        </div>
      )}
    </section>
  );
}
