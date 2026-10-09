import { focusManager, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { PropsWithChildren } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { authenticatedFetch } from "@/lib/identity";
import { RECENT_SESSIONS_TOUCHED_EVENT } from "@/lib/sessionsApi";
import { RecentSessionsUnavailableError, useRecentSessions } from "./useRecentSessions";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));

const fetchMock = vi.mocked(authenticatedFetch);

beforeEach(() => {
  fetchMock.mockReset();
});

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 404 ? "Not Found" : "OK",
    json: async () => body,
  } as unknown as Response;
}

function wrapperFor(client: QueryClient) {
  return function Wrapper({ children }: PropsWithChildren) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  };
}

describe("useRecentSessions", () => {
  it("does not fetch while disabled", () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    renderHook(() => useRecentSessions(5, false), { wrapper: wrapperFor(client) });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("stops every automatic fetch after a 404 capability signal", async () => {
    fetchMock.mockResolvedValue(jsonResponse({}, 404));
    // A retrying client proves the hook's own retry decision, not the client's.
    const client = new QueryClient({
      defaultOptions: { queries: { retry: 3, retryDelay: 0 } },
    });
    vi.useFakeTimers();
    const { result, unmount } = renderHook(() => useRecentSessions(5, true), {
      wrapper: wrapperFor(client),
    });

    try {
      await vi.waitFor(() => expect(result.current.isError).toBe(true));

      expect(result.current.error).toBeInstanceOf(RecentSessionsUnavailableError);
      expect(fetchMock).toHaveBeenCalledTimes(1);
      expect(String(fetchMock.mock.calls[0][0])).toContain("/v1/me/recent-sessions?limit=5");

      await act(async () => {
        await vi.advanceTimersByTimeAsync(3 * 60_000);
        focusManager.setFocused(false);
        focusManager.setFocused(true);
        window.dispatchEvent(new Event(RECENT_SESSIONS_TOUCHED_EVENT));
      });

      expect(fetchMock).toHaveBeenCalledTimes(1);

      // Archive / delete invalidate the Recent key; the disabled query must not
      // refetch the route the server already said it doesn't have.
      await act(async () => {
        await client.invalidateQueries({ queryKey: ["recent-sessions"] });
      });
      expect(fetchMock).toHaveBeenCalledTimes(1);
    } finally {
      unmount();
      focusManager.setFocused(undefined);
      vi.useRealTimers();
    }
  });

  it("refetches when the touched event fires", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ data: [], first_id: null, last_id: null, has_more: false }),
    );
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { result } = renderHook(() => useRecentSessions(5, true), {
      wrapper: wrapperFor(client),
    });

    await waitFor(() => expect(result.current.isSuccess && !result.current.isFetching).toBe(true));
    const before = fetchMock.mock.calls.length;

    window.dispatchEvent(new Event(RECENT_SESSIONS_TOUCHED_EVENT));

    await waitFor(() => expect(fetchMock.mock.calls.length).toBe(before + 1));
  });
});
