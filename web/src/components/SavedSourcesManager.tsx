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
import type { Host } from "@/hooks/useHosts";
import { registrySourceLabels, type RegistryItem } from "@/hooks/useAgentRegistry";
import {
  deleteUsageContextOverrides,
  fixedBufferFitsCapacity,
  patchUsageContextOverrides,
  usageContextRecordFromKey,
  usageContextLevelKey,
  resolveUsageContext,
  usageContextLevelLabel,
  proposeUsageContextCollapse,
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
  rowLabel: string;
  source: UsageContextSource;
  level: "model" | "cell" | "column";
}

const NO_REGISTRY_ITEMS: readonly RegistryItem[] = [];

function describeSource(
  source: UsageContextSource,
  hosts: Host[],
  registryItems: readonly RegistryItem[],
) {
  const hostLabel = source.hostId
    ? (hosts.find((host) => host.host_id === source.hostId)?.name ?? source.hostId)
    : "Server";
  const { agentLabel, harnessLabel } = registrySourceLabels(
    registryItems,
    source.agentName,
    source.harness,
  );
  const modelLabel = source.model || "Auto model";
  return { hostLabel, agentLabel, harnessLabel, modelLabel };
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
  initialLevel = "cell",
  sources,
  currentSourceKey,
  hosts,
  registryItems = NO_REGISTRY_ITEMS,
  currentOverride,
  reportedContextWindow = null,
  reportedAutoCompactTokenLimit = null,
}: {
  preferences: UsageContextPreferences;
  initialSource: UsageContextSource;
  initialLevel?: "cell" | "column";
  sources: UsageContextSource[];
  currentSourceKey: string;
  hosts: Host[];
  registryItems?: readonly RegistryItem[];
  currentOverride: UsageContextOverride;
  reportedContextWindow?: number | null;
  reportedAutoCompactTokenLimit?: number | null;
}) {
  const [activeSource, setActiveSource] = useState(initialSource);
  const [showColumn, setShowColumn] = useState(initialLevel === "column");
  const [keptProposal, setKeptProposal] = useState(false);
  const proposal = proposeUsageContextCollapse(preferences);
  const records = { ...preferences.overrides };
  for (const source of sources) {
    const cellKey = usageContextLevelKey(source, "cell");
    records[cellKey] ??= { contextWindowTokens: null, autoCompactBufferTokens: null };
    const columnKey = usageContextLevelKey(source, "column");
    records[columnKey] ??= { contextWindowTokens: null, autoCompactBufferTokens: null };
  }
  const rows: SavedContextSourceRow[] = Object.entries(records).flatMap(([key, override]) => {
    const record = usageContextRecordFromKey(key);
    if (!record) return [];
    const labels = describeSource(record.source, hosts, registryItems);
    const modelLabel =
      record.level === "model"
        ? labels.modelLabel
        : record.level === "cell"
          ? "All models"
          : "All hosts";
    return [
      {
        key,
        override,
        ...labels,
        modelLabel,
        source: record.source,
        level: record.level,
        rowLabel: `${labels.hostLabel} ${labels.agentLabel} ${modelLabel}`,
      },
    ];
  });
  const [selectedKeys, setSelectedKeys] = useState<string[]>([]);
  const [editingKey, setEditingKey] = useState<string | null>(null);
  const [editContext, setEditContext] = useState("");
  const [editBuffer, setEditBuffer] = useState("");
  const [editPercent, setEditPercent] = useState("");
  const [editMode, setEditMode] = useState<"tokens" | "percent">("tokens");
  const editBaselineRef = useRef<UsageContextOverride>({
    contextWindowTokens: null,
    autoCompactBufferTokens: null,
  });
  const [deleteKeys, setDeleteKeys] = useState<string[] | null>(null);
  const [batchContext, setBatchContext] = useState("");
  const [batchBuffer, setBatchBuffer] = useState("");
  const [batchPercent, setBatchPercent] = useState("");
  const [batchMode, setBatchMode] = useState<"tokens" | "percent">("tokens");
  const [batchModeSelected, setBatchModeSelected] = useState(false);
  const [batchContextAuto, setBatchContextAuto] = useState(false);
  const [batchBufferAuto, setBatchBufferAuto] = useState(false);

  const visibleRows = rows
    .filter(
      (row) =>
        row.source.agentName === activeSource.agentName &&
        row.source.harness === activeSource.harness &&
        (showColumn
          ? row.level === "column"
          : row.level !== "column" && row.source.hostId === activeSource.hostId),
    )
    .sort((left, right) => Number(right.level === "cell") - Number(left.level === "cell"));
  const modelRows = visibleRows.filter((row) => row.level === "model");
  const activeLabels = describeSource(activeSource, hosts, registryItems);
  const columnKey = usageContextLevelKey(activeSource, "column");
  const column = preferences.overrides[columnKey];
  const hostIds = [...new Set(sources.map((source) => source.hostId))];
  // JSON array keys never contain a literal newline, so joining keeps the
  // effect dependency stable without re-running on every render.
  const visibleKeySignature = modelRows.map((row) => row.key).join("\n");

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
  const editingRowPercent = editingRow?.override.autoCompactBufferPercent;
  const editingRowMode = editingRow?.override.compactBufferMode;

  useEffect(() => {
    if (editingKey === null) return;
    const baseline = editBaselineRef.current;
    if (
      !editingRowExists ||
      editingRowContext !== baseline.contextWindowTokens ||
      editingRowBuffer !== baseline.autoCompactBufferTokens ||
      editingRowPercent !== baseline.autoCompactBufferPercent ||
      editingRowMode !== baseline.compactBufferMode
    ) {
      setEditingKey(null);
    }
  }, [
    editingKey,
    editingRowExists,
    editingRowContext,
    editingRowBuffer,
    editingRowPercent,
    editingRowMode,
  ]);

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
  const editContextValue = positiveNumber(editContext);
  const editTokenValue = bufferValue(editBuffer, false);
  const editPercentValue = bufferValue(editPercent, true);
  const editInvalid =
    (editContext.trim() !== "" && editContextValue === null) ||
    (editBuffer.trim() !== "" && editTokenValue === null) ||
    (editPercent.trim() !== "" && editPercentValue === null) ||
    (editMode === "tokens" &&
      !fixedBufferFitsCapacity(
        editTokenValue,
        editingRow
          ? resolveUsageContext(
              {
                ...preferences,
                overrides: {
                  ...preferences.overrides,
                  [editingRow.key]: {
                    ...editingRow.override,
                    contextWindowTokens: editContextValue,
                  },
                },
              },
              editingRow.key,
              {
                contextWindow: editingRow.key === currentSourceKey ? reportedContextWindow : null,
                autoCompactTokenLimit: null,
              },
            ).contextWindow.value
          : null,
      ));
  const editSaveDisabled =
    editInvalid ||
    (editContextValue === null &&
      editTokenValue === null &&
      editPercentValue === null &&
      editMode !== "percent");
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
        const shown = new Set(modelRows.map((row) => row.key));
        return current.filter((key) => !shown.has(key));
      }
      const merged = [...current];
      for (const row of modelRows) if (!merged.includes(row.key)) merged.push(row.key);
      return merged;
    });
  };

  const startEdit = (row: SavedContextSourceRow) => {
    editBaselineRef.current = {
      contextWindowTokens: row.override.contextWindowTokens,
      autoCompactBufferTokens: row.override.autoCompactBufferTokens,
      autoCompactBufferPercent: row.override.autoCompactBufferPercent,
      compactBufferMode: row.override.compactBufferMode,
    };
    setEditingKey(row.key);
    setEditContext(row.override.contextWindowTokens?.toString() ?? "");
    setEditBuffer(row.override.autoCompactBufferTokens?.toString() ?? "");
    setEditPercent(row.override.autoCompactBufferPercent?.toString() ?? "");
    setEditMode(row.override.compactBufferMode ?? "tokens");
  };

  const saveEdit = () => {
    if (!editingRow) return;
    if (editSaveDisabled) return;
    const patch: UsageContextOverridePatch = {};
    if (editContextValue !== editingRow.override.contextWindowTokens)
      patch.contextWindowTokens = editContextValue;
    if (
      editMode !== (editingRow.override.compactBufferMode ?? "tokens") ||
      editTokenValue !== editingRow.override.autoCompactBufferTokens ||
      editPercentValue !== (editingRow.override.autoCompactBufferPercent ?? null)
    ) {
      patch.compactBufferMode = editMode;
      patch.autoCompactBufferTokens = editTokenValue;
      patch.autoCompactBufferPercent = editPercentValue;
    }
    patchUsageContextOverrides(preferences, [editingRow.key], patch);
    setEditingKey(null);
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
        <div className="flex flex-wrap items-center gap-2 border-b border-border pb-3 text-xs">
          <p className="min-w-0 flex-1">
            {proposal.removed.length} model records with identical values can be merged into{" "}
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
      <div className="grid min-h-0 gap-4 sm:grid-cols-[11rem_minmax(0,1fr)]">
        <nav
          aria-label="Saved source computers"
          className="max-h-40 overflow-auto border-b border-border pb-2 sm:max-h-[55vh] sm:border-r sm:border-b-0 sm:pr-3"
        >
          {hostIds.map((hostId) => (
            <div key={hostId} className="mb-3">
              <p className="mb-1 break-words text-xs font-medium">
                {hostId
                  ? (hosts.find((host) => host.host_id === hostId)?.name ?? hostId)
                  : "Server"}
              </p>
              {sources
                .filter((source) => source.hostId === hostId)
                .map((source) => {
                  const labels = describeSource(source, hosts, registryItems);
                  return (
                    <Button
                      key={usageContextLevelKey(source, "cell")}
                      variant={
                        usageContextLevelKey(source, "cell") ===
                          usageContextLevelKey(activeSource, "cell") && !showColumn
                          ? "secondary"
                          : "ghost"
                      }
                      size="sm"
                      className="mb-1 h-auto w-full justify-start whitespace-normal text-left text-xs"
                      onClick={() => {
                        setActiveSource(source);
                        setShowColumn(false);
                        setEditingKey(null);
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
          <div className="grid gap-1 text-xs">
            <h4 className="break-words font-medium">
              {activeLabels.hostLabel} × {activeLabels.agentLabel} · {activeLabels.harnessLabel}
            </h4>
            <p className="break-words text-muted-foreground">
              {activeLabels.agentLabel} · all hosts:{" "}
              {column?.contextWindowTokens?.toLocaleString() ?? "Auto"} context ·{" "}
              {column?.compactBufferMode === "percent"
                ? column.autoCompactBufferPercent == null
                  ? "Auto"
                  : `${column.autoCompactBufferPercent}%`
                : (column?.autoCompactBufferTokens?.toLocaleString() ?? "Auto")}{" "}
              buffer
            </p>
            {!showColumn ? (
              <Button
                size="xs"
                variant="ghost"
                className="justify-self-start"
                onClick={() => {
                  setShowColumn(true);
                  setSelectedKeys([]);
                  setEditingKey(null);
                }}
              >
                Edit column values
              </Button>
            ) : null}
          </div>
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
                <div className="flex gap-1" role="group" aria-label="Selected buffer unit">
                  <Button
                    size="xs"
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
                    size="xs"
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
                  Use current source
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
                <p role="alert" className="text-xs text-destructive">
                  Token buffer must be less than every selected context total; enter valid numbers.
                </p>
              ) : null}
            </div>
          ) : null}

          <div className="divide-y divide-border overflow-hidden rounded-lg border border-border">
            {modelRows.length > 0 ? (
              <div className="flex items-center gap-2 bg-muted/20 px-3 py-2 text-xs">
                <Checkbox
                  checked={allShownSelected ? true : someShownSelected ? "indeterminate" : false}
                  onCheckedChange={(checked) => toggleAllShown(checked === true)}
                  aria-label="Select all shown"
                />
                <span className="text-muted-foreground">Select all shown</span>
              </div>
            ) : null}
            {visibleRows.map((row) => {
              const isCurrent = row.key === currentSourceKey;
              const isEditing = row.key === editingKey;
              return (
                <div key={row.key} className="grid gap-2 px-3 py-2.5 text-xs">
                  <div className="flex min-w-0 flex-wrap items-center gap-2">
                    {row.level === "model" ? (
                      <Checkbox
                        checked={selectedKeySet.has(row.key)}
                        onCheckedChange={(checked) => toggleRow(row.key, checked === true)}
                        aria-label={`Select ${row.rowLabel}`}
                      />
                    ) : null}
                    <span className="min-w-0 break-all font-medium text-foreground">
                      {row.modelLabel}
                    </span>
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
                        disabled={!Object.hasOwn(preferences.overrides, row.key)}
                        onClick={() => setDeleteKeys([row.key])}
                      >
                        Delete
                      </Button>
                    </span>
                  </div>
                  {isEditing ? (
                    <div
                      className="grid gap-2 sm:grid-cols-2"
                      data-usage-context-editor
                      onKeyDown={(event) => {
                        if (event.key === "Escape") {
                          event.stopPropagation();
                          setEditingKey(null);
                        }
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
                        <div className="flex gap-1" role="group" aria-label="Edit buffer unit">
                          <Button
                            size="xs"
                            variant={editMode === "tokens" ? "secondary" : "outline"}
                            aria-pressed={editMode === "tokens"}
                            onClick={() => setEditMode("tokens")}
                          >
                            Tokens
                          </Button>
                          <Button
                            size="xs"
                            variant={editMode === "percent" ? "secondary" : "outline"}
                            aria-pressed={editMode === "percent"}
                            onClick={() => setEditMode("percent")}
                          >
                            Percent
                          </Button>
                        </div>
                        <Input
                          type="number"
                          min={0}
                          max={editMode === "percent" ? 99.999 : undefined}
                          step={editMode === "percent" ? "any" : 1}
                          value={editMode === "percent" ? editPercent : editBuffer}
                          placeholder="Auto"
                          onChange={(event) =>
                            editMode === "percent"
                              ? setEditPercent(event.target.value)
                              : setEditBuffer(event.target.value)
                          }
                          aria-label={
                            editMode === "percent"
                              ? "Edit compact buffer percent"
                              : "Edit compact buffer"
                          }
                        />
                      </div>
                      <div className="flex flex-wrap items-center gap-2 sm:col-span-2">
                        <Button size="xs" disabled={editSaveDisabled} onClick={saveEdit}>
                          Save
                        </Button>
                        <Button variant="ghost" size="xs" onClick={() => setEditingKey(null)}>
                          Cancel
                        </Button>
                        <span className="text-muted-foreground">
                          Use Delete to remove this source.
                        </span>
                      </div>
                      {editInvalid ? (
                        <span role="alert" className="text-destructive sm:col-span-2">
                          Token buffer must be less than the context total; enter valid numbers.
                        </span>
                      ) : null}
                    </div>
                  ) : (
                    <div className="grid gap-1 sm:grid-cols-2">
                      <span className="break-words text-muted-foreground">
                        {row.level === "model"
                          ? "Model override"
                          : row.level === "cell"
                            ? "Computer × agent"
                            : "Agent · all hosts"}
                      </span>
                      <SourceResolution
                        preferences={preferences}
                        sourceKey={row.key}
                        labels={describeSource(row.source, hosts, registryItems)}
                        reportedContextWindow={
                          row.key === currentSourceKey ? reportedContextWindow : null
                        }
                        reportedAutoCompactTokenLimit={
                          row.key === currentSourceKey ? reportedAutoCompactTokenLimit : null
                        }
                      />
                    </div>
                  )}
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
                ? `Delete ${deleteKeys?.length} saved sources?`
                : "Delete saved source?"}
            </DialogTitle>
            <DialogDescription>
              These sources go back to Auto and inherit lower-level values. This syncs to your other
              devices.
            </DialogDescription>
          </DialogHeader>
          <ul className="grid gap-1 text-xs text-muted-foreground">
            {deleteTargets.map((row) => (
              <li key={row.key} className="break-all">
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
