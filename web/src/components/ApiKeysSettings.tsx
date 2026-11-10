import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { apiKeysRequest, type ApiKeyList, type ManagedApiKey } from "@/lib/apiKeysApi";
import text from "@/lib/locales/apiKeys.en.json";
import { useViewerId } from "@/hooks/useViewerId";
import { getCurrentUserId } from "@/lib/identity";
import { getOmnigentHostGeneration, getOmnigentServerIdentity } from "@/lib/host";

type Action = { kind: "create" } | { kind: "edit" | "regenerate" | "delete"; key: ManagedApiKey };

function utc(epoch: number): string {
  return new Date(epoch * 1000).toISOString().slice(0, 16).replace("T", " ") + " UTC";
}

function identityScope(): string {
  return JSON.stringify([
    getCurrentUserId(),
    getOmnigentServerIdentity(),
    getOmnigentHostGeneration(),
  ]);
}

export function ApiKeysSettings() {
  useViewerId();
  return <ApiKeysPanel key={identityScope()} scope={identityScope()} />;
}

function ApiKeysPanel({ scope }: { scope: string }) {
  const [data, setData] = useState<ApiKeyList | null>(null);
  const [action, setAction] = useState<Action | null>(null);
  const [name, setName] = useState("");
  const [scopes, setScopes] = useState<string[]>(["*"]);
  const [expiration, setExpiration] = useState("");
  const [expiryChoice, setExpiryChoice] = useState("none");
  const [expiryEpoch, setExpiryEpoch] = useState<number | null>(null);
  const [value, setValue] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState("");
  const live = useRef(true);
  const current = () => live.current && identityScope() === scope;

  useEffect(() => {
    live.current = true;
    let active = true;
    void apiKeysRequest<ApiKeyList>()
      .then((result) => {
        if (active && identityScope() === scope) setData(result);
      })
      .catch((err: Error) => {
        if (active && identityScope() === scope) setError(err.message);
      });
    return () => {
      active = false;
      live.current = false;
    };
  }, [scope]);

  function close() {
    setAction(null);
    setValue(null);
    setError(null);
    setName("");
    setScopes(["*"]);
    setExpiration("");
    setExpiryChoice("none");
    setExpiryEpoch(null);
  }

  function open(next: Action) {
    close();
    setAction(next);
    if (next.kind !== "create") {
      setName(next.key.name);
      setScopes(next.key.scopes);
      let expires = next.key.expires_at;
      if (next.kind === "regenerate" && expires !== null && expires <= Date.now() / 1000) {
        const lifetime = expires - (next.key.regenerated_at ?? next.key.created_at);
        expires = lifetime > 0 ? Math.floor(Date.now() / 1000) + lifetime : null;
      }
      setExpiryEpoch(expires);
      setExpiryChoice(expires === null ? "none" : "custom");
      setExpiration(expires === null ? "" : new Date(expires * 1000).toISOString().slice(0, 10));
    }
  }

  async function save() {
    if (!action || busy) return;
    setBusy(true);
    setError(null);
    try {
      const expires_at = expiryEpoch;
      if (
        action.kind !== "delete" &&
        expires_at !== null &&
        (!Number.isFinite(expires_at) || expires_at <= Date.now() / 1000)
      ) {
        throw new Error("Choose a future UTC date");
      }
      const path = action.kind === "create" ? "" : `/${encodeURIComponent(action.key.id)}`;
      if (action.kind === "delete") {
        await apiKeysRequest(path, "DELETE");
      } else {
        const result = await apiKeysRequest<{ key: ManagedApiKey; value?: string }>(
          path + (action.kind === "regenerate" ? "/regenerate" : ""),
          action.kind === "edit" ? "PATCH" : "POST",
          action.kind === "regenerate" ? { expires_at } : { name, scopes, expires_at },
        );
        if (current() && result.value) setValue(result.value);
      }
      if (!current()) return;
      setNotice(
        action.kind === "delete"
          ? "API key deleted"
          : action.kind === "edit"
            ? "API key updated"
            : "API key ready",
      );
      if (action.kind === "edit" || action.kind === "delete") close();
      const updated = await apiKeysRequest<ApiKeyList>();
      if (current()) setData(updated);
    } catch (err) {
      if (current()) setError(err instanceof Error ? err.message : "Request failed");
    } finally {
      if (current()) setBusy(false);
    }
  }

  const labels: Record<string, string> = { usage: text.usage, tasks: text.tasks };
  const title = value
    ? "API key ready"
    : action?.kind === "create"
      ? text.newKey
      : `${action?.kind === "edit" ? "Edit" : action?.kind === "delete" ? "Delete" : "Regenerate"} API key`;
  return (
    <section className="space-y-5 py-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-lg font-medium">{text.title}</h2>
        <Button disabled={!data || busy} onClick={() => open({ kind: "create" })}>
          {text.newKey}
        </Button>
      </div>
      <p className="text-sm text-muted-foreground">{text.description}</p>
      <p role="status" className="text-sm">
        {notice}
      </p>
      {error && !action && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
      {!data && !error && <p>Loading API keys…</p>}
      {data?.keys.length === 0 && <p className="text-sm text-muted-foreground">No API keys yet.</p>}
      {data?.keys.map((key) => (
        <div key={key.id} className="space-y-2 rounded-lg border border-border p-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <span className="font-medium">{key.name}</span>{" "}
              <code className="text-xs text-muted-foreground">{key.prefix}…</code>
            </div>
            <div className="flex flex-wrap gap-2">
              {(["edit", "regenerate", "delete"] as const).map((kind) => (
                <Button
                  key={kind}
                  size="sm"
                  variant="outline"
                  disabled={busy}
                  onClick={() => open({ kind, key })}
                >
                  {kind === "edit" ? "Edit" : kind === "delete" ? "Delete" : "Regenerate"}
                </Button>
              ))}
            </div>
          </div>
          <div className="flex flex-wrap gap-2 text-xs">
            {key.scopes.map((s) => (
              <span key={s} className="rounded bg-muted px-2 py-1">
                {s === "*" ? "All permissions" : (labels[s] ?? s)}
              </span>
            ))}
          </div>
          <p
            className={`text-xs ${key.expires_at !== null && key.expires_at <= Date.now() / 1000 ? "text-destructive" : "text-muted-foreground"}`}
          >
            {key.expires_at === null
              ? "No expiration"
              : `${key.expires_at <= Date.now() / 1000 ? "Expired" : "Expires"} ${utc(key.expires_at)}`}{" "}
            · {key.last_used_at === null ? "Never used" : `Last used ${utc(key.last_used_at)}`}
          </p>
        </div>
      ))}
      {data?.read_only.map((key) => (
        <div
          key={key.name}
          className="space-y-2 rounded-lg border border-dashed border-border p-4 text-sm"
        >
          <p className="font-medium">
            {key.name} <span className="font-normal text-muted-foreground">Read-only</span>
          </p>
          <p>
            {key.source === "environment"
              ? "OMNIGENT_USAGE_READ_TOKEN (deprecated)"
              : "Server config file"}{" "}
            · {key.scopes.map((s) => labels[s] ?? s).join(", ")}
          </p>
          <p className="text-muted-foreground">
            {key.last_used_at === null
              ? "Not used since server start"
              : `Last used ${utc(key.last_used_at)} since server start`}
          </p>
          <p className="text-muted-foreground">{text.readOnlyHelp}</p>
        </div>
      ))}
      <Dialog
        open={action !== null}
        onOpenChange={(isOpen) => {
          if (!isOpen && !busy) close();
        }}
      >
        <DialogContent
          onEscapeKeyDown={(event) => {
            if (busy) event.preventDefault();
          }}
          onInteractOutside={(event) => {
            if (busy) event.preventDefault();
          }}
        >
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>
            {value
              ? text.reveal
              : action?.kind === "delete"
                ? text.deleteWarning
                : action?.kind === "regenerate"
                  ? text.regenerateWarning
                  : "Choose a name, permissions and an optional UTC expiration."}
          </DialogDescription>
          {value ? (
            <div className="space-y-4">
              <code className="block break-all rounded bg-muted p-3 text-sm select-all">
                {value}
              </code>
              <Button
                onClick={() => {
                  void navigator.clipboard
                    .writeText(value)
                    .then(() => {
                      if (current()) setNotice("Copied");
                    })
                    .catch(() => {
                      if (current()) setError("Copy failed. Select the key and copy it manually.");
                    });
                }}
              >
                Copy
              </Button>
              <Button onClick={close}>Done</Button>
            </div>
          ) : (
            <form
              className="space-y-4"
              onSubmit={(e) => {
                e.preventDefault();
                void save();
              }}
            >
              {(action?.kind === "create" || action?.kind === "edit") && (
                <>
                  <label className="block space-y-1 text-sm">
                    Name
                    <Input
                      required
                      pattern="[A-Za-z0-9][A-Za-z0-9._-]*"
                      maxLength={64}
                      value={name}
                      onChange={(e) => setName(e.target.value)}
                      disabled={busy}
                    />
                  </label>
                  <fieldset disabled={busy} className="space-y-2 text-sm">
                    <legend>Permissions</legend>
                    <label className="flex items-center gap-2">
                      <input
                        type="checkbox"
                        checked={scopes.includes("*")}
                        onChange={(e) =>
                          setScopes(e.target.checked ? ["*"] : [...(data?.available_scopes ?? [])])
                        }
                      />
                      {text.all}
                    </label>
                    {data?.available_scopes.map((scopeName) => (
                      <label key={scopeName} className="ml-4 flex items-center gap-2">
                        <input
                          type="checkbox"
                          checked={scopes.includes("*") || scopes.includes(scopeName)}
                          onChange={(e) => {
                            const explicit = scopes.includes("*") ? data.available_scopes : scopes;
                            setScopes(
                              e.target.checked
                                ? [...explicit, scopeName]
                                : explicit.filter((s) => s !== scopeName),
                            );
                          }}
                        />
                        {labels[scopeName] ?? scopeName}
                      </label>
                    ))}
                  </fieldset>
                </>
              )}
              {action?.kind !== "delete" && (
                <div className="space-y-2 text-sm">
                  <label className="flex items-center gap-2">
                    Expiration (UTC)
                    <select
                      aria-label="Expiration (UTC)"
                      className="rounded border border-border bg-background p-2"
                      disabled={busy}
                      value={expiryChoice}
                      onChange={(e) => {
                        const choice = e.target.value;
                        setExpiryChoice(choice);
                        if (choice === "none") setExpiryEpoch(null);
                        else if (choice !== "custom")
                          setExpiryEpoch(Math.floor(Date.now() / 1000) + Number(choice) * 86400);
                        else
                          setExpiryEpoch(
                            expiration ? Date.parse(`${expiration}T00:00:00Z`) / 1000 : null,
                          );
                      }}
                    >
                      <option value="none">No expiration</option>
                      <option value="30">30 days</option>
                      <option value="90">90 days</option>
                      <option value="365">1 year</option>
                      <option value="custom">Custom date</option>
                    </select>
                  </label>
                  {expiryChoice === "custom" && (
                    <label className="block">
                      Custom date
                      <Input
                        type="date"
                        required
                        value={expiration}
                        onChange={(e) => {
                          setExpiration(e.target.value);
                          setExpiryEpoch(
                            e.target.value
                              ? Date.parse(`${e.target.value}T00:00:00Z`) / 1000
                              : null,
                          );
                        }}
                        disabled={busy}
                      />
                    </label>
                  )}
                  <p className="text-muted-foreground">
                    {expiryEpoch === null ? "No expiration" : utc(expiryEpoch)}
                  </p>
                </div>
              )}
              <div className="flex justify-end gap-2">
                <Button type="button" variant="outline" disabled={busy} onClick={close}>
                  Cancel
                </Button>
                <Button
                  type="submit"
                  variant={
                    action?.kind === "delete" || action?.kind === "regenerate"
                      ? "destructive"
                      : "default"
                  }
                  disabled={
                    busy ||
                    ((action?.kind === "create" || action?.kind === "edit") && scopes.length === 0)
                  }
                >
                  {busy
                    ? "Saving…"
                    : action?.kind === "create"
                      ? "Create API key"
                      : action?.kind === "edit"
                        ? "Save changes"
                        : action?.kind === "delete"
                          ? "Delete API key"
                          : "Regenerate API key"}
                </Button>
              </div>
            </form>
          )}
          {error && (
            <p role="alert" className="text-sm text-destructive">
              {error}
            </p>
          )}
        </DialogContent>
      </Dialog>
    </section>
  );
}
