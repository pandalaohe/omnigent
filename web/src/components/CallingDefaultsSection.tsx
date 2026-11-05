// Settings › Harnesses: the per-host × harness master table of calling defaults,
// the model-catalog sync that fills its dropdowns, and the
// web-only carry-over switch. The resolution chain itself is server-side; this
// surface edits only the master layer and the cached catalogs.

import { useEffect, useMemo, useRef, useState } from "react";

import { AgentDefaultsTable } from "@/components/AgentDefaultsTable";
import { SessionDefaultModeSelect } from "@/components/SessionDefaultModeSelect";
import { HelpTip } from "@/components/HelpTip";
import { sessionDefaultModeOptions } from "@/lib/sessionDefaultModes";
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
import { useHarnessCapabilities, type HarnessCapability } from "@/lib/agentLabels";
import {
  type CallingDefaultsTable,
  callingDefaultHarnesses,
  callingHarnessLabel,
  setCallingDefaultField,
  patchCallingDefaults,
  callingDefaultsPatch,
  useCallingDefaults,
  useCallingLastEnabled,
  writeCallingLastEnabled,
} from "@/lib/callingDefaults";
import {
  listCallingDefaultCatalogs,
  syncCallingDefaults,
  type CallingDefaultsCatalogRow,
} from "@/lib/callingDefaultsApi";
import { relativeTime } from "@/lib/relativeTime";
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

function HostSpeedDefaults({
  host,
  master,
  catalogs,
  capabilities,
}: {
  host: Host;
  master: CallingDefaultsTable;
  catalogs: CallingDefaultsCatalogRow[];
  capabilities: Readonly<Record<string, HarnessCapability>>;
}) {
  const harnesses = callingDefaultHarnesses(capabilities).filter(
    (harness) =>
      harnessReadinessOnHost(harness, host).selectable &&
      sessionDefaultModeOptions(harness, "speed", [], null, "default", capabilities).length > 0,
  );
  if (!harnesses.length) return null;
  return (
    <div className="flex min-w-0 flex-col gap-2" aria-label="Speed defaults">
      {harnesses.map((harness) => {
        const entry = master[host.host_id]?.[harness];
        return (
          <label key={harness} className="flex min-w-0 items-center justify-between gap-3">
            <span className="text-sm">{callingHarnessLabel(harness)} speed</span>
            <div className="w-40 min-w-0">
              <SessionDefaultModeSelect
                harness={harness}
                field="speed"
                value={entry?.speed}
                models={catalogFor(catalogs, host.host_id, harness)?.models ?? []}
                model={entry?.model}
                capabilities={capabilities}
                label={`Speed for ${callingHarnessLabel(harness)}`}
                testId={`calling-defaults-speed-${harness}`}
                onChange={(next) => setCallingDefaultField(host.host_id, harness, "speed", next)}
              />
            </div>
          </label>
        );
      })}
    </div>
  );
}

export function CallingDefaultsSection() {
  const { data: hosts } = useHosts();
  const capabilities = useHarnessCapabilities();
  const master = useCallingDefaults();
  const carryOver = useCallingLastEnabled();
  const { data: agents } = useAvailableAgents();
  const customCatalog = useCustomAgents();
  const templates = useProjectHostAgents();
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
          <AgentDefaultsTable
            hosts={hostList}
            master={master}
            catalogs={catalogs}
            capabilities={capabilities}
            onLoadCatalog={loadCatalog}
            onChange={(next) => patchCallingDefaults(callingDefaultsPatch(master, next))}
          />
          {host && (
            <HostSpeedDefaults
              host={host}
              master={master}
              catalogs={catalogs}
              capabilities={capabilities}
            />
          )}
        </div>
      )}
    </div>
  );
}
