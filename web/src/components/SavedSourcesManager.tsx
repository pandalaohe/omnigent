import { useEffect, useState } from "react";

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
import type { Host } from "@/hooks/useHosts";
import { registrySourceLabels, type RegistryItem } from "@/hooks/useAgentRegistry";
import {
  deleteUsageContextOverrides,
  fixedBufferFitsCapacity,
  patchUsageContextOverrides,
  proposeUsageContextCollapse,
  resolveUsageContext,
  usageContextLevelKey,
  usageContextLevelLabel,
  usageContextRecordFromKey,
  writeUsageContextPreferences,
  type UsageContextOverride,
  type UsageContextOverridePatch,
  type UsageContextPreferences,
  type UsageContextSource,
} from "@/lib/usageContextPreferences";

function positiveNumber(value: string): number | null {
  if (value.trim() === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? Math.round(parsed) : null;
}

function bufferValue(value: string, percent: boolean): number | null {
  if (value.trim() === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) &&
    parsed >= 0 &&
    (percent ? parsed < 100 : Number.isInteger(parsed))
    ? parsed
    : null;
}

interface SavedContextSourceRow {
  key: string;
  override: UsageContextOverride;
  hostLabel: string;
  agentLabel: string;
  harnessLabel: string;
  modelLabel: string;
  levelLabel: string;
  rowLabel: string;
  source: UsageContextSource;
  level: "model" | "agent-model" | "cell" | "column" | "host" | "all";
}

const NO_REGISTRY_ITEMS: readonly RegistryItem[] = [];

function describeSource(
  source: UsageContextSource,
  hosts: Host[],
  registryItems: readonly RegistryItem[],
) {
  const hostLabel = source.hostId
    ? (hosts.find((host) => host.host_id === source.hostId)?.name ?? source.hostId)
    : "No host";
  const { agentLabel, harnessLabel } = registrySourceLabels(
    registryItems,
    source.agentName,
    source.harness,
  );
  return { hostLabel, agentLabel, harnessLabel, modelLabel: source.model || "Every model" };
}

function SourceResolution({
  preferences,
  sourceKey,
  labels,
  reportedContextWindow,
  reportedAutoCompactTokenLimit,
}: {
  preferences: UsageContextPreferences;
  sourceKey: string;
  labels: { hostLabel: string; agentLabel: string };
  reportedContextWindow: number | null;
  reportedAutoCompactTokenLimit: number | null;
}) {
  const resolved = resolveUsageContext(preferences, sourceKey, {
    contextWindow: reportedContextWindow,
    autoCompactTokenLimit: reportedAutoCompactTokenLimit,
  });
  return (
    <div className="grid gap-1 tabular-nums text-muted-foreground">
      <span>
        Context {resolved.contextWindow.value?.toLocaleString() ?? "Auto"} ·{" "}
        {usageContextLevelLabel(resolved.contextWindow.level, labels)}
      </span>
      <span>
        Buffer {resolved.buffer.value?.toLocaleString() ?? "Auto"}
        {resolved.buffer.mode === "percent" && resolved.buffer.value != null ? "%" : ""} ·{" "}
        {usageContextLevelLabel(resolved.buffer.level, labels)}
      </span>
      {resolved.ignored.map((item) => (
        <span key={item.level}>
          Ignored {usageContextLevelLabel(item.level, labels)}: {item.reason}; using{" "}
          {usageContextLevelLabel(resolved.buffer.level, labels)}.
        </span>
      ))}
    </div>
  );
}

export function SavedSourcesManager({
  preferences,
  initialSource,
  currentSourceKey,
  hosts,
  registryItems = NO_REGISTRY_ITEMS,
  reportedContextWindow = null,
  reportedAutoCompactTokenLimit = null,
}: {
  preferences: UsageContextPreferences;
  initialSource: UsageContextSource;
  currentSourceKey: string;
  hosts: Host[];
  registryItems?: readonly RegistryItem[];
  reportedContextWindow?: number | null;
  reportedAutoCompactTokenLimit?: number | null;
}) {
  const [activeSource, setActiveSource] = useState(initialSource);
  const currentOverride: UsageContextOverride = preferences.overrides[currentSourceKey] ?? {
    contextWindowTokens: null,
    autoCompactBufferTokens: null,
  };
  const [keptProposal, setKeptProposal] = useState(false);
  const proposal = proposeUsageContextCollapse(preferences);
  const current = usageContextRecordFromKey(currentSourceKey)?.source ?? initialSource;
  const rows: SavedContextSourceRow[] = Object.entries(preferences.overrides).flatMap(
    ([key, override]) => {
      const record = usageContextRecordFromKey(key);
      if (!record) return [];
      const labels = describeSource(record.source, hosts, registryItems);
      const scope =
        record.level === "agent-model"
          ? "Every computer"
          : record.level === "cell"
            ? "This computer"
            : record.level === "column"
              ? "Every computer"
              : record.level === "model"
                ? labels.hostLabel
                : "Every agent";
      return [
        {
          key,
          override,
          ...labels,
          modelLabel:
            record.level === "model" || record.level === "agent-model"
              ? labels.modelLabel
              : "Every model",
          levelLabel: scope,
          source: record.source,
          level: record.level,
          rowLabel: `${labels.hostLabel} ${labels.agentLabel} ${labels.modelLabel}`,
        },
      ];
    },
  );
  const [selectedKeys, setSelectedKeys] = useState<string[]>([]);
  const [deleteKeys, setDeleteKeys] = useState<string[] | null>(null);
  const [batchContext, setBatchContext] = useState("");
  const [batchBuffer, setBatchBuffer] = useState("");
  const [batchPercent, setBatchPercent] = useState("");
  const [batchMode, setBatchMode] = useState<"tokens" | "percent">("tokens");
  const [batchModeSelected, setBatchModeSelected] = useState(false);
  const [batchContextAuto, setBatchContextAuto] = useState(false);
  const [batchBufferAuto, setBatchBufferAuto] = useState(false);

  const hostAgents = new Map<string, Map<string, UsageContextSource>>();
  const addScope = (source: UsageContextSource) => {
    if (!source.agentName) return;
    const agents = hostAgents.get(source.hostId) ?? new Map<string, UsageContextSource>();
    agents.set(usageContextLevelKey(source, "cell"), { ...source, model: "" });
    hostAgents.set(source.hostId, agents);
  };
  for (const row of rows) addScope(row.source);
  addScope(initialSource);
  addScope(current);
  const hostIds = [...hostAgents.keys()].sort((left, right) =>
    (left
      ? describeSource({ ...current, hostId: left }, hosts, registryItems).hostLabel
      : "No host"
    ).localeCompare(
      right
        ? describeSource({ ...current, hostId: right }, hosts, registryItems).hostLabel
        : "No host",
    ),
  );
  const wideRows = rows.filter((row) => row.level === "host" || row.level === "all");
  const visibleRows = rows
    .filter(
      (row) =>
        row.source.agentName === activeSource.agentName &&
        row.source.harness === activeSource.harness &&
        (row.level === "agent-model"
          ? true
          : row.level === "column"
            ? false
            : row.source.hostId === activeSource.hostId),
    )
    .sort((left, right) => Number(right.level === "cell") - Number(left.level === "cell"));
  const modelRows = visibleRows.filter(
    (row) => row.level === "model" || row.level === "agent-model",
  );
  const columnRows = rows.filter(
    (row) =>
      row.level === "column" &&
      row.source.agentName === activeSource.agentName &&
      row.source.harness === activeSource.harness,
  );
  const activeLabels = describeSource(activeSource, hosts, registryItems);
  const labelsFor = (source: UsageContextSource) => ({
    hostLabel: describeSource(source, hosts, registryItems).hostLabel,
    agentLabel: describeSource(source, hosts, registryItems).agentLabel,
  });
  // JSON array keys never contain a literal newline, so joining keeps the
  // effect dependency stable without re-running on every render.
  const visibleKeySignature = modelRows.map((row) => row.key).join("\n");

  useEffect(() => {
    const visible = new Set(visibleKeySignature === "" ? [] : visibleKeySignature.split("\n"));
    setSelectedKeys((currentKeys) => {
      const next = currentKeys.filter(
        (key) => visible.has(key) && Object.hasOwn(preferences.overrides, key),
      );
      return next.length === currentKeys.length ? currentKeys : next;
    });
  }, [visibleKeySignature, preferences.overrides]);

  const selectedKeySet = new Set(selectedKeys);
  const allShownSelected =
    modelRows.length > 0 && modelRows.every((row) => selectedKeySet.has(row.key));
  const someShownSelected =
    !allShownSelected && modelRows.some((row) => selectedKeySet.has(row.key));
  const currentHasOverride =
    currentOverride.contextWindowTokens !== null ||
    currentOverride.autoCompactBufferTokens !== null ||
    currentOverride.autoCompactBufferPercent != null ||
    currentOverride.compactBufferMode === "percent";
  const contextWouldKeep = !batchContextAuto && positiveNumber(batchContext) === null;
  const bufferWouldKeep =
    !batchBufferAuto &&
    bufferValue(batchMode === "percent" ? batchPercent : batchBuffer, batchMode === "percent") ===
      null &&
    !batchModeSelected;
  const batchContextValue = batchContextAuto ? null : positiveNumber(batchContext);
  const batchTokenValue = batchBufferAuto ? null : bufferValue(batchBuffer, false);
  const batchPercentValue = batchBufferAuto ? null : bufferValue(batchPercent, true);
  const batchInvalid =
    (!batchContextAuto && batchContext.trim() !== "" && batchContextValue === null) ||
    (!batchBufferAuto &&
      batchBuffer.trim() !== "" &&
      batchMode === "tokens" &&
      batchTokenValue === null) ||
    (!batchBufferAuto &&
      batchPercent.trim() !== "" &&
      batchMode === "percent" &&
      batchPercentValue === null) ||
    selectedKeys.some((key) => {
      const override = preferences.overrides[key];
      if (!override) return false;
      const context = resolveUsageContext(
        {
          ...preferences,
          overrides: {
            ...preferences.overrides,
            [key]: {
              ...override,
              contextWindowTokens: contextWouldKeep
                ? override.contextWindowTokens
                : batchContextValue,
            },
          },
        },
        key,
        {
          contextWindow: key === currentSourceKey ? reportedContextWindow : null,
          autoCompactTokenLimit: null,
        },
      ).contextWindow.value;
      const tokenBuffer =
        batchMode === "tokens" && batchTokenValue !== null
          ? batchTokenValue
          : batchMode === "tokens" && batchBufferAuto
            ? null
            : override.autoCompactBufferTokens;
      const mode = batchModeSelected ? batchMode : (override.compactBufferMode ?? "tokens");
      return mode === "tokens" && !fixedBufferFitsCapacity(tokenBuffer, context);
    });
  const deleteTargets = (deleteKeys ?? []).flatMap((key) => {
    const row = rows.find((candidate) => candidate.key === key);
    return row ? [row] : [];
  });

  const toggleRow = (key: string, checked: boolean) => {
    setSelectedKeys((currentKeys) =>
      checked
        ? currentKeys.includes(key)
          ? currentKeys
          : [...currentKeys, key]
        : currentKeys.filter((candidate) => candidate !== key),
    );
  };

  const toggleAllShown = (checked: boolean) => {
    setSelectedKeys((currentKeys) => {
      if (!checked) {
        const shown = new Set(modelRows.map((row) => row.key));
        return currentKeys.filter((key) => !shown.has(key));
      }
      const merged = [...currentKeys];
      for (const row of modelRows) if (!merged.includes(row.key)) merged.push(row.key);
      return merged;
    });
  };

  const applyToSelected = () => {
    if (batchInvalid) return;
    const patch: UsageContextOverridePatch = {};
    if (batchContextAuto) {
      patch.contextWindowTokens = null;
    } else {
      const value = positiveNumber(batchContext);
      if (value !== null) patch.contextWindowTokens = value;
    }
    if (batchBufferAuto) {
      if (batchMode === "percent") patch.autoCompactBufferPercent = null;
      else patch.autoCompactBufferTokens = null;
    } else {
      const value = batchMode === "percent" ? batchPercentValue : batchTokenValue;
      if (value !== null) {
        if (batchMode === "percent") patch.autoCompactBufferPercent = value;
        else patch.autoCompactBufferTokens = value;
      }
    }
    if (batchModeSelected) patch.compactBufferMode = batchMode;
    patchUsageContextOverrides(preferences, selectedKeys, patch);
    setSelectedKeys([]);
    setBatchContext("");
    setBatchBuffer("");
    setBatchPercent("");
    setBatchModeSelected(false);
    setBatchContextAuto(false);
    setBatchBufferAuto(false);
  };

  const useCurrentSource = () => {
    setBatchContextAuto(currentOverride.contextWindowTokens === null);
    setBatchContext(currentOverride.contextWindowTokens?.toString() ?? "");
    setBatchBufferAuto(
      (currentOverride.compactBufferMode === "percent"
        ? currentOverride.autoCompactBufferPercent
        : currentOverride.autoCompactBufferTokens) == null,
    );
    setBatchBuffer(currentOverride.autoCompactBufferTokens?.toString() ?? "");
    setBatchPercent(currentOverride.autoCompactBufferPercent?.toString() ?? "");
    setBatchMode(currentOverride.compactBufferMode ?? "tokens");
    setBatchModeSelected(true);
  };

  const confirmDelete = () => {
    if (!deleteKeys || deleteKeys.length === 0) return;
    deleteUsageContextOverrides(preferences, deleteKeys);
    setDeleteKeys(null);
    setSelectedKeys([]);
  };

  return (
    <div className="grid min-h-0 gap-4">
      {!keptProposal && proposal.removed.length > 0 ? (
        <div className="flex flex-wrap items-center gap-2 border-b border-border pb-3 text-sm">
          <p className="min-w-0 flex-1">
            {proposal.removed.length} model values with identical values can be merged into{" "}
            {Object.keys(proposal.added).length} cell values.
          </p>
          <Button
            size="sm"
            onClick={() => {
              const overrides = { ...preferences.overrides, ...proposal.added };
              for (const key of proposal.removed) Reflect.deleteProperty(overrides, key);
              writeUsageContextPreferences({ ...preferences, overrides });
            }}
          >
            Merge
          </Button>
          <Button size="sm" variant="ghost" onClick={() => setKeptProposal(true)}>
            Keep
          </Button>
        </div>
      ) : null}
      {wideRows.length > 0 ? (
        <div className="grid gap-1 rounded-lg border border-border">
          <p className="px-3 pt-2 text-sm font-medium">Every computer</p>
          {wideRows.map((row) => (
            <div
              key={row.key}
              className="flex flex-wrap items-center justify-between gap-2 px-3 py-2 text-sm"
            >
              <span className="min-w-0 break-words">
                {row.level === "all" ? "All agents, all hosts" : `All agents on ${row.hostLabel}`}
              </span>
              <SourceResolution
                preferences={preferences}
                sourceKey={row.key}
                labels={labelsFor(row.source)}
                reportedContextWindow={null}
                reportedAutoCompactTokenLimit={null}
              />
              <Button
                variant="ghost"
                size="sm"
                aria-label={`Delete ${row.level === "all" ? "All agents, all hosts" : `All agents on ${row.hostLabel}`}`}
                onClick={() => setDeleteKeys([row.key])}
              >
                Delete
              </Button>
            </div>
          ))}
        </div>
      ) : null}
      <div className="grid min-h-0 gap-4 sm:grid-cols-[11rem_minmax(0,1fr)]">
        <nav
          aria-label="Saved source computers"
          className="max-h-40 overflow-auto border-b border-border pb-2 sm:max-h-[55vh] sm:border-r sm:border-b-0 sm:pr-3"
        >
          {hostIds.map((hostId) => (
            <div key={hostId} className="mb-3">
              <p className="mb-1 break-words text-sm font-medium">
                {describeSource({ ...activeSource, hostId }, hosts, registryItems).hostLabel}
              </p>
              {[...(hostAgents.get(hostId)?.values() ?? [])].map((source) => {
                const labels = describeSource(source, hosts, registryItems);
                return (
                  <Button
                    key={usageContextLevelKey(source, "cell")}
                    variant={
                      usageContextLevelKey(source, "cell") ===
                      usageContextLevelKey(activeSource, "cell")
                        ? "secondary"
                        : "ghost"
                    }
                    size="sm"
                    className="mb-1 h-auto w-full justify-start whitespace-normal text-left text-sm"
                    onClick={() => {
                      setActiveSource(source);
                      setSelectedKeys([]);
                    }}
                  >
                    {labels.agentLabel} · {labels.harnessLabel}
                  </Button>
                );
              })}
            </div>
          ))}
        </nav>
        <div className="grid max-h-[55vh] min-w-0 content-start gap-3 overflow-y-auto">
          <div className="grid gap-1 text-sm">
            <h4 className="break-words font-medium">
              {activeLabels.hostLabel} × {activeLabels.agentLabel} · {activeLabels.harnessLabel}
            </h4>
            <p className="break-words text-muted-foreground">
              {columnRows.length === 0
                ? "No value for every computer."
                : columnRows
                    .map(
                      (row) =>
                        `${row.override.contextWindowTokens?.toLocaleString() ?? "Auto"} context · ${
                          row.override.compactBufferMode === "percent"
                            ? (row.override.autoCompactBufferPercent ?? "Auto")
                            : (row.override.autoCompactBufferTokens?.toLocaleString() ?? "Auto")
                        } buffer for every computer`,
                    )
                    .join("; ")}
            </p>
          </div>
          {selectedKeys.length > 0 ? (
            <div className="flex flex-wrap items-end gap-2 rounded-lg border border-border bg-muted/20 p-3">
              <span className="pb-1 text-sm font-medium text-foreground">
                {selectedKeys.length} selected
              </span>
              <div className="grid gap-1 text-sm text-muted-foreground">
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
              <div className="grid gap-1 text-sm text-muted-foreground">
                <span>Compact buffer</span>
                <div className="flex gap-1" role="group" aria-label="Selected buffer unit">
                  <Button
                    size="sm"
                    variant={batchMode === "tokens" ? "secondary" : "outline"}
                    aria-pressed={batchMode === "tokens"}
                    onClick={() => {
                      setBatchMode("tokens");
                      setBatchModeSelected(true);
                    }}
                  >
                    Tokens
                  </Button>
                  <Button
                    size="sm"
                    variant={batchMode === "percent" ? "secondary" : "outline"}
                    aria-pressed={batchMode === "percent"}
                    onClick={() => {
                      setBatchMode("percent");
                      setBatchModeSelected(true);
                    }}
                  >
                    Percent
                  </Button>
                </div>
                <div className="flex items-center gap-1">
                  <Input
                    type="number"
                    min={0}
                    max={batchMode === "percent" ? 99.999 : undefined}
                    step={batchMode === "percent" ? "any" : 1}
                    value={
                      batchBufferAuto ? "" : batchMode === "percent" ? batchPercent : batchBuffer
                    }
                    placeholder={batchBufferAuto ? "Auto" : "Keep"}
                    disabled={batchBufferAuto}
                    onChange={(event) => {
                      if (batchMode === "percent") setBatchPercent(event.target.value);
                      else setBatchBuffer(event.target.value);
                      setBatchModeSelected(true);
                    }}
                    aria-label={
                      batchMode === "percent" ? "Compact buffer percent" : "Compact buffer"
                    }
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
                      setBatchModeSelected(true);
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
                  Use this session's values
                </Button>
                <Button
                  size="sm"
                  disabled={batchInvalid || (contextWouldKeep && bufferWouldKeep)}
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
              {batchInvalid ? (
                <p role="alert" className="text-sm text-destructive">
                  Token buffer must be less than every selected context total; enter valid numbers.
                </p>
              ) : null}
            </div>
          ) : null}

          <div className="divide-y divide-border overflow-hidden rounded-lg border border-border">
            {modelRows.length > 0 ? (
              <div className="flex items-center gap-2 bg-muted/20 px-3 py-2 text-sm">
                <Checkbox
                  checked={allShownSelected ? true : someShownSelected ? "indeterminate" : false}
                  onCheckedChange={(checked) => toggleAllShown(checked === true)}
                  aria-label="Select all shown"
                />
                <span className="text-muted-foreground">Select all shown</span>
              </div>
            ) : null}
            {visibleRows.length === 0 ? (
              <p className="px-3 py-3 text-sm text-muted-foreground">Nothing saved here.</p>
            ) : null}
            {visibleRows.map((row) => {
              const isCurrent = row.key === currentSourceKey;
              return (
                <div key={row.key} className="grid gap-2 px-3 py-2.5 text-sm">
                  <div className="flex min-w-0 flex-wrap items-center gap-2">
                    <Checkbox
                      checked={selectedKeySet.has(row.key)}
                      onCheckedChange={(checked) => toggleRow(row.key, checked === true)}
                      aria-label={`Select ${row.rowLabel}`}
                    />
                    <span className="min-w-0 break-all font-medium text-foreground">
                      {row.modelLabel}
                    </span>
                    <span className="text-muted-foreground">{row.levelLabel}</span>
                    {isCurrent ? (
                      <span className="rounded-full bg-primary/10 px-2 py-0.5 text-[11px] font-medium text-primary">
                        Current
                      </span>
                    ) : null}
                    <Button
                      variant="ghost"
                      size="sm"
                      className="ml-auto"
                      aria-label={`Delete ${row.rowLabel}`}
                      onClick={() => setDeleteKeys([row.key])}
                    >
                      Delete
                    </Button>
                  </div>
                  <SourceResolution
                    preferences={preferences}
                    sourceKey={row.key}
                    labels={labelsFor(row.source)}
                    reportedContextWindow={
                      row.key === currentSourceKey ? reportedContextWindow : null
                    }
                    reportedAutoCompactTokenLimit={
                      row.key === currentSourceKey ? reportedAutoCompactTokenLimit : null
                    }
                  />
                </div>
              );
            })}
          </div>
        </div>
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
                ? `Delete ${deleteKeys?.length} saved values?`
                : "Delete saved value?"}
            </DialogTitle>
            <DialogDescription>
              These values go back to Auto and inherit the next level. This syncs to your other
              devices.
            </DialogDescription>
          </DialogHeader>
          <ul className="grid gap-1 text-sm text-muted-foreground">
            {deleteTargets.map((row) => (
              <li key={row.key} className="break-all">
                {row.hostLabel} · {row.harnessLabel} · {row.modelLabel} · {row.levelLabel}
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
