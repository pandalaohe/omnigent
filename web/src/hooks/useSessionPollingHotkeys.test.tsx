import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { Conversation } from "./useConversations";
import type { AnyBlock, ElicitationBlock } from "@/lib/blocks";
import {
  ARCHIVE_SESSION_ACTION_EVENT,
  POLL_SESSIONS_ACTION_EVENT,
  choosePolledConversation,
  pendingCardsToAcknowledge,
  resetElicitationAcknowledgementsForTests,
  useAcknowledgePendingElicitations,
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

function elicitation(id: string, overrides: Partial<ElicitationBlock> = {}): AnyBlock {
  return {
    type: "elicitation",
    ctx: { agent: null, depth: 0, turn: 0, timestamp: 0, responseId: "resp_1", itemId: null },
    elicitationId: id,
    targetSessionId: null,
    message: "Allow shell command?",
    phase: "tool_call",
    policyName: "ask-before-shell",
    contentPreview: "{}",
    requestedSchema: {},
    url: null,
    status: "pending",
    response: null,
    ...overrides,
  };
}

beforeEach(() => {
  localStorage.clear();
  resetReadStateForTests();
  resetElicitationAcknowledgementsForTests();
  navigate.mockReset();
});

describe("choosePolledConversation", () => {
  it("jumps to the oldest unacknowledged needs-response row, ahead of unread and plain rows", () => {
    const rows = [
      conversation("active", 4),
      conversation("newer-card", 6, { pending_elicitations_count: 1 }),
      conversation("older-card", 2, { pending_elicitations_count: 1 }),
      conversation("plain-unread", 1),
    ];
    expect(
      choosePolledConversation(rows, "active", {
        isUnread: (row) => row.id === "plain-unread",
        needsResponse: (row) => (row.pending_elicitations_count ?? 0) > 0,
      })?.id,
    ).toBe("older-card");
  });

  it("reaches a needs-response row that is background, collapsed, and outside the window", () => {
    const rows = [
      conversation("active", 3),
      conversation("card", 2, { pending_elicitations_count: 2, background_activity_count: 1 }),
      conversation("plain", 1),
    ];
    expect(
      choosePolledConversation(rows, "active", {
        isUnread: () => false,
        needsResponse: (row) => row.id === "card",
        isCollapsed: () => true,
        isInsideWindow: (row) => row.id !== "card",
        deprioritizeBackgroundSessions: true,
      })?.id,
    ).toBe("card");
  });

  it("picks the oldest unread row before cycling", () => {
    const rows = [
      conversation("active", 4),
      conversation("newer-unread", 5),
      conversation("older-unread", 2),
      conversation("read", 3),
    ];
    expect(
      choosePolledConversation(rows, "active", {
        isUnread: (row) => row.id === "newer-unread" || row.id === "older-unread",
      })?.id,
    ).toBe("older-unread");
  });

  it("keeps background unread rows out of the unread tier while deprioritized, and admits them when not", () => {
    const rows = [
      conversation("active", 4),
      conversation("plain-unread", 5),
      conversation("background-unread", 2, { background_activity_count: 1 }),
      conversation("goal-unread", 3, { goal_state: "paused" }),
    ];
    const unread = (row: Conversation) => row.id !== "active";
    // Background and Goal rows are excluded, although both are older than the plain row.
    expect(
      choosePolledConversation(rows, "active", {
        isUnread: unread,
        deprioritizeBackgroundSessions: true,
      })?.id,
    ).toBe("plain-unread");
    // Setting off: every unread row competes, oldest first.
    expect(
      choosePolledConversation(rows, "active", {
        isUnread: unread,
        deprioritizeBackgroundSessions: false,
      })?.id,
    ).toBe("background-unread");
  });

  it("keeps a foreground-running row in its plain sidebar place", () => {
    const rows = [
      conversation("active", 3),
      conversation("running", 2, { status: "running", foreground_status: "running" }),
      conversation("plain", 1),
    ];
    expect(
      choosePolledConversation(rows, "active", {
        isUnread: () => false,
        deprioritizeBackgroundSessions: true,
      })?.id,
    ).toBe("running");
  });

  it("cycles in sidebar order after the active row, wrapping bottom to top", () => {
    const rows = [conversation("a"), conversation("b"), conversation("c"), conversation("d")];
    expect(choosePolledConversation(rows, "b", { isUnread: () => false })?.id).toBe("c");
    expect(choosePolledConversation(rows, "d", { isUnread: () => false })?.id).toBe("a");
  });

  it("skips collapsed rows in the plain cycle but jumps to them when unread", () => {
    const rows = [conversation("active"), conversation("hidden"), conversation("visible")];
    const isCollapsed = (row: Conversation) => row.id === "hidden";
    expect(
      choosePolledConversation(rows, "active", { isUnread: () => false, isCollapsed })?.id,
    ).toBe("visible");
    expect(
      choosePolledConversation(rows, "active", {
        isUnread: (row) => row.id === "hidden",
        isCollapsed,
      })?.id,
    ).toBe("hidden");
  });

  it("skips rows outside the active window in the unread tier and the plain cycle", () => {
    const rows = [conversation("active"), conversation("outside"), conversation("inside")];
    expect(
      choosePolledConversation(rows, "active", {
        isUnread: (row) => row.id === "outside",
        isInsideWindow: (row) => row.id !== "outside",
      })?.id,
    ).toBe("inside");
  });

  it("skips visited rows in the plain cycle and returns null when none remain", () => {
    const rows = [conversation("active"), conversation("seen"), conversation("fresh")];
    expect(
      choosePolledConversation(rows, "active", {
        isUnread: () => false,
        visited: new Set(["active", "seen"]),
      })?.id,
    ).toBe("fresh");
    expect(
      choosePolledConversation(rows, "active", {
        isUnread: () => false,
        visited: new Set(["active", "seen", "fresh"]),
      }),
    ).toBeNull();
  });

  it("ignores round bookkeeping for needs-response and unread jumps", () => {
    const rows = [
      conversation("active"),
      conversation("card", 2, { pending_elicitations_count: 1 }),
      conversation("unread", 1),
    ];
    const visited = new Set(["active", "card", "unread"]);
    expect(
      choosePolledConversation(rows, "active", {
        isUnread: (row) => row.id === "unread",
        needsResponse: (row) => row.id === "card",
        visited,
      })?.id,
    ).toBe("card");
    expect(
      choosePolledConversation(rows, "active", {
        isUnread: (row) => row.id === "unread",
        visited,
      })?.id,
    ).toBe("unread");
  });

  it("never returns the active row, even with a pending card", () => {
    expect(
      choosePolledConversation(
        [conversation("only", 1, { pending_elicitations_count: 2 })],
        "only",
        {
          isUnread: () => true,
          needsResponse: () => true,
        },
      ),
    ).toBeNull();
  });
});

describe("pendingCardsToAcknowledge", () => {
  it("counts the session's own pending cards and excludes child mirrors and answered cards", () => {
    const blocks = [
      elicitation("own-1"),
      elicitation("own-2", { targetSessionId: "conv_self" }),
      elicitation("child", { targetSessionId: "conv_child" }),
      elicitation("answered", { status: "responded", response: { action: "accept" } }),
    ];
    expect(pendingCardsToAcknowledge(blocks, "conv_self", undefined, "conv_self")).toBe(2);
  });

  it("keeps the loaded row's count when it is larger than the transcript's", () => {
    expect(pendingCardsToAcknowledge([elicitation("own-1")], "conv_self", 3, "conv_self")).toBe(3);
  });

  it("returns zero without an active conversation", () => {
    expect(pendingCardsToAcknowledge([elicitation("own-1")], undefined, undefined, undefined)).toBe(
      0,
    );
  });

  it("ignores the previous conversation's blocks while the route switch is in flight", () => {
    const stale = [elicitation("old-card")];
    expect(pendingCardsToAcknowledge(stale, "conv_new", undefined, "conv_old")).toBe(0);
    // A loaded row for the incoming session still acknowledges.
    expect(pendingCardsToAcknowledge(stale, "conv_new", 2, "conv_old")).toBe(2);
  });
});

describe("useSessionPollingHotkeys", () => {
  async function pressAndExpect(targetId: string) {
    act(() => window.dispatchEvent(new Event(POLL_SESSIONS_ACTION_EVENT)));
    await waitFor(() => expect(navigate).toHaveBeenLastCalledWith(`/c/${targetId}`));
  }

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
      deprioritizeBackgroundSessions: true,
      scrollToBottomOnSessionOpen: true,
      nativeMobileHeaderMode: "server",
      showGoalSessionMarkers: true,
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

  it("reaches a needs-response row outside the active window", async () => {
    const nowSeconds = Date.now() / 1000;
    writeSessionNavigationPreferences({
      pollingActiveWindowHours: 1,
      deprioritizeBackgroundSessions: true,
      scrollToBottomOnSessionOpen: true,
      nativeMobileHeaderMode: "server",
      showGoalSessionMarkers: true,
    });
    const rows = [
      conversation("active", nowSeconds),
      conversation("recent", nowSeconds - 10 * 60),
      conversation("old-card", nowSeconds - 5 * 60 * 60, { pending_elicitations_count: 1 }),
    ];
    renderHook(() =>
      useSessionPollingHotkeys({
        activeId: "active",
        getConversations: async () => rows,
        isUnread: () => false,
        onArchive: vi.fn(),
      }),
    );

    act(() => window.dispatchEvent(new Event(POLL_SESSIONS_ACTION_EVENT)));

    await waitFor(() => expect(navigate).toHaveBeenLastCalledWith("/c/old-card"));
  });

  it("does nothing when an active window contains no polling candidates", async () => {
    const getConversations = vi
      .fn()
      .mockResolvedValue([conversation("outside", Date.now() / 1000 - 2 * 60 * 60)]);
    writeSessionNavigationPreferences({
      pollingActiveWindowHours: 1,
      deprioritizeBackgroundSessions: true,
      scrollToBottomOnSessionOpen: true,
      nativeMobileHeaderMode: "server",
      showGoalSessionMarkers: true,
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

  it("recognizes a finished B session as unread from its foreground status", async () => {
    const rows = [
      conversation("active", 10),
      conversation("background-result", 20, {
        status: "running",
        foreground_status: "idle",
        background_activity_count: 1,
        viewer_last_seen: 10,
      }),
      conversation("background-idle", 30, {
        background_activity_count: 1,
        viewer_last_seen: 30,
      }),
    ];
    renderHook(() =>
      useSessionPollingHotkeys({
        activeId: "active",
        getConversations: async () => rows,
        onArchive: vi.fn(),
      }),
    );

    act(() => window.dispatchEvent(new Event(POLL_SESSIONS_ACTION_EVENT)));

    await waitFor(() => expect(navigate).toHaveBeenLastCalledWith("/c/background-result"));
  });

  it("never treats a foreground-running row as unread, even with the background setting off", async () => {
    writeSessionNavigationPreferences({
      pollingActiveWindowHours: null,
      deprioritizeBackgroundSessions: false,
      scrollToBottomOnSessionOpen: true,
      nativeMobileHeaderMode: "server",
      showGoalSessionMarkers: true,
    });
    const rows = [
      conversation("active", 3, { viewer_last_seen: 3 }),
      conversation("plain", 2, { viewer_last_seen: 2 }),
      conversation("running", 1, {
        status: "running",
        foreground_status: "running",
        viewer_last_seen: 0,
      }),
    ];
    renderHook(() =>
      useSessionPollingHotkeys({
        activeId: "active",
        getConversations: async () => rows,
        onArchive: vi.fn(),
      }),
    );

    act(() => window.dispatchEvent(new Event(POLL_SESSIONS_ACTION_EVENT)));

    await waitFor(() => expect(navigate).toHaveBeenLastCalledWith("/c/plain"));
  });

  it("replays the recorded sidebar: unread first, then plain round-robin over B and spinner rows", async () => {
    // Top→bottom: A(BG), B, C(running), D(BG), E, F(BG+unread), G(active), H(running).
    const rows = [
      conversation("A", 8, { background_activity_count: 1 }),
      conversation("B", 7),
      conversation("C", 6, { status: "running", foreground_status: "running" }),
      conversation("D", 5, { background_activity_count: 1 }),
      conversation("E", 4),
      conversation("F", 3, { background_activity_count: 1 }),
      conversation("G", 2),
      conversation("H", 1, { status: "running", foreground_status: "running" }),
    ];
    const unread = new Set(["B", "F"]);
    const props = {
      getConversations: async () => rows,
      isUnread: (row: Conversation) => unread.has(row.id),
      onArchive: vi.fn(),
    };
    const { rerender } = renderHook(
      ({ activeId }) => useSessionPollingHotkeys({ ...props, activeId }),
      { initialProps: { activeId: "G" } },
    );

    for (const expected of ["B", "C", "D", "E", "F", "H", "A", "B"]) {
      // Each press continues the round the previous press left behind.
      // eslint-disable-next-line no-await-in-loop
      await pressAndExpect(expected);
      unread.delete(expected); // A visited row stops being unread.
      rerender({ activeId: expected });
    }
  });

  it("cycles the recorded sidebar bottom-to-top when nothing is unread", async () => {
    const rows = [
      conversation("A", 8, { background_activity_count: 1 }),
      conversation("B", 7),
      conversation("C", 6, { status: "running", foreground_status: "running" }),
      conversation("D", 5, { background_activity_count: 1 }),
      conversation("E", 4),
      conversation("F", 3, { background_activity_count: 1 }),
      conversation("G", 2),
      conversation("H", 1, { status: "running", foreground_status: "running" }),
    ];
    const props = {
      getConversations: async () => rows,
      isUnread: () => false,
      onArchive: vi.fn(),
    };
    const { rerender } = renderHook(
      ({ activeId }) => useSessionPollingHotkeys({ ...props, activeId }),
      { initialProps: { activeId: "G" } },
    );

    for (const expected of ["H", "A", "B", "C", "D", "E", "F", "G"]) {
      // Each press continues the round the previous press left behind.
      // eslint-disable-next-line no-await-in-loop
      await pressAndExpect(expected);
      rerender({ activeId: expected });
    }
  });

  it("stops prioritizing a needs-response row once opened, until a new card arrives", async () => {
    let currentRows = [
      conversation("active", 3),
      conversation("next", 2),
      conversation("card", 1, { pending_elicitations_count: 1 }),
    ];
    const polling = renderHook(
      ({ activeId }) =>
        useSessionPollingHotkeys({
          activeId,
          getConversations: async () => currentRows,
          isUnread: () => false,
          onArchive: vi.fn(),
        }),
      { initialProps: { activeId: "active" } },
    );

    await pressAndExpect("card");

    // Opening the session acknowledges the cards it currently shows (ChatPage's
    // call on the open route).
    renderHook(() => useAcknowledgePendingElicitations("card", 1));
    polling.rerender({ activeId: "card" });
    await pressAndExpect("next");

    currentRows = [
      conversation("active", 3),
      conversation("next", 2),
      conversation("card", 1, { pending_elicitations_count: 2 }),
    ];
    polling.rerender({ activeId: "next" });
    await pressAndExpect("card");
  });

  it("acknowledges a card on a row outside the loaded list, so Poll moves on", async () => {
    const currentRows = [
      conversation("card", 3, { pending_elicitations_count: 1 }),
      conversation("next", 2),
      conversation("active", 1),
    ];
    const polling = renderHook(
      ({ activeId }) =>
        useSessionPollingHotkeys({
          activeId,
          getConversations: async () => currentRows,
          isUnread: () => false,
          onArchive: vi.fn(),
        }),
      { initialProps: { activeId: "active" } },
    );

    await pressAndExpect("card");
    polling.rerender({ activeId: "card" });

    // ChatPage's acknowledgement call when the row is absent from
    // useLoadedConversations(): the open transcript's own pending blocks are
    // the only count available.
    renderHook(() =>
      useAcknowledgePendingElicitations(
        "card",
        pendingCardsToAcknowledge([elicitation("card-1")], "card", undefined, "card"),
      ),
    );

    // Moving on by hand, then polling, must not pull back to the acknowledged
    // card; it continues the fresh round from next.
    polling.rerender({ activeId: "next" });
    await pressAndExpect("active");
  });

  it("lowers the acknowledged baseline when the observed card count drops", async () => {
    // Opened while showing two cards, then left.
    const ack = renderHook(({ count }) => useAcknowledgePendingElicitations("card", count), {
      initialProps: { count: 2 },
    });
    ack.unmount();

    let currentRows = [
      conversation("active", 4),
      conversation("next", 3),
      conversation("other", 2),
      conversation("card", 1, { pending_elicitations_count: 1 }),
    ];
    const polling = renderHook(
      ({ activeId }) =>
        useSessionPollingHotkeys({
          activeId,
          getConversations: async () => currentRows,
          isUnread: () => false,
          onArchive: vi.fn(),
        }),
      { initialProps: { activeId: "active" } },
    );

    // One card was answered elsewhere: observed 1 < stored 2 lowers the
    // baseline, so the row polls as a regular row.
    await pressAndExpect("next");

    // A fresh card (back to 2) exceeds the lowered baseline: needs-response again.
    currentRows = [
      conversation("active", 4),
      conversation("next", 3),
      conversation("other", 2),
      conversation("card", 1, { pending_elicitations_count: 2 }),
    ];
    polling.rerender({ activeId: "next" });
    await pressAndExpect("card");
  });

  it("clears the acknowledgement when no cards remain, so any new card re-raises the row", async () => {
    const ack = renderHook(() => useAcknowledgePendingElicitations("card", 1));
    ack.unmount();

    const withoutCards = [
      conversation("active", 5),
      conversation("next", 4),
      conversation("other", 3),
      conversation("tail", 2),
      conversation("card", 1, { pending_elicitations_count: 0 }),
    ];
    const withCard = withoutCards.map((row) =>
      row.id === "card" ? conversation("card", 1, { pending_elicitations_count: 1 }) : row,
    );
    let currentRows = withCard;
    const polling = renderHook(
      ({ activeId }) =>
        useSessionPollingHotkeys({
          activeId,
          getConversations: async () => currentRows,
          isUnread: () => false,
          onArchive: vi.fn(),
        }),
      { initialProps: { activeId: "active" } },
    );

    // Already acknowledged: the row polls as a regular row.
    await pressAndExpect("next");

    // The last card was answered elsewhere; the observation clears the baseline.
    currentRows = withoutCards;
    polling.rerender({ activeId: "next" });
    await pressAndExpect("other");

    // Any new card on the row is needs-response again.
    currentRows = withCard;
    polling.rerender({ activeId: "other" });
    await pressAndExpect("card");
  });

  it("anchors the plain cycle at the row a priority jump landed on", async () => {
    const rows = [
      conversation("a", 4),
      conversation("b", 3),
      conversation("c", 2),
      conversation("d", 1),
    ];
    const props = {
      getConversations: async () => rows,
      isUnread: (row: Conversation) => row.id === "c",
      onArchive: vi.fn(),
    };
    const { rerender } = renderHook(
      ({ activeId }) => useSessionPollingHotkeys({ ...props, activeId }),
      { initialProps: { activeId: "a" } },
    );

    await pressAndExpect("c");
    rerender({ activeId: "c" });
    await pressAndExpect("d");
  });

  it("never revisits a row early when the sidebar reorders mid-round", async () => {
    let currentRows = [conversation("a", 3), conversation("b", 2), conversation("c", 1)];
    const props = {
      getConversations: async () => currentRows,
      isUnread: () => false,
      onArchive: vi.fn(),
    };
    const { rerender } = renderHook(
      ({ activeId }) => useSessionPollingHotkeys({ ...props, activeId }),
      { initialProps: { activeId: "a" } },
    );

    await pressAndExpect("b");
    // b bumps to the top mid-round; the visited set still shields it and a.
    currentRows = [conversation("b", 3), conversation("a", 2), conversation("c", 1)];
    rerender({ activeId: "b" });
    await pressAndExpect("c");
    // Round complete: a new round starts from c and wraps to the top.
    rerender({ activeId: "c" });
    await pressAndExpect("b");
  });

  it("starts a new round from a row the user opened without polling", async () => {
    const rows = [conversation("a", 3), conversation("b", 2), conversation("c", 1)];
    const props = {
      getConversations: async () => rows,
      isUnread: () => false,
      onArchive: vi.fn(),
    };
    const { rerender } = renderHook(
      ({ activeId }) => useSessionPollingHotkeys({ ...props, activeId }),
      { initialProps: { activeId: "a" } },
    );

    await pressAndExpect("b");
    // A manual click lands on c: the round restarts there, so a is next.
    rerender({ activeId: "c" });
    await pressAndExpect("a");
  });

  it("starts a new round when the user returns by hand to the last poll target", async () => {
    const rows = [
      conversation("a", 4),
      conversation("b", 3),
      conversation("c", 2),
      conversation("d", 1),
    ];
    const props = {
      getConversations: async () => rows,
      isUnread: (row: Conversation) => row.id === "d",
      onArchive: vi.fn(),
    };
    const { rerender } = renderHook(
      ({ activeId }) => useSessionPollingHotkeys({ ...props, activeId }),
      { initialProps: { activeId: "a" } },
    );

    // Poll jumps to the unread d.
    await pressAndExpect("d");
    rerender({ activeId: "d" });
    // Manual clicks b then back to d restart the round at each landing, so a
    // — visited before the jump in the abandoned round — is next again.
    rerender({ activeId: "b" });
    rerender({ activeId: "d" });
    await pressAndExpect("a");
  });

  it("re-jumps to a still-unread row within the same round and continues after it", async () => {
    const rows = [
      conversation("a", 4),
      conversation("b", 3),
      conversation("c", 2),
      conversation("d", 1),
    ];
    const props = {
      getConversations: async () => rows,
      isUnread: (row: Conversation) => row.id === "b",
      onArchive: vi.fn(),
    };
    const { rerender } = renderHook(
      ({ activeId }) => useSessionPollingHotkeys({ ...props, activeId }),
      { initialProps: { activeId: "a" } },
    );

    await pressAndExpect("b");
    rerender({ activeId: "b" });
    await pressAndExpect("c");
    rerender({ activeId: "c" });
    // b is still unread: the unread tier ignores that it was visited this round.
    await pressAndExpect("b");
    rerender({ activeId: "b" });
    // The plain cycle continues after the jump target, skipping visited rows.
    await pressAndExpect("d");
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

    await waitFor(() =>
      expect(onArchive).toHaveBeenCalledWith(expect.objectContaining({ id: "active" })),
    );
    expect(navigate).toHaveBeenLastCalledWith("/c/recent", { replace: true });
  });

  it("archives the active session then advances by the same tiers", async () => {
    const rows = [
      conversation("a", 3),
      conversation("b", 2, { pending_elicitations_count: 1 }),
      conversation("c", 1),
    ];
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

    await waitFor(() =>
      expect(onArchive).toHaveBeenCalledWith(expect.objectContaining({ id: "a" })),
    );
    expect(navigate).toHaveBeenLastCalledWith("/c/b", { replace: true });
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

    await waitFor(() =>
      expect(onArchive).toHaveBeenCalledWith(expect.objectContaining({ id: "archive-a" })),
    );
    expect(navigate).not.toHaveBeenCalled();
  });
});
