import { useEffect, useState } from "react";

import { NumericField, SettingRow } from "@/components/SettingsFields";
import { Switch } from "@/components/ui/switch";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import {
  CHILD_FREEZE_DEFAULTS,
  CHILD_IDLE_CHANGED_EVENT,
  readChildIdlePreferences,
  writeChildIdlePreferences,
  type ChildFreezePreferences,
} from "@/lib/childIdlePreferences";

export function ChildIdleSettings() {
  const [preferences, setPreferences] = useState(readChildIdlePreferences);
  const { data: agents, isLoading } = useAvailableAgents();
  const anyEnabled =
    preferences.enabled || Object.values(preferences.agents).some((row) => row.enabled);
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
        <p className="text-sm font-medium">Child freeze monitoring</p>
        {isLoading && <p className="text-sm text-muted-foreground">Loading agents…</p>}
        {agents?.map((agent) => {
          const row = preferences.agents[agent.id] ?? CHILD_FREEZE_DEFAULTS;
          const patch = (next: Partial<ChildFreezePreferences>) =>
            writeChildIdlePreferences({
              ...preferences,
              agents: { ...preferences.agents, [agent.id]: { ...row, ...next } },
            });
          return (
            <fieldset key={agent.id} className="flex flex-col gap-3 border-t border-border pt-3">
              <legend className="text-sm font-medium">{agent.display_name}</legend>
              <SettingRow
                label="Monitor children"
                hint="Covers this agent’s children of every parent session."
              >
                <Switch
                  aria-label={`Monitor ${agent.display_name} children`}
                  checked={row.enabled}
                  onCheckedChange={(enabled) => patch({ enabled })}
                />
              </SettingRow>
              {(
                [
                  ["checkIntervalSeconds", "Check every"],
                  ["freezeThresholdSeconds", "Quiet or offline for"],
                ] as const
              ).map(([key, label]) => (
                <label
                  key={key}
                  className="flex flex-wrap items-center justify-between gap-2 text-sm"
                >
                  <span>{label}</span>
                  <span className="flex items-center gap-2 text-muted-foreground">
                    <NumericField
                      ariaLabel={`${agent.display_name} ${label.toLowerCase()} in minutes`}
                      value={row[key] / 60}
                      min={1}
                      max={10080}
                      disabled={!row.enabled}
                      onCommit={(minutes) => patch({ [key]: minutes * 60 })}
                    />{" "}
                    min
                  </span>
                </label>
              ))}
              <SettingRow
                label="Resume once after a temporary error"
                hint="Sends at most one automatic continuation per child session. Further failures are reported to its parent, including after successful turns or reconnects."
              >
                <Switch
                  aria-label={`Resume ${agent.display_name} once`}
                  checked={row.resumeOnce}
                  disabled={!row.enabled}
                  onCheckedChange={(resumeOnce) => patch({ resumeOnce })}
                />
              </SettingRow>
            </fieldset>
          );
        })}
      </div>
      <p className="text-xs text-muted-foreground">
        Monitoring starts when the server first observes a signal. Checks can delay detection by one
        check interval; reminders also wait for the shared merge window. Pending questions, archived
        sessions and running tools are skipped. Parents need a connected runner and wait until idle.
        Repeat 0 allows one notice per observed episode. Automatic continuation needs its own
        switch; uncertain delivery consumes that attempt.
      </p>
    </div>
  );
}
