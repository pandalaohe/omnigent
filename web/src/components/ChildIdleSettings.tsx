import { useEffect, useState } from "react";

import { NumericField, SettingRow } from "@/components/SettingsFields";
import { Switch } from "@/components/ui/switch";
import {
  CHILD_IDLE_CHANGED_EVENT,
  readChildIdlePreferences,
  writeChildIdlePreferences,
} from "@/lib/childIdlePreferences";

export function ChildIdleSettings() {
  const [preferences, setPreferences] = useState(readChildIdlePreferences);
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
        Tell a parent session when its children have been idle. Each delivered batch can start a
        parent turn; children receive no input. Works independently of keep-warm.
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
              disabled={!preferences.enabled}
              onCommit={(minutes) =>
                writeChildIdlePreferences({ ...preferences, [key]: minutes * 60 })
              }
            />
            min
          </span>
        </label>
      ))}
      <p className="text-xs text-muted-foreground">
        The first reminder waits for the idle threshold plus the merge window. Running sessions,
        pending questions and archived sessions are skipped. Parents must have a connected runner;
        busy parents wait until idle. Set repeat to 0 for one attempt per idle period. An uncertain
        delivery waits until the repeat interval rather than retrying immediately.
      </p>
    </div>
  );
}
