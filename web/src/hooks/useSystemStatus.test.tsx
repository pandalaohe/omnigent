// The summary must stay nudge-driven (no interval) and catch up after a
// socket reconnect; the full view must renew its `live=1` lease only while
// the tab is visible.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const socket = vi.hoisted(() => {
  const listeners = new Set<() => void>();
  return {
    connected: false,
    listeners,
    emit() {
      for (const listener of listeners) listener();
    },
  };
});

vi.mock("@/lib/sessionUpdatesSocket", () => ({
  sessionUpdatesSocket: {
    isConnected: () => socket.connected,
    subscribeStatus: (listener: () => void) => {
      socket.listeners.add(listener);
      return () => socket.listeners.delete(listener);
    },
  },
}));

import { useSystemStatus, useSystemStatusSummary } from "./useSystemStatus";

const fetchMock = vi.fn();

function mockResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 200 ? "OK" : "Error",
    json: async () => body,
  } as unknown as Response;
}

const EMPTY_SUMMARY = { revision: 0, level: "ok", findings: [] };

function makeWrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  };
}

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  socket.connected = false;
  socket.listeners.clear();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("useSystemStatusSummary", () => {
  it("fetches summary=1 with no refetch interval", async () => {
    fetchMock.mockResolvedValue(mockResponse(EMPTY_SUMMARY));
    vi.useFakeTimers();
    const { result } = renderHook(() => useSystemStatusSummary(), { wrapper: makeWrapper() });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });

    expect(result.current.isSuccess).toBe(true);
    expect(fetchMock.mock.calls[0][0]).toBe("/v1/system/status?summary=1");

    // Ten minutes of wall clock must not add a single poll.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10 * 60_000);
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("invalidates itself when the socket reconnects", async () => {
    fetchMock.mockResolvedValue(mockResponse(EMPTY_SUMMARY));
    renderHook(() => useSystemStatusSummary(), { wrapper: makeWrapper() });
    await flush();
    expect(fetchMock).toHaveBeenCalledTimes(1);

    // The stream was down; an event sent then was dropped, so the first
    // connect afterwards refetches.
    act(() => {
      socket.connected = true;
      socket.emit();
    });
    await flush();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[1][0]).toBe("/v1/system/status?summary=1");
  });
});

describe("useSystemStatus", () => {
  it("polls live=1 only while the tab is visible", async () => {
    fetchMock.mockResolvedValue(mockResponse({ ...EMPTY_SUMMARY, server: null, hosts: [] }));
    vi.useFakeTimers();
    const visibility = vi.spyOn(document, "visibilityState", "get");
    visibility.mockReturnValue("visible");

    renderHook(() => useSystemStatus({ live: true }), { wrapper: makeWrapper() });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toBe("/v1/system/status?live=1");

    // Hiding the tab stops the poll — the server then lets the fast-sampling
    // lease lapse back to 60 s.
    visibility.mockReturnValue("hidden");
    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
      await vi.advanceTimersByTimeAsync(30_000);
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);

    // Returning renews the lease at once, then the 10 s cadence resumes.
    visibility.mockReturnValue("visible");
    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[1][0]).toBe("/v1/system/status?live=1");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("does not poll or send live when live is false", async () => {
    fetchMock.mockResolvedValue(mockResponse({ ...EMPTY_SUMMARY, server: null, hosts: [] }));
    vi.useFakeTimers();
    renderHook(() => useSystemStatus({ live: false }), { wrapper: makeWrapper() });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(fetchMock.mock.calls[0][0]).toBe("/v1/system/status");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
