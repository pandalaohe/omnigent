import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { AgentPicker } from "@/components/AgentPicker";
import { NumericField, SettingRow } from "@/components/SettingsFields";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Switch } from "@/components/ui/switch";
import { reportedOnAnyHost, useAgentRegistry } from "@/hooks/useAgentRegistry";
import { useHosts } from "@/hooks/useHosts";
import { useKeepWarmPreferences } from "@/hooks/useKeepWarmPreferences";
import { useHarnessCatalog, type HarnessCapability } from "@/lib/agentLabels";
import { getOmnigentServerIdentity } from "@/lib/host";
import { authenticatedFetch, getCurrentUserId, subscribeIdentity } from "@/lib/identity";
import {
  clampHostOfflineArchiveSeconds,
  clampKeepWarmIntervalSeconds,
  clampKeepWarmMaxSeconds,
  defaultKeepWarmIntervalSeconds,
  keepWarmFamilyForHarness,
  keepWarmIntervalBoundsSeconds,
  normalizeKeepWarmPreferences,
  readKeepWarmPreferences,
  resolveKeepWarmCell,
  saveKeepWarmPreferences,
  turnOffKeepWarmCell,
  KEEP_WARM_DEFAULT_MAX_SECONDS,
  KEEP_WARM_MAX_BOUNDS_SECONDS,
  KEEP_WARM_COLD_AFTER_BOUNDS_SECONDS,
  KEEP_WARM_HOST_OFFLINE_ARCHIVE_BOUNDS_SECONDS,
  type AgentKeepWarmPreferences,
  type KeepWarmCell,
  type KeepWarmCells,
  type KeepWarmFamily,
  type KeepWarmPreferences,
} from "@/lib/keepWarmPreferences";
import { ApiError, apiErrorFromResponse } from "@/lib/sessionsApi";

const EMPTY_CAPABILITIES: Record<string, HarnessCapability> = {};
interface Conversion {
  cells: KeepWarmCells;
  columns: string[];
  already: boolean;
  migratedToCellsAt?: number;
}

async function convert(dryRun: boolean, signal: AbortSignal): Promise<Conversion> {
  const response = await authenticatedFetch("/v1/me/preferences/keep_warm/convert", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ dry_run: dryRun }),
    signal,
  });
  if (!response.ok) throw await apiErrorFromResponse(response);
  const body = (await response.json()) as Partial<Conversion>;
  if (
    !body.cells ||
    typeof body.cells !== "object" ||
    Array.isArray(body.cells) ||
    !Array.isArray(body.columns)
  ) {
    throw new Error("The server returned invalid keep-warm settings.");
  }
  const normalized = normalizeKeepWarmPreferences(body);
  return {
    cells: normalized.cells!,
    columns: normalized.columns ?? [],
    already: body.already === true,
    migratedToCellsAt: normalized.migratedToCellsAt,
  };
}

function scopeKey() {
  return JSON.stringify([getOmnigentServerIdentity(), getCurrentUserId()]);
}

function columnsFor(preferences: KeepWarmPreferences): string[] {
  return [
    ...new Set([
      ...(preferences.columns ?? []),
      ...Object.values(preferences.cells ?? {}).flatMap((rows) => Object.keys(rows)),
    ]),
  ];
}

function compactCell(row: AgentKeepWarmPreferences, family: KeepWarmFamily | null): KeepWarmCell {
  const interval =
    family === "pi"
      ? undefined
      : family
        ? clampKeepWarmIntervalSeconds(
            family,
            row.intervalSeconds ?? defaultKeepWarmIntervalSeconds(family),
          )
        : row.intervalSeconds;
  const max = clampKeepWarmMaxSeconds(row.maxSeconds);
  return {
    ...(row.main ? { main: true } : {}),
    ...(row.child ? { child: true } : {}),
    ...(interval !== undefined && (!family || interval !== defaultKeepWarmIntervalSeconds(family))
      ? { intervalSeconds: interval }
      : {}),
    ...(family !== "pi" && max !== KEEP_WARM_DEFAULT_MAX_SECONDS ? { maxSeconds: max } : {}),
    ...(row.coldAfterSeconds !== undefined ? { coldAfterSeconds: row.coldAfterSeconds } : {}),
  };
}

