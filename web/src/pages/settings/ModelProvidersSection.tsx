import { Segmented } from "@/components/ui/segmented";
import { useEffect, useState, type ReactNode } from "react";
import {
  ArrowLeftIcon,
  CheckIcon,
  CircleHelpIcon,
  LockIcon,
  PlusIcon,
  RefreshCwIcon,
  TrashIcon,
  TriangleAlertIcon,
  XIcon,
  UsersIcon,
} from "lucide-react";
import { useLocation, useNavigate } from "@/lib/routing";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Combobox, COMBOBOX_OPTION_THRESHOLD } from "@/components/ui/combobox";
import { StatusIcon } from "@/components/ui/status-icon";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  deleteModelConnection,
  fetchConnectionModels,
  getModelCatalog,
  listModelConnections,
  MODEL_CONNECTIONS_CHANGED,
  ModelConnectionError,
  refreshModelCatalog,
  saveModelConnection,
  type ConnectionDraft,
  type ModelConnection,
  type ModelRow,
  type ModelUsage,
} from "@/lib/modelConnectionsApi";

const status = {
  config: { label: "Managed in the server configuration", tone: "neutral", icon: <LockIcon /> },
  unchecked: {
    label: "Not checked against the provider catalogue",
    tone: "neutral",
    icon: <CircleHelpIcon />,
  },
  listed: { label: "Listed by the provider", tone: "ok", icon: <CheckIcon /> },
  gone: { label: "No longer listed by the provider", tone: "warn", icon: <TriangleAlertIcon /> },
  not_listed: { label: "Not in the provider catalogue", tone: "bad", icon: <XIcon /> },
} as const;
const empty: ConnectionDraft = {
  display_name: "",
  kind: "key",
  base_url: "",
  wire_api: "chat",
  models: {},
};
function slug(value: string) {
  return value
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "")
    .slice(0, 48);
}

function IconAction({
  label,
  children,
  onClick,
  disabled = false,
}: {
  label: string;
  children: ReactNode;
  onClick: () => void;
  disabled?: boolean;
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className="inline-flex">
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            aria-label={label}
            disabled={disabled}
            onClick={onClick}
          >
            {children}
          </Button>
        </span>
      </TooltipTrigger>
      <TooltipContent>{label}</TooltipContent>
    </Tooltip>
  );
}

function Usage({ usage }: { usage?: ModelUsage }) {
  const mobile = useIsMobileViewport();
  const [open, setOpen] = useState(false);
  if (!usage?.users) return <span className="text-muted-foreground">—</span>;
  const title = `Used by ${usage.users} users · ${usage.slots.map((slot) => `${slot.slot} (${slot.users})`).join("; ")}`;
  const trigger = (
    <Button
      type="button"
      variant="ghost"
      size="sm"
      aria-label={`Used by ${usage.users} users`}
      onClick={() => setOpen(!open)}
    >
      <UsersIcon className="size-3.5" />
      {usage.users}
    </Button>
  );
  const details = usage.slots.map((slot) => (
    <p key={slot.slot} className="text-sm">
      {slot.feature} · {slot.slot}: {slot.users} users
    </p>
  ));
  if (mobile)
    return (
      <>
        <Tooltip>
          <TooltipTrigger asChild>{trigger}</TooltipTrigger>
          <TooltipContent>{title}</TooltipContent>
        </Tooltip>
        <Dialog open={open} onOpenChange={setOpen}>
          <DialogContent
            className="max-h-[70vh] overflow-auto"
            style={{
              top: "auto",
              bottom: 0,
              transform: "translateX(-50%)",
              borderBottomLeftRadius: 0,
              borderBottomRightRadius: 0,
            }}
          >
            <DialogHeader>
              <DialogTitle>Used by</DialogTitle>
              <DialogDescription>Feature slots using this model</DialogDescription>
            </DialogHeader>
            {details}
          </DialogContent>
        </Dialog>
      </>
    );
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <Tooltip>
        <TooltipTrigger asChild>
          <PopoverTrigger asChild>{trigger}</PopoverTrigger>
        </TooltipTrigger>
        <TooltipContent>{title}</TooltipContent>
      </Tooltip>
      <PopoverContent className="max-h-72 overflow-auto">
        <p className="font-medium">Used by</p>
        {details}
      </PopoverContent>
    </Popover>
  );
}

