import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";
import { useNavigate, useLocation } from "@/lib/routing";
import { SettingsLabel } from "@/components/SettingsLabel";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { Segmented } from "@/components/ui/segmented";
import { browserLoginBridge, browserSettingsBridge } from "@/lib/nativeBridge";
import { BrowserGroup, Confirm, UpdateReason } from "./browserSettingsParts";

export function SignInTab() {
  const bridge = useMemo(browserLoginBridge, []),
    settings = useMemo(browserSettingsBridge, []),
    navigate = useNavigate(),
    location = useLocation();
  const [enabled, setEnabled] = useState(false),
    [busy, setBusy] = useState(false),
    [ready, setReady] = useState(false),
    [confirm, setConfirm] = useState(false),
    [status, setStatus] = useState(""),
    [localStatus, setLocalStatus] = useState(""),
    [sites, setSites] = useState<{ origin: string; id: string; label: string }[]>([]),
    [local, setLocal] = useState<{ origin: string; allowed: boolean }[]>([]),
    [bitwarden, setBitwarden] = useState<boolean | null>(null);
  useEffect(() => {
    void bridge
      ?.get()
      .then((result) => {
        if (result.ok) {
          setEnabled(result.enabled === true);
          setReady(true);
        } else setStatus("Could not read browser sign-in setting.");
      })
      .catch(() => setStatus("Could not read browser sign-in setting."));
    void bridge
      ?.fillApprovals?.()
      .then((result) => {
        if (result.ok) setSites(result.sites ?? []);
        else setStatus("Could not read approved sites.");
      })
      .catch(() => setStatus("Could not read approved sites."));
    void bridge
      ?.extensionsList?.()
      .then((result) => {
        if (result.ok)
          setBitwarden(result.extensions?.some((row) => /\bbitwarden\b/i.test(row.name)) ?? false);
      })
      .catch(() => setStatus("Could not read extensions."));
    void settings
      ?.browserLocalNetworkList?.()
      .then((result) => {
        if (result.ok) setLocal(result.sites ?? []);
        else setLocalStatus(result.error ?? "Could not read local network choices.");
      })
      .catch(() => setLocalStatus("Could not read local network choices."));
  }, [bridge, settings]);
  async function change(next: boolean) {
    if (!bridge) return;
    setBusy(true);
    try {
      const result = await bridge.set(next);
      if (result.ok) {
        setEnabled(next);
        setStatus("Saved");
      } else setStatus("Could not save browser sign-in setting.");
    } catch {
      setStatus("Could not save browser sign-in setting.");
    } finally {
      setBusy(false);
    }
  }
  async function signout() {
    if (!bridge) return;
    setBusy(true);
    try {
      const result = await bridge.clear();
      if (result.ok) {
        setConfirm(false);
        setSites([]);
        setStatus("Signed out. Open Browser again to sign in.");
      } else setStatus("Could not sign out.");
    } catch {
      setStatus("Could not sign out.");
    } finally {
      setBusy(false);
    }
  }
  async function choice(origin: string, allowed: boolean | null) {
    if (!settings?.browserLocalNetworkSet) return;
    setBusy(true);
    try {
      const result = await settings.browserLocalNetworkSet({ origin, allowed });
      if (result.ok) {
        setLocal(result.sites ?? []);
        setLocalStatus("Saved");
        if (allowed === null) toast(`Forgot the choice for ${origin}. It will ask again.`);
      } else
        setLocalStatus(
          result.cancelled
            ? "Change cancelled."
            : (result.error ?? "Could not update local network choices."),
        );
    } catch {
      setLocalStatus("Could not update local network choices.");
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <BrowserGroup title="Stay signed in" server>
        <div className="flex items-start justify-between gap-4">
          <SettingsLabel
            label="Keep in-app browser sign-ins"
            note={
              enabled
                ? "Reuse this server’s sign-ins across sessions."
                : "Each new session starts with separate sign-ins."
            }
            description="New Browser views share sign-ins saved on this desktop. Each connected server has separate storage. Existing views keep their storage until closed and reopened."
          />
          <Switch
            aria-label="Keep in-app browser sign-ins"
            checked={enabled}
            disabled={!ready || busy}
            onCheckedChange={(next) => void change(next)}
          />
        </div>
        {!bridge && <UpdateReason verb="manage browser sign-ins" />}
        <div className="mt-4 flex items-center justify-between gap-4 border-t pt-4">
          <SettingsLabel
            label="Sign out of all sites"
            note="Clears this server’s saved sign-ins on this desktop."
          />
          <Button variant="destructive" disabled={!ready || busy} onClick={() => setConfirm(true)}>
            Sign out…
          </Button>
        </div>
        {status && (
          <p role="status" className="mt-3 text-sm">
            {status}
          </p>
        )}
      </BrowserGroup>
      <BrowserGroup title="Saved login autofill" server>
        {bitwarden === false && (
          <div className="mb-4 flex flex-wrap items-center gap-3">
            <p className="text-sm text-muted-foreground">
              Add the Bitwarden extension to fill saved logins.
            </p>
            <Button
              variant="outline"
              onClick={() => {
                const params = new URLSearchParams(location.search);
                params.set("tab", "extensions");
                navigate(`${location.pathname}?${params}${location.hash}`);
              }}
            >
              Go to Extensions
            </Button>
          </div>
        )}
        {!bridge?.fillApprovals || !bridge.revokeFillApproval ? (
          <UpdateReason verb="manage saved login approvals" />
        ) : (
          <div className="divide-y">
            {!sites.length && (
              <p className="text-sm text-muted-foreground">
                No sites approved. A session asks the first time it fills a saved login.
              </p>
            )}
            {sites.map((site) => (
              <div
                key={`${site.origin}:${site.id}`}
                className="flex items-center justify-between gap-3 py-3 first:pt-0 last:pb-0"
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
                    void bridge.revokeFillApproval!(site)
                      .then((result) => {
                        if (result.ok) {
                          setSites((previous) =>
                            previous.filter(
                              (row) => row.origin !== site.origin || row.id !== site.id,
                            ),
                          );
                          toast(`Revoked for ${site.origin}. Sessions will ask again.`);
                        } else setStatus("Could not revoke approval.");
                      })
                      .catch(() => setStatus("Could not revoke approval."))
                      .finally(() => setBusy(false));
                  }}
                >
                  Revoke
                </Button>
              </div>
            ))}
          </div>
        )}
      </BrowserGroup>
      <BrowserGroup title="Local network access">
        {!settings?.browserLocalNetworkList || !settings.browserLocalNetworkSet ? (
          <UpdateReason verb="manage local network choices" />
        ) : !local.length ? (
          <p className="text-sm text-muted-foreground">
            No saved choices. Sites will ask before reaching your local network.
          </p>
        ) : (
          <div className="divide-y">
            {local.map((site) => (
              <div
                key={site.origin}
                className="flex flex-wrap items-center justify-between gap-3 py-3 first:pt-0 last:pb-0"
              >
                <span className="min-w-0 break-all text-sm">{site.origin}</span>
                <fieldset disabled={busy} className="flex items-center gap-2">
                  <Segmented
                    label={`Local network for ${site.origin}`}
                    value={site.allowed ? "allow" : "block"}
                    options={[
                      { value: "allow", label: "Allow" },
                      { value: "block", label: "Block" },
                    ]}
                    onChange={(value) => void choice(site.origin, value === "allow")}
                  />
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => void choice(site.origin, null)}
                  >
                    Forget
                  </Button>
                </fieldset>
              </div>
            ))}
          </div>
        )}
        {localStatus && (
          <p role="status" className="mt-3 text-sm">
            {localStatus}
          </p>
        )}
      </BrowserGroup>
      <Confirm
        title="Sign out of all sites?"
        description="Open Browser tabs using these sign-ins and Bitwarden windows will close. Bitwarden’s local data may be cleared. You’ll need to sign in again."
        action="Sign out and clear"
        open={confirm}
        busy={busy}
        onClose={() => setConfirm(false)}
        onConfirm={() => void signout()}
      />
    </>
  );
}
