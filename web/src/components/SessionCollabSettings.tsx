import { useEffect, useState, type ReactNode } from "react";

import { HelpTip } from "@/components/HelpTip";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { useSessionCollabPreferences } from "@/hooks/useSessionCollabPreferences";
import { Link } from "@/lib/routing";
import {
  SESSION_COLLAB_BOUNDS,
  writeSessionCollabPreferences,
  type SessionCollabInboundPolicy,
  type SessionCollabPreferences,
} from "@/lib/sessionCollabPreferences";

const HINTS = {
  enabled:
    "When off, your sessions are not offered the collaboration tools and the server refuses their requests.",
  openRate: "Opening sessions (including child sessions) faster than this rate is refused.",
  relayDepth:
    "A relay chain of messages between sessions longer than this is held until you release it.",
  pairRate: "Messages past this rate between one pair of sessions wait in a queue.",
  senderRate: "The same limit applied per sending session across all of its receivers.",
  duplicate: "An identical message to the same session inside this window is dropped.",
  undelivered: "A queued or held message not delivered within this time expires.",
  defaultInbound:
    "Applies to sessions created after you change it; each session can still change its own policy.",
  flowTimer: "Lets agents run timed flows and timed wake-ups.",
};

interface NumericFieldProps {
  value: number;
  min: number;
  max: number;
  ariaLabel: string;
  disabled: boolean;
  onCommit: (value: number) => void;
}

/**
 * Numeric input with a local draft: only a valid in-range integer is
 * committed while typing; blur restores the last committed value.
 */
function NumericField({ value, min, max, ariaLabel, disabled, onCommit }: NumericFieldProps) {
  const [draft, setDraft] = useState(value.toString());

  useEffect(() => {
    setDraft(value.toString());
  }, [value]);

  const update = (text: string) => {
    setDraft(text);
    if (!/^\d+$/.test(text)) return;
    const parsed = Number(text);
    if (parsed < min || parsed > max) return;
    onCommit(parsed);
  };

  return (
    <Input
      type="number"
      inputMode="numeric"
      min={min}
      max={max}
      step={1}
      aria-label={ariaLabel}
      disabled={disabled}
      value={draft}
      onChange={(event) => update(event.target.value)}
      onBlur={() => setDraft(value.toString())}
      className="h-9 w-20"
    />
  );
}

function SettingRow({
  label,
  hint,
  children,
}: {
  label: string;
  hint: string;
  children: ReactNode;
}) {
  return (
    <div className="flex items-start justify-between gap-6">
      <div className="flex min-w-0 items-center gap-1.5">
        <span className="text-sm font-medium text-foreground">{label}</span>
        <HelpTip label={`About ${label}`}>{hint}</HelpTip>
      </div>
      <div className="flex shrink-0 items-center gap-2">{children}</div>
    </div>
  );
}

