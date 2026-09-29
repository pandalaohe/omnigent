// useStartHealthCheck: the guidance branches, the request order and bodies of
// a run, and the error surfaces (no navigation until the first message lands).

import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  settings: { current: undefined as unknown },
  hosts: { current: undefined as unknown },
  navigate: vi.fn(),
}));

vi.mock("@/hooks/useSystemStatus", () => ({
  useSystemStatusSettings: () => mocks.settings.current,
}));

vi.mock("@/hooks/useHosts", () => ({ useHosts: () => mocks.hosts.current }));

vi.mock("@/hooks/useIsAdmin", () => ({ useIsAdmin: () => true }));

// The hook consumes `useNavigate` from the routing IoC seam, not
// react-router-dom directly.
vi.mock("@/lib/routing", () => ({ useNavigate: () => mocks.navigate }));

import { useStartHealthCheck } from "./useStartHealthCheck";

const fetchMock = vi.fn();

function mockResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 200 ? "OK" : "Error",
    json: async () => body,
  } as unknown as Response;
}

function settingsData(healthCheck: {
  project_id: string | null;
  host_id: string | null;
  prompt: string | null;
}) {
  return {
    cpu_pct: 85,
    cpu_sustain_min: 10,
    mem_pct: 90,
    disk_pct: 90,
    server_5xx_pct: 5,
    health_check: healthCheck,
    default_health_check_prompt: "Default prompt.",
  };
}

function setTarget(
  healthCheck: Parameters<typeof settingsData>[0] = {
    project_id: "project_1",
    host_id: "host_1",
    prompt: "Check it.",
  },
) {
  mocks.settings.current = { data: settingsData(healthCheck), isError: false };
  mocks.hosts.current = { data: [{ host_id: "host_1", name: "Worker", status: "online" }] };
}

function sessionWire(id: string) {
  return {
    id,
    agent_id: "ag_1",
    runner_id: "runner_1",
    status: "idle",
    created_at: 0,
    labels: {},
    host_id: "host_1",
  };
}

const BRIEF = { text: "BRIEF", generated_at: "2026-09-29T00:00:00Z" };

async function start() {
  const { result } = renderHook(() => useStartHealthCheck());
  await act(async () => {
    await result.current.start();
  });
  return result;
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  mocks.navigate.mockReset();
  setTarget();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("useStartHealthCheck", () => {
  it("shows the unset guidance without any request", async () => {
    setTarget({ project_id: null, host_id: null, prompt: null });
    const result = await start();

    expect(fetchMock).not.toHaveBeenCalled();
    expect(mocks.navigate).not.toHaveBeenCalled();
    expect(result.current.notice).toEqual({
      kind: "unset",
      message: "Set the ops project and host in Settings → System status.",
    });
  });

  it("shows host_offline without creating anything", async () => {
    mocks.hosts.current = {
      data: [{ host_id: "host_1", name: "Worker", status: "offline" }],
    };
    const result = await start();

    expect(fetchMock).not.toHaveBeenCalled();
    expect(mocks.navigate).not.toHaveBeenCalled();
    expect(result.current.notice?.kind).toBe("host_offline");
    expect(result.current.notice?.message).toContain("Worker");
  });

  it("requests brief, project session and first message, then navigates", async () => {
    fetchMock
      .mockResolvedValueOnce(mockResponse(BRIEF))
      .mockResolvedValueOnce(mockResponse(sessionWire("conv_1")))
      .mockResolvedValueOnce(mockResponse({ queued: true, forwarded: true }));

    const result = await start();

    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(fetchMock.mock.calls[0][0]).toBe("/v1/system/brief");

    const [createUrl, createInit] = fetchMock.mock.calls[1] as [string, RequestInit];
    expect(createUrl).toBe("/v1/sessions");
    expect(createInit.method).toBe("POST");
    const createBody = JSON.parse(String(createInit.body)) as Record<string, unknown>;
    expect(createBody).toMatchObject({
      project_id: "project_1",
      host_id: "host_1",
      initial_items: [],
    });
    expect(createBody.title).toMatch(/^Health check \d{4}-\d{2}-\d{2} \d{2}:\d{2}$/);
    expect(createBody).not.toHaveProperty("agent_id");
    expect(createBody).not.toHaveProperty("workspace");

    const [eventUrl, eventInit] = fetchMock.mock.calls[2] as [string, RequestInit];
    expect(eventUrl).toBe("/v1/sessions/conv_1/events");
    expect(JSON.parse(String(eventInit.body))).toEqual({
      type: "message",
      data: {
        role: "user",
        content: [{ type: "input_text", text: "Check it.\n\nBRIEF" }],
      },
    });

    expect(result.current.notice).toBeNull();
    expect(mocks.navigate).toHaveBeenCalledWith("/c/conv_1");
  });

  it("surfaces a create failure with the server message and posts no event", async () => {
    fetchMock
      .mockResolvedValueOnce(mockResponse(BRIEF))
      .mockResolvedValueOnce(
        mockResponse({ error: { message: "default agent cannot be launched" } }, 400),
      );

    const result = await start();

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(result.current.notice).toEqual({
      kind: "error",
      message: "default agent cannot be launched",
    });
    expect(mocks.navigate).not.toHaveBeenCalled();
  });

  it.each([403, 404])("adds the ownership hint on create %i", async (status) => {
    fetchMock
      .mockResolvedValueOnce(mockResponse(BRIEF))
      .mockResolvedValueOnce(mockResponse({ error: { message: "no such project" } }, status));

    const result = await start();

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(result.current.notice?.message).toBe(
      "no such project The ops project and host must belong to you.",
    );
  });

  it("links the created session when the first message fails", async () => {
    fetchMock
      .mockResolvedValueOnce(mockResponse(BRIEF))
      .mockResolvedValueOnce(mockResponse(sessionWire("conv_1")))
      .mockResolvedValueOnce(mockResponse({ error: { message: "runner unavailable" } }, 503));

    const result = await start();

    expect(result.current.notice).toEqual({
      kind: "error",
      message: "runner unavailable",
      sessionId: "conv_1",
    });
    expect(mocks.navigate).not.toHaveBeenCalled();
  });

  it("navigates when the event is persisted but not forwarded", async () => {
    fetchMock
      .mockResolvedValueOnce(mockResponse(BRIEF))
      .mockResolvedValueOnce(mockResponse(sessionWire("conv_1")))
      .mockResolvedValueOnce(mockResponse({ queued: true, forwarded: false }));

    await start();

    expect(mocks.navigate).toHaveBeenCalledWith("/c/conv_1");
  });
});
