import type { ReactNode } from "react";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { authenticatedFetch } from "@/lib/identity";
import {
  useInstructionPreview,
  useInstructionRevisions,
  useSaveInstructionLayer,
} from "./useInstructionLayers";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));
const fetch = vi.mocked(authenticatedFetch);
function setup() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return {
    client,
    wrapper: ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    ),
  };
}
beforeEach(() => fetch.mockReset());
afterEach(cleanup);

it("sends the exact scoped text and invalidates totals and history after a save", async () => {
  const { client, wrapper } = setup();
  client.setQueryData(["instruction-layers"], { data: [] });
  client.setQueryData(["instruction-layers", "preview", "host-a"], { text: "old" });
  client.setQueryData(["instruction-layers", "revisions", { scope: "host", host_id: "host-a" }], {
    data: [],
  });
  fetch.mockResolvedValue(
    new Response(
      JSON.stringify({
        over_budget_routes: [{ host_id: "host-a", agent_key: "codex", total_chars: 10001 }],
      }),
      { status: 200 },
    ),
  );
  const { result } = renderHook(useSaveInstructionLayer, { wrapper });
  const body = {
    scope: "host" as const,
    agent_key: "",
    host_id: "host-a",
    text: "😀".repeat(10001),
  };
  await act(async () => {
    await result.current.mutateAsync(body);
  });
  const [path, request] = fetch.mock.calls[0]!;
  expect(path).toBe("/v1/instruction-layers");
  expect(request?.method).toBe("PUT");
  expect(JSON.parse(request?.body as string)).toEqual(body);
  expect(client.getQueryState(["instruction-layers", "preview", "host-a"])?.isInvalidated).toBe(
    true,
  );
  await waitFor(() => expect(result.current.data?.over_budget_routes).toHaveLength(1));
  client.clear();
});
it("keeps the route identity in the query and exposes safe API errors", async () => {
  const { client, wrapper } = setup();
  fetch.mockResolvedValue(
    new Response(JSON.stringify({ error: { message: "Host not found" } }), { status: 404 }),
  );
  const { result } = renderHook(
    () => useInstructionPreview(new URLSearchParams({ host_id: "host-a", agent_key: "codex" })),
    { wrapper },
  );
  await waitFor(() => expect(result.current.isError).toBe(true));
  expect(fetch.mock.calls[0]![0]).toBe(
    "/v1/instruction-layers/preview?host_id=host-a&agent_key=codex",
  );
  expect(result.current.error?.message).toBe("Host not found");
  client.clear();
});
it("loads revision history for just the selected layer", async () => {
  const { client, wrapper } = setup();
  fetch.mockResolvedValue(
    new Response(JSON.stringify({ data: [{ id: "revision-a", text: "older" }] }), { status: 200 }),
  );
  const { result } = renderHook(
    () => useInstructionRevisions({ scope: "independent", agent_key: "codex", host_id: "host-a" }),
    { wrapper },
  );
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(fetch.mock.calls[0]![0]).toBe(
    "/v1/instruction-layers/revisions?scope=independent&agent_key=codex&host_id=host-a",
  );
  expect(result.current.data?.data[0]?.text).toBe("older");
  client.clear();
});
