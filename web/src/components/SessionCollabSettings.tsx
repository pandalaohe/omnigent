import { NumericField, SettingRow } from "@/components/SettingsFields";
import { Switch } from "@/components/ui/switch";
import { useHostColorPreferences } from "@/hooks/useHostColorPreferences";
import { useHosts, type Host } from "@/hooks/useHosts";
import { useSessionCollabPreferences } from "@/hooks/useSessionCollabPreferences";
import {
  HOST_COLORS,
  hostDisplayName,
  AUTO_HOST_COLOR,
  type HostColorPreferences,
} from "@/lib/hostColors";
import { patchHostColor } from "@/lib/hostColorPreferences";
import { Link } from "@/lib/routing";
import {
  SESSION_COLLAB_BOUNDS,
  writeSessionCollabPreferences,
  type SessionCollabPreferences,
} from "@/lib/sessionCollabPreferences";
import { cn } from "@/lib/utils";

const HINTS = {
  enabled:
    "When off, your sessions are not offered the collaboration tools and the server refuses their requests.",
  openRate: "Opening sessions (including child sessions) faster than this rate is refused.",
  relayDepth:
    "A relay chain of messages between sessions longer than this is held until you release it.",
  pairRate: "Messages past this rate between one pair of sessions wait in a queue.",
  senderRate: "The same limit applied per sending session across all of its receivers.",
  duplicate:
    "An identical message on the same thread to the same session is dropped while the earlier copy is still undelivered, or inside this window.",
  undelivered: "A queued or held message not delivered within this time expires.",
  flowTimer: "Lets agents run timed flows and timed wake-ups.",
};

/**
 * One host's colour picker: eight palette swatches and a reset back to the
 * automatic hash colour. Rows with no pick read "Automatic".
 */
function HostColorRow({ host, preferences }: { host: Host; preferences: HostColorPreferences }) {
  const stored = preferences[host.host_id];
  const selected = stored === undefined || stored === AUTO_HOST_COLOR ? null : stored;
  const name = hostDisplayName(host.host_id, host);
  return (
    <div
      className="flex items-center justify-between gap-4"
      data-testid="host-color-row"
      data-host-id={host.host_id}
    >
      <span className="min-w-0 truncate text-sm text-foreground">{name}</span>
      <div className="flex shrink-0 items-center gap-1.5">
        {HOST_COLORS.map((entry) => (
          <button
            key={entry.key}
            type="button"
            aria-label={`${name} colour: ${entry.key}`}
            aria-pressed={selected === entry.key}
            title={entry.key}
            onClick={() => patchHostColor(host.host_id, entry.key)}
            className={cn(
              "size-4 rounded-[4px] border border-black/10 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
              selected === entry.key && "ring-2 ring-ring ring-offset-1 ring-offset-card",
            )}
            style={{ backgroundColor: entry.hex }}
          />
        ))}
        {selected === null ? (
          <span className="ml-1 w-16 text-right text-xs text-muted-foreground">Automatic</span>
        ) : (
          <button
            type="button"
            onClick={() => patchHostColor(host.host_id, null)}
            className="ml-1 text-xs text-muted-foreground underline underline-offset-2 hover:text-foreground"
          >
            Reset to automatic
          </button>
        )}
      </div>
    </div>
  );
}

export function SessionCollabSettings() {
  const preferences = useSessionCollabPreferences();
  const { data: hosts } = useHosts();
  const hostColorPreferences = useHostColorPreferences();
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
              ariaLabel="Undelivered message lifetime in minutes"
              value={preferences.undeliveredTtlSeconds / 60}
              min={SESSION_COLLAB_BOUNDS.undeliveredTtlSeconds.min / 60}
              max={SESSION_COLLAB_BOUNDS.undeliveredTtlSeconds.max / 60}
              disabled={disabled}
              onCommit={(minutes) => update({ undeliveredTtlSeconds: minutes * 60 })}
            />
            <span className="text-sm text-muted-foreground">min</span>
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

      <h2 className="mt-3 text-ui font-medium">Keeping children warm</h2>
      <div className="rounded-xl border border-border bg-card p-4" data-testid="keep-warm-moved">
        <p className="text-sm text-muted-foreground">
          Keep-warm moved to{" "}
          <Link
            to="/settings/keep-warm"
            className="font-medium text-foreground underline underline-offset-2"
          >
            Settings &gt; Keep-warm
          </Link>
        </p>
      </div>

      <h2 className="mt-3 text-ui font-medium">Host colours</h2>
      <div
        className="rounded-xl border border-border bg-card p-4"
        data-testid="host-colors-settings"
      >
        <p className="mb-3 text-sm text-muted-foreground">
          The badge beside a sub-agent in the Agents rail takes its colour from the host that runs
          it. Hosts without a pick get a stable automatic colour derived from their name.
        </p>
        {hosts && hosts.length > 0 ? (
          <div className="flex flex-col gap-3">
            {hosts.map((host) => (
              <HostColorRow key={host.host_id} host={host} preferences={hostColorPreferences} />
            ))}
          </div>
        ) : (
          <p className="text-sm text-muted-foreground">No hosts are connected yet.</p>
        )}
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