function CellEditor({
  host,
  agent,
  row,
  family,
  busy,
  error,
  onSave,
  onOff,
  onClose,
}: {
  host: string;
  agent: string;
  row: AgentKeepWarmPreferences;
  family: KeepWarmFamily | null;
  busy: boolean;
  error?: string;
  onSave: (row: AgentKeepWarmPreferences, axis?: "host" | "agent") => void;
  onOff: () => void;
  onClose: () => void;
}) {
  const [draft, setDraft] = useState(row);
  const interval = family && family !== "pi" ? keepWarmIntervalBoundsSeconds(family) : null;
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !busy) onClose();
      }}
    >
      <DialogContent className="sm:max-w-md" showCloseButton={!busy}>
        <DialogHeader>
          <DialogTitle>
            {host} / {agent}
          </DialogTitle>
          <DialogDescription>
            Settings for this host and agent. Fill copies these values to one row or column.
          </DialogDescription>
        </DialogHeader>
        {error && (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        )}
        <div className="space-y-4">
          <SettingRow label="Main sessions" hint="Keep non-archived main sessions warm.">
            <Switch
              aria-label="Keep main sessions warm"
              checked={draft.main}
              disabled={busy || !family}
              onCheckedChange={(main) => setDraft({ ...draft, main })}
            />
          </SettingRow>
          <SettingRow label="Children" hint="Keep active sub-agent sessions warm.">
            <Switch
              aria-label="Keep children warm"
              checked={draft.child}
              disabled={busy || !family}
              onCheckedChange={(child) => setDraft({ ...draft, child })}
            />
          </SettingRow>
          {interval && (
            <SettingRow
              label="Every"
              hint="Quiet gap between cache-read pings. Each ping costs one cache read."
            >
              <NumericField
                ariaLabel="Keep-warm interval in minutes"
                value={(draft.intervalSeconds ?? defaultKeepWarmIntervalSeconds(family!)) / 60}
                min={interval.min / 60}
                max={interval.max / 60}
                disabled={busy}
                onCommit={(minutes) => setDraft({ ...draft, intervalSeconds: minutes * 60 })}
              />
              <span className="text-sm text-muted-foreground">min</span>
            </SettingRow>
          )}
          <SettingRow
            label="Longest"
            hint={
              family === "pi"
                ? "Pi's own warmer stops after 30 minutes."
                : "Stop warming this long after the last real turn."
            }
          >
            {family === "pi" ? (
              <span className="text-sm">30 min (Pi limit)</span>
            ) : (
              <>
                <NumericField
                  ariaLabel="Longest keep-warm run in hours"
                  value={draft.maxSeconds / 3600}
                  min={KEEP_WARM_MAX_BOUNDS_SECONDS.min / 3600}
                  max={KEEP_WARM_MAX_BOUNDS_SECONDS.max / 3600}
                  disabled={busy || !family}
                  onCommit={(hours) => setDraft({ ...draft, maxSeconds: hours * 3600 })}
                />
                <span className="text-sm text-muted-foreground">hours</span>
              </>
            )}
          </SettingRow>
          <SettingRow
            label="Cold after"
            hint="Idle time before the blue dot appears. Empty uses the platform rule; 0 means never cold."
          >
            <NumericField
              ariaLabel="Cold after in minutes"
              value={draft.coldAfterSeconds === undefined ? null : draft.coldAfterSeconds / 60}
              min={0}
              max={KEEP_WARM_COLD_AFTER_BOUNDS_SECONDS.max / 60}
              disabled={busy}
              placeholder="auto"
              onClear={() => {
                const next = { ...draft };
                delete next.coldAfterSeconds;
                setDraft(next);
              }}
              onCommit={(minutes) => setDraft({ ...draft, coldAfterSeconds: minutes * 60 })}
            />
            <span className="text-sm text-muted-foreground">min</span>
          </SettingRow>
          {!family && (
            <p className="text-xs text-muted-foreground">
              Cache warming is unavailable for this agent. Cold-after settings still apply.
            </p>
          )}
        </div>
        <div className="flex flex-wrap gap-2 border-t pt-4">
          <Button variant="outline" size="sm" disabled={busy} onClick={() => onSave(draft, "host")}>
            Fill host column
          </Button>
          <Button
            variant="outline"
            size="sm"
            disabled={busy}
            onClick={() => onSave(draft, "agent")}
          >
            Fill agent row
          </Button>
          <Button variant="ghost" size="sm" disabled={busy} onClick={onOff}>
            Turn off
          </Button>
        </div>
        <DialogFooter>
          <Button variant="outline" disabled={busy} onClick={onClose}>
            Cancel
          </Button>
          <Button disabled={busy} onClick={() => onSave(draft)}>
            {busy ? "Saving…" : "Save cell"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function KeepWarmTable() {
  const scope = useSyncExternalStore(subscribeIdentity, scopeKey, () => "");
  return <ScopedKeepWarmTable key={scope} scope={scope} />;
}

function ScopedKeepWarmTable({ scope }: { scope: string }) {
  const preferences = useKeepWarmPreferences();
  const registry = useAgentRegistry();
  const hostsQuery = useHosts({ includeSandbox: true });
  const capabilities = useHarnessCatalog(
    (catalog) => catalog.capabilitiesByHarness,
    EMPTY_CAPABILITIES,
  );
  const client = useQueryClient();
  const queryKey = ["keep-warm-conversion", scope];
  const conversion = useQuery({
    queryKey,
    queryFn: ({ signal }) => convert(true, signal),
    retry: false,
    refetchOnWindowFocus: false,
    staleTime: Infinity,
  });
  const [pending, setPending] = useState<KeepWarmPreferences | null>(null);
  const [saveError, setSaveError] = useState<Error | null>(null);
  const [busy, setBusy] = useState(false);
  const saving = useRef(false);
  const lifetime = useRef<AbortController | null>(null);
  const [editing, setEditing] = useState<{ host: string; key: string } | null>(null);
  const [removing, setRemoving] = useState<string | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    lifetime.current = controller;
    return () => controller.abort();
  }, []);

  const data = pending ?? {
    ...preferences,
    ...(conversion.data
      ? {
          cells: preferences.cells ?? conversion.data.cells,
          columns: [...new Set([...(preferences.columns ?? []), ...conversion.data.columns])],
          migratedToCellsAt: preferences.migratedToCellsAt ?? conversion.data.migratedToCellsAt,
        }
      : {}),
  };
  const columns = columnsFor(data);
  const byKey = useMemo(
    () =>
      new Map(
        [...registry.items, ...Object.values(registry.legacyProducts)].map((item) => [
          item.key,
          item,
        ]),
      ),
    [registry.items, registry.legacyProducts],
  );
  const harnessFor = (key: string) =>
    key.startsWith("harness:") ? key.slice(8) : byKey.get(key)?.harness;
  const baseLabelFor = (key: string) => byKey.get(key)?.label ?? registry.legacyLabels[key] ?? key;
  const columnsByLabel = new Map<string, string[]>();
  for (const key of columns) {
    const label = baseLabelFor(key);
    columnsByLabel.set(label, [...(columnsByLabel.get(label) ?? []), key]);
  }
  const labelFor = (key: string) => {
    const label = baseLabelFor(key);
    const matches = columnsByLabel.get(label) ?? [];
    if (matches.length < 2) return label;
    const harness = harnessFor(key);
    const suffix =
      harness && matches.filter((column) => harnessFor(column) === harness).length === 1
        ? harness
        : key;
    return `${label} · ${suffix}`;
  };
  const familyFor = (key: string) => keepWarmFamilyForHarness(harnessFor(key), capabilities);
  const rowFor = (host: string, key: string) => {
    const row = resolveKeepWarmCell(data, host, key, harnessFor(key));
    const family = familyFor(key);
    return {
      ...row,
      maxSeconds: clampKeepWarmMaxSeconds(row.maxSeconds),
      ...(family && family !== "pi"
        ? {
            intervalSeconds: clampKeepWarmIntervalSeconds(
              family,
              row.intervalSeconds ?? defaultKeepWarmIntervalSeconds(family),
            ),
          }
        : {}),
    };
  };
  const knownHosts = new Map((hostsQuery.data ?? []).map((host) => [host.host_id, host]));
  const hosts = [
    ...new Set([
      "",
      ...(hostsQuery.data ?? [])
        .filter((host) => !host.sandbox_provider)
        .map((host) => host.host_id),
      ...Object.keys(data.cells ?? {}),
    ]),
  ];
  const hostName = (id: string) => (id === "" ? "Server" : (knownHosts.get(id)?.name ?? id));
  const error = saveError ?? conversion.error;
  const unavailable = error instanceof ApiError && error.status === 404;
  const editable = !!conversion.data && !unavailable && !busy;
  const rejectedSave = saveError && "status" in saveError && saveError.status === 422;

  function discardPending() {
    setPending(null);
    setSaveError(null);
    setEditing(null);
    setRemoving(null);
  }

  async function save(change: (current: KeepWarmPreferences) => KeepWarmPreferences) {
    if (saving.current || !conversion.data || unavailable || scope !== scopeKey()) return;
    const signal = lifetime.current?.signal;
    if (!signal || signal.aborted) return;
    saving.current = true;
    setBusy(true);
    setSaveError(null);
    setPending(change(data));
    try {
      let authoritative = conversion.data;
      let freshlyConverted = false;
      if (!authoritative.already) {
        authoritative = await convert(false, signal);
        if (signal.aborted || scope !== scopeKey()) return;
        client.setQueryData(queryKey, {
          ...authoritative,
          already: true,
          columns: [...new Set([...columns, ...authoritative.columns])],
        });
        freshlyConverted = true;
      }
      const current = readKeepWarmPreferences();
      const base = normalizeKeepWarmPreferences({
        ...current,
        cells: freshlyConverted ? authoritative.cells : (current.cells ?? authoritative.cells),
        migratedToCellsAt: authoritative.migratedToCellsAt ?? current.migratedToCellsAt,
        columns: [...new Set([...columns, ...authoritative.columns, ...(current.columns ?? [])])],
      });
      const next = change(base);
      setPending(next);
      await saveKeepWarmPreferences(
        next,
        [...byKey.values()].map((item) => ({ id: item.key, harness: item.harness })),
        signal,
      );
      if (signal.aborted || scope !== scopeKey()) return;
      client.setQueryData(queryKey, {
        cells: next.cells,
        columns: next.columns ?? [],
        already: true,
        migratedToCellsAt: next.migratedToCellsAt,
      });
      setPending(null);
      setEditing(null);
      setRemoving(null);
    } catch (caught) {
      if (!signal.aborted && scope === scopeKey())
        setSaveError(
          caught instanceof Error ? caught : new Error("Couldn't save keep-warm settings."),
        );
    } finally {
      saving.current = false;
      if (!signal.aborted) setBusy(false);
    }
  }

  function editCells(row: AgentKeepWarmPreferences, axis?: "host" | "agent") {
    if (!editing) return;
    void save((base) => {
      let next: KeepWarmPreferences = {
        ...base,
        columns: columnsFor(base),
        cells: { ...base.cells },
      };
      const targets =
        axis === "host"
          ? columns.map((key) => ({ host: editing.host, key }))
          : axis === "agent"
            ? hosts.map((host) => ({ host, key: editing.key }))
            : [editing];
      for (const target of targets) {
        const cell = compactCell(row, familyFor(target.key));
        next = turnOffKeepWarmCell(next, target.host, target.key, harnessFor(target.key));
        const stored = Object.keys(cell).length ? cell : next.cells?.[target.host]?.[target.key];
        if (stored)
          next.cells![target.host] = { ...next.cells?.[target.host], [target.key]: stored };
      }
      return next;
    });
  }

  return (
    <div className="space-y-4" data-testid="keep-warm-table">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="space-y-1">
          <p className="text-sm font-medium">Keep sessions warm</p>
          <p className="text-xs text-muted-foreground">
            New hosts start off; new agents start off unless their harness has a row.
          </p>
        </div>
        <AgentPicker
          items={registry.items}
          taken={new Set(columns)}
          supports={(item) =>
            keepWarmFamilyForHarness(item.harness, capabilities)
              ? null
              : "This agent has no declared keep-warm channel"
          }
          title="Add agent"
          description="Add an agent or harness row. Agents may follow matching harness cells."
          trigger={
            <Button variant="outline" size="sm" disabled={!editable}>
              Add agent
            </Button>
          }
          isLoading={registry.isLoading}
          isError={registry.isError}
          onRetry={registry.retry}
          onPick={(key) => {
            void save((base) => ({ ...base, columns: [...new Set([...columnsFor(base), key])] }));
          }}
        />
      </div>
      {conversion.isPending && (
        <p role="status" className="text-sm text-muted-foreground">
          Loading keep-warm settings…
        </p>
      )}
      {conversion.data && !conversion.data.already && (
        <p className="rounded-md border bg-muted/40 px-3 py-2 text-sm">
          Converted from your previous settings · saved when you first edit
        </p>
      )}
      {error && (
        <div
          role="alert"
          className="space-y-2 rounded-md border border-destructive/40 px-3 py-2 text-sm"
        >
          <p>
            {unavailable
              ? "Server update needed. Keep-warm settings are read-only until the server is updated."
              : `Save wasn't confirmed: ${error.message}`}
          </p>
          {!unavailable && !conversion.data && (
            <Button
              variant="outline"
              size="sm"
              onClick={() => {
                void conversion.refetch();
              }}
            >
              Retry
            </Button>
          )}
          {rejectedSave && !busy && (
            <Button variant="outline" size="sm" onClick={discardPending}>
              Discard unsaved changes
            </Button>
          )}
        </div>
      )}
      {conversion.data ? (
        <div className="max-h-[32rem] overflow-auto rounded-lg border">
          <table className="w-full border-collapse text-left text-sm">
            <caption className="sr-only">Keep-warm settings by host and agent</caption>
            <thead>
              <tr>
                <th scope="col" className="sticky top-0 left-0 z-30 min-w-36 bg-card-solid p-3">
                  Agent / Host
                </th>
                {hosts.map((host) => (
                  <th
                    scope="col"
                    key={host}
                    className="sticky top-0 z-20 min-w-48 border-l bg-card-solid p-3 align-top"
                  >
                    <span className="block max-w-56 break-words font-medium">{hostName(host)}</span>
                    {host !== "" && (
                      <span className="mt-1 block text-xs font-normal text-muted-foreground">
                        {knownHosts.get(host)?.status ?? "Unavailable host"}
                      </span>
                    )}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {columns.map((key) => (
                <tr key={key} className="border-t">
                  <th
                    scope="row"
                    className="sticky left-0 z-10 max-w-52 bg-card-solid p-3 align-top font-medium"
                  >
                    <span className="block break-words">{labelFor(key)}</span>
                    <Button
                      variant="ghost"
                      size="sm"
                      className="-ml-2 mt-1 h-7 text-xs"
                      disabled={!editable}
                      aria-label={`Remove ${labelFor(key)} row`}
                      onClick={() => setRemoving(key)}
                    >
                      Remove
                    </Button>
                  </th>
                  {hosts.map((host) => {
                    const family = familyFor(key);
                    const row = rowFor(host, key);
                    const machine = knownHosts.get(host);
                    const deployed =
                      host === "" ||
                      (machine
                        ? reportedOnAnyHost(
                            harnessFor(key) ?? null,
                            capabilities[harnessFor(key) ?? ""],
                            [machine],
                          )
                        : false);
                    const inherited =
                      !data.cells?.[host]?.[key] &&
                      !!harnessFor(key) &&
                      !!data.cells?.[host]?.[`harness:${harnessFor(key)}`];
                    return (
                      <td key={host} className="border-l p-1 align-top">
                        <button
                          type="button"
                          disabled={!editable}
                          aria-label={`Edit ${hostName(host)} / ${labelFor(key)}`}
                          onClick={() => setEditing({ host, key })}
                          className={`w-full rounded-md p-3 text-left enabled:hover:bg-muted focus-visible:outline-2 focus-visible:outline-ring ${deployed ? "" : "bg-muted/30 text-muted-foreground"}`}
                        >
                          <span className="block font-medium">
                            {row.main ? "Main on" : "Main off"} ·{" "}
                            {row.child ? "Children on" : "Children off"}
                          </span>
                          <span className="mt-1 block text-xs text-muted-foreground">
                            {family && family !== "pi"
                              ? `Every ${row.intervalSeconds! / 60} min · `
                              : ""}
                            Longest {family === "pi" ? "30 min" : `${row.maxSeconds / 3600} h`}
                          </span>
                          <span className="block text-xs text-muted-foreground">
                            Cold:{" "}
                            {row.coldAfterSeconds === undefined
                              ? "platform rule"
                              : row.coldAfterSeconds === 0
                                ? "never"
                                : `${row.coldAfterSeconds / 60} min`}
                            {inherited ? " · follows harness" : ""}
                          </span>
                          {!deployed && <span className="mt-1 block text-xs">not deployed</span>}
                        </button>
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
          {columns.length === 0 && (
            <p className="border-t p-4 text-sm text-muted-foreground">
              All sessions are off. Add an agent to start editing its cells.
            </p>
          )}
        </div>
      ) : (
        !conversion.isPending && (
          <div className="space-y-2 rounded-lg border p-3 text-sm">
            <p className="font-medium">Previous settings</p>
            <p>
              Default: main {preferences.default.main ? "on" : "off"} · children{" "}
              {preferences.default.child ? "on" : "off"}
            </p>
            {Object.entries(preferences.agents).map(([key, row]) => (
              <p key={key}>
                {labelFor(key)}: main {row.main ? "on" : "off"} · children{" "}
                {row.child ? "on" : "off"}
              </p>
            ))}
          </div>
        )
      )}
      <div className="border-t pt-4">
        <SettingRow
          label="Archive children of an offline host after"
          hint="Children whose host stays offline this long are archived automatically. 0 turns auto-archive off."
        >
          <NumericField
            ariaLabel="Archive children of an offline host after in hours"
            value={data.hostOfflineArchiveSeconds / 3600}
            min={0}
            max={KEEP_WARM_HOST_OFFLINE_ARCHIVE_BOUNDS_SECONDS.max / 3600}
            disabled={!editable}
            onCommit={(hours) => {
              void save((base) => ({
                ...base,
                hostOfflineArchiveSeconds: clampHostOfflineArchiveSeconds(hours * 3600),
              }));
            }}
          />
          <span className="text-sm text-muted-foreground">hours (0 = off)</span>
        </SettingRow>
      </div>
      {editing && (
        <CellEditor
          key={JSON.stringify(editing)}
          host={hostName(editing.host)}
          agent={labelFor(editing.key)}
          row={rowFor(editing.host, editing.key)}
          family={familyFor(editing.key)}
          busy={busy}
          error={saveError?.message}
          onClose={() => (rejectedSave ? discardPending() : setEditing(null))}
          onSave={editCells}
          onOff={() => {
            void save((base) =>
              turnOffKeepWarmCell(base, editing.host, editing.key, harnessFor(editing.key)),
            );
          }}
        />
      )}
      <Dialog
        open={removing !== null}
        onOpenChange={(open) => {
          if (!open && !busy) {
            if (rejectedSave) discardPending();
            else setRemoving(null);
          }
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Remove {removing ? labelFor(removing) : ""} row?</DialogTitle>
            <DialogDescription>
              This clears its overrides on{" "}
              {Object.values(data.cells ?? {}).filter((rows) => removing && rows[removing]).length}{" "}
              hosts. Sessions may then follow matching harness cells, including cells that keep them
              warm.
            </DialogDescription>
          </DialogHeader>
          {saveError && (
            <p role="alert" className="text-sm text-destructive">
              {saveError.message}
            </p>
          )}
          <DialogFooter>
            <Button
              variant="outline"
              disabled={busy}
              onClick={() => (rejectedSave ? discardPending() : setRemoving(null))}
            >
              Cancel
            </Button>
            <Button
              disabled={busy}
              onClick={() => {
                if (!removing) return;
                const key = removing;
                void save((base) => ({
                  ...base,
                  columns: columnsFor(base).filter((column) => column !== key),
                  cells: Object.fromEntries(
                    Object.entries(base.cells ?? {}).map(([host, rows]) => [
                      host,
                      Object.fromEntries(Object.entries(rows).filter(([column]) => column !== key)),
                    ]),
                  ),
                }));
              }}
            >
              Remove row
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
