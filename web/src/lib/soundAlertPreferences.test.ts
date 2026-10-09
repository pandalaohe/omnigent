import { afterEach, describe, expect, it, vi } from "vitest";

const { queuePatchMock } = vi.hoisted(() => ({ queuePatchMock: vi.fn() }));
vi.mock("./userPreferencesSync", () => ({ queueUserPreferencePatch: queuePatchMock }));

import {
  SOUND_ALERT_DEVICE_DEFAULTS,
  SOUND_ALERT_DEFAULTS,
  SOUND_ALERTS_CHANGED_EVENT,
  SOUND_ALERTS_DEVICE_STORAGE_KEY,
  SOUND_ALERTS_STORAGE_KEY,
  isSoundLevelEnabled,
  readSoundAlertDevicePreferences,
  readSoundAlertPreferences,
  writeSoundAlertDevicePreferences,
  writeSoundAlertPreferences,
  type SoundAlertLevelPreferences,
  type SoundAlertPreferences,
} from "./soundAlertPreferences";

afterEach(() => {
  localStorage.clear();
  queuePatchMock.mockReset();
});

const DEFAULTS_LITERAL: SoundAlertPreferences = {
  levels: {
    done: { enabled: true, sound: "chime" },
    error: { enabled: true, sound: "alert" },
    needs_response: { enabled: true, sound: "ping" },
  },
  quietHours: { enabled: true, start: "23:00", end: "08:00" },
  primaryDeviceId: null,
  mutedSessionIds: [],
};

const DONE_DISABLED: SoundAlertLevelPreferences = { enabled: false, sound: "chime" };
const DONE_DISABLED_PREFERENCES: SoundAlertPreferences = {
  ...DEFAULTS_LITERAL,
  levels: { ...DEFAULTS_LITERAL.levels, done: DONE_DISABLED },
};

describe("sound alert preferences", () => {
  it("defaults to every level enabled with per-level sounds", () => {
    expect(readSoundAlertPreferences()).toEqual(SOUND_ALERT_DEFAULTS);
    expect(readSoundAlertPreferences()).toEqual(DEFAULTS_LITERAL);
    expect(localStorage.getItem(SOUND_ALERTS_STORAGE_KEY)).toBeNull();
  });

  it("drops invalid fields per field", () => {
    localStorage.setItem(
      SOUND_ALERTS_STORAGE_KEY,
      JSON.stringify({
        levels: {
          done: { enabled: false, sound: "pop" },
          error: { enabled: "yes", sound: "siren" },
          needs_response: "nope",
        },
        quietHours: { enabled: false, start: "25:00", end: "08:30" },
        primaryDeviceId: 42,
        mutedSessionIds: ["conv_a", "", 7, null, "conv_a", "conv_b"],
      }),
    );

    expect(readSoundAlertPreferences()).toEqual({
      levels: {
        done: { enabled: false, sound: "pop" },
        error: { enabled: true, sound: "alert" },
        needs_response: { enabled: true, sound: "ping" },
      },
      quietHours: { enabled: false, start: "23:00", end: "08:30" },
      primaryDeviceId: null,
      mutedSessionIds: ["conv_a", "conv_b"],
    });
  });

  it("keeps the last 200 unique muted session ids", () => {
    const ids = Array.from({ length: 260 }, (_, index) => `conv_${index}`);
    localStorage.setItem(
      SOUND_ALERTS_STORAGE_KEY,
      JSON.stringify({ ...DEFAULTS_LITERAL, mutedSessionIds: ids }),
    );

    const stored = readSoundAlertPreferences().mutedSessionIds;
    expect(stored).toHaveLength(200);
    expect(stored[0]).toBe("conv_60");
    expect(stored.at(-1)).toBe("conv_259");
  });

  it("treats a non-object payload as defaults", () => {
    localStorage.setItem(SOUND_ALERTS_STORAGE_KEY, '"nope"');
    expect(readSoundAlertPreferences()).toEqual(SOUND_ALERT_DEFAULTS);
  });

  it("reports a disabled level through isSoundLevelEnabled", () => {
    expect(isSoundLevelEnabled(SOUND_ALERT_DEFAULTS, "done")).toBe(true);
    expect(isSoundLevelEnabled(DONE_DISABLED_PREFERENCES, "done")).toBe(false);
    expect(isSoundLevelEnabled(DONE_DISABLED_PREFERENCES, "error")).toBe(true);
  });

  it("clears storage and queues null at defaults", () => {
    writeSoundAlertPreferences(SOUND_ALERT_DEFAULTS);

    expect(localStorage.getItem(SOUND_ALERTS_STORAGE_KEY)).toBeNull();
    expect(queuePatchMock).toHaveBeenCalledWith("sound_alerts", null);
  });

  it("writes a non-default namespace and queues the normalized value", () => {
    writeSoundAlertPreferences(DONE_DISABLED_PREFERENCES);

    expect(JSON.parse(localStorage.getItem(SOUND_ALERTS_STORAGE_KEY) ?? "null")).toEqual(
      DONE_DISABLED_PREFERENCES,
    );
    expect(queuePatchMock).toHaveBeenLastCalledWith("sound_alerts", DONE_DISABLED_PREFERENCES);
  });

  it("dispatches the changed event on every write", () => {
    const listener = vi.fn();
    window.addEventListener(SOUND_ALERTS_CHANGED_EVENT, listener);

    writeSoundAlertPreferences(SOUND_ALERT_DEFAULTS);

    expect(listener).toHaveBeenCalledTimes(1);
    window.removeEventListener(SOUND_ALERTS_CHANGED_EVENT, listener);
  });
});

describe("sound alert device preferences", () => {
  it("defaults to on at 70 percent volume", () => {
    expect(readSoundAlertDevicePreferences()).toEqual(SOUND_ALERT_DEVICE_DEFAULTS);
    expect(readSoundAlertDevicePreferences()).toEqual({
      enabled: true,
      volume: 0.7,
      systemSounds: {},
    });
  });

  it("clamps the volume into 0..1 and rejects non-numbers", () => {
    writeSoundAlertDevicePreferences({ enabled: true, volume: 2, systemSounds: {} });
    expect(readSoundAlertDevicePreferences().volume).toBe(1);

    writeSoundAlertDevicePreferences({ enabled: true, volume: -1, systemSounds: {} });
    expect(readSoundAlertDevicePreferences().volume).toBe(0);

    localStorage.setItem(SOUND_ALERTS_DEVICE_STORAGE_KEY, JSON.stringify({ volume: "loud" }));
    expect(readSoundAlertDevicePreferences()).toEqual(SOUND_ALERT_DEVICE_DEFAULTS);
  });

  it("keeps only string system sounds for known levels", () => {
    localStorage.setItem(
      SOUND_ALERTS_DEVICE_STORAGE_KEY,
      JSON.stringify({
        enabled: false,
        volume: 0.25,
        systemSounds: { done: "Glass", error: 3, bogus: "Nope" },
      }),
    );

    expect(readSoundAlertDevicePreferences()).toEqual({
      enabled: false,
      volume: 0.25,
      systemSounds: { done: "Glass" },
    });
  });
});
