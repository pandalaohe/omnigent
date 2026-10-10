import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Session } from "@/lib/types";
import type { Conversation } from "./useConversations";
import { ARCHIVE_SESSION_ACTION_EVENT, useSessionPollingHotkeys } from "./useSessionPollingHotkeys";

const { navigate, getSessionSlim, errorToast } = vi.hoisted(() => ({
  navigate: vi.fn(),
  getSessionSlim: vi.fn(),
  errorToast: vi.fn(),
}));
vi.mock("@/lib/routing", () => ({ useNavigate: () => navigate }));
vi.mock("@/lib/sessionsApi", () => ({ getSessionSlim }));
vi.mock("sonner", () => ({ toast: { error: errorToast } }));

const child = {
  id: "child",
  parentSessionId: "parent",
  title: "Research",
  createdAt: 1,
  updatedAt: 2,
  archived: false,
  labels: {},
  permissionLevel: 4,
} as Session;
const parent: Conversation = {
  id: "parent",
  object: "conversation",
  title: "Project",
  created_at: 1,
  updated_at: 2,
  labels: {},
  archived: false,
  permission_level: 4,
};

beforeEach(() => {
  localStorage.clear();
  vi.clearAllMocks();
  getSessionSlim.mockResolvedValue(child);
});

describe("archive shortcut on a child", () => {
  it("keeps the route when the archive safety warning is cancelled", async () => {
    const onArchive = vi.fn().mockResolvedValue(false);
    renderHook(() =>
      useSessionPollingHotkeys({
        activeId: "child",
        getConversations: async () => [parent],
        onArchive,
      }),
    );
    await act(async () => window.dispatchEvent(new Event(ARCHIVE_SESSION_ACTION_EVENT)));
    await waitFor(() => expect(onArchive).toHaveBeenCalledOnce());
    expect(navigate).not.toHaveBeenCalled();
  });
  it("archives only the selected child missing from the sidebar", async () => {
    const onArchive = vi.fn().mockResolvedValue(undefined);
    renderHook(() =>
      useSessionPollingHotkeys({
        activeId: "child",
        getConversations: async () => [parent],
        onArchive,
      }),
    );
    act(() => window.dispatchEvent(new Event(ARCHIVE_SESSION_ACTION_EVENT)));
    await waitFor(() => expect(onArchive).toHaveBeenCalledTimes(1));
    expect(onArchive).toHaveBeenCalledWith(
      expect.objectContaining({ id: "child", parent_session_id: "parent", updated_at: 2 }),
    );
    expect(navigate).toHaveBeenCalledWith("/c/parent", { replace: true });
  });

  it.each([
    { ...child, permissionLevel: 2 },
    { ...child, archived: true },
    { ...child, id: "another-child" },
  ])("rejects inaccessible, archived or mismatched snapshots", async (snapshot) => {
    getSessionSlim.mockResolvedValue(snapshot);
    const onArchive = vi.fn();
    renderHook(() =>
      useSessionPollingHotkeys({
        activeId: "child",
        getConversations: async () => [parent],
        onArchive,
      }),
    );
    await act(async () => window.dispatchEvent(new Event(ARCHIVE_SESSION_ACTION_EVENT)));
    expect(getSessionSlim).toHaveBeenCalledWith("child");
    expect(onArchive).not.toHaveBeenCalled();
    expect(navigate).not.toHaveBeenCalled();
  });

  it("reports a failed lookup and allows a retry", async () => {
    getSessionSlim.mockRejectedValueOnce(new Error("offline"));
    const onArchive = vi.fn().mockResolvedValue(undefined);
    renderHook(() =>
      useSessionPollingHotkeys({
        activeId: "child",
        getConversations: async () => [parent],
        onArchive,
      }),
    );
    act(() => window.dispatchEvent(new Event(ARCHIVE_SESSION_ACTION_EVENT)));
    await waitFor(() => expect(errorToast).toHaveBeenCalledTimes(1));
    expect(onArchive).not.toHaveBeenCalled();
    act(() => window.dispatchEvent(new Event(ARCHIVE_SESSION_ACTION_EVENT)));
    await waitFor(() => expect(onArchive).toHaveBeenCalledTimes(1));
  });

  it("does not navigate over a route chosen during snapshot lookup", async () => {
    let resolve!: (session: Session) => void;
    getSessionSlim.mockReturnValue(
      new Promise<Session>((done) => {
        resolve = done;
      }),
    );
    const onArchive = vi.fn().mockResolvedValue(undefined);
    const { rerender } = renderHook(
      ({ activeId }) =>
        useSessionPollingHotkeys({
          activeId,
          getConversations: async () => [parent],
          onArchive,
        }),
      { initialProps: { activeId: "child" } },
    );
    act(() => window.dispatchEvent(new Event(ARCHIVE_SESSION_ACTION_EVENT)));
    await waitFor(() => expect(getSessionSlim).toHaveBeenCalled());
    rerender({ activeId: "other" });
    await act(async () => resolve(child));
    expect(onArchive).toHaveBeenCalledWith(expect.objectContaining({ id: "child" }));
    expect(navigate).not.toHaveBeenCalled();
  });
});
