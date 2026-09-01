import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { Conversation } from "./useConversations";
import {
  ARCHIVE_SESSION_ACTION_EVENT,
  POLL_SESSIONS_ACTION_EVENT,
  choosePolledConversation,
  useSessionPollingHotkeys,
} from "./useSessionPollingHotkeys";
import {
  SESSION_NAVIGATION_STORAGE_KEY,
  writeSessionNavigationPreferences,
} from "@/lib/sessionNavigationPreferences";
import { resetReadStateForTests } from "./useUnseenConversations";

const navigate = vi.fn();
vi.mock("@/lib/routing", () => ({ useNavigate: () => navigate }));

function conversation(
  id: string,
  updatedAt = 1,
  overrides: Partial<Conversation> = {},
): Conversation {
  return {
    id,
    title: id,
    updated_at: updatedAt,
    created_at: 1,
    status: "idle",
    archived: false,
    ...overrides,
  } as Conversation;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((next) => {
    resolve = next;
  });
  return { promise, resolve };
}

describe("choosePolledConversation", () => {
  const rows = [conversation("a"), conversation("b"), conversation("c"), conversation("d")];

  it("chooses the next unread after the current session and wraps", () => {
    expect(choosePolledConversation(rows, "b", (row) => row.id === "d" || row.id === "a")?.id).toBe(
      "d",
    );
    expect(choosePolledConversation(rows, "d", (row) => row.id === "a")?.id).toBe("a");
  });

  it("falls back to the next session and wraps when none are unread", () => {
    expect(choosePolledConversation(rows, "b", () => false)?.id).toBe("c");
    expect(choosePolledConversation(rows, "d", () => false)?.id).toBe("a");
  });

  it("never returns the active session as its own unread target", () => {
    expect(choosePolledConversation([conversation("only")], "only", () => true)).toBeNull();
  });
});

