import { createElement, type ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { authenticatedFetch } from "@/lib/identity";
import { linkedTerminalInfo, linkedTerminalsKey, useLinkedTerminals } from "./useLinkedTerminals";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));
afterEach(() => vi.mocked(authenticatedFetch).mockReset());

it("retains a missing host's cached terminal until a complete inventory answers", async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const terminal = {
    terminal_id: "a".repeat(32),
    host_id: "remote",
    host_name: "Other machine",
    elevated: false,
    instance_id: "one",
    agent_tier: "read" as const,
  };
  const cached = linkedTerminalInfo(terminal);
  client.setQueryData(linkedTerminalsKey("session"), [cached]);
  vi.mocked(authenticatedFetch).mockResolvedValue(
    Response.json({ terminals: [], complete: false }),
  );
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client }, children);
  const view = renderHook(() => useLinkedTerminals("session"), { wrapper });
  await waitFor(() => expect(view.result.current.isFetching).toBe(false));
  expect(view.result.current.data).toEqual([cached]);
  vi.mocked(authenticatedFetch).mockResolvedValue(Response.json({ terminals: [], complete: true }));
  await act(async () => {
    await view.result.current.refetch();
  });
  await waitFor(() => expect(view.result.current.data).toEqual([]));
  view.unmount();
  client.clear();
});
