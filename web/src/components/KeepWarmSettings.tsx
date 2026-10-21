import { useCallback, useMemo } from "react";

import { NumericField, SettingRow } from "@/components/SettingsFields";
import { ExceptionList } from "@/components/ExceptionList";
import { HelpTip } from "@/components/HelpTip";
import { Switch } from "@/components/ui/switch";
import { useAgentRegistry, type RegistryItem } from "@/hooks/useAgentRegistry";
import { useKeepWarmPreferences } from "@/hooks/useKeepWarmPreferences";
import { useHarnessCatalog, type HarnessCapability } from "@/lib/agentLabels";
import {
  clampHostOfflineArchiveSeconds,
  clampKeepWarmColdAfterSeconds,
  keepWarmFamilyForHarness,
  keepWarmIntervalBoundsSeconds,
  resolveKeepWarmAgent,
  setKeepWarmAgent,
  setKeepWarmDefault,
  writeKeepWarmPreferences,
  KEEP_WARM_COLD_AFTER_BOUNDS_SECONDS,
  KEEP_WARM_HOST_OFFLINE_ARCHIVE_BOUNDS_SECONDS,
  KEEP_WARM_MAX_BOUNDS_SECONDS,
  type AgentKeepWarmPreferences,
  type AgentKeepWarmPatch,
  type KeepWarmFamily,
  type KeepWarmPreferences,
} from "@/lib/keepWarmPreferences";

const EMPTY_CAPABILITIES: Record<string, HarnessCapability> = {};

function KeepWarmControls({
  label,
  row,
  family,
  onPatch,
  onClearCold,
}: {
  label: string;
  row: AgentKeepWarmPreferences;
  family: KeepWarmFamily | null;
  onPatch: (next: Partial<AgentKeepWarmPreferences>) => void;
  onClearCold: () => void;
}) {
  const intervalBounds = family ? keepWarmIntervalBoundsSeconds(family) : null;
  const inactive = !row.main && !row.child;
  return (
    <div className="space-y-3">
      <SettingRow label="Main sessions" hint="Keep non-archived main sessions warm.">
        <Switch
          aria-label={`Keep main sessions warm for ${label}`}
          checked={row.main}
          onCheckedChange={(main) => onPatch({ main })}
        />
      </SettingRow>
      <SettingRow label="Children" hint="Keep active sub-agent sessions warm.">
        <Switch
          aria-label={`Keep children warm for ${label}`}
          checked={row.child}
          onCheckedChange={(child) => onPatch({ child })}
        />
      </SettingRow>
      {intervalBounds && (
        <SettingRow label="Every" hint="Quiet gap between cache-read pings.">
          <NumericField
            ariaLabel={`Keep-warm interval for ${label} in minutes`}
            value={(row.intervalSeconds ?? intervalBounds.min) / 60}
            min={intervalBounds.min / 60}
            max={intervalBounds.max / 60}
            disabled={inactive}
            onCommit={(minutes) => onPatch({ intervalSeconds: minutes * 60 })}
          />
          <span className="text-sm text-muted-foreground">min</span>
        </SettingRow>
      )}
      <SettingRow label="Longest" hint="Stop warming this long after the last real turn.">
        <NumericField
          ariaLabel={`Longest keep-warm run for ${label} in hours`}
          value={row.maxSeconds / 3600}
          min={KEEP_WARM_MAX_BOUNDS_SECONDS.min / 3600}
          max={KEEP_WARM_MAX_BOUNDS_SECONDS.max / 3600}
          disabled={inactive}
          onCommit={(hours) => onPatch({ maxSeconds: hours * 3600 })}
        />
        <span className="text-sm text-muted-foreground">hours</span>
      </SettingRow>
      <SettingRow
        label="Cold after"
        hint="Idle time before the blue dot appears; empty uses the platform rule, 0 means never cold."
      >
        <NumericField
          ariaLabel={`Cold after for ${label} in minutes`}
          value={row.coldAfterSeconds === undefined ? null : row.coldAfterSeconds / 60}
          min={0}
          max={KEEP_WARM_COLD_AFTER_BOUNDS_SECONDS.max / 60}
          disabled={false}
          placeholder="auto"
          onClear={onClearCold}
          onCommit={(minutes) => onPatch({ coldAfterSeconds: minutes * 60 })}
        />
        <span className="text-sm text-muted-foreground">min</span>
      </SettingRow>
    </div>
  );
}

