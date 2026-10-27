// Rendering contract for /system: findings, the admin-only server card, host
// state variants, the expandable process tree with subtree totals and role
// labels, the measured monitor-overhead footer, and each host's load row with
// its lines and the admin's per-host edit.

import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type * as SystemStatusModule from "@/hooks/useSystemStatus";

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

import { SystemStatusPage } from "./SystemStatusPage";

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

  it("renders each row's uptime and names hosts in the overhead footer", () => {
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

    const overhead = screen.getByTestId("system-status-overhead");
    expect(overhead).toHaveTextContent("Laptop");
    expect(overhead).not.toHaveTextContent("host_1");
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
    expect(screen.getAllByRole("button", { name: "Run health check" })).toHaveLength(2);
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
      perCore === null ? "— per core" : `${perCore.toFixed(2)} per core`,
    );
  });

  it("shows load per core with the load and cores it divides, and both lines", () => {
    const row = renderLoad({});

    expect(row).toHaveTextContent("0.93 per core");
    expect(within(row).getByTestId("host-load-equiv")).toHaveTextContent("(11.1 / 12 cores)");
    expect(within(row).getByTestId("host-load-equiv")).not.toHaveAttribute("title");
    expect(row).toHaveTextContent("Heavy-work line 4");
    expect(row).toHaveTextContent("New-session line 5");
    expect(within(row).getByTestId("host-load-lines-source")).toHaveTextContent("Default");
    // Scale runs to 1.5 x the new-session line: 4 / 7.5 and 5 / 7.5.
    expect(within(row).getByTestId("host-load-heavy-mark").style.left).toBe(`${(4 / 7.5) * 100}%`);
    expect(within(row).getByTestId("host-load-new-session-mark").style.left).toBe(
      `${(5 / 7.5) * 100}%`,
    );
    expect(within(row).getByTestId("host-load-marker").style.left).toBe(`${(0.93 / 7.5) * 100}%`);
    // The old raw-load metric is gone from the metric grid.
    expect(within(screen.getByTestId("host-card-host_1")).queryByText("9.8")).toBeNull();
  });

  it("pins a load past the scale to its end and drops the marker without a reading", () => {
    const row = renderLoad({ level: "new_session_hold", load_per_core: 21.2 });
    expect(within(row).getByTestId("host-load-marker").style.left).toBe("100%");
    cleanup();

    const unknown = renderLoad({ level: "unknown", load_per_core: null, load_equiv: null });
    expect(within(unknown).queryByTestId("host-load-marker")).toBeNull();
    expect(within(unknown).queryByTestId("host-load-equiv")).toBeNull();
    expect(within(unknown).getByTestId("host-load-scale")).toBeInTheDocument();
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
    expect(row).toHaveTextContent("Heavy-work line 3");
    expect(within(row).queryByTestId("host-load-marker")).toBeNull();
    expect(within(row).getByRole("button", { name: "Edit lines" })).toBeInTheDocument();
  });

  it("counts queued child sessions only when there are some", () => {
    const row = renderLoad({ level: "new_session_hold", load_per_core: 5.4, queued_children: 2 });
    expect(within(row).getByTestId("host-load-queued")).toHaveTextContent(
      "2 child sessions queued",
    );
    cleanup();

    const none = renderLoad({});
    expect(within(none).queryByTestId("host-load-queued")).toBeNull();
  });

  it("renders the card for an admin whose server sends no load lines in settings", () => {
    mocks.settings.current = { data: { cpu_pct: 85 } };
    const row = renderLoad({});
    fireEvent.click(within(row).getByRole("button", { name: "Edit lines" }));
    expect(within(row).getByRole("checkbox", { name: "Follow the default" })).toBeChecked();
  });

  it("offers Edit lines to admins only", () => {
    mocks.isAdmin.current = false;
    const member = renderLoad({});
    expect(within(member).queryByRole("button", { name: "Edit lines" })).toBeNull();
    cleanup();

    mocks.isAdmin.current = true;
    const admin = renderLoad({});
    expect(within(admin).getByRole("button", { name: "Edit lines" })).toBeInTheDocument();
  });

  it("refuses a heavy-work line at or above the new-session line without saving", () => {
    const row = renderLoad({});
    fireEvent.click(within(row).getByRole("button", { name: "Edit lines" }));
    fireEvent.click(within(row).getByRole("checkbox", { name: /Follow the default/ }));
    fireEvent.change(within(row).getByLabelText("Heavy-work line (per core)"), {
      target: { value: "5" },
    });
    fireEvent.change(within(row).getByLabelText("New-session line (per core)"), {
      target: { value: "5" },
    });
    fireEvent.click(within(row).getByRole("button", { name: "Save" }));

    expect(within(row).getByRole("alert")).toHaveTextContent(
      "The heavy-work line must be below the new-session line.",
    );
    expect(mocks.update.mutate).not.toHaveBeenCalled();
  });

  it("saves a per-host override, then closes the editor", () => {
    mocks.update.mutate = vi.fn((_patch: unknown, options?: { onSuccess?: () => void }) =>
      options?.onSuccess?.(),
    );
    const row = renderLoad({});
    fireEvent.click(within(row).getByRole("button", { name: "Edit lines" }));
    const heavy = within(row).getByLabelText("Heavy-work line (per core)");
    expect(heavy).toBeDisabled();
    fireEvent.click(within(row).getByRole("checkbox", { name: /Follow the default/ }));
    expect(heavy).toHaveValue(4);
    fireEvent.change(heavy, { target: { value: "3" } });
    fireEvent.change(within(row).getByLabelText("New-session line (per core)"), {
      target: { value: "6.5" },
    });
    fireEvent.click(within(row).getByRole("button", { name: "Save" }));

    expect(mocks.update.mutate).toHaveBeenCalledWith(
      { load_lines: { hosts: { host_1: { heavy: 3, new_session: 6.5 } } } },
      expect.anything(),
    );
    expect(within(row).queryByTestId("host-lines-form-host_1")).toBeNull();
  });

  it("returns an overridden host to the default and shows the default's values", () => {
    mocks.settings.current = {
      data: {
        load_lines: { default: { heavy: 3, new_session: 4.5 }, hosts: {}, queue_enabled: true },
      },
    };
    const row = renderLoad({ lines: { heavy: 2, new_session: 8 }, lines_source: "host" });
    expect(within(row).getByTestId("host-load-lines-source")).toHaveTextContent(
      "Set for this host",
    );
    fireEvent.click(within(row).getByRole("button", { name: "Edit lines" }));
    const follow = within(row).getByRole("checkbox", { name: /Follow the default \(3 \/ 4\.5\)/ });
    expect(follow).not.toBeChecked();
    expect(within(row).getByLabelText("Heavy-work line (per core)")).toHaveValue(2);

    fireEvent.click(follow);
    expect(within(row).getByLabelText("Heavy-work line (per core)")).toHaveValue(3);
    expect(within(row).getByLabelText("New-session line (per core)")).toHaveValue(4.5);
    fireEvent.click(within(row).getByRole("button", { name: "Save" }));

    expect(mocks.update.mutate).toHaveBeenCalledWith(
      { load_lines: { hosts: { host_1: null } } },
      expect.anything(),
    );
  });

  it("shows a server refusal inside the editor", () => {
    mocks.update.mutate = vi.fn((_patch: unknown, options?: { onError?: (e: Error) => void }) =>
      options?.onError?.(new Error("load lines require 0 < heavy < new_session <= 64")),
    );
    const row = renderLoad({});
    fireEvent.click(within(row).getByRole("button", { name: "Edit lines" }));
    fireEvent.click(within(row).getByRole("button", { name: "Save" }));

    expect(within(row).getByRole("alert")).toHaveTextContent(
      "load lines require 0 < heavy < new_session <= 64",
    );
    expect(within(row).getByTestId("host-lines-form-host_1")).toBeInTheDocument();
  });
});
