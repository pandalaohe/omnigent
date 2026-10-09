import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { useSoundAlertPreferences } from "@/hooks/useSoundAlertPreferences";
import { isNativeShell, listNativeSystemSounds } from "@/lib/nativeBridge";
import { getSoundDeviceId, soundDeviceLabel } from "@/lib/soundDevice";
import {
  BUILTIN_SOUNDS,
  SOUND_LEVELS,
  isSoundLevelEnabled,
  writeSoundAlertDevicePreferences,
  writeSoundAlertPreferences,
  type BuiltinSoundId,
  type SoundAlertDevicePreferences,
  type SoundAlertLevelPreferences,
  type SoundAlertQuietHours,
  type SoundLevel,
} from "@/lib/soundAlertPreferences";
import { isAudioLocked, playLevel, subscribeAudioLock } from "@/lib/soundPlayer";

const LEVEL_LABELS: Record<SoundLevel, string> = {
  done: "Done (unread dot)",
  error: "Error",
  needs_response: "Needs response",
};

// Distinguishes a device-local system-sound override from a built-in id in the
// level Select's value space.
const SYSTEM_SOUND_PREFIX = "system:";

function useAudioLocked(): boolean {
  const [locked, setLocked] = useState(isAudioLocked);
  useEffect(() => subscribeAudioLock(() => setLocked(isAudioLocked())), []);
  return locked;
}

/**
 * One-line unlock hint for outside Settings (the sidebar's list footer).
 * Hidden once audio unlocks, in a native shell, or when nothing would play.
 */
export function SoundAlertsLockedHint() {
  const audioLocked = useAudioLocked();
  const { account, device } = useSoundAlertPreferences();
  const anyLevelEnabled = SOUND_LEVELS.some((level) => isSoundLevelEnabled(account, level));
  if (!audioLocked || isNativeShell() || !device.enabled || !anyLevelEnabled) return null;
  return (
    <p className="px-2 py-1 text-ui text-muted-foreground" data-testid="sound-alerts-locked-hint">
      Click anywhere in the app to enable sounds.
    </p>
  );
}