export function SessionCollabSettings() {
  const preferences = useSessionCollabPreferences();
  const update = (patch: Partial<SessionCollabPreferences>) =>
    writeSessionCollabPreferences({ ...preferences, ...patch });
  const disabled = !preferences.enabled;

  return (
    <div className="flex flex-col gap-3" data-testid="session-collab-settings">
      <div className="rounded-xl border border-border bg-card p-4">
        <SettingRow label="Enable session collaboration" hint={HINTS.enabled}>
          <Switch
            aria-label="Enable session collaboration"
            checked={preferences.enabled}
            onCheckedChange={(enabled) => update({ enabled })}
            className="shrink-0"
          />
        </SettingRow>
      </div>

      <h2 className="mt-3 text-ui font-medium">Opening sessions</h2>
      <div className="rounded-xl border border-border bg-card p-4">
        <SettingRow label="Session open rate limit" hint={HINTS.openRate}>
          <NumericField
            ariaLabel="Session open rate limit"
            value={preferences.openRateCount}
            min={SESSION_COLLAB_BOUNDS.openRateCount.min}
            max={SESSION_COLLAB_BOUNDS.openRateCount.max}
            disabled={disabled}
            onCommit={(openRateCount) => update({ openRateCount })}
          />
          <span className="text-sm text-muted-foreground">sessions per</span>
          <NumericField
            ariaLabel="Session open rate window in minutes"
            value={preferences.openRateWindowSeconds / 60}
            min={SESSION_COLLAB_BOUNDS.openRateWindowSeconds.min / 60}
            max={SESSION_COLLAB_BOUNDS.openRateWindowSeconds.max / 60}
            disabled={disabled}
            onCommit={(minutes) => update({ openRateWindowSeconds: minutes * 60 })}
          />
          <span className="text-sm text-muted-foreground">min</span>
        </SettingRow>
      </div>

      <h2 className="mt-3 text-ui font-medium">Messages between sessions</h2>
      <div className="rounded-xl border border-border bg-card p-4">
        <SettingRow label="Relay depth limit" hint={HINTS.relayDepth}>
          <NumericField
            ariaLabel="Relay depth limit"
            value={preferences.relayDepthMax}
            min={SESSION_COLLAB_BOUNDS.relayDepthMax.min}
            max={SESSION_COLLAB_BOUNDS.relayDepthMax.max}
            disabled={disabled}
            onCommit={(relayDepthMax) => update({ relayDepthMax })}
          />
          <span className="text-sm text-muted-foreground">hops</span>
        </SettingRow>

        <div className="mt-4 border-t border-border pt-4">
          <SettingRow label="Rate per session pair" hint={HINTS.pairRate}>
            <NumericField
              ariaLabel="Rate per session pair"
              value={preferences.pairRateCount}
              min={SESSION_COLLAB_BOUNDS.pairRateCount.min}
              max={SESSION_COLLAB_BOUNDS.pairRateCount.max}
              disabled={disabled}
              onCommit={(pairRateCount) => update({ pairRateCount })}
            />
            <span className="text-sm text-muted-foreground">messages per</span>
            <NumericField
              ariaLabel="Session pair rate window in seconds"
              value={preferences.pairRateWindowSeconds}
              min={SESSION_COLLAB_BOUNDS.pairRateWindowSeconds.min}
              max={SESSION_COLLAB_BOUNDS.pairRateWindowSeconds.max}
              disabled={disabled}
              onCommit={(pairRateWindowSeconds) => update({ pairRateWindowSeconds })}
            />
            <span className="text-sm text-muted-foreground">sec</span>
          </SettingRow>
        </div>

        <div className="mt-4 border-t border-border pt-4">
          <SettingRow label="Rate per sending session" hint={HINTS.senderRate}>
            <NumericField
              ariaLabel="Rate per sending session"
              value={preferences.senderRateCount}
              min={SESSION_COLLAB_BOUNDS.senderRateCount.min}
              max={SESSION_COLLAB_BOUNDS.senderRateCount.max}
              disabled={disabled}
              onCommit={(senderRateCount) => update({ senderRateCount })}
            />
            <span className="text-sm text-muted-foreground">messages per</span>
            <NumericField
              ariaLabel="Sending session rate window in minutes"
              value={preferences.senderRateWindowSeconds / 60}
              min={SESSION_COLLAB_BOUNDS.senderRateWindowSeconds.min / 60}
              max={SESSION_COLLAB_BOUNDS.senderRateWindowSeconds.max / 60}
              disabled={disabled}
              onCommit={(minutes) => update({ senderRateWindowSeconds: minutes * 60 })}
            />
            <span className="text-sm text-muted-foreground">min</span>
          </SettingRow>
        </div>

        <div className="mt-4 border-t border-border pt-4">
          <SettingRow label="Duplicate message window" hint={HINTS.duplicate}>
            <NumericField
              ariaLabel="Duplicate message window in seconds"
              value={preferences.duplicateWindowSeconds}
              min={SESSION_COLLAB_BOUNDS.duplicateWindowSeconds.min}
              max={SESSION_COLLAB_BOUNDS.duplicateWindowSeconds.max}
              disabled={disabled}
              onCommit={(duplicateWindowSeconds) => update({ duplicateWindowSeconds })}
            />
            <span className="text-sm text-muted-foreground">sec</span>
          </SettingRow>
        </div>

        <div className="mt-4 border-t border-border pt-4">
          <SettingRow label="Undelivered message lifetime" hint={HINTS.undelivered}>
            <NumericField
              ariaLabel="Undelivered message lifetime in hours"
              value={preferences.undeliveredTtlSeconds / 3600}
              min={SESSION_COLLAB_BOUNDS.undeliveredTtlSeconds.min / 3600}
              max={SESSION_COLLAB_BOUNDS.undeliveredTtlSeconds.max / 3600}
              disabled={disabled}
              onCommit={(hours) => update({ undeliveredTtlSeconds: hours * 3600 })}
            />
            <span className="text-sm text-muted-foreground">hours</span>
          </SettingRow>
        </div>

        <div className="mt-4 border-t border-border pt-4">
          <SettingRow label="Default inbound policy for new sessions" hint={HINTS.defaultInbound}>
            <Select
              value={preferences.defaultInbound}
              onValueChange={(value) =>
                update({ defaultInbound: value as SessionCollabInboundPolicy })
              }
              disabled={disabled}
              componentId="settings.session_collab.default_inbound"
              valueHasNoPii
            >
              <SelectTrigger
                aria-label="Default inbound policy for new sessions"
                className="w-28 shrink-0"
              >
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="accept">Accept</SelectItem>
                <SelectItem value="hold">Hold</SelectItem>
                <SelectItem value="refuse">Refuse</SelectItem>
              </SelectContent>
            </Select>
          </SettingRow>
        </div>
      </div>

      <h2 className="mt-3 text-ui font-medium">Timed flows</h2>
      <div className="rounded-xl border border-border bg-card p-4">
        <SettingRow label="Timed flows" hint={HINTS.flowTimer}>
          <Switch
            aria-label="Timed flows"
            checked={preferences.flowTimerEnabled}
            disabled={disabled}
            onCheckedChange={(flowTimerEnabled) => update({ flowTimerEnabled })}
            className="shrink-0"
          />
        </SettingRow>
      </div>

      <p className="text-sm text-muted-foreground">
        Stopping the session runner when a session is archived is set per host in{" "}
        <Link
          to="/settings/runtime-resources"
          className="font-medium text-foreground underline underline-offset-2"
        >
          Runtime &amp; resources
        </Link>
      </p>
    </div>
  );
}
