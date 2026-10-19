// Settings › Harnesses: the per-host × harness master table of calling defaults,
// the model-catalog sync that fills its dropdowns, and the
// web-only carry-over switch. The resolution chain itself is server-side; this
// surface edits only the master layer and the cached catalogs.

import { useEffect, useMemo, useRef, useState } from "react";
import { PlusIcon, Trash2Icon } from "lucide-react";

import { EFFORT_SELECT_NONE, MODEL_SELECT_DEFAULT } from "@/components/HarnessConfigControls";
import { SessionDefaultModeSelect } from "@/components/SessionDefaultModeSelect";
import { HelpTip } from "@/components/HelpTip";
import { sessionDefaultModeOptions } from "@/lib/sessionDefaultModes";
import { reconcileSpeed } from "@/lib/speedTiers";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { useHosts, type Host } from "@/hooks/useHosts";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import {
  addCallingDefaultEntry,
  type CallingDefaultEntry,
  type CallingDefaultsTable,
  CALLING_DEFAULT_HARNESSES,
  callingHarnessLabel,
  removeCallingDefaultEntry,
  SDK_NATIVE_PARENT,
  setCallingDefaultField,
  useCallingDefaults,
  useCallingLastEnabled,
  writeCallingLastEnabled,
} from "@/lib/callingDefaults";
import {
  listCallingDefaultCatalogs,
  syncCallingDefaults,
  type CallingDefaultsCatalogRow,
} from "@/lib/callingDefaultsApi";
import { nativeModelLabel, normalizeEffortLabel } from "@/lib/composerModelLabel";
import { effortLevelsFor, reconcileEffortOnModelChange } from "@/lib/modelEffortOptions";
import { relativeTime } from "@/lib/relativeTime";
import type { NativeModelOption } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import { selectableSessionAgents } from "@/lib/agentGrouping";
import { harnessReadinessOnHost } from "@/lib/harnessSetup";
import { setProjectHostAgent, useProjectHostAgents } from "@/lib/projectHostAgents";
import { customAgentForPicker, useCustomAgents } from "@/lib/customAgentsApi";

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function pairKey(hostId: string, harness: string): string {
  return `${hostId}\u0000${harness}`;
}

function catalogFor(
  catalogs: readonly CallingDefaultsCatalogRow[],
  hostId: string,
  harness: string,
): CallingDefaultsCatalogRow | undefined {
  return catalogs.find((row) => row.host_id === hostId && row.harness === harness);
}

/** Merge filtered-sync rows over the loaded list, replacing matching pairs. */
function mergeCatalogRows(
  previous: readonly CallingDefaultsCatalogRow[],
  next: readonly CallingDefaultsCatalogRow[],
): CallingDefaultsCatalogRow[] {
  const byPair = new Map(previous.map((row) => [pairKey(row.host_id, row.harness), row]));
  for (const row of next) byPair.set(pairKey(row.host_id, row.harness), row);
  return [...byPair.values()];
}

function lastSyncLabel(fetchedAt: number | null): string {
  if (fetchedAt === null) return "Not synced yet";
  const relative = relativeTime(fetchedAt * 1000);
  return relative === "now" ? "Last sync just now" : `Last sync ${relative} ago`;
}

/** Whether the host reports this harness as configured; unknown passes. */
function harnessOnHost(host: Host, harness: string): boolean {
  const readiness = host.configured_harnesses;
  if (!readiness) return true;
  const value = readiness[harness];
  return value === undefined || value === true;
}

function modelLabelFor(model: string, catalog: CallingDefaultsCatalogRow | undefined): string {
  const option = catalog?.models.find((row) => row.id === model);
  return option ? nativeModelLabel(option) : model;
}

function entrySummary(
  entry: CallingDefaultEntry | undefined,
  catalog: CallingDefaultsCatalogRow | undefined,
): string {
  if (!entry) return "";
  const parts: string[] = [];
  if (entry.model) parts.push(modelLabelFor(entry.model, catalog));
  if (entry.effort) parts.push(normalizeEffortLabel(entry.effort));
  return parts.join(" · ");
}

