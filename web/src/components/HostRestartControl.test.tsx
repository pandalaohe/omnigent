import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { authenticatedFetch } from "@/lib/identity";
import type * as IdentityModule from "@/lib/identity";
import type { Host } from "@/hooks/useHosts";
import { HostRestartControl } from "./HostRestartControl";

vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  authenticatedFetch: vi.fn(),
}));

const host: Host = {
  host_id: "host_build",
  name: "build-machine",
  owner: "owner",
  status: "online",
  supervised: true,
  capabilities: ["host_restart"],
};
const preview = {
  preview_id: "preview_1",
  host_id: host.host_id,
  supervisor: "launchd",
  running_sessions: ["session_one", "session_unknown"],
  runner_count: 3,
  caller_on_host: true,
};
const status = {
  phase: "draining",
  reason: null,
  offline_seconds: null,
  version: null,
  cut_sessions: [],
};
const fetchMock = vi.mocked(authenticatedFetch);
let client: QueryClient;

function show(fields: Partial<Host> = {}) {
  client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  client.setQueryData(["hosts"], []);
  client.setQueryData(["system-status"], {});
  return render(
    <QueryClientProvider client={client}>
      <HostRestartControl
        host={{ ...host, ...fields }}
        sessionTitles={new Map([["session_one", "Fix sign-in"]])}
      />
    </QueryClientProvider>,
  );
}

function respond(value: unknown, code = 200) {
  return Response.json(value, { status: code });
}

async function openPreview() {
  fireEvent.click(screen.getByRole("button", { name: "Restart" }));
  if (vi.isFakeTimers()) {
    await act(() => vi.advanceTimersByTimeAsync(20));
    expect(screen.getByText("launchd")).toBeVisible();
  } else {
    await screen.findByText("launchd");
  }
}

beforeEach(() => {
  fetchMock.mockReset();
  fetchMock.mockResolvedValue(respond(preview));
});
afterEach(() => {
  cleanup();
  client?.clear();
  vi.useRealTimers();
});

