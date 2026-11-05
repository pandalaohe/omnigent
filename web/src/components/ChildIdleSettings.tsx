import { useCallback, useEffect, useId, useState } from "react";

import { NumericField, SettingRow } from "@/components/SettingsFields";
import { AgentNameTag } from "@/components/AgentNameTag";
import { ExceptionList } from "@/components/ExceptionList";
import { HelpTip } from "@/components/HelpTip";
import { Switch } from "@/components/ui/switch";
import { useAgentRegistry } from "@/hooks/useAgentRegistry";
import {
  CHILD_IDLE_CHANGED_EVENT,
  readChildIdlePreferences,
  writeChildIdlePreferences,
  type ChildFreezePreferences,
} from "@/lib/childIdlePreferences";

function FreezeControls({
  value,
  onPatch,
  label,
}: {
  value: ChildFreezePreferences;
  onPatch: (next: Partial<ChildFreezePreferences>) => void;
  label: string;
}) {
  return (
    <div className="space-y-3">
      <SettingRow label="Is stuck, offline or hit an error" indent={1}>
        <Switch
          aria-label={`Monitor ${label} children`}
          checked={value.enabled}
          onCheckedChange={(enabled) => onPatch({ enabled })}
        />
        <NumericField
          ariaLabel={`${label} quiet or offline for in minutes`}
          value={value.freezeThresholdSeconds / 60}
          min={1}
          max={10080}
          disabled={!value.enabled}
          onCommit={(minutes) => onPatch({ freezeThresholdSeconds: minutes * 60 })}
        />
        <span className="text-sm text-muted-foreground">min</span>
      </SettingRow>
      <SettingRow label="Check every" indent={2}>
        <NumericField
          ariaLabel={`${label} check every in minutes`}
          value={value.checkIntervalSeconds / 60}
          min={1}
          max={10080}
          disabled={!value.enabled}
          onCommit={(minutes) => onPatch({ checkIntervalSeconds: minutes * 60 })}
        />
        <span className="text-sm text-muted-foreground">min</span>
      </SettingRow>
      <SettingRow
        label="Resume once after a temporary error"
        hint="Sends at most one automatic continuation per child session."
        indent={2}
      >
        <Switch
          aria-label={`Resume ${label} once`}
          checked={value.resumeOnce}
          disabled={!value.enabled}
          onCheckedChange={(resumeOnce) => onPatch({ resumeOnce })}
        />
      </SettingRow>
    </div>
  );
}

