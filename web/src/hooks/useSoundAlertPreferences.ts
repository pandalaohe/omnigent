import { useEffect, useState } from "react";

import {
  SOUND_ALERTS_CHANGED_EVENT,
  readSoundAlertDevicePreferences,
  readSoundAlertPreferences,
  type SoundAlertDevicePreferences,
  type SoundAlertPreferences,
} from "@/lib/soundAlertPreferences";

export function useSoundAlertPreferences(): {
  account: SoundAlertPreferences;
  device: SoundAlertDevicePreferences;
} {
  const [account, setAccount] = useState(readSoundAlertPreferences);
  const [device, setDevice] = useState(readSoundAlertDevicePreferences);

  useEffect(() => {
    const refresh = () => {
      setAccount(readSoundAlertPreferences());
      setDevice(readSoundAlertDevicePreferences());
    };
    window.addEventListener(SOUND_ALERTS_CHANGED_EVENT, refresh);
    window.addEventListener("storage", refresh);
    return () => {
      window.removeEventListener(SOUND_ALERTS_CHANGED_EVENT, refresh);
      window.removeEventListener("storage", refresh);
    };
  }, []);

  return { account, device };
}
