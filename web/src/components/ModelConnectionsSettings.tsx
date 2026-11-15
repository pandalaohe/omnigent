import { useEffect, useState } from "react";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Button } from "@/components/ui/button";
import {
  listModelConnections,
  saveModelConnection,
  deleteModelConnection,
  fetchConnectionModels,
  MODEL_CONNECTIONS_CHANGED,
  type ModelConnection,
  type ConnectionDraft,
} from "@/lib/modelConnectionsApi";

const empty: ConnectionDraft = {
  display_name: "",
  kind: "key",
  base_url: "",
  wire_api: "chat",
  models: {},
};
const fieldClass = "min-w-0 rounded border border-input bg-background p-2";

export function ModelConnectionsSettings() {
  const [rows, setRows] = useState<ModelConnection[]>([]);
  const [admin, setAdmin] = useState(false);
  const [draft, setDraft] = useState(empty);
  const [name, setName] = useState("");
  const [editing, setEditing] = useState(false);
  const [existing, setExisting] = useState(false);
  const [key, setKey] = useState("");
  const [keySource, setKeySource] = useState("stored");
  const [models, setModels] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const refresh = async () => {
    const result = await listModelConnections();
    setRows(result.connections);
    setAdmin(result.can_manage);
  };
  useEffect(() => {
    void refresh().catch(() => setNotice("Couldn't load providers"));
  }, []);
  const value = (): ConnectionDraft => ({
    ...draft,
    models: Object.fromEntries(
      models
        .split(/\n|,/)
        .map((s) => s.trim())
        .filter(Boolean)
        .slice(0, 50)
        .map((s, i) => [`model-${i}`, s]),
    ),
    ...(key ? { key: keySource === "env" ? { env: key } : { value: key } } : {}),
  });
  const run = async (action: () => Promise<void>) => {
    setBusy(true);
    setNotice("");
    try {
      await action();
    } catch {
      setNotice("Provider request failed. Check the endpoint and key.");
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="flex min-w-0 flex-col gap-3">
      <p className="text-sm text-muted-foreground">
        Server-wide providers for speech and text models. Administrators manage endpoints and keys.
      </p>
      {rows.map((row) => (
        <div
          key={row.name}
          className="flex flex-wrap items-center justify-between gap-2 rounded border border-border p-3"
        >
          <div className="min-w-0 basis-full sm:flex-1">
            <div className="break-words text-ui font-medium">{row.display_name}</div>
            <div className="break-words text-sm text-muted-foreground">
              <span className="[overflow-wrap:anywhere]">{row.base_url}</span> ·{" "}
              {row.editable ? row.wire_api : "From configuration file"}
              <span className="block">{row.key_set ? "Key configured" : "Key unavailable"}</span>
            </div>
          </div>
          {admin && row.editable && (
            <Button
              variant="outline"
              disabled={busy}
              onClick={() =>
                void run(async () => {
                  const entry: ConnectionDraft = {
                    display_name: row.display_name,
                    kind: row.kind,
                    base_url: row.base_url,
                    wire_api: row.wire_api,
                    models: {},
                  };
                  const result = await fetchConnectionModels(row.name, entry);
                  if (!result.ok) {
                    setNotice(`Fetch models failed: ${result.error_kind}`);
                    return;
                  }
                  entry.models = Object.fromEntries(
                    result.model_ids.slice(0, 50).map((model, i) => [`model-${i}`, model]),
                  );
                  await saveModelConnection(row.name, entry);
                  await refresh();
                  window.dispatchEvent(new Event(MODEL_CONNECTIONS_CHANGED));
                  setNotice(`Models updated for ${row.display_name}`);
                })
              }
            >
              Fetch models
            </Button>
          )}
          {admin && row.editable && (
            <Button
              disabled={busy}
              variant="outline"
              onClick={() => {
                setName(row.name);
                setDraft({
                  display_name: row.display_name,
                  kind: row.kind,
                  base_url: row.base_url,
                  wire_api: row.wire_api,
                  models: {},
                });
                setModels(row.models.join("\n"));
                setKey("");
                setKeySource(row.key_env ? "env" : "stored");
                setExisting(true);
                setEditing(true);
              }}
            >
              Edit provider
            </Button>
          )}
        </div>
      ))}
      {admin && !editing && (
        <Button
          variant="outline"
          className="self-start"
          onClick={() => {
            setName("");
            setDraft(empty);
            setModels("");
            setKey("");
            setKeySource("stored");
            setExisting(false);
            setEditing(true);
          }}
        >
          Add provider
        </Button>
      )}
      {admin && editing && (
        <fieldset
          disabled={busy}
          className="flex min-w-0 flex-col gap-3 rounded border border-border p-3"
        >
          <label className="flex flex-col gap-1 text-ui">
            Provider ID
            <input
              className={fieldClass}
              value={name}
              disabled={existing}
              pattern="[a-z0-9][a-z0-9-]{0,47}"
              onChange={(e) => setName(e.target.value)}
            />
          </label>
          <label className="flex flex-col gap-1 text-ui">
            Display name
            <input
              className={fieldClass}
              value={draft.display_name}
              onChange={(e) => setDraft({ ...draft, display_name: e.target.value })}
            />
          </label>
          <label className="flex flex-col gap-1 text-ui">
            Endpoint
            <input
              className={fieldClass}
              placeholder="https://provider.example/v1"
              value={draft.base_url}
              onChange={(e) => setDraft({ ...draft, base_url: e.target.value })}
            />
          </label>
          <div className="grid min-w-0 grid-cols-[7rem_minmax(0,1fr)] items-center gap-3 text-ui">
            <label htmlFor="model-provider-api-type">API type</label>
            <Select
              value={draft.wire_api}
              disabled={busy}
              onValueChange={(selectedValue) =>
                setDraft({ ...draft, wire_api: selectedValue as ConnectionDraft["wire_api"] })
              }
            >
              <SelectTrigger id="model-provider-api-type" className="w-full min-w-0 max-w-64">
                <SelectValue />
              </SelectTrigger>
              <SelectContent position="popper" align="start">
                <SelectItem value="chat">OpenAI-compatible chat</SelectItem>
                <SelectItem value="responses">OpenAI Responses</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="grid min-w-0 grid-cols-[7rem_minmax(0,1fr)] items-center gap-3 text-ui">
            <label htmlFor="model-provider-key-source">Key source</label>
            <Select
              value={keySource}
              disabled={busy}
              onValueChange={(selectedValue) => {
                setKeySource(selectedValue);
                setKey("");
              }}
            >
              <SelectTrigger id="model-provider-key-source" className="w-full min-w-0 max-w-64">
                <SelectValue />
              </SelectTrigger>
              <SelectContent position="popper" align="start">
                <SelectItem value="stored">Stored key</SelectItem>
                <SelectItem value="env">Environment variable</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <label className="flex flex-col gap-1 text-ui">
            {keySource === "env" ? "Environment variable name" : "API key"}
            <input
              className={fieldClass}
              type={keySource === "env" ? "text" : "password"}
              autoComplete="off"
              value={key}
              placeholder={existing ? "Leave empty to keep current key" : "Required"}
              onChange={(e) => setKey(e.target.value)}
            />
          </label>
          <p className="text-sm text-muted-foreground">
            For an endpoint without authentication, enter a placeholder key.
          </p>
          <label className="flex flex-col gap-1 text-ui">
            Models
            <textarea
              className={fieldClass}
              value={models}
              rows={3}
              placeholder="Fetch models or enter one model ID per line"
              onChange={(e) => setModels(e.target.value)}
            />
          </label>
          <div className="flex flex-wrap gap-2">
            <Button
              variant="outline"
              onClick={() =>
                void run(async () => {
                  const result = await fetchConnectionModels(name, value());
                  if (!result.ok) {
                    setNotice(`Fetch models failed: ${result.error_kind}`);
                    return;
                  }
                  setModels(result.model_ids.slice(0, 50).join("\n"));
                  setNotice(
                    `Fetched ${result.model_ids.length} models. Save provider to use them.`,
                  );
                })
              }
            >
              Fetch models
            </Button>
            <Button
              onClick={() =>
                void run(async () => {
                  await saveModelConnection(name, value());
                  setKey("");
                  setEditing(false);
                  await refresh();
                  window.dispatchEvent(new Event(MODEL_CONNECTIONS_CHANGED));
                  setNotice("Provider saved");
                })
              }
            >
              Save provider
            </Button>
            <Button
              variant="ghost"
              onClick={() => {
                setKey("");
                setEditing(false);
              }}
            >
              Cancel
            </Button>
            {existing && (
              <Button
                variant="destructive"
                onClick={() =>
                  void run(async () => {
                    await deleteModelConnection(name);
                    setKey("");
                    setEditing(false);
                    await refresh();
                    window.dispatchEvent(new Event(MODEL_CONNECTIONS_CHANGED));
                    setNotice("Provider removed. Existing selections may need updating.");
                  })
                }
              >
                Remove provider
              </Button>
            )}
          </div>
        </fieldset>
      )}
      <span role="status" className="text-sm text-muted-foreground">
        {notice}
      </span>
    </div>
  );
}
