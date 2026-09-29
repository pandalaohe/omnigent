// Rendering contract for /system: findings, the admin-only server card, host
// state variants, the expandable process tree with subtree totals, and the
// measured monitor-overhead footer.

import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  status: { current: undefined as unknown },
  history: new Map<string, unknown[]>(),
}));

vi.mock("@/hooks/useSystemStatus", () => ({
  useSystemStatus: () => mocks.status.current,
  useSystemHistory: (target: string | null) => ({
    data: target === null ? undefined : (mocks.history.get(target) ?? []),
  }),
  // Threshold lines are covered by the settings-form tests; the page only
  // needs the hook to resolve (members get no settings at all).
  useSystemStatusSettings: () => ({ data: null }),
}));

vi.mock("@/hooks/useIsAdmin", () => ({ useIsAdmin: () => true }));

vi.mock("@/hooks/useSidebarData", () => ({
  useLoadedConversations: () => ({
    data: {
      pages: [{ data: [{ id: "conv_hot", title: "Fix login timeout" }] }],
    },
    isLoading: false,
  }),
}));

import { SystemStatusPage } from "./SystemStatusPage";

const MB = 1024 * 1024;

function hostSnapshot() {
  return {
    sampled_at: "2026-09-29T09:25:00+00:00",
    interval_s: 60,
    machine: {
      cpu_pct: 91,
      mem_used: 21 * MB,
      mem_total: 32 * MB,
      disk_used: 71,
      disk_total: 100,
      load1: 9.8,
    },
    processes: [
      {
        pid: 100,
        ppid: 999,
        name: "runner",
        role: "runner",
        session_id: "conv_hot",
        cpu_pct: 5,
        rss: 100 * MB,
      },
      {
        pid: 101,
        ppid: 100,
        name: "harness",
        role: "harness",
        session_id: null,
        cpu_pct: 10,
        rss: 200 * MB,
      },
      {
        pid: 0,
        ppid: 100,
        name: "2 other processes",
        role: "folded",
        session_id: null,
        cpu_pct: 1,
        rss: 10 * MB,
      },
      {
        pid: 1,
        ppid: 0,
        name: "host daemon",
        role: "daemon",
        session_id: null,
        cpu_pct: 1,
        rss: 50 * MB,
      },
    ],
    runner_count: 1,
    sampler_cpu_ms: 12,
    monitor_rss_delta: 18 * MB,
  };
}

function makeView({ server }: { server: boolean }) {
  return {
    revision: 4,
    level: "amber",
    findings: [
      {
        id: "host_1:cpu",
        target: "host_1",
        kind: "cpu",
        level: "amber",
        since: 0,
        detail: "cpu above 85% for 10 minutes",
        top_session: "conv_hot",
      },
    ],
    server:
      server === false
        ? null
        : {
            state: "online",
            since: 0,
            last_point: {
              t: 0,
              cpu: 12,
              rss: 410 * MB,
              in_flight: 3,
              websockets: 14,
              req: 1000,
              err: 1,
              load1: 1.2,
              disk_pct: 40,
            },
          },
    hosts: [
      {
        host_id: "host_1",
        owner: "alice",
        name: "Laptop",
        state: "online",
        since: 0,
        last_snapshot: hostSnapshot(),
        last_runner_count: 1,
      },
      {
        host_id: "host_2",
        owner: "alice",
        name: "Old Mac",
        state: "offline",
        since: Date.now() / 1000 - 7300,
        last_snapshot: null,
        last_runner_count: 0,
      },
      {
        host_id: "host_3",
        owner: "alice",
        name: "Coaster",
        state: "needs_update",
        since: 0,
        last_snapshot: null,
        last_runner_count: 0,
      },
    ],
    monitor_overhead: {
      server: { cpu_pct: 0.1, mem_estimate_bytes: 6 * MB, estimated: true },
      hosts: { host_1: { cpu_pct: 0.2, rss_delta: 18 * MB } },
    },
  };
}

function renderPage() {
  return render(
    <MemoryRouter>
      <SystemStatusPage />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  mocks.history.clear();
  mocks.history.set("host_1", [{ t: 0, cpu_avg: 10 }]);
  mocks.history.set("server", [{ t: 0, cpu: 12 }]);
});

afterEach(cleanup);

describe("SystemStatusPage", () => {
  it("renders findings, the server card, host states and the overhead footer", () => {
    mocks.status.current = {
      data: makeView({ server: true }),
      isLoading: false,
      isError: false,
      error: null,
    };
    renderPage();

    expect(screen.getByText(/cpu above 85% for 10 minutes/)).toBeInTheDocument();

    const serverCard = screen.getByTestId("system-status-server-card");
    expect(within(serverCard).getByText("12.0%")).toBeInTheDocument();
    expect(within(serverCard).getByText("14")).toBeInTheDocument();

    expect(within(screen.getByTestId("host-card-host_1")).getByText("Laptop")).toBeInTheDocument();
    expect(screen.getByText("Last seen 2h ago.")).toBeInTheDocument();
    expect(screen.getByText("Update host to see resources.")).toBeInTheDocument();

    const overhead = screen.getByTestId("system-status-overhead");
    expect(overhead).toHaveTextContent("Monitor overhead");
    expect(overhead).toHaveTextContent("memory (estimate)");
    expect(overhead).toHaveTextContent("resident increase (upper bound)");
  });

  it("hides the server card for a member (server: null)", () => {
    mocks.status.current = {
      data: makeView({ server: false }),
      isLoading: false,
      isError: false,
      error: null,
    };
    renderPage();

    expect(screen.queryByTestId("system-status-server-card")).toBeNull();
    expect(within(screen.getByTestId("host-card-host_1")).getByText("Laptop")).toBeInTheDocument();
  });

  it("nests the process tree and totals a parent's own and subtree CPU / RSS", () => {
    mocks.status.current = {
      data: makeView({ server: false }),
      isLoading: false,
      isError: false,
      error: null,
    };
    renderPage();

    fireEvent.click(screen.getByRole("button", { name: "View processes" }));
    const tree = screen.getByTestId("process-tree");

    // runner: own 5 + harness 10 + folded 1 = 16.0% and 100 + 200 + 10 MB.
    const runnerRow = within(tree).getByText("runner").closest("tr");
    expect(runnerRow).not.toBeNull();
    expect(within(runnerRow as HTMLElement).getByText("16.0%")).toBeInTheDocument();
    expect(within(runnerRow as HTMLElement).getByText("310 MB")).toBeInTheDocument();

    const harnessRow = within(tree).getByText("harness").closest("tr");
    expect(harnessRow).toHaveAttribute("data-depth", "1");
    expect(within(tree).getByText("2 other processes")).toBeInTheDocument();
    const sessionLink = within(tree).getByRole("link", { name: "Fix login timeout" });
    expect(sessionLink).toHaveAttribute("href", "/c/conv_hot");
  });
});
