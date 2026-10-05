import { useEffect, useMemo } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { NumericField, SettingRow } from "@/components/SettingsFields";
import { Switch } from "@/components/ui/switch";
import {
  prefetchAvailableAgentDetails,
  useAvailableAgents,
  type AvailableAgent,
} from "@/hooks/useAvailableAgents";
import { useKeepWarmPreferences } from "@/hooks/useKeepWarmPreferences";
import {
  clampHostOfflineArchiveSeconds,
  keepWarmFamilyForHarness,
  keepWarmIntervalBoundsSeconds,
  resolveKeepWarmAgent,
  setKeepWarmAgent,
  writeKeepWarmPreferences,
  KEEP_WARM_HOST_OFFLINE_ARCHIVE_BOUNDS_SECONDS,
  KEEP_WARM_MAX_BOUNDS_SECONDS,
  type AgentKeepWarmPatch,
  type KeepWarmFamily,
  type KeepWarmPreferences,
} from "@/lib/keepWarmPreferences";

const HINTS = {
  archive:
    "Children whose host stays offline this long are archived automatically. 0 turns auto-archive off.",
};

/** One-line explanations for the four per-agent controls. */
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
    </div>
  );
}

interface SupportedAgent {
  agent: AvailableAgent;
  family: KeepWarmFamily;
}

export function KeepWarmSettings() {
  const preferences = useKeepWarmPreferences();
  const queryClient = useQueryClient();
  const { data: agents, isLoading, isPlaceholderData } = useAvailableAgents();
  // Session-discovered agents arrive with a null harness; resolve them through
  // the pickers' own prefetch, which patches the shared cache in place and
  // rerenders this list with the resolved harness.
  useEffect(() => {
    for (const agent of agents ?? []) {
      void prefetchAvailableAgentDetails(agent, queryClient);
    }
  }, [agents, queryClient]);
  const supported = useMemo(() => {
    const rows: SupportedAgent[] = [];
    for (const agent of agents ?? []) {
      const family = keepWarmFamilyForHarness(agent.harness);
      if (family) rows.push({ agent, family });
    }
    return rows;
  }, [agents]);

  const update = (next: KeepWarmPreferences) => writeKeepWarmPreferences(next, agents ?? []);
  // The catalog-only placeholder omits session-discovered agents, so it is not
  // a complete list: no rows and no stored-row writes until the merged list
  // lands. An unresolved harness is left out of `supported` until prefetch
  // fills it rather than dropped from the source list.
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
      {agentsReady &&
        supported.map(({ agent, family }) => {
          const row = resolveKeepWarmAgent(preferences, agent.id, family);
          const intervalBounds = keepWarmIntervalBoundsSeconds(family);
          const inactive = !row.main && !row.child;
          const patch = (next: AgentKeepWarmPatch) =>
            update(setKeepWarmAgent(preferences, agent.id, family, next));
          return (
            <div
              key={agent.id}
              className="rounded-xl border border-border bg-card p-4"
              data-testid="keep-warm-agent-row"
              data-agent-id={agent.id}
            >
              <SettingRow label={agent.display_name} hint={<AgentHint />}>
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
              </SettingRow>
            </div>
          );
        })}

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
