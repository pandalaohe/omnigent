import { useMemo, useState } from "react";
import { useQueries } from "@tanstack/react-query";

import { HelpTip } from "@/components/HelpTip";
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
import { sessionAgentQueryOptions } from "@/hooks/useAgents";
import { useAvailableAgents, type AvailableAgent } from "@/hooks/useAvailableAgents";
import { useKeepWarmPreferences } from "@/hooks/useKeepWarmPreferences";
import {
  clampHostOfflineArchiveSeconds,
  keepWarmFamilyForHarness,
  keepWarmIntervalBoundsSeconds,
  planKeepWarmCopy,
  resolveKeepWarmAgent,
  setKeepWarmAgent,
  writeKeepWarmPreferences,
  KEEP_WARM_COLD_AFTER_BOUNDS_SECONDS,
  KEEP_WARM_HOST_OFFLINE_ARCHIVE_BOUNDS_SECONDS,
  KEEP_WARM_MAX_BOUNDS_SECONDS,
  KEEP_WARM_COPY_FIELDS,
  type AgentKeepWarmPatch,
  type KeepWarmCopyField,
  type KeepWarmFamily,
  type KeepWarmPreferences,
  type ResolvedAgentKeepWarm,
} from "@/lib/keepWarmPreferences";

const HINTS = {
  archive:
    "Children whose host stays offline this long are archived automatically. 0 turns auto-archive off.",
};

/** One-line explanations for the five per-agent controls. */
function AgentHint() {
  return (
    <div className="space-y-2">
      <p>
        <span className="font-medium text-foreground">Main</span> covers every non-archived session
        shown in your sidebar.
      </p>
      <p>
        <span className="font-medium text-foreground">Children</span> are sub-agent sessions started
        by another session.
      </p>
      <p>
        <span className="font-medium text-foreground">Every</span> is the quiet gap before a
        keep-warm ping; each ping costs one cache read.
      </p>
      <p>
        <span className="font-medium text-foreground">Longest</span> stops warming that many hours
        after the session's last real turn.
      </p>
      <p>
        <span className="font-medium text-foreground">Cold after</span> is how long an idle session
        counts as warm before the blue dot appears. Empty uses the platform's own rule; 0 means
        never cold.
      </p>
    </div>
  );
}

interface SupportedAgent {
  agent: AvailableAgent;
  family: KeepWarmFamily;
}

const COPY_FIELD_LABELS: Record<KeepWarmCopyField, string> = {
  main: "Main sessions",
  child: "Children",
  intervalSeconds: "Every",
  maxSeconds: "Longest",
  coldAfterSeconds: "Cold after",
};

function copyValue(field: KeepWarmCopyField, source: ResolvedAgentKeepWarm): string {
  const value = source[field];
  if (typeof value === "boolean") return value ? "On" : "Off";
  if (value === null) return "Auto";
  if (field === "maxSeconds") return `${value / 3600} hours`;
  if (field === "coldAfterSeconds" && value === 0) return "Never";
  return `${value / 60} min`;
}

