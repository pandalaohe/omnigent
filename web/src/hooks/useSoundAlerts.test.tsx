import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, renderHook } from "@testing-library/react";

const { initAudioMock, playLevelMock, isNativeShellMock, useLoadedConversationsMock } = vi.hoisted(
  () => ({
    initAudioMock: vi.fn(),
    playLevelMock: vi.fn().mockResolvedValue(undefined),
    isNativeShellMock: vi.fn().mockReturnValue(false),
    useLoadedConversationsMock: vi.fn(),
  }),
);

vi.mock("@/lib/soundPlayer", () => ({
  initAudio: initAudioMock,
  playLevel: playLevelMock,
}));
vi.mock("@/lib/nativeBridge", () => ({ isNativeShell: isNativeShellMock }));
vi.mock("@/hooks/useSidebarData", () => ({
  useLoadedConversations: useLoadedConversationsMock,
}));

import type { Conversation } from "@/hooks/useConversations";
import {
  SOUND_ALERTS_DEVICE_STORAGE_KEY,
  SOUND_ALERTS_STORAGE_KEY,
} from "@/lib/soundAlertPreferences";
import { useSoundAlerts } from "./useSoundAlerts";

function conv(id: string, pendingElicitations = 0): Conversation {
  return {
    id,
    object: "conversation",
    title: id,
    created_at: 0,
    updated_at: 0,
    labels: {},
    permission_level: null,
    pending_elicitations_count: pendingElicitations,
  };
}

function setConversations(list: Conversation[]): void {
  useLoadedConversationsMock.mockReturnValue({
    data: { pages: [{ data: list }] },
    isLoading: false,
  });
}

describe("useSoundAlerts", () => {
  beforeEach(() => {
    localStorage.clear();
    initAudioMock.mockClear();
    playLevelMock.mockClear();
    isNativeShellMock.mockReturnValue(false);
    setConversations([]);
  });

  afterEach(() => cleanup());

  it("initializes audio once on mount", () => {
    renderHook(() => useSoundAlerts());

    expect(initAudioMock).toHaveBeenCalledTimes(1);
    expect(initAudioMock).toHaveBeenCalledWith({ native: false });
  });

  it("plays needs_response once when a session starts awaiting", () => {
    setConversations([conv("conv_a", 0)]);
    const { rerender } = renderHook(() => useSoundAlerts());

    act(() => {
      setConversations([conv("conv_a", 1)]);
      rerender();
    });

    expect(playLevelMock).toHaveBeenCalledTimes(1);
    expect(playLevelMock).toHaveBeenCalledWith(
      "needs_response",
      expect.objectContaining({
        levels: expect.objectContaining({
          needs_response: { enabled: true, sound: "ping" },
        }),
      }),
      expect.objectContaining({ enabled: true, volume: 0.7 }),
    );
  });

  it("does not play when the device master switch is off", () => {
    localStorage.setItem(
      SOUND_ALERTS_DEVICE_STORAGE_KEY,
      JSON.stringify({ enabled: false, volume: 0.7, systemSounds: {} }),
    );
    setConversations([conv("conv_a", 0)]);
    const { rerender } = renderHook(() => useSoundAlerts());

    act(() => {
      setConversations([conv("conv_a", 1)]);
      rerender();
    });

    expect(playLevelMock).not.toHaveBeenCalled();
  });

  it("does not play when the level is disabled", () => {
    localStorage.setItem(
      SOUND_ALERTS_STORAGE_KEY,
      JSON.stringify({
        levels: { needs_response: { enabled: false, sound: "ping" } },
      }),
    );
    setConversations([conv("conv_a", 0)]);
    const { rerender } = renderHook(() => useSoundAlerts());

    act(() => {
      setConversations([conv("conv_a", 1)]);
      rerender();
    });

    expect(playLevelMock).not.toHaveBeenCalled();
  });

  it("stays quiet on the first snapshot", () => {
    setConversations([conv("conv_a", 1)]);

    renderHook(() => useSoundAlerts());

    expect(playLevelMock).not.toHaveBeenCalled();
  });

  it("waits while the conversation list is still loading", () => {
    useLoadedConversationsMock.mockReturnValue({ data: undefined, isLoading: true });

    renderHook(() => useSoundAlerts());

    expect(playLevelMock).not.toHaveBeenCalled();
  });
});
