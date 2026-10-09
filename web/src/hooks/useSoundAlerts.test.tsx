import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, renderHook } from "@testing-library/react";

const {
  initAudioMock,
  playLevelMock,
  isAudioLockedMock,
  subscribeAudioLockMock,
  isNativeShellMock,
  setNativeSoundAlertsActiveMock,
  getLegacyNativeNotificationSoundMock,
  useLoadedConversationsMock,
  useSessionErrorStatesMock,
  useSessionNavigationPreferencesMock,
  useUnseenTickMock,
  isConversationUnseenMock,
  isExplicitlyUnreadMock,
  socketStatusListeners,
  socketFrameListeners,
  socketConnectedRef,
  socketHelloMock,
  socketActivityMock,
  claimFetchMock,
  getSoundDeviceIdMock,
  soundDeviceLabelMock,
  canRingOnThisDeviceMock,
  audioLock,
} = vi.hoisted(() => {
  const lockState = { locked: false, listeners: new Set<() => void>() };
  return {
    initAudioMock: vi.fn(),
    playLevelMock: vi.fn().mockResolvedValue(undefined),
    isAudioLockedMock: vi.fn(() => lockState.locked),
    subscribeAudioLockMock: vi.fn((listener: () => void) => {
      lockState.listeners.add(listener);
      return () => lockState.listeners.delete(listener);
    }),
    isNativeShellMock: vi.fn().mockReturnValue(false),
    setNativeSoundAlertsActiveMock: vi.fn(),
    getLegacyNativeNotificationSoundMock: vi.fn().mockResolvedValue(null),
    useLoadedConversationsMock: vi.fn(),
    useSessionErrorStatesMock: vi.fn(),
    useSessionNavigationPreferencesMock: vi.fn(),
    useUnseenTickMock: vi.fn().mockReturnValue(0),
    isConversationUnseenMock: vi.fn().mockReturnValue(false),
    isExplicitlyUnreadMock: vi.fn().mockReturnValue(false),
    socketStatusListeners: new Set<() => void>(),
    socketFrameListeners: new Set<(frame: { type: string; [key: string]: unknown }) => void>(),
    socketConnectedRef: { current: false },
    socketHelloMock: vi.fn(),
    socketActivityMock: vi.fn(),
    claimFetchMock: vi.fn(),
    getSoundDeviceIdMock: vi.fn().mockReturnValue("dev_test"),
    soundDeviceLabelMock: vi.fn().mockReturnValue("Test device"),
    canRingOnThisDeviceMock: vi.fn().mockReturnValue(true),
    audioLock: lockState,
  };
});

vi.mock("@/lib/soundPlayer", () => ({
  initAudio: initAudioMock,
  playLevel: playLevelMock,
  isAudioLocked: isAudioLockedMock,
  subscribeAudioLock: subscribeAudioLockMock,
}));
vi.mock("@/lib/nativeBridge", () => ({
  isNativeShell: isNativeShellMock,
  setNativeSoundAlertsActive: setNativeSoundAlertsActiveMock,
  getLegacyNativeNotificationSound: getLegacyNativeNotificationSoundMock,
}));
vi.mock("@/lib/identity", () => ({ authenticatedFetch: claimFetchMock }));
vi.mock("@/lib/soundDevice", () => ({
  getSoundDeviceId: getSoundDeviceIdMock,
  soundDeviceLabel: soundDeviceLabelMock,
  canRingOnThisDevice: canRingOnThisDeviceMock,
}));
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
    subscribe: (listener: (frame: { type: string; [key: string]: unknown }) => void) => {
      socketFrameListeners.add(listener);
      return () => socketFrameListeners.delete(listener);
    },
    setHello: socketHelloMock,
    sendActivity: socketActivityMock,
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

/** Claim request bodies, in call order. */
function claimBodies(): Record<string, unknown>[] {
  return claimFetchMock.mock.calls.map(([, init]) => {
    const body = (init as RequestInit | undefined)?.body;
    return JSON.parse(typeof body === "string" ? body : "{}") as Record<string, unknown>;
  });
}

function emitFrame(frame: { type: string; [key: string]: unknown }): void {
  act(() => {
    for (const listener of socketFrameListeners) listener(frame);
  });
}

