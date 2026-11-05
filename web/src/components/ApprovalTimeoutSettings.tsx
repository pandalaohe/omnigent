import { useEffect, useState } from "react";

import { useApprovalTimeoutPreferences } from "@/hooks/useApprovalTimeoutPreferences";
import {
  MAX_APPROVAL_TIMEOUT_MINUTES,
  MIN_APPROVAL_TIMEOUT_MINUTES,
  writeApprovalTimeoutPreferences,
} from "@/lib/approvalTimeoutPreferences";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { SettingRow } from "@/components/SettingsFields";

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
      <SettingRow
        label="Timeout"
        note={preferences.asyncApprovals ? "Used when Async approvals is off." : null}
      >
        <Input
          id="approval-timeout-minutes"
          aria-label="Timeout"
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
      </SettingRow>
      <div className="mt-5 border-t border-border pt-5">
        <SettingRow
          label="Stop turn on timeout"
          hint="Stop a turn when its blocking approval request times out."
        >
          <Switch
            aria-label="Stop the turn when the timeout expires"
            checked={preferences.stopTurn}
            onCheckedChange={(enabled) =>
              writeApprovalTimeoutPreferences({ ...preferences, stopTurn: enabled })
            }
            className="shrink-0"
          />
        </SettingRow>
      </div>
      <div className="mt-5 border-t border-border pt-5">
        <SettingRow
          label="Async approvals"
          hint="Let the agent continue while a call waits for approval, then retry it once approved. Blocking approval timeouts apply only when this is off."
        >
          <Switch
            aria-label="Async Approvals"
            checked={preferences.asyncApprovals}
            onCheckedChange={(enabled) =>
              writeApprovalTimeoutPreferences({ ...preferences, asyncApprovals: enabled })
            }
            className="shrink-0"
          />
        </SettingRow>
      </div>
    </section>
  );
}