describe("Host restart", () => {
  it.each([
    { status: "offline" as const },
    { supervised: false },
    { supervised: undefined },
    { capabilities: [] },
    { capabilities: undefined },
  ])("hides unsupported or offline hosts (%j)", (fields) => {
    show(fields);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("previews consequences and requires confirmation; cancel has no side effects", async () => {
    show();
    await openPreview();
    expect(screen.getByText("Fix sign-in")).toBeVisible();
    expect(screen.getByText("session_unknown")).toBeVisible();
    expect(screen.getByText("3")).toBeVisible();
    expect(screen.getByText(/calling session runs on this host/)).toBeVisible();
    expect(screen.getByText(/closes all terminals/)).toBeVisible();
    expect(JSON.parse(fetchMock.mock.calls[0][1]!.body as string)).toEqual({
      dry_run: true,
      preview_id: null,
      force: false,
      drain_timeout_s: 120,
      requested_by_session_id: null,
    });
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(screen.getByRole("button", { name: "Restart" })).toHaveFocus());
  });

  it("returns focus to Restart after Escape", async () => {
    show();
    await openPreview();
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    await waitFor(() => expect(screen.getByRole("button", { name: "Restart" })).toHaveFocus());
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("blocks duplicate confirmation and dismissal while the real POST is pending", async () => {
    show();
    await openPreview();
    let accept!: (response: Response) => void;
    fetchMock.mockReturnValueOnce(
      new Promise<Response>((resolve) => {
        accept = resolve;
      }),
    );
    fetchMock.mockResolvedValue(respond({ ...status, phase: "done" }));
    const confirm = screen.getByRole("button", { name: "Restart host" });
    fireEvent.click(confirm);
    fireEvent.click(confirm);
    await waitFor(() => expect(confirm).toBeDisabled());
    expect(fetchMock.mock.calls.filter((call) => call[1]?.method === "POST")).toHaveLength(2);
    expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    expect(screen.getByRole("dialog")).toBeVisible();
    accept(respond({ request_id: "request_1", phase: "draining" }));
    expect(await screen.findByText("Host restarted")).toBeVisible();
    expect(fetchMock.mock.calls.filter((call) => call[1]?.method === "POST")).toHaveLength(2);
  });

  it("expires previews and requires a fresh preview", async () => {
    vi.useFakeTimers();
    show();
    await openPreview();
    await act(() => vi.advanceTimersByTimeAsync(120_001));
    expect(screen.getByRole("button", { name: "Restart host" })).toBeDisabled();
    expect(screen.getByText(/Preview expired/)).toBeVisible();
    vi.useRealTimers();
    fetchMock.mockResolvedValue(respond({ ...preview, preview_id: "preview_2" }));
    fireEvent.click(screen.getByRole("button", { name: "Refresh preview" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Restart host" })).toBeEnabled());
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("keeps preview errors actionable without offering a real restart", async () => {
    fetchMock.mockResolvedValue(respond({ detail: "host is not supervised" }, 409));
    show();
    fireEvent.click(screen.getByRole("button", { name: "Restart" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("host is not supervised");
    expect(screen.getByRole("button", { name: "Restart host" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Refresh preview" })).toBeEnabled();
  });

  it("sends the confirmed preview once and polls through reconnect to done, even offline", async () => {
    vi.useFakeTimers();
    let current = { ...status } as Record<string, unknown>;
    fetchMock.mockImplementation(async (_url, options) => {
      if (options?.method === "POST") {
        return respond(
          JSON.parse(options.body as string).dry_run
            ? preview
            : { request_id: "request_1", phase: "draining" },
        );
      }
      return respond(current);
    });
    const view = show();
    await openPreview();
    fireEvent.click(screen.getByRole("button", { name: "Restart host" }));
    await act(() => vi.advanceTimersByTimeAsync(20));
    expect(screen.getByText("Waiting for running turns to finish")).toBeVisible();
    expect(JSON.parse(fetchMock.mock.calls[1][1]!.body as string)).toMatchObject({
      dry_run: false,
      preview_id: "preview_1",
    });
    current = { ...status, phase: "stopping" };
    await act(() => vi.advanceTimersByTimeAsync(2100));
    expect(screen.getByText("Stopping the host")).toBeVisible();
    current = { ...status, phase: "waiting_reconnect" };
    await act(() => vi.advanceTimersByTimeAsync(2100));
    expect(screen.getByText("Waiting for the host to reconnect")).toBeVisible();
    view.rerender(
      <QueryClientProvider client={client}>
        <HostRestartControl
          host={{ ...host, status: "offline", capabilities: [] }}
          sessionTitles={new Map()}
        />
      </QueryClientProvider>,
    );
    current = {
      ...status,
      phase: "done",
      offline_seconds: 12,
      version: "1.2.3",
      cut_sessions: ["session_one"],
    };
    await act(() => vi.advanceTimersByTimeAsync(2100));
    expect(screen.getByText("Host restarted")).toBeVisible();
    expect(screen.getByText("Time offline: 12 seconds")).toBeVisible();
    expect(screen.getByText("Version: 1.2.3")).toBeVisible();
    expect(screen.getByText("session_one")).toBeVisible();
    const count = fetchMock.mock.calls.length;
    await act(() => vi.advanceTimersByTimeAsync(10_000));
    expect(fetchMock).toHaveBeenCalledTimes(count);
    expect(fetchMock.mock.calls.filter((call) => call[1]?.method === "POST")).toHaveLength(2);
    expect(client.getQueryState(["hosts"])?.isInvalidated).toBe(true);
    expect(client.getQueryState(["system-status"])?.isInvalidated).toBe(true);
  });

  it("shows a failed restart reason and stops polling", async () => {
    vi.useFakeTimers();
    fetchMock
      .mockResolvedValueOnce(respond(preview))
      .mockResolvedValueOnce(respond({ request_id: "request_failed", phase: "draining" }))
      .mockResolvedValue(
        respond({ ...status, phase: "failed", reason: "No reconnect within 120 seconds" }),
      );
    show();
    await openPreview();
    fireEvent.click(screen.getByRole("button", { name: "Restart host" }));
    await act(() => vi.advanceTimersByTimeAsync(20));
    await act(() => vi.advanceTimersByTimeAsync(20));
    expect(screen.getByText("Host restart failed")).toBeVisible();
    expect(screen.getByText("No reconnect within 120 seconds")).toBeVisible();
    const count = fetchMock.mock.calls.length;
    await act(() => vi.advanceTimersByTimeAsync(6000));
    expect(fetchMock).toHaveBeenCalledTimes(count);
  });

  it("does not replay a failed real POST and requires another preview", async () => {
    fetchMock
      .mockResolvedValueOnce(respond(preview))
      .mockRejectedValue(new Error("Connection lost"));
    show();
    await openPreview();
    fireEvent.click(screen.getByRole("button", { name: "Restart host" }));
    expect(await screen.findByText(/Connection lost/)).toBeVisible();
    expect(screen.getByRole("button", { name: "Restart host" })).toBeDisabled();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("recovers status polling after a transient read failure without restarting again", async () => {
    vi.useFakeTimers();
    fetchMock
      .mockResolvedValueOnce(respond(preview))
      .mockResolvedValueOnce(respond({ request_id: "request_1", phase: "draining" }))
      .mockRejectedValueOnce(new Error("Network unavailable"))
      .mockResolvedValue(respond({ ...status, phase: "done" }));
    show();
    await openPreview();
    fireEvent.click(screen.getByRole("button", { name: "Restart host" }));
    await act(() => vi.advanceTimersByTimeAsync(20));
    await act(() => vi.advanceTimersByTimeAsync(20));
    expect(screen.getByText(/Could not check restart status/)).toBeVisible();
    await act(() => vi.advanceTimersByTimeAsync(2100));
    expect(screen.getByText("Host restarted")).toBeVisible();
    expect(screen.queryByText(/Could not check restart status/)).not.toBeInTheDocument();
    expect(fetchMock.mock.calls.filter((call) => call[1]?.method === "POST")).toHaveLength(2);
  });
});