interface CatalogNote {
  text: string;
  destructive: boolean;
}

function catalogNote(row: CallingDefaultsCatalogRow | undefined, host: Host): CatalogNote | null {
  if (row?.error) return { text: row.error, destructive: true };
  if (row?.stale)
    return { text: host.status === "online" ? "stale" : "offline", destructive: false };
  return null;
}

function ModelSettingSelect({
  hostId,
  harness,
  value,
  catalog,
  onChange,
  onOpen,
}: {
  hostId: string;
  harness: string;
  value: string | undefined;
  catalog: CallingDefaultsCatalogRow | undefined;
  onChange: (model: string | null) => void;
  onOpen: (hostId: string, harness: string) => void;
}) {
  const label = callingHarnessLabel(harness);
  const options = (catalog?.models ?? []).map((model) => ({
    id: model.id,
    label: nativeModelLabel(model),
  }));
  if (value && !options.some((option) => option.id === value)) {
    options.unshift({ id: value, label: value });
  }
  return (
    <Select
      value={value ?? MODEL_SELECT_DEFAULT}
      onValueChange={(next) => onChange(next === MODEL_SELECT_DEFAULT ? null : next)}
      onOpenChange={(open) => {
        if (open) onOpen(hostId, harness);
      }}
    >
      <SelectTrigger
        className="h-8 w-full min-w-0"
        aria-label={`Model for ${label}`}
        data-testid={`calling-defaults-model-${harness}`}
      >
        <SelectValue />
      </SelectTrigger>
      <SelectContent position="popper" align="start" className="w-(--radix-select-trigger-width)">
        <SelectItem value={MODEL_SELECT_DEFAULT}>Default</SelectItem>
        {options.map((option) => (
          <SelectItem key={option.id} value={option.id}>
            <span className="block min-w-0 truncate" title={option.label}>
              {option.label}
            </span>
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

function EffortSettingSelect({
  hostId,
  harness,
  model,
  value,
  models,
  onChange,
  onOpen,
}: {
  hostId: string;
  harness: string;
  model: string | undefined;
  value: string | undefined;
  models: readonly NativeModelOption[];
  onChange: (effort: string | null) => void;
  onOpen: (hostId: string, harness: string) => void;
}) {
  const label = callingHarnessLabel(harness);
  const levels = Array.from(new Set(effortLevelsFor(harness, models, model ?? null) ?? []));
  const acp = harness === "acp" || harness.startsWith("acp:");
  if (acp && levels.length === 0) {
    return (
      <span
        className="text-sm text-muted-foreground"
        data-testid={`calling-defaults-effort-unavailable-${harness}`}
      >
        No advertised effort options
      </span>
    );
  }
  if (!acp && value && !levels.includes(value)) levels.unshift(value);
  return (
    <Select
      value={value ?? EFFORT_SELECT_NONE}
      onValueChange={(next) => onChange(next === EFFORT_SELECT_NONE ? null : next)}
      onOpenChange={(open) => {
        if (open) onOpen(hostId, harness);
      }}
    >
      <SelectTrigger
        className="h-8 w-full min-w-0"
        aria-label={`Effort for ${label}`}
        data-testid={`calling-defaults-effort-${harness}`}
      >
        <SelectValue />
      </SelectTrigger>
      <SelectContent position="popper" align="start" className="w-(--radix-select-trigger-width)">
        <SelectItem value={EFFORT_SELECT_NONE}>{acp ? "No override" : "Default"}</SelectItem>
        {acp && value && !levels.includes(value) && (
          <SelectItem value={value} disabled>
            {value} (unavailable)
          </SelectItem>
        )}
        {levels.map((level) => (
          <SelectItem key={level} value={level}>
            {normalizeEffortLabel(level)}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

function DeleteEntryButton({ harness, onRemove }: { harness: string; onRemove: () => void }) {
  return (
    <Button
      type="button"
      variant="ghost"
      size="icon-xs"
      className="shrink-0 text-muted-foreground hover:text-destructive"
      aria-label={`Delete ${callingHarnessLabel(harness)} setting`}
      title="Delete setting"
      onClick={onRemove}
      data-testid={`calling-defaults-delete-${harness}`}
    >
      <Trash2Icon />
    </Button>
  );
}

function AddHarnessControl({
  options,
  onAdd,
}: {
  options: readonly string[];
  onAdd: (harness: string) => void;
}) {
  const [adding, setAdding] = useState(false);
  if (options.length === 0) return null;
  if (!adding) {
    return (
      <Button
        type="button"
        variant="ghost"
        size="sm"
        className="text-primary"
        onClick={() => setAdding(true)}
        data-testid="calling-defaults-add-row"
      >
        <PlusIcon />
        Add harness settings
      </Button>
    );
  }
  return (
    <Select
      value=""
      onValueChange={(harness) => {
        onAdd(harness);
        setAdding(false);
      }}
    >
      <SelectTrigger
        className="h-8 w-full max-w-56"
        aria-label="Add harness settings"
        data-testid="calling-defaults-add-harness"
      >
        <SelectValue placeholder="Choose a harness" />
      </SelectTrigger>
      <SelectContent position="popper" align="start" className="w-(--radix-select-trigger-width)">
        {options.map((harness) => (
          <SelectItem key={harness} value={harness}>
            {callingHarnessLabel(harness)}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

function HostDefaultsTable({
  host,
  master,
  catalogs,
  isMobile,
  onLoadCatalog,
}: {
  host: Host;
  master: CallingDefaultsTable;
  catalogs: CallingDefaultsCatalogRow[];
  isMobile: boolean;
  onLoadCatalog: (hostId: string, harness: string) => void;
}) {
  const hostId = host.host_id;
  const hostTable = master[hostId] ?? {};
  const harnesses = [
    ...CALLING_DEFAULT_HARNESSES,
    ...Object.keys(host.configured_harnesses ?? {}).filter((harness) => harness.startsWith("acp:")),
  ];

  const entryRows: { harness: string; entry: CallingDefaultEntry }[] = [];
  const known = new Set<string>();
  for (const harness of harnesses) {
    const entry = hostTable[harness];
    if (entry) {
      entryRows.push({ harness, entry });
      known.add(harness);
    }
  }
  for (const [harness, entry] of Object.entries(hostTable)) {
    if (!known.has(harness)) entryRows.push({ harness, entry });
  }
  const followsRows = Object.entries(SDK_NATIVE_PARENT)
    .filter(([sdk]) => !(sdk in hostTable))
    .map(([sdk, native]) => ({ sdk, native }));
  const candidates = harnesses.filter(
    (harness) =>
      !(harness in hostTable) && !(harness in SDK_NATIVE_PARENT) && harnessOnHost(host, harness),
  );

  const setModel = (harness: string, entry: CallingDefaultEntry, next: string | null) => {
    setCallingDefaultField(hostId, harness, "model", next);
    const rows = catalogFor(catalogs, hostId, harness)?.models ?? [];
    const reconciled = reconcileEffortOnModelChange(harness, rows, next, entry.effort ?? null);
    if (reconciled !== (entry.effort ?? null)) {
      setCallingDefaultField(hostId, harness, "effort", reconciled);
    }
    if (entry.speed && (harness === "codex" || harness === "codex-native")) {
      const reconciledSpeed = reconcileSpeed(entry.speed, rows, next);
      if (reconciledSpeed !== entry.speed) {
        setCallingDefaultField(hostId, harness, "speed", reconciledSpeed);
      }
    }
  };

  const renderEntryControls = (harness: string, entry: CallingDefaultEntry) => [
    <ModelSettingSelect
      key="model"
      hostId={hostId}
      harness={harness}
      value={entry.model}
      catalog={catalogFor(catalogs, hostId, harness)}
      onChange={(next) => setModel(harness, entry, next)}
      onOpen={onLoadCatalog}
    />,
    <EffortSettingSelect
      key="effort"
      hostId={hostId}
      harness={harness}
      model={entry.model}
      value={entry.effort}
      models={catalogFor(catalogs, hostId, harness)?.models ?? []}
      onChange={(next) => setCallingDefaultField(hostId, harness, "effort", next)}
      onOpen={onLoadCatalog}
    />,
    ...(["speed", "permission"] as const).map((field) => (
      <SessionDefaultModeSelect
        key={field}
        harness={harness}
        field={field}
        value={entry[field]}
        models={catalogFor(catalogs, hostId, harness)?.models ?? []}
        model={entry.model}
        label={`${field === "speed" ? "Speed" : "Permission"} for ${callingHarnessLabel(harness)}`}
        testId={`calling-defaults-${field}-${harness}`}
        onChange={(next) => setCallingDefaultField(hostId, harness, field, next)}
      />
    )),
  ];

  if (!isMobile) {
    return (
      <div className="flex min-w-0 flex-col">
        <table
          className="w-full table-fixed border-collapse text-ui"
          data-testid="calling-defaults-table"
        >
          <colgroup>
            <col className="w-[18%]" />
            <col className="w-[24%]" />
            <col className="w-[16%]" />
            <col className="w-[16%]" />
            <col className="w-[20%]" />
            <col className="w-[6%]" />
          </colgroup>
          <thead>
            <tr className="border-b border-border text-left text-muted-foreground">
              <th scope="col" className="py-2 pr-3 font-normal">
                Harness
              </th>
              <th scope="col" className="py-2 pr-3 font-normal">
                Model
              </th>
              <th scope="col" className="py-2 pr-3 font-normal">
                Effort
              </th>
              <th scope="col" className="py-2 pr-3 font-normal">
                Speed
              </th>
              <th scope="col" className="py-2 pr-3 font-normal">
                Permission
              </th>
              <th scope="col" className="py-2">
                <span className="sr-only">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {entryRows.map(({ harness, entry }) => {
              const note = catalogNote(catalogFor(catalogs, hostId, harness), host);
              const [modelControl, effortControl, speedControl, permissionControl] =
                renderEntryControls(harness, entry);
              return (
                <tr
                  key={harness}
                  className="border-b border-border/60 align-middle"
                  data-testid={`calling-defaults-row-${harness}`}
                >
                  <td className="min-w-0 py-2 pr-3">
                    <span className="block truncate" title={callingHarnessLabel(harness)}>
                      {callingHarnessLabel(harness)}
                    </span>
                    {note && (
                      <span
                        className={cn(
                          "block truncate text-xs",
                          note.destructive ? "text-destructive" : "text-muted-foreground",
                        )}
                        title={note.text}
                      >
                        {note.text}
                      </span>
                    )}
                  </td>
                  <td className="min-w-0 py-2 pr-3">{modelControl}</td>
                  <td className="min-w-0 py-2 pr-3">{effortControl}</td>
                  <td className="min-w-0 py-2 pr-3">{speedControl}</td>
                  <td className="min-w-0 py-2 pr-3">{permissionControl}</td>
                  <td className="py-2 text-right">
                    <DeleteEntryButton
                      harness={harness}
                      onRemove={() => removeCallingDefaultEntry(hostId, harness)}
                    />
                  </td>
                </tr>
              );
            })}
            {followsRows.map(({ sdk, native }) => {
              const summary = entrySummary(hostTable[native], catalogFor(catalogs, hostId, native));
              const text = summary
                ? `Follows ${callingHarnessLabel(native)} (${summary})`
                : `Follows ${callingHarnessLabel(native)}`;
              return (
                <tr
                  key={sdk}
                  className="border-b border-border/60 align-middle text-muted-foreground"
                  data-testid={`calling-defaults-follows-${sdk}`}
                >
                  <td className="min-w-0 py-2 pr-3">
                    <span className="block truncate" title={callingHarnessLabel(sdk)}>
                      {callingHarnessLabel(sdk)}
                    </span>
                  </td>
                  <td className="min-w-0 py-2 pr-3" colSpan={4}>
                    <span className="block truncate" title={text}>
                      {text}
                    </span>
                  </td>
                  <td className="py-2 text-right">
                    <Button
                      type="button"
                      variant="ghost"
                      size="xs"
                      onClick={() => addCallingDefaultEntry(hostId, sdk)}
                      data-testid={`calling-defaults-set-separately-${sdk}`}
                    >
                      Set separately
                    </Button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {candidates.length > 0 && (
          <div className="border-t border-border px-1 py-2">
            <AddHarnessControl
              options={candidates}
              onAdd={(harness) => addCallingDefaultEntry(hostId, harness)}
            />
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="flex min-w-0 flex-col gap-2">
      {entryRows.map(({ harness, entry }) => {
        const note = catalogNote(catalogFor(catalogs, hostId, harness), host);
        const [modelControl, effortControl, speedControl, permissionControl] = renderEntryControls(
          harness,
          entry,
        );
        return (
          <div
            key={harness}
            className="flex min-w-0 flex-col gap-3 rounded-lg border border-border p-3"
            data-testid={`calling-defaults-row-${harness}`}
          >
            <div className="flex min-w-0 items-start justify-between gap-3">
              <span
                className="min-w-0 truncate text-ui font-medium"
                title={callingHarnessLabel(harness)}
              >
                {callingHarnessLabel(harness)}
              </span>
              <DeleteEntryButton
                harness={harness}
                onRemove={() => removeCallingDefaultEntry(hostId, harness)}
              />
            </div>
            {note && (
              <span
                className={cn(
                  "block truncate text-xs",
                  note.destructive ? "text-destructive" : "text-muted-foreground",
                )}
                title={note.text}
              >
                {note.text}
              </span>
            )}
            <label className="flex min-w-0 flex-col gap-1">
              <span className="text-xs text-muted-foreground">Model</span>
              {modelControl}
            </label>
            <label className="flex min-w-0 flex-col gap-1">
              <span className="text-xs text-muted-foreground">Effort</span>
              {effortControl}
            </label>
            {(["speed", "permission"] as const).map((field, index) =>
              sessionDefaultModeOptions(harness, field).length > 0 ? (
                <label key={field} className="flex min-w-0 flex-col gap-1">
                  <span className="text-xs text-muted-foreground">
                    {field === "speed" ? "Speed" : "Permission"}
                  </span>
                  {index === 0 ? speedControl : permissionControl}
                </label>
              ) : null,
            )}
          </div>
        );
      })}
      {followsRows.map(({ sdk, native }) => {
        const summary = entrySummary(hostTable[native], catalogFor(catalogs, hostId, native));
        const text = summary
          ? `Follows ${callingHarnessLabel(native)} (${summary})`
          : `Follows ${callingHarnessLabel(native)}`;
        return (
          <div
            key={sdk}
            className="flex min-w-0 flex-col items-start gap-2 rounded-lg border border-border p-3"
            data-testid={`calling-defaults-follows-${sdk}`}
          >
            <span className="min-w-0 truncate text-ui font-medium" title={callingHarnessLabel(sdk)}>
              {callingHarnessLabel(sdk)}
            </span>
            <span className="min-w-0 text-sm text-muted-foreground">{text}</span>
            <Button
              type="button"
              variant="ghost"
              size="xs"
              onClick={() => addCallingDefaultEntry(hostId, sdk)}
              data-testid={`calling-defaults-set-separately-${sdk}`}
            >
              Set separately
            </Button>
          </div>
        );
      })}
      <AddHarnessControl
        options={candidates}
        onAdd={(harness) => addCallingDefaultEntry(hostId, harness)}
      />
    </div>
  );
}

export function CallingDefaultsSection() {
  const { data: hosts } = useHosts();
  const master = useCallingDefaults();
  const carryOver = useCallingLastEnabled();
  const { data: agents } = useAvailableAgents();
  const customCatalog = useCustomAgents();
  const templates = useProjectHostAgents();
  const isMobile = useIsMobileViewport();
  const [selectedHostId, setSelectedHostId] = useState<string | null>(null);
  const [catalogs, setCatalogs] = useState<CallingDefaultsCatalogRow[]>([]);
  const [syncing, setSyncing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const requestedPairs = useRef(new Set<string>());
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  useEffect(() => {
    void listCallingDefaultCatalogs()
      .then((rows) => {
        if (alive.current) setCatalogs(rows);
      })
      .catch((err: unknown) => {
        if (alive.current) setError(errorText(err));
      });
  }, []);

  const handleSync = async () => {
    setSyncing(true);
    setError(null);
    try {
      const rows = await syncCallingDefaults();
      if (!alive.current) return;
      setCatalogs(rows);
      requestedPairs.current.clear();
    } catch (err: unknown) {
      if (alive.current) setError(errorText(err));
    } finally {
      if (alive.current) setSyncing(false);
    }
  };

  // One filtered sync per pair until the next full sync, so opening a dropdown
  // on a pair with no cached row fills it without retrying on every open.
  const loadCatalog = (hostId: string, harness: string) => {
    if (catalogFor(catalogs, hostId, harness)) return;
    const key = pairKey(hostId, harness);
    if (requestedPairs.current.has(key)) return;
    requestedPairs.current.add(key);
    void syncCallingDefaults({ hostId, harness })
      .then((rows) => {
        if (alive.current) setCatalogs((previous) => mergeCatalogRows(previous, rows));
      })
      .catch((err: unknown) => {
        if (alive.current) setError(errorText(err));
      });
  };

  const hostList = hosts ?? [];
  const host = hostList.find((candidate) => candidate.host_id === selectedHostId) ?? hostList[0];
  const templateAgents = selectableSessionAgents([
    ...(agents ?? []).filter((agent) => !agent.templateId),
    ...(customCatalog.data ?? []).map(customAgentForPicker),
  ]).filter((agent) =>
    agent.members?.length
      ? agent.members.every(
          (member) =>
            harnessReadinessOnHost(
              member.harness,
              member.host_id
                ? hostList.find((candidate) => candidate.host_id === member.host_id)
                : host,
            ).selectable,
        )
      : harnessReadinessOnHost(agent.harness, host).selectable,
  );
  const lastFetchedAt = useMemo(() => {
    let newest: number | null = null;
    for (const row of catalogs) {
      if (row.fetched_at === null) continue;
      if (newest === null || row.fetched_at > newest) newest = row.fetched_at;
    }
    return newest;
  }, [catalogs]);

  return (
    <div className="flex min-w-0 flex-col gap-6" data-testid="calling-defaults-section">
      <div className="flex min-w-0 flex-wrap items-center gap-3">
        <Button
          type="button"
          variant="outline"
          size="sm"
          loading={syncing}
          onClick={() => void handleSync()}
          data-testid="calling-defaults-sync"
        >
          Sync models
        </Button>
        <span className="min-w-0 truncate text-sm text-muted-foreground">
          {lastSyncLabel(lastFetchedAt)}
        </span>
      </div>
      {error !== null && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
      <div className="flex items-start gap-2 text-sm text-muted-foreground">
        <p>Defaults by host and harness. Projects can override them.</p>
        <HelpTip label="How session defaults are chosen">
          <p>
            Model, effort, speed and permission are resolved separately: explicit choice → project
            on this host and harness → this master table → SDK&apos;s Native project and master
            defaults → the Agent or harness default. A matching legacy project model can also take
            precedence over the master model.
          </p>
          <p>
            New Chat can reuse the last Agent, model, effort and speed for a project on this host;
            permission is resolved independently. Ordinary child sessions use the parent&apos;s
            project and host as context, without copying its running model. Named members use their
            saved settings and snapshot; empty model or effort can inherit when that snapshot is
            made. Smart Routing skips fixed model, effort, speed and permission defaults on
            automatic routes.
          </p>
        </HelpTip>
      </div>
      <div className="flex min-w-0 items-start justify-between gap-6">
        <div className="min-w-0 flex-1">
          <span className="text-ui font-medium">Reuse last session choices</span>
          <span className="mt-0.5 block text-sm text-muted-foreground">
            Reuses agent, model, effort and speed for this project and host; permission uses its
            defaults.
          </span>
        </div>
        <Switch
          aria-label="Reuse last session choices"
          checked={carryOver}
          onCheckedChange={writeCallingLastEnabled}
          className="shrink-0"
          data-testid="calling-defaults-carry-over"
          componentId="settings.calling_defaults.carry_over"
        />
      </div>
      {hostList.length === 0 ? (
        <p className="text-sm text-muted-foreground">No hosts are connected.</p>
      ) : (
        <div className="flex min-w-0 flex-col gap-4">
          <div className="flex min-w-0 flex-wrap gap-2">
            {hostList.map((candidate) => {
              const selected = candidate.host_id === host?.host_id;
              const name = candidate.name || candidate.host_id;
              return (
                <Button
                  key={candidate.host_id}
                  type="button"
                  variant={selected ? "secondary" : "outline"}
                  size="sm"
                  className="max-w-full"
                  aria-pressed={selected}
                  onClick={() => setSelectedHostId(candidate.host_id)}
                  data-testid={`calling-defaults-host-${candidate.host_id}`}
                >
                  <span className="min-w-0 truncate" title={name}>
                    {name}
                  </span>
                  {candidate.status !== "online" && (
                    <span className="shrink-0 text-xs text-muted-foreground">(offline)</span>
                  )}
                </Button>
              );
            })}
          </div>
          {host && (
            <div className="flex min-w-0 flex-col gap-2 rounded-md border p-3">
              <label className="text-ui font-medium" htmlFor="new-project-agent">
                New project agent
              </label>
              <Select
                value={templates[host.host_id] ?? "__none__"}
                onValueChange={(value) =>
                  setProjectHostAgent(host.host_id, value === "__none__" ? null : value)
                }
              >
                <SelectTrigger
                  id="new-project-agent"
                  className="w-full min-w-0"
                  data-testid="new-project-agent"
                >
                  <SelectValue />
                </SelectTrigger>
                <SelectContent position="popper" className="w-(--radix-select-trigger-width)">
                  <SelectItem value="__none__">No template</SelectItem>
                  {templateAgents.map((agent) => (
                    <SelectItem key={agent.id} value={agent.id}>
                      {agent.display_name}
                    </SelectItem>
                  ))}
                  {templates[host.host_id] &&
                    !templateAgents.some((agent) => agent.id === templates[host.host_id]) && (
                      <SelectItem value={templates[host.host_id]!} disabled>
                        Unavailable agent
                      </SelectItem>
                    )}
                </SelectContent>
              </Select>
              <p className="text-sm text-muted-foreground">
                Used for new bindings when the project has no All hosts agent. Existing bindings
                keep their agent.
              </p>
            </div>
          )}
          {host && (
            <HostDefaultsTable
              host={host}
              master={master}
              catalogs={catalogs}
              isMobile={isMobile}
              onLoadCatalog={loadCatalog}
            />
          )}
        </div>
      )}
    </div>
  );
}
