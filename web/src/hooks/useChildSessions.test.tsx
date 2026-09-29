import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook } from "@testing-library/react";
import type { PropsWithChildren } from "react";
import { describe, expect, it, vi } from "vitest";

import { authenticatedFetch } from "@/lib/identity";
import { fetchChildSessions, useChildSessions } from "./useChildSessions";

vi.mock("@/lib/identity", () => ({
  authenticatedFetch: vi.fn(),
}));

const fetchMock = vi.mocked(authenticatedFetch);

function wrapper({ children }: PropsWithChildren) {
  return (
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      {children}
    </QueryClientProvider>
  );
}

function wireChild(id: string, overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id,
    title: null,
    tool: null,
    session_name: null,
    current_task_status: null,
    busy: false,
    ...overrides,
  };
}

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: "OK",
    json: async () => body,
  } as Response;
}

describe("useChildSessions", () => {
  it("does not fetch child sessions for a provisional conversation", () => {
    const { result } = renderHook(() => useChildSessions("temp:pending-create"), { wrapper });

    expect(result.current.children).toEqual([]);
    expect(authenticatedFetch).not.toHaveBeenCalled();
  });
});

describe("fetchChildSessions", () => {
  it("requests the active zone and follows the cursor to the last page", async () => {
    fetchMock
      .mockResolvedValueOnce(
        jsonResponse({
          object: "list",
          data: [wireChild("c1")],
          has_more: true,
          last_id: "c1",
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          object: "list",
          data: [wireChild("c2")],
          has_more: false,
          last_id: "c2",
        }),
      );

    const children = await fetchChildSessions("conv_parent");

    expect(children.map((child) => child.id)).toEqual(["c1", "c2"]);
    const firstUrl = String(fetchMock.mock.calls[0][0]);
    expect(firstUrl).toContain("zone=active");
    expect(firstUrl).toContain("limit=100");
    expect(firstUrl).not.toContain("after=");
    const secondUrl = String(fetchMock.mock.calls[1][0]);
    expect(secondUrl).toContain("after=c1");
  });

  it("maps the effective placement and agent fields", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        object: "list",
        data: [
          wireChild("c1", {
            host_id: "host-1",
            cwd: "/work/rail",
            git_branch: "feature/scc18",
            harness: "codex-native",
            agent_id: "ag_1",
            agent_name: "codex",
            sub_agent_name: "researcher",
            archived: false,
            archived_at: null,
            created_at: 1700000000,
            warm_state: "warm",
          }),
        ],
        has_more: false,
        last_id: "c1",
      }),
    );

    const [row] = await fetchChildSessions("conv_parent");

    expect(row).toMatchObject({
      host_id: "host-1",
      cwd: "/work/rail",
      git_branch: "feature/scc18",
      harness: "codex-native",
      agent_id: "ag_1",
      agent_name: "codex",
      sub_agent_name: "researcher",
      archived: false,
      archived_at: null,
      created_at: 1700000000,
      warm_state: "warm",
    });
  });

  it("throws when a page claims more with a repeated cursor", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({
        object: "list",
        data: [wireChild("c1")],
        has_more: true,
        last_id: "c1",
      }),
    );

    await expect(fetchChildSessions("conv_parent")).rejects.toThrow(/pagination/i);
  });

  it("throws when a page claims more without a cursor", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({
        object: "list",
        data: [wireChild("c1")],
        has_more: true,
        last_id: null,
      }),
    );

    await expect(fetchChildSessions("conv_parent")).rejects.toThrow(/pagination/i);
  });

  it("surfaces a non-OK response as an error", async () => {
    fetchMock.mockResolvedValue({
      ok: false,
      status: 500,
      statusText: "Internal Server Error",
      json: async () => ({}),
    } as Response);

    await expect(fetchChildSessions("conv_parent")).rejects.toThrow("500");
  });
});
