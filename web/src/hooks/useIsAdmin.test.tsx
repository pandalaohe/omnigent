import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const identity = vi.hoisted(() => ({
  isAdmin: false,
  resolution: Promise.resolve<string | null>(null),
}));

vi.mock("@/lib/identity", () => ({
  getCurrentIsAdmin: () => identity.isAdmin,
  resolveIdentity: () => identity.resolution,
}));

import { useIsAdmin } from "./useIsAdmin";

function wrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}

beforeEach(() => {
  identity.isAdmin = false;
  identity.resolution = Promise.resolve<string | null>(null);
});

afterEach(cleanup);

describe("useIsAdmin", () => {
  it("fetches on mount and turns true once identity resolves", async () => {
    // The app mounts before GET /v1/me settles. The seeded false covers first
    // paint, but it must NOT count as fresh data — with initialData the query
    // never runs, the seeded false sticks for staleTime, and the admin nav
    // stays hidden (the bug). The queryFn awaits the memoized identity probe.
    let resolve!: (value: string | null) => void;
    identity.resolution = new Promise((finish) => {
      resolve = finish;
    });

    const { result } = renderHook(() => useIsAdmin(), { wrapper: wrapper() });
    expect(result.current).toBe(false);

    identity.isAdmin = true;
    resolve("alice");

    await waitFor(() => expect(result.current).toBe(true));
  });

  it("paints true on the first render when identity already resolved", () => {
    identity.isAdmin = true;
    identity.resolution = Promise.resolve("alice");

    const { result } = renderHook(() => useIsAdmin(), { wrapper: wrapper() });
    expect(result.current).toBe(true);
  });
});
