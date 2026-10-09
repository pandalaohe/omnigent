import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, renderHook } from "@testing-library/react";

const {
  initAudioMock,
  playLevelMock,
  isNativeShellMock,
  useLoadedConversationsMock,
  useSessionErrorStatesMock,
  useSessionNavigationPreferencesMock,
  useUnseenTickMock,
  isConversationUnseenMock,
  isExplicitlyUnreadMock,
  socketStatusListeners,
  socketConnectedRef,
} = vi.hoisted(() => ({
  initAudioMock: vi.fn(),
  playLevelMock: vi.fn().mockResolvedValue(undefined),
  isNativeShellMock: vi.fn().mockReturnValue(false),
  useLoadedConversationsMock: vi.fn(),
  useSessionErrorStatesMock: vi.fn(),
  useSessionNavigationPreferencesMock: vi.fn(),
  useUnseenTickMock: vi.fn().mockReturnValue(0),
  isConversationUnseenMock: vi.fn().mockReturnValue(false),
  isExplicitlyUnreadMock: vi.fn().mockReturnValue(false),
  socketStatusListeners: new Set<() => void>(),
  socketConnectedRef: { current: false },
}));

vi.mock("@/lib/soundPlayer", () => ({
  initAudio: initAudioMock,
  playLevel: playLevelMock,
}));
vi.mock("@/lib/nativeBridge", () => ({ isNativeShell: isNativeShellMock }));
vi.mock("@/hooks/useSidebarData", () => ({
  useLoadedConversations: useLoadedConversationsMock,
}));
vi.mock("@/hooks/useSessionErrors", () => ({
  useSessionErrorStates: useSessionErrorStatesMock,
}));
vi.mock("@/hooks/useSessionNavigationPreferences", () => ({
  useSessionNavigationPreferences: useSessionNavigationPreferencesMock,
}));
vi.mock("@/hooks/useUnseenConversations", () => ({
  isConversationUnseen: isConversationUnseenMock,
  isExplicitlyUnread: isExplicitlyUnreadMock,
  useUnseenTick: useUnseenTickMock,
}));
vi.mock("@/lib/sessionUpdatesSocket", () => ({
  sessionUpdatesSocket: {
    isConnected: () => socketConnectedRef.current,
    subscribeStatus: (listener: () => void) => {
      socketStatusListeners.add(listener);
      return () => socketStatusListeners.delete(listener);
    },
  },
}));

import type { Conversation } from "@/hooks/useConversations";
import type { LatestSessionError } from "@/lib/sessionError";
import {
  SOUND_ALERTS_DEVICE_STORAGE_KEY,
  SOUND_ALERTS_STORAGE_KEY,
} from "@/lib/soundAlertPreferences";
import { useSoundAlerts } from "./useSoundAlerts";

function conv(id: string, partial: Partial<Conversation> = {}): Conversation {
  return {
    id,
    object: "conversation",
    title: id,
    created_at: 0,
    updated_at: 100,
    labels: {},
    permission_level: null,
    pending_elicitations_count: 0,
    ...partial,
  };
}

function setConversations(list: Conversation[]): void {
  useLoadedConversationsMock.mockReturnValue({
    data: { pages: [{ data: list, first_id: null, last_id: null, has_more: false }] },
    isLoading: false,
  });
}

const latestErrorById = new Map<string, LatestSessionError | null>();

