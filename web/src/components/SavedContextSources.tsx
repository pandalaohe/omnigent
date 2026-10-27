import { useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { SavedSourcesManager } from "@/components/SavedSourcesManager";
import type { Host } from "@/hooks/useHosts";
import { registrySourceLabels, type RegistryItem } from "@/hooks/useAgentRegistry";
import {
  usageContextRecordFromKey,
  usageContextSourceFromKey,
  usageContextLevelKey,
  type UsageContextOverride,
  type UsageContextPreferences,
  type UsageContextSource,
} from "@/lib/usageContextPreferences";

const NO_REGISTRY_ITEMS: readonly RegistryItem[] = [];

function savedValue(override: UsageContextOverride | undefined): string {
  if (!override) return "—";
  const buffer =
    override.compactBufferMode === "percent"
      ? override.autoCompactBufferPercent == null
        ? "Auto"
        : `${override.autoCompactBufferPercent}%`
      : (override.autoCompactBufferTokens?.toLocaleString() ?? "Auto");
  return `${override.contextWindowTokens?.toLocaleString() ?? "Auto"} / ${buffer}`;
}

export function SavedContextSources({
  preferences,
  currentSourceKey,
  hosts,
  registryItems = NO_REGISTRY_ITEMS,
  currentOverride,
  reportedContextWindow = null,
  reportedAutoCompactTokenLimit = null,
}: {
  preferences: UsageContextPreferences;
  currentSourceKey: string;
  hosts: Host[];
  registryItems?: readonly RegistryItem[];
  currentOverride: UsageContextOverride;
  reportedContextWindow?: number | null;
  reportedAutoCompactTokenLimit?: number | null;
}) {
  const [manager, setManager] = useState<{
    source: UsageContextSource;
    level: "cell" | "column";
  } | null>(null);
  const current = usageContextSourceFromKey(currentSourceKey);
  const records = Object.entries(preferences.overrides).flatMap(([key, override]) => {
    const record = usageContextRecordFromKey(key);
    return record ? [{ ...record, key, override }] : [];
  });
  const columns = new Map<string, UsageContextSource>();
  for (const source of [...records.map((record) => record.source), ...(current ? [current] : [])]) {
    columns.set(usageContextLevelKey(source, "column"), { ...source, model: "" });
  }
  const hostIds = [
    ...new Set([
      "",
      ...hosts.map((host) => host.host_id),
      ...records
        .filter((record) => record.level !== "column")
        .map((record) => record.source.hostId),
      ...(current ? [current.hostId] : []),
    ]),
  ];
  const sources = hostIds.flatMap((hostId) =>
    [...columns.values()].map((source) => ({ ...source, hostId })),
  );
  const hostLabel = (hostId: string) =>
    hostId ? (hosts.find((host) => host.host_id === hostId)?.name ?? hostId) : "Server";

  return (
    <div className="grid gap-3 border-t border-border pt-4" data-testid="saved-context-sources">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h4 className="text-sm font-medium">Saved sources</h4>
          <p className="mt-1 text-xs text-muted-foreground">
            Context / buffer by computer and agent. Model overrides are managed separately.
          </p>
        </div>
        <Button
          size="sm"
          variant="outline"
          disabled={sources.length === 0}
          onClick={() => setManager({ source: current ?? sources[0]!, level: "cell" })}
        >
          Manage saved sources
        </Button>
      </div>
      {columns.size === 0 ? (
        <p className="text-xs text-muted-foreground">Open a session to save context values.</p>
      ) : (
        <div className="max-h-80 overflow-auto rounded-lg border border-border">
          <table
            className="w-full border-collapse text-xs"
            aria-label="Saved context sources by computer and agent"
          >
            <thead className="sticky top-0 z-10 bg-background">
              <tr>
                <th scope="col" className="p-3 text-left font-medium">
                  Computer
                </th>
                {[...columns].map(([key, source]) => {
                  const labels = registrySourceLabels(
                    registryItems,
                    source.agentName,
                    source.harness,
                  );
                  return (
                    <th
                      key={key}
                      scope="col"
                      className="min-w-40 border-l border-border p-0 text-left font-normal"
                    >
                      <button
                        type="button"
                        className="grid w-full gap-1 p-3 text-left hover:bg-muted/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
                        aria-label={`Edit ${labels.agentLabel} · ${labels.harnessLabel} · all hosts`}
                        onClick={() => setManager({ source, level: "column" })}
                      >
                        <span className="break-words font-medium">
                          {labels.agentLabel} · {labels.harnessLabel}
                        </span>
                        <span className="tabular-nums text-muted-foreground">
                          {savedValue(preferences.overrides[key])} · all hosts
                        </span>
                      </button>
                    </th>
                  );
                })}
              </tr>
            </thead>
            <tbody>
              {hostIds.map((hostId) => (
                <tr key={hostId} className="border-t border-border">
                  <th scope="row" className="max-w-48 break-words p-3 text-left font-medium">
                    {hostLabel(hostId)}
                  </th>
                  {[...columns.values()].map((column) => {
                    const source = { ...column, hostId };
                    const key = usageContextLevelKey(source, "cell");
                    const count = records.filter(
                      (record) =>
                        record.level === "model" &&
                        record.source.hostId === hostId &&
                        record.source.agentName === column.agentName &&
                        record.source.harness === column.harness,
                    ).length;
                    const labels = registrySourceLabels(
                      registryItems,
                      source.agentName,
                      source.harness,
                    );
                    return (
                      <td key={key} className="border-l border-border p-0">
                        <button
                          type="button"
                          className="grid w-full gap-1 p-3 text-left hover:bg-muted/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
                          aria-label={`Manage ${hostLabel(hostId)} × ${labels.agentLabel} · ${labels.harnessLabel}`}
                          onClick={() => setManager({ source, level: "cell" })}
                        >
                          <span className="tabular-nums">
                            {savedValue(preferences.overrides[key])}
                          </span>
                          <span className="text-muted-foreground">
                            {count} model {count === 1 ? "override" : "overrides"}
                          </span>
                        </button>
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <Dialog
        open={manager !== null}
        onOpenChange={(open) => {
          if (!open) setManager(null);
        }}
      >
        <DialogContent
          className="max-h-[90vh] overflow-y-auto sm:max-w-4xl"
          onEscapeKeyDown={(event) => {
            if (
              event.target instanceof Element &&
              event.target.closest("[data-usage-context-editor]")
            )
              event.preventDefault();
          }}
        >
          <DialogHeader>
            <DialogTitle>Manage saved sources</DialogTitle>
            <DialogDescription>
              Model override → computer × agent → agent · all hosts → reported. Empty fields
              inherit.
            </DialogDescription>
          </DialogHeader>
          {manager ? (
            <SavedSourcesManager
              {...{
                preferences,
                currentSourceKey,
                hosts,
                registryItems,
                currentOverride,
                reportedContextWindow,
                reportedAutoCompactTokenLimit,
                sources,
              }}
              initialSource={manager.source}
              initialLevel={manager.level}
            />
          ) : null}
        </DialogContent>
      </Dialog>
    </div>
  );
}
