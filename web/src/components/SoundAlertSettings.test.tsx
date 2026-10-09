import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

const { playLevelMock, nativeShellState, listSystemSoundsMock } = vi.hoisted(() => ({
  playLevelMock: vi.fn(),
  nativeShellState: { value: false },
  listSystemSoundsMock: vi.fn().mockResolvedValue([] as string[]),
}));

vi.mock("@/lib/soundPlayer", () => ({
  isAudioLocked: () => false,
  subscribeAudioLock: () => () => {},
  playLevel: playLevelMock,
}));

vi.mock("@/lib/nativeBridge", () => ({
  isElectronShell: () => false,
  isIOSShell: () => false,
  isNativeShell: () => nativeShellState.value,
  listNativeSystemSounds: listSystemSoundsMock,
}));

// Radix Select uses a portal + pointer events jsdom can't drive; a native
// <select> lets the tests drive the sound choice directly.
vi.mock("@/components/ui/select", async () => {
  const { Children, isValidElement } = await import("react");
  const SelectTrigger = ({ children }: { children?: ReactNode }) => children;
  const Select = ({
    value,
    onValueChange,
    children,
  }: {
    value: string;
    onValueChange: (value: string) => void;
    children: ReactNode;
  }) => {
    const kids = Children.toArray(children);
    const trigger = kids.find((child) => isValidElement(child) && child.type === SelectTrigger);
    const testId =
      isValidElement(trigger) && trigger.props && typeof trigger.props === "object"
        ? (trigger.props as Record<string, unknown>)["data-testid"]
        : undefined;
    return (
      <select
        data-testid={typeof testId === "string" ? testId : undefined}
        value={value}
        onChange={(event) => onValueChange(event.target.value)}
      >
        {kids.filter((child) => !(isValidElement(child) && child.type === SelectTrigger))}
      </select>
    );
  };
  return {
    Select,
    SelectTrigger,
    SelectValue: () => null,
    SelectContent: ({ children }: { children: ReactNode }) => children,
    SelectGroup: ({ children }: { children: ReactNode }) => children,
    SelectLabel: () => null,
    SelectItem: ({ value, children }: { value: string; children: ReactNode }) => (
      <option value={value}>{children}</option>
    ),
  };
});

import { SoundAlertSettings } from "./SoundAlertSettings";
import {
  SOUND_ALERTS_DEVICE_STORAGE_KEY,
  SOUND_ALERTS_STORAGE_KEY,
} from "@/lib/soundAlertPreferences";

interface StoredPreferences {
  levels: Record<string, { enabled: boolean; sound: string }>;
  mutedSessionIds?: string[];
  primaryDeviceId?: string | null;
}

function stored(): StoredPreferences | null {
  return JSON.parse(
    localStorage.getItem(SOUND_ALERTS_STORAGE_KEY) ?? "null",
  ) as StoredPreferences | null;
}

function deviceStored(): { systemSounds?: Record<string, string> } | null {
  return JSON.parse(localStorage.getItem(SOUND_ALERTS_DEVICE_STORAGE_KEY) ?? "null") as {
    systemSounds?: Record<string, string>;
  } | null;
}

beforeEach(() => {
  localStorage.clear();
  playLevelMock.mockClear();
  nativeShellState.value = false;
  listSystemSoundsMock.mockReset();
  listSystemSoundsMock.mockResolvedValue([]);
});

afterEach(() => cleanup());

describe("SoundAlertSettings", () => {
  it("writes a level switch to the shared namespace", () => {
    render(<SoundAlertSettings />);
    const toggle = screen.getByTestId("sound-alert-level-done");
    expect(toggle).toHaveAttribute("aria-checked", "true");

    fireEvent.click(toggle);

    expect(toggle).toHaveAttribute("aria-checked", "false");
    expect(stored()?.levels.done.enabled).toBe(false);
    expect(stored()?.levels.error.enabled).toBe(true);
  });

  it("writes a sound choice from the select", () => {
    render(<SoundAlertSettings />);

    fireEvent.change(screen.getByTestId("sound-alert-sound-done"), { target: { value: "pop" } });

    expect(stored()?.levels.done.sound).toBe("pop");
  });

  it("resumes audio for the preview", () => {
    render(<SoundAlertSettings />);

    fireEvent.click(screen.getAllByRole("button", { name: "Play" })[0]);

    expect(playLevelMock).toHaveBeenCalledWith("done", expect.anything(), expect.anything(), {
      resume: true,
    });
  });

  it("summarizes muted sessions and can unmute all", () => {
    localStorage.setItem(
      SOUND_ALERTS_STORAGE_KEY,
      JSON.stringify({ mutedSessionIds: ["conv_a", "conv_b"] }),
    );
    render(<SoundAlertSettings />);

    expect(screen.getByTestId("sound-alert-muted-sessions")).toHaveTextContent("2 muted sessions");

    fireEvent.click(screen.getByTestId("sound-alert-unmute-all"));

    expect(stored()?.mutedSessionIds ?? []).toEqual([]);
    expect(screen.queryByTestId("sound-alert-muted-sessions")).not.toBeInTheDocument();
  });

  it("makes this device the primary device", () => {
    render(<SoundAlertSettings />);

    fireEvent.click(screen.getByTestId("sound-alert-make-primary"));

    // The written account preference is this device's persisted id, and the
    // row flips to the primary-device summary.
    const deviceId = localStorage.getItem("omnigent:sound-alerts-device-id");
    expect(deviceId).not.toBeNull();
    expect(stored()?.primaryDeviceId).toBe(deviceId);
    expect(screen.getByTestId("sound-alert-primary-device")).toHaveTextContent(
      /^This device \(.+\) is the primary device$/,
    );
  });

  it("lists system sounds in native mode and shows a device override", async () => {
    nativeShellState.value = true;
    listSystemSoundsMock.mockResolvedValue(["Glass", "Ping"]);
    localStorage.setItem(
      SOUND_ALERTS_DEVICE_STORAGE_KEY,
      JSON.stringify({ systemSounds: { done: "Glass" } }),
    );
    render(<SoundAlertSettings />);

    expect(await screen.findAllByRole("option", { name: "Glass" })).not.toHaveLength(0);
    expect(screen.getByTestId("sound-alert-sound-done")).toHaveValue("system:Glass");
    expect(screen.getByTestId("sound-alert-system-sounds-hint")).toHaveTextContent(
      "System sounds apply to this device only.",
    );
  });

  it("writes a device override when a system sound is chosen", async () => {
    nativeShellState.value = true;
    listSystemSoundsMock.mockResolvedValue(["Glass"]);
    render(<SoundAlertSettings />);
    await screen.findAllByRole("option", { name: "Glass" });

    fireEvent.change(screen.getByTestId("sound-alert-sound-done"), {
      target: { value: "system:Glass" },
    });

    expect(deviceStored()?.systemSounds).toEqual({ done: "Glass" });
  });

  it("clears the device override when a built-in sound is chosen", async () => {
    nativeShellState.value = true;
    listSystemSoundsMock.mockResolvedValue(["Glass"]);
    localStorage.setItem(
      SOUND_ALERTS_DEVICE_STORAGE_KEY,
      JSON.stringify({ systemSounds: { done: "Glass" } }),
    );
    render(<SoundAlertSettings />);
    await screen.findAllByRole("option", { name: "Glass" });

    fireEvent.change(screen.getByTestId("sound-alert-sound-done"), { target: { value: "pop" } });

    expect(stored()?.levels.done.sound).toBe("pop");
    expect(deviceStored()?.systemSounds?.done).toBeUndefined();
  });
});
