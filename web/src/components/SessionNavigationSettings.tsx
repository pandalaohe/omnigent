import { useEffect, useState } from "react";

import { useSessionNavigationPreferences } from "@/hooks/useSessionNavigationPreferences";
import {
  MAX_SESSION_POLLING_WINDOW_HOURS,
  writeSessionNavigationPreferences,
} from "@/lib/sessionNavigationPreferences";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";

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
    <section className="mt-8 border-t border-border pt-5" data-testid="session-navigation-settings">
      <div className="flex items-start justify-between gap-6">
        <div className="min-w-0 flex-1">
          <label
            htmlFor="session-polling-active-window"
            className="text-sm font-medium text-foreground"
          >
            Polling window
          </label>
          <p
            className="mt-1 text-sm text-muted-foreground"
            title="Limits unread-first and next-session polling by recent session activity."
          >
            Blank includes all sessions.
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <Input
            id="session-polling-active-window"
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
        </div>
      </div>
      <div className="mt-5 flex items-start justify-between gap-6 border-t border-border pt-5">
        <div className="min-w-0 flex-1">
          <span className="text-sm font-medium text-foreground">Background sessions</span>
          <p
            className="mt-1 text-sm text-muted-foreground"
            title="Sessions showing B remain available, but Poll visits actionable and unread non-background sessions first."
          >
            Keep sessions showing B in the second polling pass.
          </p>
        </div>
        <Switch
          aria-label="Deprioritize background sessions while polling"
          checked={preferences.deprioritizeBackgroundSessions}
          onCheckedChange={(enabled) =>
            writeSessionNavigationPreferences({
              ...preferences,
              deprioritizeBackgroundSessions: enabled,
            })
          }
          className="shrink-0"
        />
      </div>
    </section>
  );
}

/** Appearance preference shared by Mobile Web and both native phone shells. */
export function MobileSessionTitleSetting() {
  const preferences = useSessionNavigationPreferences();

  return (
    <div
      className="flex items-start justify-between gap-6"
      data-testid="mobile-session-title-setting"
    >
      <div className="min-w-0 flex-1">
        <span className="text-ui font-medium">Mobile title</span>
        <span
          className="mt-1 block text-sm text-muted-foreground"
          title="Shows a truncated title on Mobile Web, iOS, and Android. In the apps, the Server switcher moves into the open left sidebar."
        >
          Show the current session title.
        </span>
      </div>
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
    </div>
  );
}