function Choice({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: [string, string][];
  onChange: (value: string) => void;
}) {
  return (
    <label className="grid grid-cols-[9rem_minmax(0,1fr)] items-center gap-3 text-ui">
      <span>{label}</span>
      <Select value={value} onValueChange={onChange}>
        <SelectTrigger aria-label={label} className="max-w-80">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {options.map(([key, name]) => (
            <SelectItem key={key} value={key}>
              {name}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </label>
  );
}

function ProviderEditor({
  provider,
  onSaved,
  onRefreshed,
  onRemoved,
  onBack,
}: {
  provider?: ModelConnection;
  onSaved: (row: ModelConnection) => void;
  onRefreshed: (row: ModelConnection) => void;
  onRemoved: () => void;
  onBack: () => void;
}) {
  const [draft, setDraft] = useState<ConnectionDraft>(
    provider
      ? {
          ...provider,
          models: Object.fromEntries(provider.models.map((row) => [row.key, row.model_id])),
        }
      : empty,
  );
  const [models, setModels] = useState<ModelRow[]>(provider?.models ?? []);
  const [name, setName] = useState(provider?.name ?? "");
  const [idEdited, setIdEdited] = useState(false);
  const [keySource, setKeySource] = useState(provider?.key_source === "env" ? "env" : "stored");
  const [key, setKey] = useState("");
  const [catalog, setCatalog] = useState(provider?.catalog ?? { fetched_at: null, ids: [] });
  const [filter, setFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [confirm, setConfirm] = useState(false);
  const [newKey, setNewKey] = useState("");
  const [locks, setLocks] = useState<Record<string, ModelUsage>>({});
  const editable = provider?.editable !== false;
  useEffect(() => {
    if (provider?.editable)
      void getModelCatalog(provider.name)
        .then((cache) => setCatalog((previous) => ({ ...previous, ...cache })))
        .catch(() => {});
  }, [provider?.name, provider?.editable]);

  const value = (): ConnectionDraft => ({
    display_name: draft.display_name,
    kind: draft.kind,
    base_url: draft.base_url,
    wire_api: draft.wire_api,
    models: Object.fromEntries(
      models.filter((row) => row.model_id.trim()).map((row) => [row.key, row.model_id.trim()]),
    ),
    model_display_names: Object.fromEntries(models.map((row) => [row.key, row.display_name])),
    ...(key ? { key: keySource === "env" ? { env: key } : { value: key } } : {}),
  });
  async function run(action: () => Promise<void>) {
    setBusy(true);
    setNotice("");
    try {
      await action();
    } catch (error) {
      if (error instanceof ModelConnectionError && error.detail.kind === "exists") {
        setName(`${name.slice(0, 46)}-2`);
        setNotice("Provider id already exists. A different id has been suggested.");
      } else {
        if (error instanceof ModelConnectionError && error.detail.rows) {
          const blocked = error.detail.rows;
          setLocks(blocked);
          setModels((rows) => [
            ...rows,
            ...(provider?.models ?? []).filter(
              (row) => blocked[row.key] && !rows.some((item) => item.key === row.key),
            ),
          ]);
          setFilter("");
          setStatusFilter("all");
        }
        setNotice(error instanceof Error ? error.message : "Provider request failed");
      }
    } finally {
      setBusy(false);
    }
  }
  function update(rowKey: string, change: Partial<ModelRow>) {
    setModels((rows) => rows.map((row) => (row.key === rowKey ? { ...row, ...change } : row)));
  }
  const dirty =
    JSON.stringify(value()) !==
    JSON.stringify(
      provider
        ? {
            display_name: provider.display_name,
            kind: provider.kind,
            base_url: provider.base_url,
            wire_api: provider.wire_api,
            models: Object.fromEntries(provider.models.map((row) => [row.key, row.model_id])),
            model_display_names: Object.fromEntries(
              provider.models.map((row) => [row.key, row.display_name]),
            ),
          }
        : empty,
    );
  const visible = models.filter(
    (row) =>
      `${row.display_name} ${row.model_id}`.toLowerCase().includes(filter.toLowerCase()) &&
      (statusFilter === "all" ||
        (statusFilter === "used"
          ? !!row.used_by?.users
          : row.status === "gone" || row.status === "not_listed")),
  );

  return (
    <div className="flex min-w-0 flex-col gap-5">
      <div className="flex items-center gap-2">
        <IconAction label="Back to model providers" onClick={onBack}>
          <ArrowLeftIcon />
        </IconAction>
        <h1 className="text-xl font-semibold">{provider?.display_name ?? "New model provider"}</h1>
      </div>
      <fieldset disabled={busy || !editable} className="flex min-w-0 flex-col gap-3">
        <label className="grid grid-cols-[9rem_minmax(0,1fr)] items-center gap-3 text-ui">
          Display name
          <Input
            aria-label="Provider display name"
            className="max-w-80"
            value={draft.display_name}
            maxLength={100}
            onChange={(event) => {
              setDraft({ ...draft, display_name: event.target.value });
              if (!provider && !idEdited) setName(slug(event.target.value));
            }}
          />
        </label>
        <label className="grid grid-cols-[9rem_minmax(0,1fr)] items-center gap-3 text-ui">
          Provider id
          <Input
            aria-label="Provider id"
            className="max-w-80"
            value={name}
            maxLength={48}
            readOnly={!!provider}
            onChange={(event) => {
              setIdEdited(true);
              setName(event.target.value);
            }}
          />
        </label>
        <Choice
          label="Kind"
          value={draft.kind}
          options={[
            ["key", "API key"],
            ["gateway", "Gateway"],
            ["local", "Local"],
          ]}
          onChange={(kind) => setDraft({ ...draft, kind: kind as ConnectionDraft["kind"] })}
        />
        <label className="grid grid-cols-[9rem_minmax(0,1fr)] items-center gap-3 text-ui">
          Endpoint
          <Input
            aria-label="Endpoint"
            className="max-w-xl"
            value={draft.base_url}
            onChange={(event) => setDraft({ ...draft, base_url: event.target.value })}
          />
        </label>
        <Choice
          label="API type"
          value={draft.wire_api}
          options={[
            ["chat", "OpenAI-compatible chat"],
            ["responses", "OpenAI Responses"],
          ]}
          onChange={(wire_api) =>
            setDraft({ ...draft, wire_api: wire_api as ConnectionDraft["wire_api"] })
          }
        />
        <Choice
          label="Key source"
          value={keySource}
          options={[
            ["stored", "Stored key"],
            ["env", "Environment variable"],
          ]}
          onChange={(source) => {
            setKeySource(source);
            setKey("");
          }}
        />
        <label className="grid grid-cols-[9rem_minmax(0,1fr)] items-center gap-3 text-ui">
          {keySource === "env" ? "Variable name" : "API key"}
          <Input
            aria-label={keySource === "env" ? "Environment variable name" : "API key"}
            className="max-w-80"
            type={keySource === "env" ? "text" : "password"}
            autoComplete="off"
            value={key}
            placeholder={provider ? "Leave empty to keep current key" : "Required"}
            onChange={(event) => setKey(event.target.value)}
          />
        </label>
        <p className="text-sm text-muted-foreground">
          For an endpoint without authentication, enter a placeholder key.
        </p>
        <Button
          type="button"
          variant="outline"
          className="self-start"
          onClick={() =>
            void run(async () => {
              const result = await fetchConnectionModels(name, value());
              setNotice(
                result.ok ? "Connection works" : `Connection check failed: ${result.error_kind}`,
              );
            })
          }
        >
          Check connection
        </Button>
      </fieldset>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="font-medium">
            Models <span className="text-muted-foreground">{models.length}/50</span>
          </h2>
          <p className="text-sm text-muted-foreground">
            Last fetched:{" "}
            {catalog.fetched_at ? new Date(catalog.fetched_at).toLocaleString() : "Never"}
          </p>
        </div>
        <div className="flex gap-1">
          {editable && (
            <>
              <IconAction
                label={models.length >= 50 ? "Limit of 50 models reached" : "Add model"}
                disabled={busy || models.length >= 50}
                onClick={() => {
                  const rowKey = `model-${crypto.randomUUID().replace(/-/g, "").slice(0, 8)}`;
                  setFilter("");
                  setStatusFilter("all");
                  setNewKey(rowKey);
                  setModels([
                    ...models,
                    { key: rowKey, display_name: "", model_id: "", status: "unchecked" },
                  ]);
                }}
              >
                <PlusIcon />
              </IconAction>
              <IconAction
                label="Refresh model catalogue"
                disabled={busy || !provider}
                onClick={() =>
                  void run(async () => {
                    const result = await refreshModelCatalog(name);
                    if ("ok" in result) {
                      setNotice(`Refresh failed: ${result.error_kind}`);
                      return;
                    }
                    setCatalog(result.catalog ?? { fetched_at: null, ids: [] });
                    setModels((rows) =>
                      rows.map((row) => ({
                        ...row,
                        used_by: result.models.find((saved) => saved.key === row.key)?.used_by,
                        status:
                          result.models.find(
                            (saved) => saved.key === row.key && saved.model_id === row.model_id,
                          )?.status ?? "unchecked",
                      })),
                    );
                    setLocks({});
                    onRefreshed(result);
                    setNotice("Model catalogue refreshed");
                  })
                }
              >
                <RefreshCwIcon />
              </IconAction>
            </>
          )}
        </div>
      </div>
      <div className="flex flex-wrap gap-2">
        <Input
          aria-label="Filter models"
          className="max-w-64"
          value={filter}
          placeholder="Filter models"
          onChange={(event) => setFilter(event.target.value)}
        />
        <Segmented
          label="Model status"
          value={statusFilter}
          onChange={setStatusFilter}
          options={[
            { value: "all", label: "All" },
            { value: "used", label: "In use" },
            { value: "attention", label: "Needs attention" },
          ]}
        />
      </div>
      <div className="max-h-[430px] overflow-auto rounded-lg border">
        <table className="w-full min-w-[560px] table-fixed text-ui">
          <colgroup>
            <col className="w-[28%]" />
            <col className="w-7" />
            <col />
            <col className="w-[60px]" />
            <col className="w-[72px]" />
            <col className="w-12" />
          </colgroup>
          <thead className="sticky top-0 z-10 bg-background text-left text-muted-foreground">
            <tr>
              <th className="p-2 font-normal">Display name</th>
              <th>
                <span className="sr-only">Maps to</span>
              </th>
              <th className="p-2 font-normal">Requested model</th>
              <th className="p-2 font-normal">
                <span className="sr-only">Status</span>
              </th>
              <th className="p-2 font-normal">Used by</th>
              <th>
                <span className="sr-only">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {visible.map((row) => {
              const state = status[row.status];
              const locked = locks[row.key] ?? row.used_by;
              return (
                <tr
                  key={row.key}
                  ref={(node) => {
                    if (row.key === newKey) node?.scrollIntoView({ block: "nearest" });
                  }}
                  className={`border-t ${row.status === "gone" ? "bg-amber-500/5" : row.status === "not_listed" ? "bg-red-500/5" : ""}`}
                >
                  <td className="p-2">
                    <Input
                      aria-label={`Display name ${row.key}`}
                      className="truncate"
                      title={row.display_name}
                      value={row.display_name}
                      disabled={!editable || busy}
                      maxLength={100}
                      onChange={(event) => update(row.key, { display_name: event.target.value })}
                    />
                  </td>
                  <td aria-hidden="true" className="text-center text-muted-foreground">
                    →
                  </td>
                  <td
                    className="p-1"
                    title={row.model_id}
                    onKeyDownCapture={(event) => {
                      if (event.key === "Escape" && row.key === newKey && !row.model_id) {
                        setModels((rows) => rows.filter((item) => item.key !== row.key));
                        setNewKey("");
                      }
                    }}
                    onBlur={(event) => {
                      if (
                        !event.currentTarget.contains(event.relatedTarget) &&
                        !row.model_id &&
                        !provider?.models.some((saved) => saved.key === row.key) &&
                        !(event.target instanceof HTMLInputElement && event.target.value.trim())
                      )
                        setModels((rows) => rows.filter((item) => item.key !== row.key));
                    }}
                  >
                    {editable ? (
                      <fieldset disabled={busy}>
                        <Combobox
                          mode="editable"
                          value={row.model_id}
                          options={catalog.ids.map((id) => ({ key: id, label: id }))}
                          aria-label={`Requested model ${row.key}`}
                          autoOpen={row.key === newKey}
                          onValueChange={(model_id) => {
                            update(row.key, {
                              model_id,
                              status: catalog.ids.includes(model_id)
                                ? "listed"
                                : catalog.complete
                                  ? "not_listed"
                                  : "unchecked",
                            });
                            setNewKey("");
                          }}
                        />
                      </fieldset>
                    ) : (
                      <span className="block truncate" title={row.model_id}>
                        {row.model_id}
                      </span>
                    )}
                  </td>
                  <td className="p-2">
                    <StatusIcon {...state} />
                  </td>
                  <td className="p-1">
                    <Usage usage={row.used_by} />
                  </td>
                  <td className="p-1">
                    {editable && (
                      <IconAction
                        label={
                          locked?.users
                            ? `In use: ${locked.slots.map((slot) => `${slot.slot}, ${slot.users} users`).join("; ")}`
                            : "Remove model"
                        }
                        disabled={busy || !!locked?.users}
                        onClick={() => setModels(models.filter((item) => item.key !== row.key))}
                      >
                        {locked?.users ? <LockIcon /> : <TrashIcon />}
                      </IconAction>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        <p className="border-t px-3 py-2 text-sm text-muted-foreground">
          Showing {visible.length} of {models.length}
        </p>
        {!visible.length && (
          <p className="p-4 text-sm text-muted-foreground">
            No models match. Add a model or change the filter.
          </p>
        )}
      </div>
      {editable && (
        <div className="flex flex-wrap items-center gap-2">
          <Button
            type="button"
            disabled={busy || !name || !draft.base_url || !dirty}
            onClick={() =>
              void run(async () => {
                const saved = await saveModelConnection(name, value(), !provider);
                setKey("");
                setLocks({});
                setModels(saved.models);
                setDraft({
                  ...saved,
                  models: Object.fromEntries(saved.models.map((row) => [row.key, row.model_id])),
                });
                setCatalog(saved.catalog ?? { fetched_at: null, ids: [] });
                onSaved(saved);
                window.dispatchEvent(new Event(MODEL_CONNECTIONS_CHANGED));
                setNotice("Provider saved");
              })
            }
          >
            Save
          </Button>
          <Button
            type="button"
            variant="outline"
            disabled={busy || !dirty}
            onClick={() => {
              if (!provider) {
                onBack();
                return;
              }
              setDraft({
                ...provider,
                models: Object.fromEntries(provider.models.map((row) => [row.key, row.model_id])),
              });
              setModels(provider.models);
              setKeySource(provider.key_source === "env" ? "env" : "stored");
              setKey("");
              setFilter("");
              setNewKey("");
              setLocks({});
              setNotice("Changes discarded");
            }}
          >
            Discard
          </Button>
          {provider && (
            <Button
              type="button"
              variant="outline"
              className="ml-auto text-destructive"
              disabled={busy || !!provider.used_by}
              onClick={() => setConfirm(true)}
            >
              Remove provider
            </Button>
          )}
        </div>
      )}
      {!!provider?.used_by && (
        <p className="text-sm text-muted-foreground">
          Used by {provider.used_by} users. Change their feature settings before removing this
          provider.
        </p>
      )}
      <p role="status" className="text-sm text-muted-foreground">
        {notice}
      </p>
      <Dialog open={confirm} onOpenChange={setConfirm}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Remove provider?</DialogTitle>
            <DialogDescription>
              The provider and its stored key will be deleted. This cannot be undone.
            </DialogDescription>
          </DialogHeader>
          {notice && (
            <p role="alert" className="text-sm text-destructive">
              {notice}
            </p>
          )}
          <div className="flex gap-2">
            <Button type="button" variant="outline" onClick={() => setConfirm(false)}>
              Cancel
            </Button>
            <Button
              type="button"
              disabled={busy}
              onClick={() =>
                void run(async () => {
                  await deleteModelConnection(name);
                  window.dispatchEvent(new Event(MODEL_CONNECTIONS_CHANGED));
                  onRemoved();
                })
              }
            >
              Remove provider
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}

export function ModelProvidersSection() {
  const location = useLocation();
  const navigate = useNavigate();
  const [connections, setConnections] = useState<ModelConnection[]>([]);
  const [admin, setAdmin] = useState<boolean>();
  const [notice, setNotice] = useState("");
  const refresh = () =>
    listModelConnections().then((result) => {
      setConnections(result.connections);
      setAdmin(result.can_manage);
    });
  useEffect(() => {
    void refresh().catch(() =>
      setNotice("Couldn't load model providers. Reopen this page to retry."),
    );
  }, []);
  const routeName = location.pathname.split("/model-providers/")[1];
  const name = routeName ? decodeURIComponent(routeName) : undefined;
  const create = new URLSearchParams(location.search).has("create");
  const provider = connections.find((row) => row.name === name);
  if (admin === false) return <p>Administrator access required.</p>;
  if (admin === undefined) return <p role="status">{notice || "Loading model providers…"}</p>;
  const back = () => navigate("/settings/model-providers");
  return (
    <TooltipProvider>
      <div className="mx-auto flex w-full max-w-4xl flex-col gap-5 p-6">
        {create || provider ? (
          <ProviderEditor
            key={create ? "create" : provider?.name}
            provider={create ? undefined : provider}
            onBack={back}
            onRefreshed={(fresh) => {
              setConnections((rows) =>
                rows.map((row) =>
                  row.name === fresh.name
                    ? {
                        ...row,
                        used_by: fresh.used_by,
                        models: row.models.map((model) => ({
                          ...model,
                          used_by: fresh.models.find((saved) => saved.key === model.key)?.used_by,
                        })),
                      }
                    : row,
                ),
              );
            }}
            onSaved={(saved) => {
              setConnections((rows) => [...rows.filter((row) => row.name !== saved.name), saved]);
              navigate(`/settings/model-providers/${encodeURIComponent(saved.name)}`);
            }}
            onRemoved={() => {
              void refresh();
              back();
            }}
          />
        ) : name ? (
          <>
            <p>Provider not found.</p>
            <Button type="button" onClick={back}>
              Back to model providers
            </Button>
          </>
        ) : (
          <ProviderList
            rows={connections}
            onCreate={() => navigate("/settings/model-providers?create")}
            onPick={(id) => navigate(`/settings/model-providers/${encodeURIComponent(id)}`)}
          />
        )}
      </div>
    </TooltipProvider>
  );
}

function ProviderList({
  rows,
  onCreate,
  onPick,
}: {
  rows: ModelConnection[];
  onCreate: () => void;
  onPick: (name: string) => void;
}) {
  return (
    <>
      <div className="flex items-center justify-between gap-3">
        <h1 className="text-xl font-semibold">Model providers</h1>
        <IconAction label="Add provider" onClick={onCreate}>
          <PlusIcon />
        </IconAction>
      </div>
      <p className="text-sm text-muted-foreground">
        Connect models once. Choose and test them in each feature's settings.
      </p>
      {rows.length > COMBOBOX_OPTION_THRESHOLD && (
        <Combobox
          mode="select"
          options={rows.map((row) => ({
            key: row.name,
            label: row.display_name,
            description: row.base_url,
          }))}
          value=""
          onValueChange={onPick}
          aria-label="Find provider"
          placeholder="Find provider"
        />
      )}
      <div className="flex max-h-[400px] flex-col gap-2 overflow-auto">
        {rows.map((row) => (
          <button
            type="button"
            key={row.name}
            className="flex min-w-0 items-center justify-between gap-3 rounded-xl border p-4 text-left hover:bg-muted/50"
            onClick={() => onPick(row.name)}
          >
            <span className="min-w-0">
              <span className="block truncate font-medium">{row.display_name}</span>
              <span className="block truncate text-sm text-muted-foreground">{row.base_url}</span>
            </span>
            <span className="shrink-0 text-sm text-muted-foreground">
              {row.models.length} models
            </span>
          </button>
        ))}
      </div>
      {!rows.length && (
        <p className="text-muted-foreground">
          No model providers yet. Add a provider to get started.
        </p>
      )}
    </>
  );
}
