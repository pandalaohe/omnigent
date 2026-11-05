import { useEffect, useState } from "react";

import { useSessionNavigationPreferences } from "@/hooks/useSessionNavigationPreferences";
import {
  MAX_SESSION_POLLING_WINDOW_HOURS,
  writeSessionNavigationPreferences,
} from "@/lib/sessionNavigationPreferences";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { SettingRow } from "@/components/SettingsFields";

export function SessionNavigationSettings() {
  const preferences = useSessionNavigationPreferences();
  const [hoursDraft, setHoursDraft] = useState(
    preferences.pollingActiveWindowHours?.toString() ?? "",
  );

  useEffect(() => {
    setHoursDraft(preferences.pollingActiveWindowHours?.toString() ?? "");
  }, [preferences.pollingActiveWindowHours]);

  const updateHours = (value: string) => {
    setHoursDraft(value);
    if (value === "") {
      writeSessionNavigationPreferences({
        ...preferences,
        pollingActiveWindowHours: null,
      });
      return;
    }
    if (!/^\d+$/.test(value)) return;
    const hours = Number(value);
    if (hours < 1 || hours > MAX_SESSION_POLLING_WINDOW_HOURS) return;
    writeSessionNavigationPreferences({ ...preferences, pollingActiveWindowHours: hours });
  };

  const restoreHoursDraft = () => {
    setHoursDraft(preferences.pollingActiveWindowHours?.toString() ?? "");
  };

  return (
    <section className="mt-3 space-y-3" data-testid="session-navigation-settings">
      <SettingRow
        label="Polling window"
        indent={1}
        hint="Limits polling by recent session activity. Leave blank to include all sessions."
      >
        <Input
          id="session-polling-active-window"
          aria-label="Polling window"
          type="number"
          inputMode="numeric"
          min={1}
          max={MAX_SESSION_POLLING_WINDOW_HOURS}
          step={1}
          placeholder="All"
          value={hoursDraft}
          onChange={(event) => updateHours(event.target.value)}
          onBlur={restoreHoursDraft}
          className="h-9 w-20"
        />
        <span className="text-sm text-muted-foreground">h</span>
      </SettingRow>
      <SettingRow
        label="Background and Goal sessions"
        indent={1}
        hint="Keep unread sessions with background work or a Goal in polling order. Sessions waiting for your answer still come first."
      >
        <Switch
          aria-label="Deprioritize background and Goal sessions while polling"
          checked={preferences.deprioritizeBackgroundSessions}
          onCheckedChange={(enabled) =>
            writeSessionNavigationPreferences({
              ...preferences,
              deprioritizeBackgroundSessions: enabled,
            })
          }
          className="shrink-0"
        />
      </SettingRow>
    </section>
  );
}

export function OpenAtLatestSetting() {
  const preferences = useSessionNavigationPreferences();
  return (
    <SettingRow
      label="Open at latest"
      hint="Scroll to the bottom when switching sessions, including with Poll."
    >
      <Switch
        aria-label="Scroll to the bottom when opening a session"
        checked={preferences.scrollToBottomOnSessionOpen}
        onCheckedChange={(enabled) =>
          writeSessionNavigationPreferences({
            ...preferences,
            scrollToBottomOnSessionOpen: enabled,
          })
        }
      />
    </SettingRow>
  );
}

export function GoalMarkersSetting() {
  const preferences = useSessionNavigationPreferences();
  return (
    <SettingRow
      label="Goal markers"
      hint="Show Goal outlines and badges in the session list, including Goals managed by the agent."
    >
      <Switch
        aria-label="Show Goal markers in the session list"
        checked={preferences.showGoalSessionMarkers}
        onCheckedChange={(enabled) =>
          writeSessionNavigationPreferences({ ...preferences, showGoalSessionMarkers: enabled })
        }
        className="shrink-0"
      />
    </SettingRow>
  );
}

/** Appearance preference shared by Mobile Web and both native phone shells. */
export function MobileSessionTitleSetting() {
  const preferences = useSessionNavigationPreferences();

  return (
    <div data-testid="mobile-session-title-setting">
      <SettingRow
        label="Mobile title"
        hint="Show the current session title on mobile. In the apps, the Server switcher moves into the left sidebar."
      >
        <Switch
          aria-label="Show session title in the mobile top bar"
          checked={preferences.nativeMobileHeaderMode === "conversation-title"}
          onCheckedChange={(enabled) =>
            writeSessionNavigationPreferences({
              ...preferences,
              nativeMobileHeaderMode: enabled ? "conversation-title" : "server",
            })
          }
          className="shrink-0"
        />
      </SettingRow>
    </div>
  );
}
