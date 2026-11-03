// Rendering contract for /system: findings, the admin-only server card, host
// state variants, the expandable process tree with subtree totals and role
// labels, the measured monitor-overhead footer, and each host's load row with
// its lines and the admin's per-host edit.

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type * as SystemStatusModule from "@/hooks/useSystemStatus";
import type * as HostsModule from "@/hooks/useHosts";
import type { Host } from "@/hooks/useHosts";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";
import type * as IdentityModule from "@/lib/identity";

const mocks = vi.hoisted(() => ({
  status: { current: undefined as unknown },
  history: new Map<string, unknown[]>(),
  isAdmin: { current: true },
  healthCheck: {
    current: { start: vi.fn(), pending: false, notice: null } as {
      start: ReturnType<typeof vi.fn>;
      pending: boolean;
      notice: unknown;
    },
  },
  settings: { current: { data: null } as { data: unknown } },
  update: { mutate: vi.fn(), isPending: false },
  hosts: { current: [] as Host[] },
}));

vi.mock("@/hooks/useSystemStatus", async (importOriginal) => ({
  // The line rule and parser are the real ones; only the queries are faked.
  ...(await importOriginal<typeof SystemStatusModule>()),
  useSystemStatus: () => mocks.status.current,
  useSystemHistory: (target: string | null) => ({
    data: target === null ? undefined : (mocks.history.get(target) ?? []),
  }),
  // Threshold lines are covered by the settings-form tests; the page only
  // needs the hook to resolve (members get no settings at all).
  useSystemStatusSettings: () => mocks.settings.current,
  useUpdateSystemStatusSettings: () => mocks.update,
}));

vi.mock("@/hooks/useIsAdmin", () => ({ useIsAdmin: () => mocks.isAdmin.current }));
vi.mock("@/hooks/useHosts", async (importOriginal) => ({
  ...(await importOriginal<typeof HostsModule>()),
  useHosts: () => ({ data: mocks.hosts.current }),
  // The idle-CLI fold on each owned card is covered by its own test file; here
  // it only needs to exist without a request.
  useHostCliRetention: () => ({ data: undefined, isLoading: false, isError: false }),
  useHostCliReleases: () => ({ data: [], isLoading: false, isError: false }),
  useReplaceHostCliRetention: () => ({ mutateAsync: vi.fn(), isPending: false, error: null }),
  useResetHostCliRetention: () => ({ mutateAsync: vi.fn(), isPending: false, error: null }),
}));
vi.mock("@/lib/archiveWorktreePreferences", () => ({
  useArchiveWorktreePreference: () => ({ preference: { data: { mode: "never" } } }),
}));
vi.mock("@/hooks/useAgents", () => ({ useAgents: () => ({ data: [] }) }));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  authenticatedFetch: vi.fn(),
}));

vi.mock("@/hooks/useStartHealthCheck", () => ({
  useStartHealthCheck: () => mocks.healthCheck.current,
}));

// One stable reference, like the real query cache: a fresh object per render
// would defeat the page's memoized process tree.
vi.mock("@/hooks/useSidebarData", () => {
  const loaded = {
    data: {
      pages: [{ data: [{ id: "conv_hot", title: "Fix login timeout" }] }],
    },
    isLoading: false,
  };
  return { useLoadedConversations: () => loaded };
});

import { fanOutLabels, SystemStatusPage } from "./SystemStatusPage";

const MB = 1024 * 1024;

