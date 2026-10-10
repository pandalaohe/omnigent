import type { ReactNode } from "react";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import type { ServerInfo } from "@/lib/capabilities";
import { getOmnigentHostGeneration, getOmnigentServerIdentity } from "@/lib/host";
import { authenticatedFetch } from "@/lib/identity";
import { fetchSessionWorktreeStatus, useWorktreeStatus } from "./useWorktreeStatus";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));
vi.mock("@/lib/host", () => ({
  getOmnigentServerIdentity: vi.fn(() => "server-a"),
  getOmnigentHostGeneration: vi.fn(() => 1),
}));

const fetchMock = vi.mocked(authenticatedFetch);
const status = {
  own: {
    state: "clean",
    reason: null,
    path: "/opt/work/sample-app/worktrees/ui",
    branch: "feature/ui",
    merged: false,
    merge_target: "main",
    files: [],
  },
  aggregate: { state: "clean", reason: null },
  blockers: [],
  session_count: 1,
};
const response = (body: unknown, code = 200) =>
  new Response(JSON.stringify(body), { status: code });

function setup(info: "loading" | { worktree_status?: boolean } = { worktree_status: true }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <CapabilitiesProvider info={info === "loading" ? info : (info as ServerInfo)}>
        {children}
      </CapabilitiesProvider>
    </QueryClientProvider>
  );
  return { client, wrapper };
}

beforeEach(() => {
  fetchMock.mockReset();
  fetchMock.mockResolvedValue(response(status));
  vi.mocked(getOmnigentServerIdentity).mockReturnValue("server-a");
  vi.mocked(getOmnigentHostGeneration).mockReturnValue(1);
});
afterEach(() => cleanup());

describe("fetchSessionWorktreeStatus", () => {
  it("encodes IDs, requests refresh for archive preflight, and preserves merge context", async () => {
    const result = await fetchSessionWorktreeStatus("host/session", true);
    expect(fetchMock).toHaveBeenCalledWith(
      "/v1/sessions/host%2Fsession/worktree-status?refresh=true",
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
    expect(result.own.merged).toBe(false);
    expect(result.aggregate.state).toBe("clean");
  });

  it.each([
    { ...status, aggregate: { state: "safe", reason: null } },
    { ...status, own: { ...status.own, merged: "yes" } },
    { ...status, blockers: [{ session_id: "past", title: "Past child", state: "dirty" }] },
    { ...status, session_count: -1 },
    { aggregate: status.aggregate },
  ])("rejects malformed success bodies instead of treating them as safe", async (body) => {
    fetchMock.mockResolvedValue(response(body));
    await expect(fetchSessionWorktreeStatus("one")).rejects.toThrow(
      "Invalid worktree status response",
    );
  });

  it("rejects HTTP errors", async () => {
    fetchMock.mockResolvedValue(response({ detail: "host unavailable" }, 503));
    await expect(fetchSessionWorktreeStatus("one")).rejects.toThrow("HTTP 503");
  });
});

describe("useWorktreeStatus", () => {
  it.each(["loading", {}, { worktree_status: false }] as const)(
    "does not request an unsupported server (%j)",
    (info) => {
      const { wrapper } = setup(info);
      const { result } = renderHook(() => useWorktreeStatus("one"), { wrapper });
      expect(result.current.supported).toBe(false);
      expect(fetchMock).not.toHaveBeenCalled();
    },
  );

  it("does not request without a session ID", () => {
    const { wrapper } = setup();
    renderHook(() => useWorktreeStatus(null), { wrapper });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("hides cached green while refreshing and after a failed refetch", async () => {
    const { wrapper } = setup();
    const { result } = renderHook(() => useWorktreeStatus("one"), { wrapper });
    await waitFor(() => expect(result.current.data?.aggregate.state).toBe("clean"));
    let fail!: (error: Error) => void;
    fetchMock.mockImplementation(
      () =>
        new Promise((_, reject) => {
          fail = reject;
        }),
    );
    let refresh!: Promise<unknown>;
    act(() => {
      refresh = result.current.refetch();
    });
    await waitFor(() => expect(result.current.isFetching).toBe(true));
    expect(result.current.data).toBeUndefined();
    await act(async () => {
      fail(new Error("offline"));
      await refresh;
    });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.data).toBeUndefined();
  });

  it("keys a session's cache by server identity and generation", async () => {
    const { wrapper } = setup();
    const { result, rerender } = renderHook(() => useWorktreeStatus("one"), { wrapper });
    await waitFor(() => expect(result.current.data).toBeDefined());
    vi.mocked(getOmnigentServerIdentity).mockReturnValue("server-b");
    vi.mocked(getOmnigentHostGeneration).mockReturnValue(2);
    rerender();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
  });
});
