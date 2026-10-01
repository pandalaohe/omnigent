import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import type { Host } from "@/hooks/useHosts";
import { BRAIN_HARNESS_LABELS } from "@/lib/agentLabels";
import {
  deleteUsageContextOverrides,
  patchUsageContextOverrides,
  usageContextSourceFromKey,
  writeUsageContextOverride,
  type UsageContextOverride,
  type UsageContextOverridePatch,
  type UsageContextPreferences,
  type UsageContextSource,
} from "@/lib/usageContextPreferences";

/** Radix Select rejects an empty item value, so "All" needs a sentinel. */
const ALL_FILTER = "__all__";

function positiveNumber(value: string): number | null {
  if (value.trim() === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? Math.round(parsed) : null;
}

interface SavedContextSourceRow {
  key: string;
  override: UsageContextOverride;
  hostLabel: string;
  agentLabel: string;
  harnessLabel: string;
  modelLabel: string;
  rowLabel: string;
}

function describeSource(source: UsageContextSource, hosts: Host[]) {
  const hostLabel = source.hostId
    ? (hosts.find((host) => host.host_id === source.hostId)?.name ?? source.hostId)
    : "Server";
  const agentLabel = source.agentName || "Unknown agent";
  const harnessLabel = source.harness
    ? (BRAIN_HARNESS_LABELS[source.harness] ?? source.harness)
    : "Unknown";
  const modelLabel = source.model || "Auto model";
  return { hostLabel, agentLabel, harnessLabel, modelLabel };
}

export function SavedContextSources({
  preferences,
  currentSourceKey,
  hosts,
  currentOverride,
}: {
  preferences: UsageContextPreferences;
  currentSourceKey: string;
  hosts: Host[];
  currentOverride: UsageContextOverride;
}) {
  const rows: SavedContextSourceRow[] = Object.entries(preferences.overrides).flatMap(
    ([key, override]) => {
      const source = usageContextSourceFromKey(key);
      if (!source) return [];
      const labels = describeSource(source, hosts);
      return [
        {
          key,
          override,
          ...labels,
          rowLabel: `${labels.hostLabel} ${labels.agentLabel} ${labels.modelLabel}`,
        },
      ];
    },
  );
  const hostOptions = [...new Set(rows.map((row) => row.hostLabel))].sort((a, b) =>
    a.localeCompare(b),
  );
  const agentOptions = [...new Set(rows.map((row) => row.agentLabel))].sort((a, b) =>
    a.localeCompare(b),
  );

  const [hostFilter, setHostFilter] = useState(ALL_FILTER);
  const [agentFilter, setAgentFilter] = useState(ALL_FILTER);
  const [selectedKeys, setSelectedKeys] = useState<string[]>([]);
  const [editingKey, setEditingKey] = useState<string | null>(null);
  const [editContext, setEditContext] = useState("");
  const [editBuffer, setEditBuffer] = useState("");
  const editBaselineRef = useRef<UsageContextOverride>({
    contextWindowTokens: null,
    autoCompactBufferTokens: null,
  });
  const [deleteKeys, setDeleteKeys] = useState<string[] | null>(null);
  const [batchContext, setBatchContext] = useState("");
  const [batchBuffer, setBatchBuffer] = useState("");
  const [batchContextAuto, setBatchContextAuto] = useState(false);
  const [batchBufferAuto, setBatchBufferAuto] = useState(false);

  const effectiveHostFilter = hostOptions.includes(hostFilter) ? hostFilter : ALL_FILTER;
  const effectiveAgentFilter = agentOptions.includes(agentFilter) ? agentFilter : ALL_FILTER;
  const visibleRows = rows.filter(
    (row) =>
      (effectiveHostFilter === ALL_FILTER || row.hostLabel === effectiveHostFilter) &&
      (effectiveAgentFilter === ALL_FILTER || row.agentLabel === effectiveAgentFilter),
  );
  // JSON array keys never contain a literal newline, so joining keeps the
  // effect dependency stable without re-running on every render.
  const visibleKeySignature = visibleRows.map((row) => row.key).join("\n");

  useEffect(() => {
    const visible = new Set(visibleKeySignature === "" ? [] : visibleKeySignature.split("\n"));
    setSelectedKeys((current) => {
      const next = current.filter(
        (key) => visible.has(key) && Object.hasOwn(preferences.overrides, key),
      );
      return next.length === current.length ? current : next;
    });
  }, [visibleKeySignature, preferences.overrides]);

  const editingRow = rows.find((row) => row.key === editingKey) ?? null;
  const editingRowExists = editingRow !== null;
  const editingRowContext = editingRow?.override.contextWindowTokens;
  const editingRowBuffer = editingRow?.override.autoCompactBufferTokens;

  useEffect(() => {
    if (editingKey === null) return;
    const baseline = editBaselineRef.current;
    if (
      !editingRowExists ||
      editingRowContext !== baseline.contextWindowTokens ||
      editingRowBuffer !== baseline.autoCompactBufferTokens
    ) {
      setEditingKey(null);
    }
  }, [editingKey, editingRowExists, editingRowContext, editingRowBuffer]);

  if (rows.length === 0) return null;

  const selectedKeySet = new Set(selectedKeys);
  const allShownSelected =
    visibleRows.length > 0 && visibleRows.every((row) => selectedKeySet.has(row.key));
  const someShownSelected =
    !allShownSelected && visibleRows.some((row) => selectedKeySet.has(row.key));
  const currentHasOverride =
    currentOverride.contextWindowTokens !== null ||
    currentOverride.autoCompactBufferTokens !== null;
  const contextWouldKeep = !batchContextAuto && positiveNumber(batchContext) === null;
  const bufferWouldKeep = !batchBufferAuto && positiveNumber(batchBuffer) === null;
  const editSaveDisabled =
    positiveNumber(editContext) === null && positiveNumber(editBuffer) === null;
  const anyFilterRendered = hostOptions.length >= 2 || agentOptions.length >= 2;
  const anyFilterActive = effectiveHostFilter !== ALL_FILTER || effectiveAgentFilter !== ALL_FILTER;
  const deleteTargets = (deleteKeys ?? []).flatMap((key) => {
    const row = rows.find((candidate) => candidate.key === key);
    return row ? [row] : [];
  });

  const toggleRow = (key: string, checked: boolean) => {
    setSelectedKeys((current) =>
      checked
        ? current.includes(key)
          ? current
          : [...current, key]
        : current.filter((candidate) => candidate !== key),
    );
  };

  const toggleAllShown = (checked: boolean) => {
    setSelectedKeys((current) => {
      if (!checked) {
        const shown = new Set(visibleRows.map((row) => row.key));
        return current.filter((key) => !shown.has(key));
      }
      const merged = [...current];
      for (const row of visibleRows) if (!merged.includes(row.key)) merged.push(row.key);
      return merged;
    });
  };

  const startEdit = (row: SavedContextSourceRow) => {
    editBaselineRef.current = {
      contextWindowTokens: row.override.contextWindowTokens,
      autoCompactBufferTokens: row.override.autoCompactBufferTokens,
    };
    setEditingKey(row.key);
    setEditContext(row.override.contextWindowTokens?.toString() ?? "");
    setEditBuffer(row.override.autoCompactBufferTokens?.toString() ?? "");
  };

  const saveEdit = () => {
    if (!editingRow) return;
    writeUsageContextOverride(preferences, editingRow.key, {
      contextWindowTokens: positiveNumber(editContext),
      autoCompactBufferTokens: positiveNumber(editBuffer),
    });
    setEditingKey(null);
  };

  const applyToSelected = () => {
    const patch: UsageContextOverridePatch = {};
    if (batchContextAuto) {
      patch.contextWindowTokens = null;
    } else {
      const value = positiveNumber(batchContext);
      if (value !== null) patch.contextWindowTokens = value;
    }
    if (batchBufferAuto) {
      patch.autoCompactBufferTokens = null;
    } else {
      const value = positiveNumber(batchBuffer);
      if (value !== null) patch.autoCompactBufferTokens = value;
    }
    patchUsageContextOverrides(preferences, selectedKeys, patch);
    setSelectedKeys([]);
    setBatchContext("");
    setBatchBuffer("");
    setBatchContextAuto(false);
    setBatchBufferAuto(false);
  };

  const useCurrentSource = () => {
    setBatchContextAuto(currentOverride.contextWindowTokens === null);
    setBatchContext(currentOverride.contextWindowTokens?.toString() ?? "");
    setBatchBufferAuto(currentOverride.autoCompactBufferTokens === null);
    setBatchBuffer(currentOverride.autoCompactBufferTokens?.toString() ?? "");
  };

  const confirmDelete = () => {
    if (!deleteKeys || deleteKeys.length === 0) return;
    deleteUsageContextOverrides(preferences, deleteKeys);
    setDeleteKeys(null);
    setSelectedKeys([]);
  };

  return (
    <div className="grid gap-2 border-t border-border pt-4" data-testid="saved-context-sources">
      <div>
        <h4 className="text-sm font-medium text-foreground">Saved sources</h4>
        <p className="mt-1 text-xs text-muted-foreground">
          Each computer, agent, harness, and model combination keeps its own values.
        </p>
      </div>

      {anyFilterRendered ? (
        <div className="flex flex-wrap items-end gap-2">
          {hostOptions.length >= 2 ? (
            <div className="grid gap-1 text-xs text-muted-foreground">
              <span>Computer</span>
              <Select value={effectiveHostFilter} onValueChange={setHostFilter}>
                <SelectTrigger aria-label="Computer" size="sm" className="w-44">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={ALL_FILTER}>All computers</SelectItem>
                  {hostOptions.map((host) => (
                    <SelectItem key={host} value={host}>
                      {host}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          ) : null}
          {agentOptions.length >= 2 ? (
            <div className="grid gap-1 text-xs text-muted-foreground">
              <span>Agent</span>
              <Select value={effectiveAgentFilter} onValueChange={setAgentFilter}>
                <SelectTrigger aria-label="Agent" size="sm" className="w-44">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={ALL_FILTER}>All agents</SelectItem>
                  {agentOptions.map((agent) => (
                    <SelectItem key={agent} value={agent}>
                      {agent}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          ) : null}
          <div className="flex flex-wrap items-center gap-2 pb-0.5">
            <span className="text-xs text-muted-foreground">
              Showing {visibleRows.length} of {rows.length}
            </span>
            {anyFilterActive ? (
              <Button
                variant="ghost"
                size="xs"
                onClick={() => {
                  setHostFilter(ALL_FILTER);
                  setAgentFilter(ALL_FILTER);
                }}
              >
                Reset filters
              </Button>
            ) : null}
          </div>
        </div>
      ) : null}

      {selectedKeys.length > 0 ? (
        <div className="flex flex-wrap items-end gap-2 rounded-lg border border-border bg-muted/20 p-3">
          <span className="pb-1 text-xs font-medium text-foreground">
            {selectedKeys.length} selected
          </span>
          <div className="grid gap-1 text-xs text-muted-foreground">
            <span>Context total</span>
            <div className="flex items-center gap-1">
              <Input
                type="number"
                min={1}
                step={1000}
                value={batchContextAuto ? "" : batchContext}
                placeholder={batchContextAuto ? "Auto" : "Keep"}
                disabled={batchContextAuto}
                onChange={(event) => setBatchContext(event.target.value)}
                aria-label="Context total"
                className="w-32"
              />
              <Button
                variant={batchContextAuto ? "secondary" : "outline"}
                size="sm"
                aria-pressed={batchContextAuto}
                aria-label="Auto context total"
                onClick={() => {
                  setBatchContextAuto((auto) => !auto);
                  setBatchContext("");
                }}
              >
                Auto
              </Button>
            </div>
          </div>
          <div className="grid gap-1 text-xs text-muted-foreground">
            <span>Compact buffer</span>
            <div className="flex items-center gap-1">
              <Input
                type="number"
                min={1}
                step={1000}
                value={batchBufferAuto ? "" : batchBuffer}
                placeholder={batchBufferAuto ? "Auto" : "Keep"}
                disabled={batchBufferAuto}
                onChange={(event) => setBatchBuffer(event.target.value)}
                aria-label="Compact buffer"
                className="w-32"
              />
              <Button
                variant={batchBufferAuto ? "secondary" : "outline"}
                size="sm"
                aria-pressed={batchBufferAuto}
                aria-label="Auto compact buffer"
                onClick={() => {
                  setBatchBufferAuto((auto) => !auto);
                  setBatchBuffer("");
                }}
              >
                Auto
              </Button>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-1 pb-1">
            <Button
              variant="outline"
              size="sm"
              disabled={!currentHasOverride}
              onClick={useCurrentSource}
            >
              Use current source
            </Button>
            <Button
              size="sm"
              disabled={contextWouldKeep && bufferWouldKeep}
              onClick={applyToSelected}
            >
              Apply to {selectedKeys.length} selected
            </Button>
            <Button
              variant="destructive"
              size="sm"
              onClick={() => setDeleteKeys([...selectedKeys])}
            >
              Delete selected
            </Button>
            <Button variant="ghost" size="sm" onClick={() => setSelectedKeys([])}>
              Clear selection
            </Button>
          </div>
        </div>
      ) : null}

      <div className="divide-y divide-border overflow-hidden rounded-lg border border-border">
        <div className="flex items-center gap-2 bg-muted/20 px-3 py-2 text-xs">
          <Checkbox
            checked={allShownSelected ? true : someShownSelected ? "indeterminate" : false}
            onCheckedChange={(checked) => toggleAllShown(checked === true)}
            aria-label="Select all shown"
          />
          <span className="text-muted-foreground">Select all shown</span>
        </div>
        {visibleRows.map((row) => {
          const isCurrent = row.key === currentSourceKey;
          const isEditing = row.key === editingKey;
          return (
            <div key={row.key} className="grid gap-2 px-3 py-2.5 text-xs">
              <div className="flex min-w-0 flex-wrap items-center gap-2">
                <Checkbox
                  checked={selectedKeySet.has(row.key)}
                  onCheckedChange={(checked) => toggleRow(row.key, checked === true)}
                  aria-label={`Select ${row.rowLabel}`}
                />
                <span className="truncate font-medium text-foreground">{row.hostLabel}</span>
                {isCurrent ? (
                  <span className="rounded-full bg-primary/10 px-2 py-0.5 text-[11px] font-medium text-primary">
                    Current
                  </span>
                ) : null}
                <span className="ml-auto flex shrink-0 items-center gap-1">
                  <Button
                    variant="ghost"
                    size="xs"
                    aria-label={`Edit ${row.rowLabel}`}
                    onClick={() => startEdit(row)}
                  >
                    Edit
                  </Button>
                  <Button
                    variant="ghost"
                    size="xs"
                    aria-label={`Delete ${row.rowLabel}`}
                    onClick={() => setDeleteKeys([row.key])}
                  >
                    Delete
                  </Button>
                </span>
              </div>
              {isEditing ? (
                <div
                  className="grid gap-2 sm:grid-cols-2"
                  onKeyDown={(event) => {
                    if (event.key === "Escape") setEditingKey(null);
                  }}
                >
                  <div className="grid gap-1 text-muted-foreground">
                    <span>Context total</span>
                    <Input
                      type="number"
                      min={1}
                      step={1000}
                      value={editContext}
                      placeholder="Auto"
                      onChange={(event) => setEditContext(event.target.value)}
                      aria-label="Edit context total"
                    />
                  </div>
                  <div className="grid gap-1 text-muted-foreground">
                    <span>Compact buffer</span>
                    <Input
                      type="number"
                      min={1}
                      step={1000}
                      value={editBuffer}
                      placeholder="Auto"
                      onChange={(event) => setEditBuffer(event.target.value)}
                      aria-label="Edit compact buffer"
                    />
                  </div>
                  <div className="flex flex-wrap items-center gap-2 sm:col-span-2">
                    <Button size="xs" disabled={editSaveDisabled} onClick={saveEdit}>
                      Save
                    </Button>
                    <Button variant="ghost" size="xs" onClick={() => setEditingKey(null)}>
                      Cancel
                    </Button>
                    <span className="text-muted-foreground">Use Delete to remove this source.</span>
                  </div>
                </div>
              ) : (
                <div className="grid gap-1 sm:grid-cols-2">
                  <span className="truncate text-muted-foreground">
                    {row.agentLabel} · {row.harnessLabel} · {row.modelLabel}
                  </span>
                  <span className="tabular-nums text-muted-foreground sm:text-right">
                    Context {row.override.contextWindowTokens?.toLocaleString() ?? "Auto"} · Compact
                    buffer {row.override.autoCompactBufferTokens?.toLocaleString() ?? "Auto"}
                  </span>
                </div>
              )}
            </div>
          );
        })}
      </div>

      <Dialog
        open={(deleteKeys?.length ?? 0) > 0}
        onOpenChange={(open) => {
          if (!open) setDeleteKeys(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {(deleteKeys?.length ?? 0) > 1
                ? `Delete ${deleteKeys?.length} saved sources?`
                : "Delete saved source?"}
            </DialogTitle>
            <DialogDescription>
              These sources go back to Auto and follow the values each session reports. This syncs
              to your other devices.
            </DialogDescription>
          </DialogHeader>
          <ul className="grid gap-1 text-xs text-muted-foreground">
            {deleteTargets.map((row) => (
              <li key={row.key} className="truncate">
                {row.hostLabel} · {row.harnessLabel} · {row.modelLabel}
                {row.key === currentSourceKey ? " (Current)" : ""}
              </li>
            ))}
          </ul>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setDeleteKeys(null)}>
              Cancel
            </Button>
            <Button variant="destructive" onClick={confirmDelete}>
              {(deleteKeys?.length ?? 0) > 1 ? `Delete ${deleteKeys?.length}` : "Delete"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