function hostSnapshot() {
  const now = Date.now() / 1000;
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
        name: "python3.12",
        role: "runner",
        session_id: "conv_hot",
        cpu_pct: 5,
        rss: 100 * MB,
        started_at: now - (2 * 3600 + 30 * 60),
      },
      {
        pid: 101,
        ppid: 100,
        name: "claude",
        role: "harness",
        session_id: null,
        cpu_pct: 10,
        rss: 200 * MB,
        started_at: now - (3 * 86400 + 5 * 3600),
      },
      {
        pid: 0,
        ppid: 100,
        name: "2 other processes",
        role: "folded",
        session_id: null,
        cpu_pct: 1,
        rss: 10 * MB,
        started_at: null,
      },
      {
        pid: 1,
        ppid: 0,
        name: "python3.12",
        role: "daemon",
        session_id: null,
        cpu_pct: 1,
        rss: 50 * MB,
      },
      {
        pid: 2,
        ppid: 1,
        name: "python3.12",
        role: "zygote",
        session_id: null,
        cpu_pct: 0.5,
        rss: 20 * MB,
        started_at: now - 90,
      },
      {
        pid: 3,
        ppid: 1,
        name: "tmux",
        role: "tmux",
        session_id: "conv_hot",
        cpu_pct: 0.2,
        rss: 5 * MB,
        started_at: now - 30,
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
  vi.mocked(authenticatedFetch).mockReset();
  mocks.hosts.current = [];
  mocks.history.clear();
  mocks.history.set("host_1", [{ t: 0, cpu_avg: 10 }]);
  mocks.history.set("server", [{ t: 0, cpu: 12 }]);
  mocks.isAdmin.current = true;
  mocks.healthCheck.current = { start: vi.fn(), pending: false, notice: null };
  mocks.settings.current = { data: null };
  mocks.update.mutate = vi.fn();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("SystemStatusPage", () => {
  it.each(["online", "offline", "unsupported"] as const)(
    "offers Terminal only on the owned online capable card: %s",
    (state) => {
      mocks.isAdmin.current = false;
      mocks.status.current = {
        data: makeView({ server: false }),
        isLoading: false,
        isError: false,
      };
      mocks.hosts.current = [
        {
          host_id: "host_1",
          name: "host-a",
          owner: "alice",
          status: state === "offline" ? "offline" : "online",
          capabilities: state === "unsupported" ? [] : ["host_terminals_v1"],
        },
      ];
      const client = new QueryClient();
      render(
        <QueryClientProvider client={client}>
          <MemoryRouter>
            <SystemStatusPage />
          </MemoryRouter>
        </QueryClientProvider>,
      );
      const link = within(screen.getByTestId("host-card-host_1")).queryByRole("link", {
        name: "Terminal",
      });
      if (state === "online") expect(link).toHaveAttribute("href", "/hosts/host_1/terminals");
      else expect(link).toBeNull();
      expect(
        within(screen.getByTestId("host-card-host_2")).queryByRole("link", { name: "Terminal" }),
      ).toBeNull();
      client.clear();
    },
  );
  it("keeps an in-flight restart mounted when the system-status refresh fails", async () => {
    const data = makeView({ server: false });
    mocks.status.current = { data, isLoading: false, isError: false };
    mocks.hosts.current = [
      {
        host_id: "host_1",
        name: "Laptop",
        owner: "alice",
        status: "online",
        supervised: true,
        capabilities: ["host_restart"],
      },
    ];
    const fetchMock = vi.mocked(authenticatedFetch);
    fetchMock
      .mockResolvedValueOnce(
        Response.json({
          preview_id: "preview_1",
          host_id: "host_1",
          supervisor: "launchd",
          running_sessions: [],
          runner_count: 1,
          caller_on_host: false,
        }),
      )
      .mockResolvedValueOnce(Response.json({ request_id: "request_1", phase: "draining" }))
      .mockResolvedValue(
        Response.json({
          phase: "draining",
          reason: null,
          offline_seconds: null,
          version: null,
          cut_sessions: [],
        }),
      );
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const content = (
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <SystemStatusPage />
        </MemoryRouter>
      </QueryClientProvider>
    );
    const view = render(content);
    fireEvent.click(screen.getByRole("button", { name: "Restart" }));
    await screen.findByText("launchd");
    fireEvent.click(screen.getByRole("button", { name: "Restart host" }));
    await screen.findByText("Waiting for running turns to finish");
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    mocks.status.current = {
      data,
      isLoading: false,
      isError: true,
      error: new Error("Network unavailable"),
    };
    // A fresh element forces the page to observe the changed query snapshot.
    view.rerender(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <SystemStatusPage />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(screen.getByText("Waiting for running turns to finish")).toBeVisible();
    expect(screen.getByText(/Unable to refresh system status/)).toBeInTheDocument();
    fireEvent.click(
      within(screen.getByRole("dialog")).getAllByRole("button", { name: "Close" })[0]!,
    );
    expect(screen.getByRole("button", { name: "View restart" })).toBeVisible();
    expect(fetchMock.mock.calls.filter((call) => call[1]?.method === "POST")).toHaveLength(2);
    view.unmount();
    client.clear();
  });
  it("offers restart only on an owned capable host, including for a non-admin owner", () => {
    mocks.isAdmin.current = false;
    mocks.status.current = { data: makeView({ server: false }), isLoading: false, isError: false };
    mocks.hosts.current = [
      {
        host_id: "host_1",
        name: "Laptop",
        owner: "alice",
        status: "online",
        supervised: true,
        capabilities: ["host_restart"],
      },
    ];
    const client = new QueryClient();
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <SystemStatusPage />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(
      within(screen.getByTestId("host-card-host_1")).getByRole("button", { name: "Restart" }),
    ).toBeVisible();
    expect(
      within(screen.getByTestId("host-card-host_2")).queryByRole("button", { name: "Restart" }),
    ).not.toBeInTheDocument();
    client.clear();
  });
  it("renders findings, the server card and host states, with no page subtitle", () => {
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
    // Disk is the server's data-dir usage, the metric its alert line governs.
    expect(within(serverCard).getByText("40.0%")).toBeInTheDocument();

    expect(within(screen.getByTestId("host-card-host_1")).getByText("Laptop")).toBeInTheDocument();
    expect(screen.getByText("Last seen 2h ago.")).toBeInTheDocument();
    expect(screen.getByText("Update host to see resources.")).toBeInTheDocument();

    // The subtitle that restated the page is gone; the overhead table lives in
    // Monitor settings now.
    expect(screen.queryByText(/Resources on this server and on every visible host/)).toBeNull();
    expect(screen.queryByTestId("system-status-overhead")).toBeNull();
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
    const sessionLink = within(runnerRow as HTMLElement).getByRole("link", {
      name: "Fix login timeout",
    });
    expect(sessionLink).toHaveAttribute("href", "/c/conv_hot");
  });

  it("keeps a long session id inside a fixed-layout table in a scroll container", () => {
    const sessionId = "0123456789abcdef0123456789abcdef";
    const view = makeView({ server: false });
    const snapshot = view.hosts[0].last_snapshot;
    if (snapshot === null) throw new Error("host_1 is expected to have a snapshot");
    snapshot.processes[0].session_id = sessionId;
    mocks.status.current = {
      data: view,
      isLoading: false,
      isError: false,
      error: null,
    };
    renderPage();

    fireEvent.click(screen.getByRole("button", { name: "View processes" }));

    const scroll = screen.getByTestId("process-tree-scroll");
    const tree = screen.getByTestId("process-tree");
    expect(scroll).toContainElement(tree);
    expect(tree).toHaveClass("table-fixed");

    // No sidebar title for this id, so the id itself is the label and tooltip.
    const sessionLink = within(tree).getByRole("link", { name: sessionId });
    expect(sessionLink).toHaveAttribute("title", sessionId);
    expect(sessionLink).toHaveAttribute("href", `/c/${sessionId}`);
  });

  it("labels process rows by role and keeps the OS name and pid in the title", () => {
    mocks.status.current = {
      data: makeView({ server: false }),
      isLoading: false,
      isError: false,
      error: null,
    };
    renderPage();

    fireEvent.click(screen.getByRole("button", { name: "View processes" }));

    expect(screen.getByText("host daemon")).toHaveAttribute("title", "python3.12 · pid 1");
    expect(screen.getByText("runner zygote")).toHaveAttribute("title", "python3.12 · pid 2");
    expect(screen.getByText("runner")).toHaveAttribute("title", "python3.12 · pid 100");
    expect(screen.getByText("harness")).toHaveAttribute("title", "claude · pid 101");
    expect(screen.getByText("tmux")).toHaveAttribute("title", "tmux · pid 3");
    expect(screen.getByText("2 other processes")).toHaveAttribute("title", "2 other processes");
  });

  it("renders each row's uptime", () => {
    // Uptime is minute-granular, so start on a minute boundary for exact labels.
    vi.useFakeTimers({ now: new Date("2026-09-30T12:00:00Z") });
    mocks.status.current = {
      data: makeView({ server: false }),
      isLoading: false,
      isError: false,
      error: null,
    };
    renderPage();

    fireEvent.click(screen.getByRole("button", { name: "View processes" }));
    const tree = screen.getByTestId("process-tree");
    const rowFor = (label: string) => within(tree).getByText(label).closest("tr") as HTMLElement;
    const uptimeCell = (label: string) =>
      rowFor(label).querySelector("td:last-child") as HTMLElement;

    expect(uptimeCell("runner")).toHaveTextContent("2h 30m");
    expect(uptimeCell("harness")).toHaveTextContent("3d 5h");
    expect(uptimeCell("runner zygote")).toHaveTextContent("1m");
    expect(uptimeCell("tmux")).toHaveTextContent("<1m");
    // A null start time (folded row) and a missing one (older host) both show "—".
    expect(uptimeCell("2 other processes")).toHaveTextContent("—");
    expect(uptimeCell("host daemon")).toHaveTextContent("—");
  });

  it("advances a process row's uptime once a minute despite the memoized tree", () => {
    vi.useFakeTimers({ now: new Date("2026-09-30T12:00:00Z") });
    const nowMs = Date.now();
    const view = makeView({ server: false });
    const snapshot = view.hosts[0].last_snapshot;
    if (snapshot === null) throw new Error("host_1 is expected to have a snapshot");
    snapshot.processes[0].started_at = nowMs / 1000 - 120;
    mocks.status.current = { data: view, isLoading: false, isError: false, error: null };
    renderPage();

    fireEvent.click(screen.getByRole("button", { name: "View processes" }));
    const tree = screen.getByTestId("process-tree");
    const uptimeCell = (label: string) =>
      within(tree).getByText(label).closest("tr")?.querySelector("td:last-child") as HTMLElement;

    expect(uptimeCell("runner")).toHaveTextContent("2m");

    act(() => {
      vi.advanceTimersByTime(60_000);
    });
    expect(uptimeCell("runner")).toHaveTextContent("3m");
  });

  it("offers Run health check to admins only", () => {
    mocks.status.current = {
      data: makeView({ server: true }),
      isLoading: false,
      isError: false,
      error: null,
    };

    mocks.isAdmin.current = false;
    const member = renderPage();
    expect(screen.queryByRole("button", { name: "Run health check" })).toBeNull();
    member.unmount();

    mocks.isAdmin.current = true;
    renderPage();
    // One action, in the page header: the findings banner keeps no copy of it.
    expect(screen.getAllByRole("button", { name: "Run health check" })).toHaveLength(1);
  });

  it("shows the host sample age while the snapshot is fresh", () => {
    vi.useFakeTimers({ now: new Date("2026-09-30T12:00:00Z") });
    const nowMs = Date.now();
    const view = makeView({ server: false });
    const snapshot = view.hosts[0].last_snapshot;
    if (snapshot === null) throw new Error("host_1 is expected to have a snapshot");
    snapshot.sampled_at = new Date(nowMs - 4_000).toISOString();
    snapshot.interval_s = 10;
    mocks.status.current = { data: view, isLoading: false, isError: false, error: null };
    mocks.history.set("host_1", [{ t: nowMs / 1000 - 40, cpu_avg: 10 }]);
    renderPage();

    const freshness = within(screen.getByTestId("host-card-host_1")).getByTestId("freshness");
    expect(freshness).toHaveTextContent("Updated 4 s ago");
    expect(freshness).not.toHaveAttribute("data-stale");
  });

  it("marks a stale host sample and its sparkline's latest point amber", () => {
    vi.useFakeTimers({ now: new Date("2026-09-30T12:00:00Z") });
    const nowMs = Date.now();
    const view = makeView({ server: false });
    const snapshot = view.hosts[0].last_snapshot;
    if (snapshot === null) throw new Error("host_1 is expected to have a snapshot");
    snapshot.sampled_at = new Date(nowMs - 200_000).toISOString();
    snapshot.interval_s = 10;
    mocks.status.current = { data: view, isLoading: false, isError: false, error: null };
    mocks.history.set("host_1", [
      { t: nowMs / 1000 - 3_600, cpu_avg: 10 },
      { t: nowMs / 1000 - 60, cpu_avg: 20 },
    ]);
    renderPage();

    const hostCard = screen.getByTestId("host-card-host_1");
    const freshness = within(hostCard).getByTestId("freshness");
    expect(freshness).toHaveTextContent("Stale — last sample 3 min ago");
    expect(freshness).toHaveAttribute("data-stale", "true");
    expect(within(hostCard).getByTestId("sparkline-latest")).toHaveClass("bg-amber-500");
  });

  it("shows the server point's age in the server card", () => {
    vi.useFakeTimers({ now: new Date("2026-09-30T12:00:00Z") });
    const nowMs = Date.now();
    const view = makeView({ server: true });
    if (view.server === null) throw new Error("the server card is expected");
    view.server.last_point.t = nowMs / 1000 - 23;
    mocks.status.current = { data: view, isLoading: false, isError: false, error: null };
    renderPage();

    const serverCard = screen.getByTestId("system-status-server-card");
    expect(within(serverCard).getByTestId("freshness")).toHaveTextContent("Updated 23 s ago");
  });

  it("labels the host sparkline axis from its first history point", () => {
    vi.useFakeTimers({ now: new Date("2026-09-30T12:00:00Z") });
    const nowMs = Date.now();
    mocks.status.current = {
      data: makeView({ server: false }),
      isLoading: false,
      isError: false,
      error: null,
    };
    mocks.history.set("host_1", [
      { t: nowMs / 1000 - 3 * 3_600, cpu_avg: 10 },
      { t: nowMs / 1000 - 60, cpu_avg: 20 },
    ]);
    renderPage();

    const axis = within(screen.getByTestId("host-card-host_1")).getByTestId("sparkline-axis");
    expect(axis).toHaveTextContent("3 h ago");
    expect(axis).toHaveTextContent("now");
  });

  it("ages the freshness label as seconds tick by", () => {
    vi.useFakeTimers({ now: new Date("2026-09-30T12:00:00Z") });
    const nowMs = Date.now();
    const view = makeView({ server: false });
    const snapshot = view.hosts[0].last_snapshot;
    if (snapshot === null) throw new Error("host_1 is expected to have a snapshot");
    snapshot.sampled_at = new Date(nowMs - 4_000).toISOString();
    snapshot.interval_s = 10;
    mocks.status.current = { data: view, isLoading: false, isError: false, error: null };
    renderPage();

    const freshness = within(screen.getByTestId("host-card-host_1")).getByTestId("freshness");
    expect(freshness).toHaveTextContent("Updated 4 s ago");

    act(() => {
      vi.advanceTimersByTime(5_000);
    });
    expect(freshness).toHaveTextContent("Updated 9 s ago");
  });
});

type LoadFields = Partial<
  Pick<
    SystemStatusModule.SystemStatusHost,
    | "state"
    | "load_per_core"
    | "load_equiv"
    | "estimate"
    | "cpu_count"
    | "level"
    | "reason"
    | "lines"
    | "lines_source"
    | "queued_children"
    | "mem_pct"
  >
>;

/** host_1 with load fields as the server reports them (12 cores, default lines 4 / 5). */
function viewWithLoad(fields: LoadFields) {
  const view = makeView({ server: false });
  Object.assign(view.hosts[0], {
    load_per_core: 0.93,
    load_equiv: 11.1,
    estimate: false,
    cpu_count: 12,
    level: "normal",
    reason: null,
    lines: { heavy: 4, new_session: 5 },
    lines_source: "default",
    queued_children: 0,
    mem_pct: 66,
    ...fields,
  });
  return view;
}

function renderLoad(fields: LoadFields) {
  mocks.status.current = {
    data: viewWithLoad(fields),
    isLoading: false,
    isError: false,
    error: null,
  };
  renderPage();
  return screen.getByTestId("host-load-host_1");
}

describe("SystemStatusPage host load", () => {
  it.each([
    ["spare", 0.4, "Spare capacity"],
    ["normal", 2.1, "Normal"],
    ["heavy_hold", 4.3, "Above heavy-work line"],
    ["new_session_hold", 6.2, "Above new-session line"],
    ["unknown", null, "Incomplete data"],
  ] as const)("names level %s in text beside its colour", (level, perCore, label) => {
    const row = renderLoad({ level, load_per_core: perCore });

    const levelText = within(row).getByTestId("host-load-level");
    expect(levelText).toHaveTextContent(label);
    expect(levelText).toHaveAttribute("data-level", level);
    expect(row).toHaveTextContent(
      perCore === null ? "No reading per core" : `${perCore.toFixed(2)} per core`,
    );
  });

  it("draws the ruler with a numbered tick per line and the load on it", () => {
    const row = renderLoad({});

    expect(row).toHaveTextContent("0.93 per core");
    expect(within(row).getByTestId("host-load-equiv")).toHaveTextContent("(11.1 / 12 cores)");
    expect(within(row).getByTestId("host-load-equiv")).not.toHaveAttribute("title");
    expect(within(row).getByTestId("host-load-tick-heavy")).toHaveTextContent("4");
    expect(within(row).getByTestId("host-load-tick-new_session")).toHaveTextContent("5");
    // The lines are named under the scale, and the old Default word is gone.
    expect(row).toHaveTextContent("Heavy work");
    expect(row).toHaveTextContent("No new session");
    expect(row).not.toHaveTextContent("Default");
    // Scale runs to 1.5 x the new-session line: 4 / 7.5 and 5 / 7.5.
    expect(within(row).getByTestId("host-load-heavy-mark").style.left).toBe(`${(4 / 7.5) * 100}%`);
    expect(within(row).getByTestId("host-load-new-session-mark").style.left).toBe(
      `${(5 / 7.5) * 100}%`,
    );
    expect(within(row).getByTestId("host-load-marker").style.left).toBe(`${(0.93 / 7.5) * 100}%`);
    // A host following the default carries no own-lines mark.
    expect(within(row).queryByTestId("host-load-own-lines")).toBeNull();
    // The old raw-load metric is gone from the metric grid.
    expect(within(screen.getByTestId("host-card-host_1")).queryByText("9.8")).toBeNull();
  });

  it("marks a host that sets its own lines", () => {
    const row = renderLoad({ lines: { heavy: 2, new_session: 8 }, lines_source: "host" });
    expect(within(row).getByTestId("host-load-own-lines")).toHaveTextContent("Own lines");
    expect(within(row).getByTestId("host-load-tick-heavy")).toHaveTextContent("2");
  });

  it("pins a load past the scale to its end and drops the marker without a reading", () => {
    const row = renderLoad({ level: "new_session_hold", load_per_core: 21.2 });
    expect(within(row).getByTestId("host-load-marker").style.left).toBe("100%");
    cleanup();

    const unknown = renderLoad({ level: "unknown", load_per_core: null, load_equiv: null });
    expect(within(unknown).queryByTestId("host-load-marker")).toBeNull();
    expect(within(unknown).queryByTestId("host-load-equiv")).toBeNull();
    expect(within(unknown).getByTestId("host-load-scale")).toBeInTheDocument();
    expect(unknown).toHaveTextContent("No reading per core");
  });

  it("marks a Windows load as an estimate", () => {
    const row = renderLoad({ estimate: true, load_equiv: 14, cpu_count: 24, load_per_core: 0.58 });

    const equiv = within(row).getByTestId("host-load-equiv");
    expect(equiv).toHaveTextContent("(est. 14.0 / 24 cores)");
    expect(equiv).toHaveAttribute("title", expect.stringContaining("Estimated"));
  });

  it("names memory as the reason for a heavy-work hold", () => {
    const row = renderLoad({
      level: "heavy_hold",
      reason: "memory",
      load_per_core: 0.5,
      mem_pct: 92,
    });
    expect(within(row).getByTestId("host-load-level")).toHaveTextContent(
      "Above heavy-work line (memory 92%)",
    );
  });

  it("asks for a host update when the host cannot report a level", () => {
    const row = renderLoad({ state: "needs_update", level: "unknown", load_per_core: null });

    const levelText = within(row).getByTestId("host-load-level");
    expect(levelText).toHaveTextContent("Host update needed");
    expect(levelText).toHaveAttribute("data-level", "needs_update");
    // The host still sends resources, so the "no resources" note stays away.
    expect(
      within(screen.getByTestId("host-card-host_1")).queryByText("Update host to see resources."),
    ).toBeNull();
  });

  it("keeps an offline host's lines visible and editable without resources", () => {
    const view = viewWithLoad({});
    Object.assign(view.hosts[1], {
      level: "unknown",
      lines: { heavy: 3, new_session: 6 },
      lines_source: "host",
    });
    mocks.status.current = { data: view, isLoading: false, isError: false, error: null };
    renderPage();

    const row = screen.getByTestId("host-load-host_2");
    expect(within(row).getByTestId("host-load-level")).toHaveTextContent("Incomplete data");
    expect(within(row).getByTestId("host-load-tick-heavy")).toHaveTextContent("3");
    expect(within(row).queryByTestId("host-load-marker")).toBeNull();
    expect(within(row).getByTestId("host-load-own-lines")).toBeInTheDocument();
  });

  it("counts queued child sessions only when there are some", () => {
    const row = renderLoad({ level: "new_session_hold", load_per_core: 5.4, queued_children: 2 });
    expect(within(row).getByTestId("host-load-queued")).toHaveTextContent(
      "2 child sessions queued",
    );
    expect(within(row).getByTestId("host-load-queued")).toHaveTextContent("below No new session");
    cleanup();

    const none = renderLoad({});
    expect(within(none).queryByTestId("host-load-queued")).toBeNull();
  });

  it("gives a member plain numbers, with nothing to open", () => {
    mocks.isAdmin.current = false;
    const row = renderLoad({});

    const tick = within(row).getByTestId("host-load-tick-heavy");
    expect(tick.tagName).toBe("SPAN");
    expect(tick).toHaveTextContent("4");
    expect(within(row).queryByRole("button", { name: /line, currently/ })).toBeNull();
    fireEvent.click(tick);
    expect(within(row).queryByRole("alert")).toBeNull();
    expect(mocks.update.mutate).not.toHaveBeenCalled();
  });

  // The line editor opens in a portal, so it is queried from the screen while
  // the ruler itself stays scoped to the card's row.
  it("opens one host's line from its tick, saving only that host's pair", () => {
    mocks.update.mutate = vi.fn((_patch: unknown, options?: { onSuccess?: () => void }) =>
      options?.onSuccess?.(),
    );
    const row = renderLoad({});
    fireEvent.click(within(row).getByTestId("host-load-tick-heavy"));

    const input = screen.getByLabelText("Heavy work (per core)");
    expect(input).toHaveValue(4);
    fireEvent.change(input, { target: { value: "3" } });
    fireEvent.submit(screen.getByTestId("host-line-form-heavy"));

    expect(mocks.update.mutate).toHaveBeenCalledWith(
      { load_lines: { hosts: { host_1: { heavy: 3, new_session: 5 } } } },
      expect.anything(),
    );
    expect(screen.queryByTestId("host-line-form-heavy")).toBeNull();
    // The card's own tick is still the saved value until the data refetches.
    expect(within(row).getByTestId("host-load-tick-new_session")).toHaveTextContent("5");
  });

  it("saves on Enter and closes on Esc", () => {
    mocks.update.mutate = vi.fn();
    const row = renderLoad({});
    fireEvent.click(within(row).getByTestId("host-load-tick-new_session"));
    const input = screen.getByLabelText("No new session (per core)");
    fireEvent.change(input, { target: { value: "6" } });
    fireEvent.keyDown(input, { key: "Enter" });

    expect(mocks.update.mutate).toHaveBeenCalledWith(
      { load_lines: { hosts: { host_1: { heavy: 4, new_session: 6 } } } },
      expect.anything(),
    );
    // Enter does not close by itself: the editor closes when the save lands.
    fireEvent.keyDown(input, { key: "Escape" });
    expect(screen.queryByTestId("host-line-form-new_session")).toBeNull();
  });

  it("refuses an invalid line in place and keeps the value", () => {
    const row = renderLoad({});
    fireEvent.click(within(row).getByTestId("host-load-tick-heavy"));
    const input = screen.getByLabelText("Heavy work (per core)");
    fireEvent.change(input, { target: { value: "6" } });
    fireEvent.submit(screen.getByTestId("host-line-form-heavy"));

    expect(screen.getByRole("alert")).toHaveTextContent(
      "The heavy-work line must be below the new-session line.",
    );
    expect(mocks.update.mutate).not.toHaveBeenCalled();
    expect(input).toHaveValue(6);
    // The ruler still shows the saved value.
    expect(within(row).getByTestId("host-load-tick-heavy")).toHaveTextContent("4");
  });

  it("resets an overridden host to the default from the editor", () => {
    mocks.update.mutate = vi.fn((_patch: unknown, options?: { onSuccess?: () => void }) =>
      options?.onSuccess?.(),
    );
    const row = renderLoad({ lines: { heavy: 2, new_session: 8 }, lines_source: "host" });
    fireEvent.click(within(row).getByTestId("host-load-tick-heavy"));
    fireEvent.click(screen.getByRole("button", { name: "Reset to default" }));

    expect(mocks.update.mutate).toHaveBeenCalledWith(
      { load_lines: { hosts: { host_1: null } } },
      expect.anything(),
    );
  });

  it("keeps Reset to default's own action on Enter", () => {
    mocks.update.mutate = vi.fn((_patch: unknown, options?: { onSuccess?: () => void }) =>
      options?.onSuccess?.(),
    );
    const row = renderLoad({ lines: { heavy: 2, new_session: 8 }, lines_source: "host" });
    fireEvent.click(within(row).getByTestId("host-load-tick-heavy"));
    const reset = screen.getByRole("button", { name: "Reset to default" });
    reset.focus();
    fireEvent.keyDown(reset, { key: "Enter" });
    fireEvent.click(reset);

    // One call: the reset. Enter must not also submit the form as a save.
    expect(mocks.update.mutate).toHaveBeenCalledTimes(1);
    expect(mocks.update.mutate).toHaveBeenCalledWith(
      { load_lines: { hosts: { host_1: null } } },
      expect.anything(),
    );
  });

  it("offers no reset to a host that follows the default", () => {
    const row = renderLoad({});
    fireEvent.click(within(row).getByTestId("host-load-tick-heavy"));
    expect(screen.queryByRole("button", { name: "Reset to default" })).toBeNull();
  });

  it("shows a server refusal inside the editor", () => {
    mocks.update.mutate = vi.fn((_patch: unknown, options?: { onError?: (e: Error) => void }) =>
      options?.onError?.(new Error("load lines require 0 < heavy < new_session <= 64")),
    );
    const row = renderLoad({});
    fireEvent.click(within(row).getByTestId("host-load-tick-heavy"));
    fireEvent.submit(screen.getByTestId("host-line-form-heavy"));

    expect(screen.getByRole("alert")).toHaveTextContent(
      "load lines require 0 < heavy < new_session <= 64",
    );
    expect(screen.getByTestId("host-line-form-heavy")).toBeInTheDocument();
  });
});

describe("fanOutLabels", () => {
  it("moves two labels apart while their marks keep their places", () => {
    // Heavy work 4.9 and No new session 5 on a 300 px ruler: 196 px and 200 px
    // apart naturally, far closer than one label is wide.
    const natural = [196, 200];
    const placed = fanOutLabels(natural, 34, 300);

    expect(placed[0]).toBeLessThan(natural[0]);
    expect(placed[1]).toBeGreaterThan(natural[1]);
    expect(placed[1] - placed[0]).toBeGreaterThanOrEqual(34);
    // The marks themselves are not moved by this.
    expect(natural).toEqual([196, 200]);
  });

  it("leaves labels alone when they already clear each other", () => {
    expect(fanOutLabels([60, 200], 34, 300)).toEqual([60, 200]);
  });

  it("keeps both labels inside the ruler at an edge", () => {
    const placed = fanOutLabels([294, 297], 34, 300);
    expect(Math.min(...placed)).toBeGreaterThanOrEqual(0);
    expect(Math.max(...placed)).toBeLessThanOrEqual(300);
    expect(placed[1] - placed[0]).toBeGreaterThanOrEqual(34);
  });

  it("does nothing before the ruler has a width", () => {
    expect(fanOutLabels([10, 12], 34, 0)).toEqual([10, 12]);
  });
});

describe("SystemStatusPage alert lines", () => {
  const settingsView = {
    cpu_pct: 85,
    cpu_sustain_min: 10,
    mem_pct: 90,
    disk_pct: 90,
    server_5xx_pct: 5,
  };

  function renderWithSettings() {
    mocks.settings.current = { data: settingsView };
    mocks.status.current = {
      data: makeView({ server: true }),
      isLoading: false,
      isError: false,
      error: null,
    };
    renderPage();
    return screen.getByTestId("system-status-server-card");
  }

  it("writes each alert line under the metric it governs, once each", () => {
    const card = renderWithSettings();

    expect(within(card).getByTestId("alert-cpu_pct")).toHaveTextContent("alert 85%");
    expect(within(card).getByTestId("alert-mem_pct")).toHaveTextContent("alert 90%");
    expect(within(card).getByTestId("alert-disk_pct")).toHaveTextContent("alert 90%");
    expect(within(card).getByTestId("alert-server_5xx_pct")).toHaveTextContent("alert 5%");
    // One editing place: no second copy on a host card.
    expect(
      within(screen.getByTestId("host-card-host_1")).queryByTestId("alert-cpu_pct"),
    ).toBeNull();
  });

  it("saves the CPU line with its sustained window, and says every host shares it", () => {
    const card = renderWithSettings();
    mocks.update.mutate = vi.fn((_patch: unknown, options?: { onSuccess?: () => void }) =>
      options?.onSuccess?.(),
    );
    fireEvent.click(within(card).getByTestId("alert-cpu_pct"));

    expect(screen.getByText(/Every host shares this line/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Alert when CPU is above, in percent"), {
      target: { value: "80" },
    });
    fireEvent.change(screen.getByLabelText("Sustained window, in minutes"), {
      target: { value: "3" },
    });
    fireEvent.submit(screen.getByLabelText("Alert when CPU is above, in percent").closest("form")!);

    expect(mocks.update.mutate).toHaveBeenCalledWith(
      { cpu_pct: 80, cpu_sustain_min: 3 },
      expect.anything(),
    );
  });

  it("sends one field for a single-number line", () => {
    const card = renderWithSettings();
    mocks.update.mutate = vi.fn();
    fireEvent.click(within(card).getByTestId("alert-mem_pct"));
    fireEvent.change(screen.getByLabelText("Alert when Memory is above, in percent"), {
      target: { value: "75" },
    });
    fireEvent.submit(
      screen.getByLabelText("Alert when Memory is above, in percent").closest("form")!,
    );

    expect(mocks.update.mutate).toHaveBeenCalledWith({ mem_pct: 75 }, expect.anything());
  });

  it("sends the stored percentage back unchanged when only the window moves", () => {
    mocks.settings.current = { data: { ...settingsView, cpu_pct: 85.555 } };
    mocks.status.current = {
      data: makeView({ server: true }),
      isLoading: false,
      isError: false,
      error: null,
    };
    mocks.update.mutate = vi.fn();
    renderPage();
    // The label rounds for reading; the field holds what the server stored.
    const card = screen.getByTestId("system-status-server-card");
    expect(within(card).getByTestId("alert-cpu_pct")).toHaveTextContent("alert 85.56%");
    fireEvent.click(within(card).getByTestId("alert-cpu_pct"));
    const percent = screen.getByLabelText("Alert when CPU is above, in percent");
    expect(percent).toHaveValue(85.555);
    fireEvent.change(screen.getByLabelText("Sustained window, in minutes"), {
      target: { value: "3" },
    });
    fireEvent.submit(percent.closest("form")!);

    expect(mocks.update.mutate).toHaveBeenCalledWith(
      { cpu_pct: 85.555, cpu_sustain_min: 3 },
      expect.anything(),
    );
  });

  it("refuses a percentage outside the range in place", () => {
    const card = renderWithSettings();
    fireEvent.click(within(card).getByTestId("alert-disk_pct"));
    const input = screen.getByLabelText("Alert when Disk is above, in percent");
    fireEvent.change(input, { target: { value: "0" } });
    fireEvent.submit(input.closest("form")!);

    expect(screen.getByRole("alert")).toHaveTextContent(
      "Enter a percentage above 0 and at most 100.",
    );
    expect(mocks.update.mutate).not.toHaveBeenCalled();
  });

  it("closes on Esc without saving", () => {
    const card = renderWithSettings();
    fireEvent.click(within(card).getByTestId("alert-server_5xx_pct"));
    const input = screen.getByLabelText("Alert when 5xx failure rate is above, in percent");
    fireEvent.change(input, { target: { value: "9" } });
    fireEvent.keyDown(input, { key: "Escape" });

    expect(screen.queryByLabelText("Alert when 5xx failure rate is above, in percent")).toBeNull();
    expect(mocks.update.mutate).not.toHaveBeenCalled();
  });

  it("leaves a member with no alert line to open", () => {
    mocks.isAdmin.current = false;
    mocks.status.current = {
      data: makeView({ server: true }),
      isLoading: false,
      isError: false,
      error: null,
    };
    renderPage();

    expect(screen.queryByTestId("alert-cpu_pct")).toBeNull();
  });
});

describe("SystemStatusPage chart and panel entry", () => {
  it("labels the alert line in a gutter that opens nothing", () => {
    mocks.settings.current = { data: { cpu_pct: 85 } };
    mocks.status.current = {
      data: makeView({ server: true }),
      isLoading: false,
      isError: false,
      error: null,
    };
    renderPage();

    const label = within(screen.getByTestId("system-status-server-card")).getByTestId(
      "sparkline-threshold-label",
    );
    expect(label).toHaveTextContent("85%");
    fireEvent.click(label);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.queryByLabelText(/Alert when/)).toBeNull();
    expect(document.activeElement).toBe(document.body);
  });

  it("opens Monitor settings from the header, and from ?monitor=1", async () => {
    mocks.status.current = {
      data: makeView({ server: true }),
      isLoading: false,
      isError: false,
      error: null,
    };
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: "Monitor settings" }));
    expect(await screen.findByRole("dialog", { name: "Monitor settings" })).toBeInTheDocument();
    cleanup();

    // The old Settings link lands here with the panel already open.
    mocks.settings.current = {
      data: { cpu_pct: 85, health_check: {}, default_health_check_prompt: "" },
    };
    const client = new QueryClient();
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/system?monitor=1"]}>
          <SystemStatusPage />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(await screen.findByRole("dialog", { name: "Monitor settings" })).toBeInTheDocument();
  });
});
