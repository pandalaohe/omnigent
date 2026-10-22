import type { InstructionLayers } from "./useInstructionLayers";
import type { Host } from "@/hooks/useHosts";
import type { ReactNode } from "react";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { authenticatedFetch } from "@/lib/identity";
import {
  instructionRegistryItems,
  instructionGridAxes,
  hasInstructionText,
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

it("orders explicit axes first, then held axes, skipping unknown and removed hosts", () => {
  const hosts = ["host-a", "host-b", "host-c"].map((host_id) => ({ host_id })) as Host[];
  const data = {
    data: [
      { scope: "agent", agent_key: "codex", text: "C" },
      { scope: "independent", agent_key: "claude", host_id: "host-a", text: "I" },
      {
        scope: "independent",
        agent_key: "removed",
        host_id: "host-b",
        text: "R",
        host_removed: true,
      },
      { scope: "independent", agent_key: "missing", host_id: "unknown", text: "M" },
      { scope: "independent", agent_key: "unknown", host_id: "host-c", text: "U" },
      { scope: "agent", agent_key: "empty", text: "\u0085\u001c" },
      { scope: "host", host_id: "host-b", text: "Host" },
      { scope: "host", host_id: "unknown", text: "Missing host" },
    ],
    agents: ["codex", "claude", "removed", "missing", "empty", "new"].map((key) => ({
      key,
      display_name: key,
    })),
  } as InstructionLayers;
  const axes = instructionGridAxes(data, hosts, {
    hosts: ["unknown", "host-b"],
    agents: ["unknown", "new"],
  });
  expect(axes.agents.map((agent) => agent.key)).toEqual(["new", "codex", "claude"]);
  expect(axes.hosts.map((host) => host.host_id)).toEqual(["host-b", "host-a"]);
  expect(axes.agentLayers.get("claude")).toEqual([data.data[1]]);
  expect(axes.hostLayers.get("host-b")).toEqual([data.data[6]]);
  expect(axes.hostLayers.get("host-a")).toEqual([data.data[1]]);
});
it.each([
  [null, false],
  ["", false],
  [" \n\u0085\u001c\u001f", false],
  ["\ufeff", true],
  ["Text", true],
])("uses the server blank rule for %j", (text, held) => {
  expect(hasInstructionText(text)).toBe(held);
});
it("uses descriptor harnesses for section and owner-host deployment", () => {
  const agents = [{ key: "plugin", display_name: "Plugin", harnesses: ["acp:plugin"] }];
  const capabilities = { acp: { integration_mode: "acp-subprocess" } };
  const items = instructionRegistryItems(agents, capabilities, [
    {
      host_id: "host-a",
      name: "Example",
      owner: "local",
      status: "offline",
      configured_harnesses: { "acp:plugin": false },
    } satisfies Host,
  ]);
  expect(items[0]).toMatchObject({ key: "plugin", section: "acp", deployed: false });
  expect(instructionRegistryItems(agents, capabilities, [])[0].deployed).toBe(true);
});
