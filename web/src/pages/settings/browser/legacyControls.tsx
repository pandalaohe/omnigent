import { useState, useEffect, useMemo, useId, useCallback } from "react";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { SettingsLabel } from "@/components/SettingsLabel";
import { BrowserExtensionsControl } from "@/components/BrowserExtensionsControl";
import { browserLoginBridge } from "@/lib/nativeBridge";
import { readOpenLinksInApp, writeOpenLinksInApp } from "@/lib/linkOpenPreferences";
import { MOD_KEY } from "@/components/KeyboardShortcut";
/**
 * Where a plain click on a web link in chat content opens — desktop shells
 * with the embedded browser only. Off keeps the current behavior (the
 * default external browser); on routes the link into the conversation's
 * in-app browser tabs. Shift-click always stays external.
 */
export function OpenLinksInAppControl() {
  const [enabled, setEnabled] = useState(readOpenLinksInApp);
  const labelId = useId();
  const descriptionId = useId();
  const toggle = useCallback((next: boolean) => {
    setEnabled(next);
    writeOpenLinksInApp(next);
  }, []);

  return (
    <div className="flex items-start justify-between gap-6">
      <SettingsLabel
        label="Open links in the in-app browser"
        note="Keep web links beside the chat."
        labelId={labelId}
        descriptionId={descriptionId}
        className="flex-1"
        description={`Open chat links in-app, reusing matching tabs. ${MOD_KEY}+click also opens in-app; Shift+click opens your default browser.`}
      />
      <Switch
        aria-labelledby={labelId}
        aria-describedby={descriptionId}
        checked={enabled}
        onCheckedChange={toggle}
        data-testid="open-links-in-app-toggle"
        className="mt-0.5 shrink-0"
        componentId="settings.general.open_links_in_app"
      />
    </div>
  );
}

export function BrowserLoginControl() {
  const bridge = useMemo(browserLoginBridge, []);
  const [enabled, setEnabled] = useState(false);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState("");
  const [cleared, setCleared] = useState(false);
  const [approvedSites, setApprovedSites] = useState<
    { origin: string; id: string; label: string }[]
  >([]);
  useEffect(() => {
    if (!bridge?.fillApprovals || !bridge.revokeFillApproval) return;
    let mounted = true;
    void bridge
      .fillApprovals()
      .then((result) => {
        if (!mounted) return;
        if (result.ok) setApprovedSites(result.sites ?? []);
        else setError(result.error || "Could not read approved sites.");
      })
      .catch(() => {
        if (mounted) setError("Could not read approved sites.");
      });
    return () => {
      mounted = false;
    };
  }, [bridge]);
  const labelId = useId();
  const descriptionId = useId();

  useEffect(() => {
    if (!bridge) return;
    let mounted = true;
    void bridge
      .get()
      .then((result) => {
        if (!mounted) return;
        if (result.ok) setEnabled(result.enabled === true);
        else setError(result.error ?? "Could not read browser login setting.");
      })
      .catch(() => {
        if (mounted) setError("Could not read browser login setting.");
      })
      .finally(() => {
        if (mounted) setBusy(false);
      });
    return () => {
      mounted = false;
    };
  }, [bridge]);

  if (!bridge) return null;
  const change = async (next: boolean) => {
    setBusy(true);
    setError("");
    setCleared(false);
    try {
      const result = await bridge.set(next);
      if (result.ok) setEnabled(next);
      else setError(result.error ?? "Could not save browser login setting.");
    } catch {
      setError("Could not save browser login setting.");
    } finally {
      setBusy(false);
    }
  };
  const clear = async () => {
    setBusy(true);
    setError("");
    setCleared(false);
    try {
      const result = await bridge.clear();
      if (result.ok) setCleared(true);
      else setError(result.error ?? "Could not clear browser login.");
    } catch {
      setError("Could not clear browser login.");
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="space-y-3">
      <div className="flex items-start justify-between gap-6">
        <SettingsLabel
          label="Keep in-app browser login"
          note="Reuse this server's saved browser login."
          labelId={labelId}
          descriptionId={descriptionId}
          className="flex-1"
          description="On this desktop app, new Browser views share a login saved on disk across sessions. Each connected server has separate browser storage. Existing views keep their current storage until closed and reopened."
        />
        <Switch
          aria-labelledby={labelId}
          aria-describedby={descriptionId}
          checked={enabled}
          disabled={busy}
          onCheckedChange={(next) => void change(next)}
          componentId="settings.general.browser_login"
        />
      </div>
      <Button
        variant="outline"
        disabled={busy}
        onClick={() => void clear()}
        componentId="settings.general.clear_browser_login"
      >
        Clear browser login for this server
      </Button>
      <p className="text-sm text-muted-foreground">
        This closes open Browser views and Bitwarden windows using this server&apos;s saved login.
        Bitwarden&apos;s local data may also be cleared.
      </p>
      <BrowserExtensionsControl />
      {bridge.fillApprovals && bridge.revokeFillApproval && (
        <div className="space-y-2">
          <p className="text-sm font-medium">Approved sites</p>
          <p className="text-sm text-muted-foreground">
            Sessions may fill these saved logins without asking again on this desktop.
          </p>
          {approvedSites.length === 0 && (
            <p className="text-sm text-muted-foreground">
              No sites approved. A session asks the first time it fills a saved login.
            </p>
          )}
          {approvedSites.map((site) => (
            <div
              key={`${site.origin}:${site.id}`}
              className="flex items-center justify-between gap-3"
            >
              <span className="min-w-0 break-all text-sm">
                {site.origin} · {site.label}
              </span>
              <Button
                variant="outline"
                size="sm"
                disabled={busy}
                onClick={() => {
                  setBusy(true);
                  setError("");
                  void bridge.revokeFillApproval!(site)
                    .then((result) => {
                      if (result.ok)
                        setApprovedSites((rows) =>
                          rows.filter((row) => row.origin !== site.origin || row.id !== site.id),
                        );
                      else setError(result.error || "Could not revoke approval.");
                    })
                    .catch(() => setError("Could not revoke approval."))
                    .finally(() => setBusy(false));
                }}
              >
                Revoke
              </Button>
            </div>
          ))}
        </div>
      )}
      {cleared && (
        <p role="status" className="text-sm text-muted-foreground">
          Browser login cleared. Open Browser again to sign in.
        </p>
      )}
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
    </div>
  );
}
