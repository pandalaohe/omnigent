import { useEffect, useMemo, useState } from "react";
import { XIcon } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Segmented } from "@/components/ui/segmented";
import { SettingsLabel } from "@/components/SettingsLabel";
import { browserAccessBridge, browserSettingsBridge, type BrowserAccess } from "@/lib/nativeBridge";
import { BrowserGroup, UpdateReason } from "./browserSettingsParts";
import { OpenLinksInAppControl } from "./OpenLinksInAppControl";
import { validAddressEntry } from "./browserAddressEntry";
import text from "@/lib/locales/browserAccess.en.json";

const keys = ["blocked", "ask", "allowed"] as const;
export function AccessTab() {
  const bridge = useMemo(browserAccessBridge, []),
    settings = useMemo(browserSettingsBridge, []);
  const [access, setAccess] = useState<BrowserAccess | null>(null),
    [saved, setSaved] = useState<BrowserAccess | null>(null),
    [inputs, setInputs] = useState({ blocked: "", ask: "", allowed: "" }),
    [errors, setErrors] = useState({ blocked: "", ask: "", allowed: "" }),
    [busy, setBusy] = useState(false),
    [message, setMessage] = useState(""),
    [servers, setServers] = useState<{ origin: string; current: boolean; entries: string[] }[]>([]);
  useEffect(() => {
    void bridge
      ?.get()
      .then((result) => {
        if (result.ok && result.access) {
          setAccess(result.access);
          setSaved(result.access);
        } else setMessage(text.loadFailed);
      })
      .catch(() => setMessage(text.loadFailed));
    void settings
      ?.browserRememberedList?.()
      .then((result) => {
        if (result.ok) setServers(result.servers ?? []);
        else setMessage(result.error ?? "Could not read remembered addresses.");
      })
      .catch(() => setMessage("Could not read remembered addresses."));
  }, [bridge, settings]);
  const changes =
    access && saved
      ? Number(access.level !== saved.level) +
        keys.reduce(
          (n, key) =>
            n +
            access[key].filter((value) => !saved[key].includes(value)).length +
            saved[key].filter((value) => !access[key].includes(value)).length,
          0,
        )
      : 0;
  function add(key: (typeof keys)[number]) {
    if (!access) return;
    const value = inputs[key].trim();
    let error = "";
    if (!validAddressEntry(value)) error = "Use host, host:port, IP, IPv4 CIDR or *.domain.";
    else if (keys.some((kind) => access[kind].includes(value)))
      error = "This address is already listed.";
    setErrors((previous) => ({ ...previous, [key]: error }));
    if (error) return;
    setAccess({ ...access, [key]: [...access[key], value] });
    setInputs({ ...inputs, [key]: "" });
    setMessage("");
  }
  async function save() {
    if (!access || !bridge) return;
    setBusy(true);
    try {
      const result = await bridge.set(access);
      if (result.ok) {
        const next = result.access ?? access;
        setSaved(next);
        setAccess(next);
        setMessage(text.saved);
      } else setMessage(result.error ?? text.failed);
    } catch {
      setMessage(text.failed);
    } finally {
      setBusy(false);
    }
  }
  async function forget(origin: string, entry: string) {
    if (!settings?.browserRememberedForget) return;
    setBusy(true);
    try {
      const result = await settings.browserRememberedForget({ origin, entry });
      if (result.ok) {
        setServers((previous) =>
          previous
            .map((server) =>
              server.origin === origin
                ? { ...server, entries: server.entries.filter((value) => value !== entry) }
                : server,
            )
            .filter((server) => server.entries.length),
        );
        toast(`Forgot ${entry}. The agent will ask again.`);
        setMessage("Forgot remembered address.");
      } else setMessage(result.error ?? "Could not update remembered addresses.");
    } catch {
      setMessage("Could not update remembered addresses.");
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <BrowserGroup title="Links">
        <OpenLinksInAppControl />
      </BrowserGroup>
      <BrowserGroup title="Internal addresses">
        <fieldset disabled={!access || busy} className="space-y-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <SettingsLabel
              label="Ask before opening"
              note="Choose when internal addresses need approval."
            />
            <Segmented
              label="Ask before opening"
              value={access?.level ?? "agent"}
              onChange={(level) => {
                if (access) setAccess({ ...access, level });
              }}
              options={[
                { value: "none", label: "Never" },
                { value: "agent", label: "Agent navigation" },
                { value: "all", label: "All navigation" },
              ]}
            />
          </div>
          <div className="grid gap-4 md:grid-cols-3">
            {keys.map((key) => (
              <div key={key} className="min-w-0 rounded-lg border p-3">
                <h3
                  className={`mb-3 text-sm font-medium ${key === "blocked" ? "text-destructive" : key === "ask" ? "text-warning" : "text-info"}`}
                >
                  {text[key]}
                </h3>
                <div className="mb-3 flex flex-wrap gap-2">
                  {access?.[key].map((entry) => (
                    <span
                      key={entry}
                      className="inline-flex max-w-full items-center gap-1 rounded-md bg-muted px-2 py-1 text-xs"
                    >
                      <span className="min-w-0 break-all">{entry}</span>
                      <button
                        type="button"
                        aria-label={`Remove ${entry} from ${text[key]}`}
                        className="rounded p-1 focus-visible:ring-2 focus-visible:ring-ring"
                        onClick={() =>
                          setAccess({
                            ...access,
                            [key]: access[key].filter((value) => value !== entry),
                          })
                        }
                      >
                        <XIcon className="size-3" />
                      </button>
                    </span>
                  ))}
                </div>
                <form
                  onSubmit={(event) => {
                    event.preventDefault();
                    add(key);
                  }}
                >
                  <Input
                    aria-label={`Add to ${text[key]}`}
                    placeholder="Add address · Enter"
                    maxLength={256}
                    value={inputs[key]}
                    onChange={(event) => {
                      setInputs({ ...inputs, [key]: event.target.value });
                      setErrors({ ...errors, [key]: "" });
                    }}
                  />
                  {errors[key] && (
                    <p role="alert" className="mt-2 text-xs text-destructive">
                      {errors[key]}
                    </p>
                  )}
                </form>
              </div>
            ))}
          </div>
        </fieldset>
        {!bridge && <UpdateReason verb="manage internal addresses" />}
        <p className="mt-4 text-xs text-muted-foreground">
          Always blocked: cloud metadata and link-local addresses.
        </p>
        {changes > 0 && (
          <div className="mt-4 flex flex-wrap items-center gap-3 border-t pt-4">
            <span className="flex-1 text-sm">{changes} unsaved changes</span>
            <Button
              variant="outline"
              disabled={busy}
              onClick={() => {
                setAccess(saved);
                setInputs({ blocked: "", ask: "", allowed: "" });
                setErrors({ blocked: "", ask: "", allowed: "" });
                setMessage("Changes discarded.");
              }}
            >
              Discard
            </Button>
            <Button variant="outline" disabled={busy} onClick={() => void save()}>
              Save
            </Button>
          </div>
        )}
        <p role="status" className="mt-3 text-sm">
          {message}
        </p>
      </BrowserGroup>
      <BrowserGroup title="Remembered for agents" server>
        {!settings?.browserRememberedList || !settings.browserRememberedForget ? (
          <UpdateReason verb="manage remembered addresses" />
        ) : !servers.length ? (
          <p className="text-sm text-muted-foreground">
            No remembered addresses. Agents ask when they need access.
          </p>
        ) : (
          servers.map((server) => (
            <div key={server.origin} className="mb-4">
              <h3 className="mb-2 break-all text-sm font-medium">
                {server.origin}{" "}
                {server.current && (
                  <span className="rounded bg-muted px-2 py-1 text-xs">This server</span>
                )}
              </h3>
              {server.entries.map((entry) => (
                <div key={entry} className="flex items-center justify-between gap-3 border-t py-2">
                  <span className="break-all text-sm">{entry}</span>
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={busy}
                    onClick={() => void forget(server.origin, entry)}
                  >
                    Forget
                  </Button>
                </div>
              ))}
            </div>
          ))
        )}
      </BrowserGroup>
    </>
  );
}
