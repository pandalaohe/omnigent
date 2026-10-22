import { useCallback, useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  getConnectedApps,
  revokeConnectedApp,
  type ConnectedApp,
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
  const [apps, setApps] = useState<ConnectedApp[] | null>(null);
  const [loadedScope, setLoadedScope] = useState<string | null>(null);
  const [code, setCode] = useState("");
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

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
    void refresh();
    window.addEventListener("focus", refresh);
    return () => {
      lifecycle.current += 1;
      window.removeEventListener("focus", refresh);
    };
  }, [refresh]);

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
    </section>
  );
}