export function KeepWarmSettings() {
  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(null);
  const [copySource, setCopySource] = useState<{
    id: string;
    name: string;
    values: ResolvedAgentKeepWarm;
  } | null>(null);
  const [copyTargets, setCopyTargets] = useState<Set<string>>(new Set());
  const [copyFields, setCopyFields] = useState<Set<KeepWarmCopyField>>(
    new Set(KEEP_WARM_COPY_FIELDS),
  );
  const preferences = useKeepWarmPreferences();
  const { data: agents, isLoading, isPlaceholderData } = useAvailableAgents();
  // Session-discovered agents arrive with a null harness; resolve them read-only
  // so the pickers' shared cache and native-duplicate dedup stay untouched.
  const unresolved = useMemo(
    () =>
      (agents ?? []).filter(
        (agent): agent is AvailableAgent & { sessionId: string } =>
          agent.harness === null && agent.sessionId !== undefined,
      ),
    [agents],
  );
  const resolvedHarnessById = useQueries({
    queries: unresolved.map((agent) => sessionAgentQueryOptions(agent.sessionId)),
    // Structurally shared, so the record keeps its identity until a detail lands.
    combine: (results) => {
      const byId: Record<string, string | null> = {};
      results.forEach((result, index) => {
        const detail = result.data;
        if (detail) byId[unresolved[index].id] = detail.harness ?? null;
      });
      return byId;
    },
  });
  const resolvedAgents = useMemo(() => {
    if (agents === undefined || Object.keys(resolvedHarnessById).length === 0) return agents;
    return agents.map((agent) =>
      agent.harness === null && agent.id in resolvedHarnessById
        ? { ...agent, harness: resolvedHarnessById[agent.id] }
        : agent,
    );
  }, [agents, resolvedHarnessById]);
  const supported = useMemo(() => {
    const rows: SupportedAgent[] = [];
    for (const agent of resolvedAgents ?? []) {
      const family = keepWarmFamilyForHarness(agent.harness);
      if (family) rows.push({ agent, family });
    }
    return rows;
  }, [resolvedAgents]);
  const selected = supported.find(({ agent }) => agent.id === selectedAgentId) ?? supported[0];
  const copyCandidates = (resolvedAgents ?? []).filter(
    (agent) => agent.id !== (copySource?.id ?? selected?.agent.id),
  );
  const copyPlan = copySource
    ? planKeepWarmCopy(
        preferences,
        copySource.id,
        copySource.values,
        copyCandidates,
        copyTargets,
        copyFields,
      )
    : null;

  const openCopy = () => {
    if (!selected || !agentsReady) return;
    setCopySource({
      id: selected.agent.id,
      name: selected.agent.display_name,
      values: resolveKeepWarmAgent(preferences, selected.agent.id, selected.family),
    });
    setCopyTargets(
      new Set(
        (resolvedAgents ?? []).map((agent) => agent.id).filter((id) => id !== selected.agent.id),
      ),
    );
    setCopyFields(new Set(KEEP_WARM_COPY_FIELDS));
  };

  const toggleTarget = (id: string) => {
    setCopyTargets((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };
  const toggleField = (field: KeepWarmCopyField) => {
    setCopyFields((current) => {
      const next = new Set(current);
      if (next.has(field)) next.delete(field);
      else next.add(field);
      return next;
    });
  };

  const update = (next: KeepWarmPreferences) =>
    writeKeepWarmPreferences(next, resolvedAgents ?? []);
  // The catalog-only placeholder omits session-discovered agents, so it is not
  // a complete list: no rows and no stored-row writes until the merged list
  // lands. An unresolved harness is left out of `supported` until its detail
  // query resolves rather than dropped from the source list.
  const agentsReady = agents !== undefined && !isPlaceholderData;
  const agentsLoading = !agentsReady && (isLoading || isPlaceholderData);

  return (
    <div className="flex flex-col gap-3" data-testid="keep-warm-settings">
      {agentsLoading && (
        <p role="status" className="text-sm text-muted-foreground">
          Loading agents…
        </p>
      )}
      {!agentsLoading && supported.length === 0 && (
        <p className="text-sm text-muted-foreground">No agents on this server support keep-warm.</p>
      )}
      {agentsReady && supported.length > 0 && (
        <div className="rounded-xl border border-border bg-card p-4">
          <label className="flex flex-col gap-1 text-sm font-medium text-foreground sm:max-w-sm">
            Agent
            <select
              aria-label="Keep-warm Agent"
              className="h-9 w-full rounded-lg border border-input bg-background px-3 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              value={selected?.agent.id}
              onChange={(event) => setSelectedAgentId(event.target.value)}
            >
              {supported.map(({ agent }) => (
                <option key={agent.id} value={agent.id}>
                  {agent.display_name}
                </option>
              ))}
            </select>
          </label>
          {selected &&
            (() => {
              const { agent, family } = selected;
              const row = resolveKeepWarmAgent(preferences, agent.id, family);
              const intervalBounds = keepWarmIntervalBoundsSeconds(family);
              const inactive = !row.main && !row.child;
              const patch = (next: AgentKeepWarmPatch) =>
                update(setKeepWarmAgent(preferences, agent.id, family, next));
              return (
                <div
                  key={agent.id}
                  className="mt-4 border-t border-border pt-4"
                  data-testid="keep-warm-agent-row"
                  data-agent-id={agent.id}
                >
                  <div className="flex items-center gap-1.5">
                    <span className="text-sm font-medium text-foreground">
                      {agent.display_name}
                    </span>
                    <HelpTip label={`About ${agent.display_name}`}>
                      <AgentHint />
                    </HelpTip>
                  </div>
                  <div className="mt-2 flex flex-wrap items-center gap-3">
                    <span className="flex items-center gap-1.5 text-sm text-muted-foreground">
                      Main
                      <Switch
                        aria-label={`Keep main sessions warm for ${agent.display_name}`}
                        checked={row.main}
                        onCheckedChange={(main) => patch({ main })}
                        className="shrink-0"
                      />
                    </span>
                    <span className="flex items-center gap-1.5 text-sm text-muted-foreground">
                      Children
                      <Switch
                        aria-label={`Keep children warm for ${agent.display_name}`}
                        checked={row.child}
                        onCheckedChange={(child) => patch({ child })}
                        className="shrink-0"
                      />
                    </span>
                    <span className="flex items-center gap-1.5 text-sm text-muted-foreground">
                      Every
                      <NumericField
                        ariaLabel={`Keep-warm interval for ${agent.display_name} in minutes`}
                        value={row.intervalSeconds / 60}
                        min={intervalBounds.min / 60}
                        max={intervalBounds.max / 60}
                        disabled={inactive}
                        onCommit={(minutes) => patch({ intervalSeconds: minutes * 60 })}
                      />
                      min
                    </span>
                    <span className="flex items-center gap-1.5 text-sm text-muted-foreground">
                      Longest
                      <NumericField
                        ariaLabel={`Longest keep-warm run for ${agent.display_name} in hours`}
                        value={row.maxSeconds / 3600}
                        min={KEEP_WARM_MAX_BOUNDS_SECONDS.min / 3600}
                        max={KEEP_WARM_MAX_BOUNDS_SECONDS.max / 3600}
                        disabled={inactive}
                        onCommit={(hours) => patch({ maxSeconds: hours * 3600 })}
                      />
                      hours
                    </span>
                    <span className="flex items-center gap-1.5 text-sm text-muted-foreground">
                      Cold after
                      <NumericField
                        ariaLabel={`Cold after for ${agent.display_name} in minutes`}
                        value={row.coldAfterSeconds === null ? null : row.coldAfterSeconds / 60}
                        min={0}
                        max={KEEP_WARM_COLD_AFTER_BOUNDS_SECONDS.max / 60}
                        disabled={false}
                        placeholder="auto"
                        onClear={() => patch({ coldAfterSeconds: null })}
                        onCommit={(minutes) => patch({ coldAfterSeconds: minutes * 60 })}
                      />
                      min
                    </span>
                  </div>
                  <div className="mt-4 flex justify-end">
                    <Button
                      variant="outline"
                      onClick={openCopy}
                      disabled={copyCandidates.length === 0}
                    >
                      Apply to other deployed agents
                    </Button>
                  </div>
                </div>
              );
            })()}
        </div>
      )}

      {agentsReady && supported.length > 1 && (
        <details className="rounded-xl border border-border bg-card p-4">
          <summary className="cursor-pointer text-sm font-medium text-foreground">
            Agent overview
          </summary>
          <ul className="mt-3 space-y-2 text-sm text-muted-foreground">
            {supported.map(({ agent, family }) => {
              const row = resolveKeepWarmAgent(preferences, agent.id, family);
              return (
                <li
                  key={agent.id}
                  className="flex flex-wrap items-center justify-between gap-2 border-t border-border pt-2"
                >
                  <span className="text-foreground">{agent.display_name}</span>
                  <span>
                    Main {row.main ? "on" : "off"} · Children {row.child ? "on" : "off"} · Every{" "}
                    {row.intervalSeconds / 60} min
                  </span>
                </li>
              );
            })}
          </ul>
        </details>
      )}

      <Dialog
        open={copySource !== null}
        onOpenChange={(open) => {
          if (!open) setCopySource(null);
        }}
      >
        <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle>Apply to other deployed agents</DialogTitle>
            <DialogDescription>
              Source: {copySource?.name}. Only values supported by each target will be copied.
              Skipped fields keep their current values.
            </DialogDescription>
          </DialogHeader>
          <fieldset className="flex flex-wrap gap-x-4 gap-y-2">
            <legend className="mb-2 text-sm font-medium text-foreground">Fields</legend>
            {KEEP_WARM_COPY_FIELDS.map((field) => (
              <label key={field} className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={copyFields.has(field)}
                  onChange={() => toggleField(field)}
                />
                {COPY_FIELD_LABELS[field]}
              </label>
            ))}
          </fieldset>
          <div className="space-y-3">
            <h3 className="text-sm font-medium text-foreground">Targets and preview</h3>
            {copyCandidates.map((agent) => {
              const result = copyPlan?.results.find((item) => item.agentId === agent.id);
              return (
                <div key={agent.id} className="rounded-lg border border-border p-3 text-sm">
                  <label className="flex items-center gap-2 font-medium text-foreground">
                    <input
                      type="checkbox"
                      checked={copyTargets.has(agent.id)}
                      onChange={() => toggleTarget(agent.id)}
                    />
                    {agent.display_name}
                  </label>
                  {result && (
                    <div className="mt-2 space-y-1 pl-6 text-muted-foreground">
                      <p>
                        {result.applied.length > 0
                          ? `Apply: ${result.applied.map((field) => `${COPY_FIELD_LABELS[field]} ${copyValue(field, copySource!.values)}`).join(" · ")}`
                          : "No compatible fields to apply"}
                      </p>
                      {result.skipped.length > 0 && (
                        <p>
                          Skip:{" "}
                          {result.skipped
                            .map(({ field, reason }) => `${COPY_FIELD_LABELS[field]} (${reason})`)
                            .join(" · ")}
                        </p>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setCopySource(null)}>
              Cancel
            </Button>
            <Button
              disabled={!copyPlan?.results.some((result) => result.applied.length > 0)}
              onClick={() => {
                if (copyPlan) update(copyPlan.preferences);
                setCopySource(null);
              }}
            >
              Apply selected fields
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <h2 className="mt-3 text-ui font-medium">Hosts</h2>
      <div className="rounded-xl border border-border bg-card p-4">
        <SettingRow label="Archive children of an offline host after" hint={HINTS.archive}>
          <NumericField
            ariaLabel="Archive children of an offline host after in hours"
            value={preferences.hostOfflineArchiveSeconds / 3600}
            min={0}
            max={KEEP_WARM_HOST_OFFLINE_ARCHIVE_BOUNDS_SECONDS.max / 3600}
            disabled={!agentsReady}
            onCommit={(hours) =>
              update({
                ...preferences,
                hostOfflineArchiveSeconds: clampHostOfflineArchiveSeconds(hours * 3600),
              })
            }
          />
          <span className="text-sm text-muted-foreground">hours (0 = off)</span>
        </SettingRow>
      </div>
    </div>
  );
}
