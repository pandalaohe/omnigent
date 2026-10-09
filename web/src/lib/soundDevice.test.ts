import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { isElectronShellMock, isIOSShellMock } = vi.hoisted(() => ({
  isElectronShellMock: vi.fn().mockReturnValue(false),
  isIOSShellMock: vi.fn().mockReturnValue(false),
}));

vi.mock("@/lib/nativeBridge", () => ({
  isElectronShell: isElectronShellMock,
  isIOSShell: isIOSShellMock,
}));

import { canRingOnThisDevice, getSoundDeviceId, soundDeviceLabel } from "./soundDevice";

const DEVICE_SOUNDS = { enabled: true, volume: 0.7, systemSounds: {} };

function setOs(platform: string, userAgent: string): void {
  vi.spyOn(window.navigator, "platform", "get").mockReturnValue(platform);
  vi.spyOn(window.navigator, "userAgent", "get").mockReturnValue(userAgent);
}

describe("soundDevice", () => {
  beforeEach(() => {
    localStorage.clear();
    isElectronShellMock.mockReturnValue(false);
    isIOSShellMock.mockReturnValue(false);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("mints one id and keeps it across calls", () => {
    const first = getSoundDeviceId();

    expect(first).not.toBe("");
    expect(getSoundDeviceId()).toBe(first);
    // Pinned literal: the id must persist under this exact localStorage key.
    expect(localStorage.getItem("omnigent:sound-alerts-device-id")).toBe(first);
  });

  it("labels the macOS desktop app", () => {
    setOs("MacIntel", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)");
    isElectronShellMock.mockReturnValue(true);

    expect(soundDeviceLabel()).toBe("Mac desktop app");
  });

  it("labels the Windows desktop app", () => {
    setOs("Win32", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)");
    isElectronShellMock.mockReturnValue(true);

    expect(soundDeviceLabel()).toBe("Windows desktop app");
  });

  it("labels a Mac browser", () => {
    setOs("MacIntel", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)");

    expect(soundDeviceLabel()).toBe("Mac browser");
  });

  it("labels a Windows browser", () => {
    setOs("Win32", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)");

    expect(soundDeviceLabel()).toBe("Windows browser");
  });

  it("falls back to a generic browser label on other platforms", () => {
    setOs("Linux x86_64", "Mozilla/5.0 (X11; Linux x86_64)");

    expect(soundDeviceLabel()).toBe("Browser");
  });

  it("never rings in the iOS shell", () => {
    isIOSShellMock.mockReturnValue(true);

    expect(canRingOnThisDevice(DEVICE_SOUNDS)).toBe(false);
  });

  it("follows the device master switch elsewhere", () => {
    expect(canRingOnThisDevice(DEVICE_SOUNDS)).toBe(true);
    expect(canRingOnThisDevice({ ...DEVICE_SOUNDS, enabled: false })).toBe(false);
  });
});