/** The persisted device preferences, or null. */
function deviceStored(): Record<string, unknown> | null {
  return JSON.parse(localStorage.getItem(SOUND_ALERTS_DEVICE_STORAGE_KEY) ?? "null") as Record<
    string,
    unknown
  > | null;
}

const latestErrorById = new Map<string, LatestSessionError | null>();

describe("useSoundAlerts", () => {
  beforeEach(() => {
    vi.useFakeTimers({ now: new Date(2026, 0, 15, 12, 0, 0) });
    localStorage.clear();
    initAudioMock.mockClear();
    playLevelMock.mockClear();
    isAudioLockedMock.mockClear();
    subscribeAudioLockMock.mockClear();
    audioLock.locked = false;
    audioLock.listeners.clear();
    isNativeShellMock.mockReturnValue(false);
    setNativeSoundAlertsActiveMock.mockClear();
    getLegacyNativeNotificationSoundMock.mockReset();
    getLegacyNativeNotificationSoundMock.mockResolvedValue(null);
    isConversationUnseenMock.mockReturnValue(false);
    isExplicitlyUnreadMock.mockReturnValue(false);
    useUnseenTickMock.mockReturnValue(0);
    useSessionNavigationPreferencesMock.mockReturnValue({ showGoalSessionMarkers: true });
    latestErrorById.clear();
    useSessionErrorStatesMock.mockImplementation((rows: Conversation[]) =>
      rows.map((row) => latestErrorById.get(row.id) ?? null),
    );
    socketStatusListeners.clear();
    socketFrameListeners.clear();
    socketConnectedRef.current = false;
    socketHelloMock.mockClear();
    socketActivityMock.mockClear();
    claimFetchMock.mockReset();
    claimFetchMock.mockResolvedValue({ status: 202 });
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

  it("announces this device to the alert registry", () => {
    renderHook(() => useSoundAlerts());

    expect(socketHelloMock).toHaveBeenCalledWith({
      device_id: "dev_test",
      device_label: "Test device",
      can_ring: true,
    });
  });

  it("sends at most one activity frame per 10 s of interaction", () => {
    renderHook(() => useSoundAlerts());

    act(() => {
      window.dispatchEvent(new Event("keydown"));
      window.dispatchEvent(new Event("pointerdown"));
      window.dispatchEvent(new Event("focus"));
    });
    expect(socketActivityMock).toHaveBeenCalledTimes(1);

    act(() => {
      vi.advanceTimersByTime(10_000);
    });
    act(() => {
      window.dispatchEvent(new Event("focus"));
    });
    expect(socketActivityMock).toHaveBeenCalledTimes(2);
  });

  it("claims needs_response once when a session starts awaiting", () => {
    setConversations([conv("conv_a", { pending_elicitations_count: 0 })]);
    const { rerender } = renderHook(() => useSoundAlerts());

    act(() => {
      setConversations([conv("conv_a", { pending_elicitations_count: 1, updated_at: 200 })]);
      rerender();
    });

    expect(playLevelMock).not.toHaveBeenCalled();
    expect(claimFetchMock).toHaveBeenCalledTimes(1);
    expect(claimFetchMock).toHaveBeenCalledWith(
      "/v1/me/sound-alerts/claim",
      expect.objectContaining({ method: "POST" }),
    );
    expect(claimBodies()).toHaveLength(1);
    expect(claimBodies()[0]).toMatchObject({
      session_id: "conv_a",
      level: "needs_response",
    });
    expect(String(claimBodies()[0].alert_id)).toContain("conv_a:needs_response");
  });

  it("still claims when the device master switch is off", () => {
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

    // Another device may still play it; the switch only silences this one.
    expect(claimFetchMock).toHaveBeenCalledTimes(1);
    expect(playLevelMock).not.toHaveBeenCalled();
  });

  it("does not claim when the level is disabled", () => {
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

    expect(claimFetchMock).not.toHaveBeenCalled();
    expect(playLevelMock).not.toHaveBeenCalled();
  });

  it("does not claim an alert for a muted session", () => {
    localStorage.setItem(SOUND_ALERTS_STORAGE_KEY, JSON.stringify({ mutedSessionIds: ["conv_a"] }));
    setConversations([conv("conv_a", { pending_elicitations_count: 0 })]);
    const { rerender } = renderHook(() => useSoundAlerts());

    act(() => {
      setConversations([conv("conv_a", { pending_elicitations_count: 1, updated_at: 200 })]);
      rerender();
    });

    expect(claimFetchMock).not.toHaveBeenCalled();
    expect(playLevelMock).not.toHaveBeenCalled();
  });

  it("stays quiet on the first snapshot", () => {
    setConversations([conv("conv_a", { pending_elicitations_count: 1 })]);

    renderHook(() => useSoundAlerts());

    expect(claimFetchMock).not.toHaveBeenCalled();
  });

  it("waits while the conversation list is still loading", () => {
    useLoadedConversationsMock.mockReturnValue({ data: undefined, isLoading: true });

    renderHook(() => useSoundAlerts());

    expect(claimFetchMock).not.toHaveBeenCalled();
  });

  it("claims one done after the settle when a turn ends with nothing running", () => {
    setConversations([conv("conv_a")]);
    const { rerender } = renderHook(() => useSoundAlerts());

    act(() => {
      isConversationUnseenMock.mockReturnValue(true);
      setConversations([conv("conv_a", { updated_at: 200 })]);
      rerender();
    });
    expect(claimFetchMock).not.toHaveBeenCalled();

    act(() => {
      vi.advanceTimersByTime(10_000);
    });

    expect(claimFetchMock).toHaveBeenCalledTimes(1);
    expect(claimBodies()[0]).toMatchObject({ session_id: "conv_a", level: "done" });
  });

  it("stays silent while background work covers the dot, then claims when it clears", () => {
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
    expect(claimFetchMock).not.toHaveBeenCalled();

    act(() => {
      setConversations([conv("conv_a", { updated_at: 300 })]);
      rerender();
    });
    act(() => {
      vi.advanceTimersByTime(10_000);
    });

    expect(claimFetchMock).toHaveBeenCalledTimes(1);
    expect(claimBodies()[0]).toMatchObject({ session_id: "conv_a", level: "done" });
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

    expect(claimFetchMock).not.toHaveBeenCalled();
  });

  it("claims needs_response only, never done, for an awaiting row", () => {
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

    expect(claimFetchMock).toHaveBeenCalledTimes(1);
    expect(claimBodies()[0]).toMatchObject({ session_id: "conv_a", level: "needs_response" });
  });

  it("claims error when the session status fails", () => {
    setConversations([conv("conv_a")]);
    const { rerender } = renderHook(() => useSoundAlerts());

    act(() => {
      setConversations([conv("conv_a", { updated_at: 200, status: "failed" })]);
      rerender();
    });

    expect(claimFetchMock).toHaveBeenCalledTimes(1);
    expect(claimBodies()[0]).toMatchObject({ session_id: "conv_a", level: "error" });
  });

  it("claims error when an idle row has a latest error", () => {
    setConversations([conv("conv_a")]);
    const { rerender } = renderHook(() => useSoundAlerts());

    act(() => {
      latestErrorById.set("conv_a", "error");
      setConversations([conv("conv_a", { updated_at: 200 })]);
      rerender();
    });

    expect(claimFetchMock).toHaveBeenCalledTimes(1);
    expect(claimBodies()[0]).toMatchObject({ session_id: "conv_a", level: "error" });
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

    expect(claimFetchMock).not.toHaveBeenCalled();
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

    expect(claimFetchMock).toHaveBeenCalledTimes(1);
    expect(claimBodies()[0]).toMatchObject({ session_id: "conv_a", level: "done" });
  });

  it("re-claims a still-awaiting row when the socket reconnects", () => {
    setConversations([conv("conv_a", { pending_elicitations_count: 1 })]);
    renderHook(() => useSoundAlerts());
    expect(claimFetchMock).not.toHaveBeenCalled();

    act(() => {
      socketConnectedRef.current = true;
      for (const listener of socketStatusListeners) listener();
    });
    act(() => {
      vi.advanceTimersByTime(1_500);
    });

    expect(claimFetchMock).toHaveBeenCalledTimes(1);
    expect(claimBodies()[0]).toMatchObject({ session_id: "conv_a", level: "needs_response" });

    act(() => {
      socketConnectedRef.current = false;
      for (const listener of socketStatusListeners) listener();
      socketConnectedRef.current = true;
      for (const listener of socketStatusListeners) listener();
    });
    act(() => {
      vi.advanceTimersByTime(1_500);
    });

    // The reconnect re-sends the identical claim; the server drops the repeat.
    expect(claimFetchMock).toHaveBeenCalledTimes(2);
    expect(claimBodies()[1]).toEqual(claimBodies()[0]);
  });

  it("plays a sound_alert frame delivered to this connection", () => {
    renderHook(() => useSoundAlerts());

    emitFrame({
      type: "sound_alert",
      alert_id: "conv_a:done:200",
      session_id: "conv_a",
      level: "done",
    });

    expect(playLevelMock).toHaveBeenCalledTimes(1);
    expect(playLevelMock).toHaveBeenCalledWith("done", expect.anything(), expect.anything());
  });

  it("rings locally when the server has no claim route", async () => {
    claimFetchMock.mockResolvedValue({ status: 404 });
    setConversations([conv("conv_a")]);
    const { rerender } = renderHook(() => useSoundAlerts());

    await act(async () => {
      setConversations([conv("conv_a", { updated_at: 200, status: "failed" })]);
      rerender();
    });

    expect(claimFetchMock).toHaveBeenCalledTimes(1);
    expect(playLevelMock).toHaveBeenCalledTimes(1);
    expect(playLevelMock).toHaveBeenCalledWith("error", expect.anything(), expect.anything());
  });

  it("tells a native shell its own alert sounds are live", () => {
    isNativeShellMock.mockReturnValue(true);

    renderHook(() => useSoundAlerts());

    expect(setNativeSoundAlertsActiveMock).toHaveBeenCalledWith(true);
  });

  it("migrates the legacy shell sound setting into device preferences", async () => {
    isNativeShellMock.mockReturnValue(true);
    getLegacyNativeNotificationSoundMock.mockResolvedValue({ enabled: true, name: "Glass" });

    renderHook(() => useSoundAlerts());
    await act(async () => {
      await Promise.resolve();
    });

    expect(deviceStored()).toMatchObject({
      enabled: true,
      systemSounds: { done: "Glass", error: "Glass", needs_response: "Glass" },
      legacySoundMigrated: true,
    });
  });

  it("migrates a disabled legacy switch to a disabled device master switch", async () => {
    isNativeShellMock.mockReturnValue(true);
    getLegacyNativeNotificationSoundMock.mockResolvedValue({ enabled: false, name: null });

    renderHook(() => useSoundAlerts());
    await act(async () => {
      await Promise.resolve();
    });

    expect(deviceStored()).toMatchObject({
      enabled: false,
      systemSounds: {},
      legacySoundMigrated: true,
    });
  });

  it("runs the legacy migration only once", async () => {
    isNativeShellMock.mockReturnValue(true);
    getLegacyNativeNotificationSoundMock.mockResolvedValue({ enabled: true, name: "Glass" });

    const first = renderHook(() => useSoundAlerts());
    await act(async () => {
      await Promise.resolve();
    });
    expect(getLegacyNativeNotificationSoundMock).toHaveBeenCalledTimes(1);

    first.unmount();
    renderHook(() => useSoundAlerts());
    await act(async () => {
      await Promise.resolve();
    });

    expect(getLegacyNativeNotificationSoundMock).toHaveBeenCalledTimes(1);
  });

  it("does not advertise ringing while audio is locked, then re-announces once unlocked", () => {
    audioLock.locked = true;
    renderHook(() => useSoundAlerts());

    expect(socketHelloMock).toHaveBeenLastCalledWith(expect.objectContaining({ can_ring: false }));

    act(() => {
      audioLock.locked = false;
      for (const listener of audioLock.listeners) listener();
    });

    expect(socketHelloMock).toHaveBeenLastCalledWith(expect.objectContaining({ can_ring: true }));
  });
});