export function ChildIdleSettings() {
  const [preferences, setPreferences] = useState(readChildIdlePreferences);
  const coverageId = useId();
  const registry = useAgentRegistry();
  const renderEditor = useCallback(
    (
      row: ChildFreezePreferences,
      patch: (next: Partial<ChildFreezePreferences>) => void,
      key: string,
    ) => (
      <FreezeControls
        label={
          registry.items.find((item) => item.key === key)?.label ??
          registry.legacyLabels[key] ??
          key
        }
        value={row}
        onPatch={patch}
      />
    ),
    [registry.items, registry.legacyLabels],
  );
  const anyEnabled =
    preferences.enabled ||
    preferences.freezeDefault.enabled ||
    Object.values(preferences.agents).some((row) => row.enabled);
  useEffect(() => {
    const refresh = () => setPreferences(readChildIdlePreferences());
    window.addEventListener(CHILD_IDLE_CHANGED_EVENT, refresh);
    window.addEventListener("storage", refresh);
    return () => {
      window.removeEventListener(CHILD_IDLE_CHANGED_EVENT, refresh);
      window.removeEventListener("storage", refresh);
    };
  }, []);

  return (
    <div className="flex flex-col gap-4" data-testid="child-idle-settings">
      <div className="flex items-center gap-1.5 text-ui font-medium">
        Tell the parent session when a child…
        <HelpTip label="About child check-ins" className="size-11 md:size-4">
          Each check-in can start a parent turn. Completion and approval notices are unaffected.
          Works independently of keep-warm. Checks can delay detection by one interval. Pending
          questions, archived sessions and running tools are skipped.
        </HelpTip>
      </div>
      <SettingRow label="Is idle" indent={1}>
        <Switch
          aria-label="Remind parent sessions about idle children"
          checked={preferences.enabled}
          onCheckedChange={(enabled) => writeChildIdlePreferences({ ...preferences, enabled })}
        />
        <NumericField
          ariaLabel="Idle threshold in minutes"
          value={preferences.idleThresholdSeconds / 60}
          min={1}
          max={10080}
          disabled={!preferences.enabled}
          onCommit={(minutes) =>
            writeChildIdlePreferences({ ...preferences, idleThresholdSeconds: minutes * 60 })
          }
        />
        <span className="text-sm text-muted-foreground">min</span>
      </SettingRow>
      <FreezeControls
        label="all agents"
        value={preferences.freezeDefault}
        onPatch={(next) =>
          writeChildIdlePreferences({
            ...preferences,
            freezeDefault: { ...preferences.freezeDefault, ...next },
          })
        }
      />
      <div className="pl-8 md:pl-12">
        <ExceptionList
          rows={preferences.agents}
          items={registry.items}
          legacyLabels={registry.legacyLabels}
          savedItems={registry.legacyProducts}
          renderLabel={(key, name) => {
            const item =
              registry.items.find((candidate) => candidate.key === key) ??
              registry.legacyProducts?.[key];
            return (
              <AgentNameTag
                agentId={
                  key.startsWith("harness:")
                    ? (Object.values(registry.legacyProducts ?? {}).find(
                        (product) => product.harness === item?.harness,
                      )?.key ?? null)
                    : key
                }
                name={name}
                harness={item?.harness ?? null}
              />
            );
          }}
          summary={(row) =>
            row.enabled
              ? `Monitor · every ${row.checkIntervalSeconds / 60} min · quiet ${row.freezeThresholdSeconds / 60} min${row.resumeOnce ? " · resume once" : ""}`
              : "Monitoring off"
          }
          editor={renderEditor}
          onAdd={(key) =>
            writeChildIdlePreferences({
              ...preferences,
              agents: { ...preferences.agents, [key]: { ...preferences.freezeDefault } },
            })
          }
          onPatch={(key, next) =>
            writeChildIdlePreferences({
              ...preferences,
              agents: { ...preferences.agents, [key]: { ...preferences.agents[key], ...next } },
            })
          }
          onRemove={(key) =>
            writeChildIdlePreferences({
              ...preferences,
              agents: Object.fromEntries(
                Object.entries(preferences.agents).filter(([id]) => id !== key),
              ),
            })
          }
          isLoading={registry.isLoading}
          isError={registry.isError}
          onRetry={registry.retry}
        />
      </div>
      <SettingRow label="Combine reminders for">
        <NumericField
          ariaLabel="Merge window in minutes"
          value={preferences.mergeWindowSeconds / 60}
          min={0}
          max={1440}
          disabled={!anyEnabled}
          onCommit={(minutes) =>
            writeChildIdlePreferences({ ...preferences, mergeWindowSeconds: minutes * 60 })
          }
        />
        <span className="text-sm text-muted-foreground">min</span>
      </SettingRow>
      <SettingRow
        label="Remind again every"
        hint="Repeat a check-in when a child's state has not changed. 0 sends no repeat reminders."
      >
        <NumericField
          ariaLabel="Repeat interval in minutes"
          value={preferences.repeatSeconds / 60}
          min={0}
          max={10080}
          disabled={!anyEnabled}
          onCommit={(minutes) =>
            writeChildIdlePreferences({ ...preferences, repeatSeconds: minutes * 60 })
          }
        />
        <span className="text-sm text-muted-foreground">min</span>
      </SettingRow>
      <SettingRow
        label="At most one notice per parent every"
        hint="0 uses per-child reminders without a shared parent interval. A parent still waiting for this interval may hear about a child later."
      >
        <NumericField
          ariaLabel="Minimum check-in gap in minutes"
          value={preferences.minGapSeconds / 60}
          min={0}
          max={1440}
          disabled={!anyEnabled}
          onCommit={(minutes) =>
            writeChildIdlePreferences({ ...preferences, minGapSeconds: minutes * 60 })
          }
        />
        <span className="text-sm text-muted-foreground">min</span>
      </SettingRow>
      <fieldset className="space-y-3" disabled={!anyEnabled}>
        <legend className="mb-3 text-sm font-medium">Check-in coverage</legend>
        {(
          [
            ["changed", "Changed children only"],
            ["summary", "Changed children + all other open children"],
          ] as const
        ).map(([value, label]) => (
          <label key={value} className="flex items-start gap-2.5 text-ui">
            <input
              type="radio"
              name={coverageId}
              value={value}
              checked={preferences.coverage === value}
              onChange={() => writeChildIdlePreferences({ ...preferences, coverage: value })}
              className="mt-1 accent-primary"
            />
            <span>{label}</span>
          </label>
        ))}
      </fieldset>
    </div>
  );
}
