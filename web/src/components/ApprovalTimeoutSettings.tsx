import { useEffect, useState } from "react";

import { useApprovalTimeoutPreferences } from "@/hooks/useApprovalTimeoutPreferences";
import {
  MAX_APPROVAL_TIMEOUT_MINUTES,
  MIN_APPROVAL_TIMEOUT_MINUTES,
  writeApprovalTimeoutPreferences,
} from "@/lib/approvalTimeoutPreferences";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";

export function ApprovalTimeoutSettings() {
  const preferences = useApprovalTimeoutPreferences();
  const [minutesDraft, setMinutesDraft] = useState(preferences.timeoutMinutes.toString());

  useEffect(() => {
    setMinutesDraft(preferences.timeoutMinutes.toString());
  }, [preferences.timeoutMinutes]);

  const updateMinutes = (value: string) => {
    setMinutesDraft(value);
    if (!/^\d+$/.test(value)) return;
    const minutes = Number(value);
    if (minutes < MIN_APPROVAL_TIMEOUT_MINUTES || minutes > MAX_APPROVAL_TIMEOUT_MINUTES) return;
    writeApprovalTimeoutPreferences({ ...preferences, timeoutMinutes: minutes });
  };

  const restoreMinutesDraft = () => {
    setMinutesDraft(preferences.timeoutMinutes.toString());
  };

  return (
    <section className="flex flex-col" data-testid="approval-timeout-settings">
      <div className="flex items-start justify-between gap-6">
        <label htmlFor="approval-timeout-minutes" className="text-sm font-medium text-foreground">
          Timeout
        </label>
        <div className="flex shrink-0 items-center gap-2">
          <Input
            id="approval-timeout-minutes"
            type="number"
            inputMode="numeric"
            min={MIN_APPROVAL_TIMEOUT_MINUTES}
            max={MAX_APPROVAL_TIMEOUT_MINUTES}
            step={1}
            value={minutesDraft}
            onChange={(event) => updateMinutes(event.target.value)}
            onBlur={restoreMinutesDraft}
            className="h-9 w-20"
          />
          <span className="text-sm text-muted-foreground">min</span>
        </div>
      </div>
      <div className="mt-5 flex items-start justify-between gap-6 border-t border-border pt-5">
        <span className="text-sm font-medium text-foreground">Stop Turn on Timeout</span>
        <Switch
          aria-label="Stop the turn when the timeout expires"
          checked={preferences.stopTurn}
          onCheckedChange={(enabled) =>
            writeApprovalTimeoutPreferences({ ...preferences, stopTurn: enabled })
          }
          className="shrink-0"
        />
      </div>
    </section>
  );
}
