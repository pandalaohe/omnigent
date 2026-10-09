import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { useSoundAlertPreferences } from "@/hooks/useSoundAlertPreferences";
import { isNativeShell } from "@/lib/nativeBridge";
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
import { isAudioLocked, playBuiltinSound, subscribeAudioLock } from "@/lib/soundPlayer";

const LEVEL_LABELS: Record<SoundLevel, string> = {
  done: "Done (unread dot)",
  error: "Error",
  needs_response: "Needs response",
};

function useAudioLocked(): boolean {
  const [locked, setLocked] = useState(isAudioLocked);
  useEffect(() => subscribeAudioLock(() => setLocked(isAudioLocked())), []);
  return locked;
}

export function SoundAlertSettings() {
  const { account, device } = useSoundAlertPreferences();
  const audioLocked = useAudioLocked();

  const updateLevel = (level: SoundLevel, patch: Partial<SoundAlertLevelPreferences>) =>
    writeSoundAlertPreferences({
      ...account,
      levels: { ...account.levels, [level]: { ...account.levels[level], ...patch } },
    });
  const updateQuietHours = (patch: Partial<SoundAlertQuietHours>) =>
    writeSoundAlertPreferences({ ...account, quietHours: { ...account.quietHours, ...patch } });
  const updateDevice = (patch: Partial<SoundAlertDevicePreferences>) =>
    writeSoundAlertDevicePreferences({ ...device, ...patch });

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
            <Select
              value={account.levels[level].sound}
              onValueChange={(sound) => updateLevel(level, { sound: sound as BuiltinSoundId })}
            >
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
              </SelectContent>
            </Select>
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => void playBuiltinSound(account.levels[level].sound, device.volume)}
            >
              Play
            </Button>
          </div>
        </div>
      ))}

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

      {audioLocked && !isNativeShell() && (
        <p className="text-sm text-muted-foreground">Click anywhere in the app to enable sounds.</p>
      )}
    </div>
  );
}
