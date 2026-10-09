// Stable per-device identity and a short human label for sound alerts.
//
// The id persists in localStorage so reloads keep registering as the same
// device; the label derives from the shell and OS and is shown in settings.

import { isElectronShell, isIOSShell } from "@/lib/nativeBridge";
import type { SoundAlertDevicePreferences } from "@/lib/soundAlertPreferences";

const SOUND_DEVICE_ID_STORAGE_KEY = "omnigent:sound-alerts-device-id";

/** Stable random device id, minted and persisted on first use. */
export function getSoundDeviceId(): string {
  if (typeof window === "undefined") return "";
  try {
    const existing = window.localStorage.getItem(SOUND_DEVICE_ID_STORAGE_KEY);
    if (existing) return existing;
  } catch {
    // Storage denial: mint an id for this page below.
  }
  const id =
    typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
      ? crypto.randomUUID()
      : `dev-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  try {
    window.localStorage.setItem(SOUND_DEVICE_ID_STORAGE_KEY, id);
  } catch {
    // Storage denial or quota exhaustion must not break the settings page.
  }
  return id;
}

type OsFamily = "mac" | "windows" | null;

function detectOsFamily(): OsFamily {
  const platform = typeof navigator === "undefined" ? "" : navigator.platform;
  const userAgent = typeof navigator === "undefined" ? "" : navigator.userAgent;
  if (/Mac/i.test(platform) || /Macintosh|Mac OS X/i.test(userAgent)) return "mac";
  if (/Win/i.test(platform) || /Windows/i.test(userAgent)) return "windows";
  return null;
}

/** Short label like ``"Mac desktop app"``, ``"Windows browser"``, ``"Browser"``. */
export function soundDeviceLabel(): string {
  const os = detectOsFamily();
  const osName = os === "mac" ? "Mac" : os === "windows" ? "Windows" : null;
  if (isElectronShell()) return osName ? `${osName} desktop app` : "Desktop app";
  return osName ? `${osName} browser` : "Browser";
}

/** Whether this device may play an alert; the iOS shell never does. */
export function canRingOnThisDevice(device: SoundAlertDevicePreferences): boolean {
  if (isIOSShell()) return false;
  return device.enabled;
}
