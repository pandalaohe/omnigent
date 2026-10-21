import { useCallback, useEffect, useState } from "react";

import { NumericField, SettingRow } from "@/components/SettingsFields";
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
      <SettingRow label="Monitor children" hint="Detect children that stop making progress.">
        <Switch
          aria-label={`Monitor ${label} children`}
          checked={value.enabled}
          onCheckedChange={(enabled) => onPatch({ enabled })}
        />
      </SettingRow>
      {(
        [
          ["checkIntervalSeconds", "Check every"],
          ["freezeThresholdSeconds", "Quiet or offline for"],
        ] as const
      ).map(([key, text]) => (
        <label key={key} className="flex flex-wrap items-center justify-between gap-2 text-sm">
          <span>{text}</span>
          <span className="flex items-center gap-2 text-muted-foreground">
            <NumericField
              ariaLabel={`${label} ${text.toLowerCase()} in minutes`}
              value={value[key] / 60}
              min={1}
              max={10080}
              disabled={!value.enabled}
              onCommit={(minutes) => onPatch({ [key]: minutes * 60 })}
            />{" "}
            min
          </span>
        </label>
      ))}
      <SettingRow
        label="Resume once after a temporary error"
        hint="Sends at most one automatic continuation per child session."
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
      <p className="text-sm text-muted-foreground">
        Tell a parent when a child stays idle, stops after a temporary service error, loses its
        runner, or stops making progress. Each notice can start a parent turn. Works independently
        of keep-warm.
      </p>
      <SettingRow
        label="Remind parent sessions"
        hint="Applies to your child sessions on every agent."
      >
        <Switch
          aria-label="Remind parent sessions about idle children"
          checked={preferences.enabled}
          onCheckedChange={(enabled) => writeChildIdlePreferences({ ...preferences, enabled })}
        />
      </SettingRow>
      {(
        [
          ["idleThresholdSeconds", "Idle for", "Idle threshold in minutes", 1, 10080],
          ["mergeWindowSeconds", "Combine reminders for", "Merge window in minutes", 0, 1440],
          ["repeatSeconds", "Remind again after", "Repeat interval in minutes", 0, 10080],
        ] as const
      ).map(([key, label, ariaLabel, min, max]) => (
        <label key={key} className="flex flex-wrap items-center justify-between gap-2 text-sm">
          <span className="font-medium text-foreground">{label}</span>
          <span className="flex items-center gap-2 text-muted-foreground">
            <NumericField
              ariaLabel={ariaLabel}
              value={preferences[key] / 60}
              min={min}
              max={max}
              disabled={key === "idleThresholdSeconds" ? !preferences.enabled : !anyEnabled}
              onCommit={(minutes) =>
                writeChildIdlePreferences({ ...preferences, [key]: minutes * 60 })
              }
            />
            min
          </span>
        </label>
      ))}
      <div className="flex flex-col gap-4">
        <div className="flex items-center gap-1.5 text-sm font-medium">
          Child freeze monitoring{" "}
          <HelpTip label="About child freeze monitoring">
            Checks can delay detection by one interval. Pending questions, archived sessions and
            running tools are skipped. Automatic continuation makes one attempt per child.
          </HelpTip>
        </div>
        <fieldset className="space-y-3 border-t border-border pt-3">
          <legend className="text-sm font-medium">All agents</legend>
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
        </fieldset>
        <ExceptionList
          rows={preferences.agents}
          items={registry.items}
          legacyLabels={registry.legacyLabels}
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
          onRemove={(key) => {
            const agents = Object.fromEntries(
              Object.entries(preferences.agents).filter(([id]) => id !== key),
            );
            writeChildIdlePreferences({ ...preferences, agents });
          }}
          isLoading={registry.isLoading}
          isError={registry.isError}
          onRetry={registry.retry}
        />
      </div>
    </div>
  );
}