describe("useSoundAlerts", () => {
  beforeEach(() => {
    vi.useFakeTimers({ now: new Date(2026, 0, 15, 12, 0, 0) });
    localStorage.clear();
    initAudioMock.mockClear();
    playLevelMock.mockClear();
    isNativeShellMock.mockReturnValue(false);
    isConversationUnseenMock.mockReturnValue(false);
    isExplicitlyUnreadMock.mockReturnValue(false);
    useUnseenTickMock.mockReturnValue(0);
    useSessionNavigationPreferencesMock.mockReturnValue({ showGoalSessionMarkers: true });
    latestErrorById.clear();
    useSessionErrorStatesMock.mockImplementation((rows: Conversation[]) =>
      rows.map((row) => latestErrorById.get(row.id) ?? null),
    );
    socketStatusListeners.clear();
    socketConnectedRef.current = false;
    setConversations([]);
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
  });

  it("initializes audio once on mount", () => {
    renderHook(() => useSoundAlerts());

    expect(initAudioMock).toHaveBeenCalledTimes(1);
    expect(initAudioMock).toHaveBeenCalledWith({ native: false });
  });

  it("plays needs_response once when a session starts awaiting", () => {
    setConversations([conv("conv_a", { pending_elicitations_count: 0 })]);
    const { rerender } = renderHook(() => useSoundAlerts());

    act(() => {
      setConversations([conv("conv_a", { pending_elicitations_count: 1, updated_at: 200 })]);
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
    setConversations([conv("conv_a", { pending_elicitations_count: 0 })]);
    const { rerender } = renderHook(() => useSoundAlerts());

    act(() => {
      setConversations([conv("conv_a", { pending_elicitations_count: 1, updated_at: 200 })]);
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
    setConversations([conv("conv_a", { pending_elicitations_count: 0 })]);
    const { rerender } = renderHook(() => useSoundAlerts());

    act(() => {
      setConversations([conv("conv_a", { pending_elicitations_count: 1, updated_at: 200 })]);
      rerender();
    });

    expect(playLevelMock).not.toHaveBeenCalled();
  });

  it("stays quiet on the first snapshot", () => {
    setConversations([conv("conv_a", { pending_elicitations_count: 1 })]);

    renderHook(() => useSoundAlerts());

    expect(playLevelMock).not.toHaveBeenCalled();
  });

  it("waits while the conversation list is still loading", () => {
    useLoadedConversationsMock.mockReturnValue({ data: undefined, isLoading: true });

    renderHook(() => useSoundAlerts());

    expect(playLevelMock).not.toHaveBeenCalled();
  });

  it("plays one done after the settle when a turn ends with nothing running", () => {
    setConversations([conv("conv_a")]);
    const { rerender } = renderHook(() => useSoundAlerts());

    act(() => {
      isConversationUnseenMock.mockReturnValue(true);
      setConversations([conv("conv_a", { updated_at: 200 })]);
      rerender();
    });
    expect(playLevelMock).not.toHaveBeenCalled();

    act(() => {
      vi.advanceTimersByTime(10_000);
    });

    expect(playLevelMock).toHaveBeenCalledTimes(1);
    expect(playLevelMock).toHaveBeenCalledWith("done", expect.anything(), expect.anything());
  });

  it("stays silent while background work covers the dot, then plays when it clears", () => {
    setConversations([conv("conv_a", { background_activity_count: 1 })]);
    const { rerender } = renderHook(() => useSoundAlerts());

    act(() => {
      isConversationUnseenMock.mockReturnValue(true);
      setConversations([conv("conv_a", { updated_at: 200, background_activity_count: 1 })]);
      rerender();
    });
    act(() => {
      vi.advanceTimersByTime(10_000);
    });
    expect(playLevelMock).not.toHaveBeenCalled();

    act(() => {
      setConversations([conv("conv_a", { updated_at: 300 })]);
      rerender();
    });
    act(() => {
      vi.advanceTimersByTime(10_000);
    });

    expect(playLevelMock).toHaveBeenCalledTimes(1);
    expect(playLevelMock).toHaveBeenCalledWith("done", expect.anything(), expect.anything());
  });

  it("cancels the settle when the session starts running again", () => {
    setConversations([conv("conv_a")]);
    const { rerender } = renderHook(() => useSoundAlerts());

    act(() => {
      isConversationUnseenMock.mockReturnValue(true);
      setConversations([conv("conv_a", { updated_at: 200 })]);
      rerender();
    });
    act(() => {
      setConversations([conv("conv_a", { updated_at: 300, foreground_status: "running" })]);
      rerender();
    });
    act(() => {
      vi.advanceTimersByTime(10_000);
    });

    expect(playLevelMock).not.toHaveBeenCalled();
  });

  it("plays needs_response only, never done, for an awaiting row", () => {
    setConversations([conv("conv_a", { pending_elicitations_count: 0 })]);
    const { rerender } = renderHook(() => useSoundAlerts());

    act(() => {
      isConversationUnseenMock.mockReturnValue(true);
      setConversations([conv("conv_a", { updated_at: 200, pending_elicitations_count: 1 })]);
      rerender();
    });
    act(() => {
      vi.advanceTimersByTime(10_000);
    });

    expect(playLevelMock).toHaveBeenCalledTimes(1);
    expect(playLevelMock).toHaveBeenCalledWith(
      "needs_response",
      expect.anything(),
      expect.anything(),
    );
  });

  it("plays error when the session status fails", () => {
    setConversations([conv("conv_a")]);
    const { rerender } = renderHook(() => useSoundAlerts());

    act(() => {
      setConversations([conv("conv_a", { updated_at: 200, status: "failed" })]);
      rerender();
    });

    expect(playLevelMock).toHaveBeenCalledTimes(1);
    expect(playLevelMock).toHaveBeenCalledWith("error", expect.anything(), expect.anything());
  });

  it("plays error when an idle row has a latest error", () => {
    setConversations([conv("conv_a")]);
    const { rerender } = renderHook(() => useSoundAlerts());

    act(() => {
      latestErrorById.set("conv_a", "error");
      setConversations([conv("conv_a", { updated_at: 200 })]);
      rerender();
    });

    expect(playLevelMock).toHaveBeenCalledTimes(1);
    expect(playLevelMock).toHaveBeenCalledWith("error", expect.anything(), expect.anything());
  });

  it("stays silent when already read while background work clears", () => {
    setConversations([conv("conv_a", { background_activity_count: 1 })]);
    const { rerender } = renderHook(() => useSoundAlerts());

    act(() => {
      setConversations([conv("conv_a", { updated_at: 200 })]);
      rerender();
    });
    act(() => {
      vi.advanceTimersByTime(10_000);
    });

    expect(playLevelMock).not.toHaveBeenCalled();
  });

  it("follows the dot when goal markers are off and the goal is active", () => {
    useSessionNavigationPreferencesMock.mockReturnValue({ showGoalSessionMarkers: false });
    setConversations([conv("conv_a", { goal_state: "active" })]);
    const { rerender } = renderHook(() => useSoundAlerts());

    act(() => {
      isConversationUnseenMock.mockReturnValue(true);
      setConversations([conv("conv_a", { updated_at: 200, goal_state: "active" })]);
      rerender();
    });
    act(() => {
      vi.advanceTimersByTime(10_000);
    });

    expect(playLevelMock).toHaveBeenCalledTimes(1);
    expect(playLevelMock).toHaveBeenCalledWith("done", expect.anything(), expect.anything());
  });

  it("rings a still-awaiting row once when the socket reconnects", () => {
    setConversations([conv("conv_a", { pending_elicitations_count: 1 })]);
    renderHook(() => useSoundAlerts());
    expect(playLevelMock).not.toHaveBeenCalled();

    act(() => {
      socketConnectedRef.current = true;
      for (const listener of socketStatusListeners) listener();
    });
    act(() => {
      vi.advanceTimersByTime(1_500);
    });

    expect(playLevelMock).toHaveBeenCalledTimes(1);
    expect(playLevelMock).toHaveBeenCalledWith(
      "needs_response",
      expect.anything(),
      expect.anything(),
    );

    act(() => {
      socketConnectedRef.current = false;
      for (const listener of socketStatusListeners) listener();
      socketConnectedRef.current = true;
      for (const listener of socketStatusListeners) listener();
    });
    act(() => {
      vi.advanceTimersByTime(1_500);
    });

    expect(playLevelMock).toHaveBeenCalledTimes(1);
  });
});