export function SoundAlertSettings() {
  const { account, device } = useSoundAlertPreferences();
  const audioLocked = useAudioLocked();
  const deviceId = getSoundDeviceId();
  const deviceLabel = soundDeviceLabel();
  const isPrimaryDevice = account.primaryDeviceId === deviceId;
  const [systemSounds, setSystemSounds] = useState<string[]>([]);

  // System sounds exist only inside a native shell; the list arrives async.
  useEffect(() => {
    if (!isNativeShell()) return;
    let cancelled = false;
    void listNativeSystemSounds().then((names) => {
      if (!cancelled) setSystemSounds(names);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  const updateLevel = (level: SoundLevel, patch: Partial<SoundAlertLevelPreferences>) =>
    writeSoundAlertPreferences({
      ...account,
      levels: { ...account.levels, [level]: { ...account.levels[level], ...patch } },
    });
  const updateQuietHours = (patch: Partial<SoundAlertQuietHours>) =>
    writeSoundAlertPreferences({ ...account, quietHours: { ...account.quietHours, ...patch } });
  const updateDevice = (patch: Partial<SoundAlertDevicePreferences>) =>
    writeSoundAlertDevicePreferences({ ...device, ...patch });

  const soundValue = (level: SoundLevel): string => {
    const override = device.systemSounds[level];
    return override ? `${SYSTEM_SOUND_PREFIX}${override}` : account.levels[level].sound;
  };

  const chooseSound = (level: SoundLevel, value: string) => {
    if (value.startsWith(SYSTEM_SOUND_PREFIX)) {
      const name = value.slice(SYSTEM_SOUND_PREFIX.length);
      if (!name) return;
      updateDevice({ systemSounds: { ...device.systemSounds, [level]: name } });
      return;
    }
    // A built-in choice clears this level's device override.
    const nextSystemSounds: Partial<Record<SoundLevel, string>> = {};
    for (const other of SOUND_LEVELS) {
      const override = device.systemSounds[other];
      if (other !== level && override) nextSystemSounds[other] = override;
    }
    updateDevice({ systemSounds: nextSystemSounds });
    updateLevel(level, { sound: value as BuiltinSoundId });
  };

  return (
    <div className="flex flex-col gap-3" data-testid="sound-alert-settings">
      <div className="flex items-center justify-between gap-6">
        <span className="text-sm font-medium text-foreground">Play sounds on this device</span>
        <Switch
          aria-label="Play sounds on this device"
          checked={device.enabled}
          onCheckedChange={(enabled) => updateDevice({ enabled })}
        />
      </div>

      <div className="flex items-center justify-between gap-6">
        <span className="text-sm text-muted-foreground">Volume</span>
        <div className="flex shrink-0 items-center gap-2">
          <input
            type="range"
            min={0}
            max={100}
            step={1}
            aria-label="Sound volume"
            value={Math.round(device.volume * 100)}
            onChange={(event) => updateDevice({ volume: Number(event.target.value) / 100 })}
            className="h-2 w-40 cursor-pointer accent-primary"
          />
          <span className="w-10 text-right text-sm tabular-nums text-muted-foreground">
            {Math.round(device.volume * 100)}%
          </span>
        </div>
      </div>

      <div className="flex flex-wrap items-center justify-between gap-3 border-t border-border pt-3">
        <div className="flex flex-col gap-0.5">
          <span className="text-sm text-foreground">Primary device</span>
          <span className="text-sm text-muted-foreground">
            Plays when none of your devices was used in the last 5 minutes.
          </span>
        </div>
        {isPrimaryDevice ? (
          <span className="text-sm text-muted-foreground" data-testid="sound-alert-primary-device">
            This device ({deviceLabel}) is the primary device
          </span>
        ) : (
          <Button
            type="button"
            variant="outline"
            size="sm"
            data-testid="sound-alert-make-primary"
            onClick={() => writeSoundAlertPreferences({ ...account, primaryDeviceId: deviceId })}
          >
            Make this the primary device
          </Button>
        )}
      </div>

      {SOUND_LEVELS.map((level) => (
        <div
          key={level}
          className="flex flex-wrap items-center justify-between gap-3 border-t border-border pt-3"
        >
          <label className="flex items-center gap-2 text-sm text-foreground">
            <Switch
              aria-label={`Play sound for ${LEVEL_LABELS[level]}`}
              data-testid={`sound-alert-level-${level}`}
              checked={isSoundLevelEnabled(account, level)}
              onCheckedChange={(enabled) => updateLevel(level, { enabled })}
            />
            {LEVEL_LABELS[level]}
          </label>
          <div className="flex items-center gap-2">
            <Select value={soundValue(level)} onValueChange={(value) => chooseSound(level, value)}>
              <SelectTrigger
                aria-label={`${LEVEL_LABELS[level]} sound`}
                data-testid={`sound-alert-sound-${level}`}
                className="w-32"
              >
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {BUILTIN_SOUNDS.map((sound) => (
                  <SelectItem key={sound.id} value={sound.id}>
                    {sound.label}
                  </SelectItem>
                ))}
                {systemSounds.length > 0 && (
                  <SelectGroup>
                    <SelectLabel>System sounds</SelectLabel>
                    {systemSounds.map((name) => (
                      <SelectItem key={`system:${name}`} value={`${SYSTEM_SOUND_PREFIX}${name}`}>
                        {name}
                      </SelectItem>
                    ))}
                  </SelectGroup>
                )}
              </SelectContent>
            </Select>
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() =>
                // The click is a user gesture: unlock before playing so the
                // first preview in a fresh tab is audible.
                void playLevel(level, account, device, { resume: true })
              }
            >
              Play
            </Button>
          </div>
        </div>
      ))}

      {systemSounds.length > 0 && (
        <p className="text-sm text-muted-foreground" data-testid="sound-alert-system-sounds-hint">
          System sounds apply to this device only.
        </p>
      )}

      <div className="flex flex-wrap items-center justify-between gap-3 border-t border-border pt-3">
        <label className="flex items-center gap-2 text-sm text-foreground">
          <Switch
            aria-label="Quiet hours"
            checked={account.quietHours.enabled}
            onCheckedChange={(enabled) => updateQuietHours({ enabled })}
          />
          Quiet hours
        </label>
        <div className="flex items-center gap-2">
          <Input
            type="time"
            aria-label="Quiet hours start"
            className="w-28"
            value={account.quietHours.start}
            onChange={(event) =>
              updateQuietHours({ start: event.target.value || account.quietHours.start })
            }
          />
          <span className="text-sm text-muted-foreground">to</span>
          <Input
            type="time"
            aria-label="Quiet hours end"
            className="w-28"
            value={account.quietHours.end}
            onChange={(event) =>
              updateQuietHours({ end: event.target.value || account.quietHours.end })
            }
          />
        </div>
      </div>

      {account.mutedSessionIds.length > 0 && (
        <div
          className="flex flex-wrap items-center justify-between gap-3 border-t border-border pt-3"
          data-testid="sound-alert-muted-sessions"
        >
          <span className="text-sm text-muted-foreground">
            {account.mutedSessionIds.length} muted{" "}
            {account.mutedSessionIds.length === 1 ? "session" : "sessions"}
          </span>
          <Button
            type="button"
            variant="outline"
            size="sm"
            data-testid="sound-alert-unmute-all"
            onClick={() => writeSoundAlertPreferences({ ...account, mutedSessionIds: [] })}
          >
            Unmute all
          </Button>
        </div>
      )}

      {audioLocked && !isNativeShell() && (
        <p className="text-sm text-muted-foreground">Click anywhere in the app to enable sounds.</p>
      )}
    </div>
  );
}