describe("useSessionPollingHotkeys", () => {
  beforeEach(() => {
    localStorage.clear();
    resetReadStateForTests();
    navigate.mockReset();
  });

  it("polls from either its keyboard shortcut or the shared action event", async () => {
    const rows = [conversation("a"), conversation("b")];
    renderHook(() =>
      useSessionPollingHotkeys({
        activeId: "a",
        getConversations: async () => rows,
        isUnread: () => false,
        onArchive: vi.fn(),
      }),
    );

    act(() =>
      window.dispatchEvent(new KeyboardEvent("keydown", { code: "Backquote", altKey: true })),
    );
    await waitFor(() => expect(navigate).toHaveBeenLastCalledWith("/c/b"));

    act(() => window.dispatchEvent(new Event(POLL_SESSIONS_ACTION_EVENT)));
    await waitFor(() => expect(navigate).toHaveBeenCalledTimes(2));
  });

  it("limits polling candidates by updated_at when an active window is configured", async () => {
    vi.useFakeTimers({ now: new Date("2027-01-15T12:00:00Z") });
    const nowSeconds = Date.now() / 1000;
    writeSessionNavigationPreferences({
      pollingActiveWindowHours: 2,
      nativeMobileHeaderMode: "server",
    });
    const rows = [
      conversation("active", nowSeconds - 10 * 60 * 60),
      conversation("old-unread", nowSeconds - 3 * 60 * 60),
      conversation("recent", nowSeconds - 30 * 60),
    ];
    renderHook(() =>
      useSessionPollingHotkeys({
        activeId: "active",
        getConversations: async () => rows,
        isUnread: (row) => row.id === "old-unread",
        onArchive: vi.fn(),
      }),
    );

    act(() => window.dispatchEvent(new Event(POLL_SESSIONS_ACTION_EVENT)));
    await vi.runAllTimersAsync();
    expect(navigate).toHaveBeenLastCalledWith("/c/recent");
    vi.useRealTimers();
  });

  it("does nothing when an active window contains no polling candidates", async () => {
    const getConversations = vi
      .fn()
      .mockResolvedValue([conversation("outside", Date.now() / 1000 - 2 * 60 * 60)]);
    writeSessionNavigationPreferences({
      pollingActiveWindowHours: 1,
      nativeMobileHeaderMode: "server",
    });
    renderHook(() =>
      useSessionPollingHotkeys({
        activeId: "outside",
        getConversations,
        onArchive: vi.fn(),
      }),
    );

    act(() => window.dispatchEvent(new Event(POLL_SESSIONS_ACTION_EVENT)));

    await waitFor(() => expect(getConversations).toHaveBeenCalledOnce());
    expect(navigate).not.toHaveBeenCalled();
  });

  it("seeds unread state from newly loaded pages before choosing a target", async () => {
    const rows = [
      conversation("seed-active", 10),
      conversation("seed-unread", 20, { viewer_unread: true }),
      conversation("seed-next", 30),
    ];
    renderHook(() =>
      useSessionPollingHotkeys({
        activeId: "seed-active",
        getConversations: async () => rows,
        onArchive: vi.fn(),
      }),
    );

    act(() => window.dispatchEvent(new Event(POLL_SESSIONS_ACTION_EVENT)));

    await waitFor(() => expect(navigate).toHaveBeenLastCalledWith("/c/seed-unread"));
  });

  it("does not navigate from a poll that resolves after the active route changes", async () => {
    const pendingRows = deferred<Conversation[]>();
    const props = {
      activeId: "race-a",
      getConversations: () => pendingRows.promise,
      onArchive: vi.fn().mockResolvedValue(undefined),
    };
    const { rerender } = renderHook(
      ({ activeId }) => useSessionPollingHotkeys({ ...props, activeId }),
      { initialProps: { activeId: "race-a" } },
    );

    act(() => window.dispatchEvent(new Event(POLL_SESSIONS_ACTION_EVENT)));
    rerender({ activeId: "race-b" });
    await act(async () => {
      pendingRows.resolve([conversation("race-a"), conversation("race-b")]);
      await pendingRows.promise;
    });

    expect(navigate).not.toHaveBeenCalled();
  });

  it("keeps an old active session archivable while filtering the next target", async () => {
    const nowSeconds = Date.now() / 1000;
    localStorage.setItem(
      SESSION_NAVIGATION_STORAGE_KEY,
      JSON.stringify({ pollingActiveWindowHours: 1, nativeMobileHeaderMode: "server" }),
    );
    const rows = [
      conversation("active", nowSeconds - 5 * 60 * 60),
      conversation("recent", nowSeconds - 10 * 60),
    ];
    const onArchive = vi.fn().mockResolvedValue(undefined);
    renderHook(() =>
      useSessionPollingHotkeys({
        activeId: "active",
        getConversations: async () => rows,
        isUnread: () => false,
        onArchive,
      }),
    );

    act(() => window.dispatchEvent(new Event(ARCHIVE_SESSION_ACTION_EVENT)));

    await waitFor(() => expect(onArchive).toHaveBeenCalledWith("active"));
    expect(navigate).toHaveBeenLastCalledWith("/c/recent", { replace: true });
  });

  it("archives the active session then advances using the same unread-first rule", async () => {
    const rows = [conversation("a"), conversation("b"), conversation("c")];
    const onArchive = vi.fn().mockResolvedValue(undefined);
    renderHook(() =>
      useSessionPollingHotkeys({
        activeId: "a",
        getConversations: async () => rows,
        isUnread: (row) => row.id === "c",
        onArchive,
      }),
    );

    act(() => window.dispatchEvent(new Event(ARCHIVE_SESSION_ACTION_EVENT)));

    await waitFor(() => expect(onArchive).toHaveBeenCalledWith("a"));
    expect(navigate).toHaveBeenLastCalledWith("/c/c", { replace: true });
  });

  it("archives the trigger-time session without overriding a newer route", async () => {
    const pendingRows = deferred<Conversation[]>();
    const onArchive = vi.fn().mockResolvedValue(undefined);
    const props = {
      getConversations: () => pendingRows.promise,
      isUnread: () => false,
      onArchive,
    };
    const { rerender } = renderHook(
      ({ activeId }) => useSessionPollingHotkeys({ ...props, activeId }),
      { initialProps: { activeId: "archive-a" } },
    );

    act(() => window.dispatchEvent(new Event(ARCHIVE_SESSION_ACTION_EVENT)));
    rerender({ activeId: "archive-b" });
    await act(async () => {
      pendingRows.resolve([conversation("archive-a"), conversation("archive-b")]);
      await pendingRows.promise;
    });

    await waitFor(() => expect(onArchive).toHaveBeenCalledWith("archive-a"));
    expect(navigate).not.toHaveBeenCalled();
  });
});
