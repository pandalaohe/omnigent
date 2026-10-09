import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
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

  it("treats a 404 as a capability signal and does not retry", async () => {
    fetchMock.mockResolvedValue(jsonResponse({}, 404));
    // A retrying client proves the hook's own retry decision, not the client's.
    const client = new QueryClient({
      defaultOptions: { queries: { retry: 3, retryDelay: 0 } },
    });
    const { result } = renderHook(() => useRecentSessions(5, true), {
      wrapper: wrapperFor(client),
    });

    await waitFor(() => expect(result.current.isError).toBe(true));

    expect(result.current.error).toBeInstanceOf(RecentSessionsUnavailableError);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(String(fetchMock.mock.calls[0][0])).toContain("/v1/me/recent-sessions?limit=5");
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