export function KeepWarmSettings() {
  const preferences = useKeepWarmPreferences();
  const registry = useAgentRegistry();
  const capabilities = useHarnessCatalog(
    (catalog) => catalog.capabilitiesByHarness,
    EMPTY_CAPABILITIES,
  );
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
  const familyFor = useCallback(
    (item: RegistryItem | undefined) => keepWarmFamilyForHarness(item?.harness, capabilities),
    [capabilities],
  );
  const update = useCallback(
    (next: KeepWarmPreferences) =>
      writeKeepWarmPreferences(
        next,
        [...registry.items, ...Object.values(registry.legacyProducts)].map((item) => ({
          id: item.key,
          harness: item.harness,
        })),
      ),
    [registry.items, registry.legacyProducts],
  );
  const patchException = useCallback(
    (key: string, patch: AgentKeepWarmPatch) => {
      const family = familyFor(byKey.get(key));
      if (family) update(setKeepWarmAgent(preferences, key, family, patch));
      else {
        const current = preferences.agents[key];
        const { coldAfterSeconds, ...fields } = patch;
        const next: AgentKeepWarmPreferences = { ...current, ...fields };
        if (coldAfterSeconds === null) delete next.coldAfterSeconds;
        else if (coldAfterSeconds !== undefined)
          next.coldAfterSeconds = clampKeepWarmColdAfterSeconds(coldAfterSeconds);
        update({ ...preferences, agents: { ...preferences.agents, [key]: next } });
      }
    },
    [byKey, familyFor, preferences, update],
  );
  const renderEditor = useCallback(
    (
      row: AgentKeepWarmPreferences,
      patch: (next: Partial<AgentKeepWarmPreferences>) => void,
      key: string,
    ) => {
      const item = byKey.get(key);
      return (
        <>
          {!familyFor(item) && (
            <p className="mb-3 text-xs text-muted-foreground">
              This saved agent has no declared keep-warm channel. Its interval is preserved.
            </p>
          )}
          <KeepWarmControls
            label={item?.label ?? registry.legacyLabels[key] ?? key}
            row={
              familyFor(item)
                ? {
                    ...resolveKeepWarmAgent(preferences, key, familyFor(item)!),
                    coldAfterSeconds: row.coldAfterSeconds,
                  }
                : row
            }
            family={familyFor(item)}
            onPatch={patch}
            onClearCold={() => patchException(key, { coldAfterSeconds: null })}
          />
        </>
      );
    },
    [byKey, familyFor, patchException, preferences, registry.legacyLabels],
  );

  return (
    <div className="flex flex-col gap-4" data-testid="keep-warm-settings">
      <div className="rounded-xl border border-border bg-card p-4">
        <div className="flex items-center gap-1.5 text-sm font-medium">
          All agents
          <HelpTip label="About keep-warm">
            <p>Main covers non-archived sessions. Children are sub-agent sessions.</p>
            <p>
              Each ping costs one cache read. The interval follows the harness family unless an
              exception sets it.
            </p>
            <p>
              Cold after is when an idle session shows a blue dot; empty uses the platform rule and
              0 means never cold.
            </p>
          </HelpTip>
        </div>
        <div className="mt-3 border-t border-border pt-3">
          <KeepWarmControls
            label="all agents"
            row={preferences.default}
            family={null}
            onPatch={(patch) => update(setKeepWarmDefault(preferences, patch))}
            onClearCold={() => update(setKeepWarmDefault(preferences, { coldAfterSeconds: null }))}
          />
        </div>
      </div>

      <ExceptionList
        rows={preferences.agents}
        items={registry.items}
        legacyLabels={registry.legacyLabels}
        savedItems={registry.legacyProducts}
        summary={(row) =>
          `${row.main ? "Main on" : "Main off"} · ${row.child ? "Children on" : "Children off"} · ${row.intervalSeconds ? `every ${row.intervalSeconds / 60} min · ` : ""}longest ${row.maxSeconds / 3600} hours`
        }
        editor={renderEditor}
        supports={(item) =>
          familyFor(item) ? null : "This agent has no declared keep-warm channel"
        }
        onAdd={(key) => {
          const family = familyFor(byKey.get(key));
          if (family) update(setKeepWarmAgent(preferences, key, family, {}));
        }}
        onPatch={patchException}
        onRemove={(key) => {
          const agents = Object.fromEntries(
            Object.entries(preferences.agents).filter(([id]) => id !== key),
          );
          update({ ...preferences, agents });
        }}
        isLoading={registry.isLoading}
        isError={registry.isError}
        onRetry={registry.retry}
      />

      <h2 className="mt-3 text-ui font-medium">Hosts</h2>
      <div className="rounded-xl border border-border bg-card p-4">
        <SettingRow
          label="Archive children of an offline host after"
          hint="Children whose host stays offline this long are archived automatically. 0 turns auto-archive off."
        >
          <NumericField
            ariaLabel="Archive children of an offline host after in hours"
            value={preferences.hostOfflineArchiveSeconds / 3600}
            min={0}
            max={KEEP_WARM_HOST_OFFLINE_ARCHIVE_BOUNDS_SECONDS.max / 3600}
            disabled={false}
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
