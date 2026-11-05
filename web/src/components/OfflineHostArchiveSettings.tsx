import { useEffect, useRef, useState, useSyncExternalStore } from "react";

import { NumericField, SettingRow } from "@/components/SettingsFields";
import { useKeepWarmPreferences } from "@/hooks/useKeepWarmPreferences";
import { getOmnigentServerIdentity } from "@/lib/host";
import { getCurrentUserId, subscribeIdentity } from "@/lib/identity";
import {
  KEEP_WARM_HOST_OFFLINE_ARCHIVE_BOUNDS_SECONDS,
  readKeepWarmPreferences,
  saveKeepWarmPreferences,
} from "@/lib/keepWarmPreferences";

function preferenceScope() {
  return JSON.stringify([getOmnigentServerIdentity(), getCurrentUserId()]);
}

export function OfflineHostArchiveSettings() {
  const scope = useSyncExternalStore(subscribeIdentity, preferenceScope, () => "");
  return <ScopedOfflineHostArchiveSettings key={scope} />;
}

function ScopedOfflineHostArchiveSettings() {
  const preferences = useKeepWarmPreferences();
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const pending = useRef<AbortController | null>(null);
  useEffect(() => () => pending.current?.abort(), []);

  const save = async (hours: number) => {
    if (pending.current) return;
    const controller = new AbortController();
    pending.current = controller;
    setBusy(true);
    setSaved(false);
    setError(null);
    try {
      await saveKeepWarmPreferences(
        { ...readKeepWarmPreferences(), hostOfflineArchiveSeconds: hours * 3600 },
        [],
        controller.signal,
      );
      if (!controller.signal.aborted) setSaved(true);
    } catch (caught) {
      if (!controller.signal.aborted)
        setError(caught instanceof Error ? caught.message : "Couldn't save the archive delay.");
    } finally {
      pending.current = null;
      if (!controller.signal.aborted) setBusy(false);
    }
  };

  return (
    <div className="space-y-2">
      <SettingRow
        label="Archive children of an offline host"
        hint="Children whose host stays offline this long are archived automatically."
        note="0 = off"
      >
        <NumericField
          ariaLabel="Archive children of an offline host after in hours"
          value={preferences.hostOfflineArchiveSeconds / 3600}
          min={0}
          max={KEEP_WARM_HOST_OFFLINE_ARCHIVE_BOUNDS_SECONDS.max / 3600}
          disabled={busy}
          onCommit={(hours) => void save(hours)}
        />
        <span className="text-sm text-muted-foreground">h</span>
      </SettingRow>
      {(busy || saved) && (
        <p role="status" className="text-sm text-muted-foreground">
          {busy ? "Saving…" : "Saved."}
        </p>
      )}
      {error && (
        <p role="alert" className="text-sm text-destructive">
          Save wasn&apos;t confirmed: {error}
        </p>
      )}
    </div>
  );
}
