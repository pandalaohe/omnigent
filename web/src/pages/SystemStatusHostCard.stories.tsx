import type { Meta, StoryObj } from "@storybook/react-vite";
import type { SystemHostSnapshot, SystemStatusHost } from "@/hooks/useSystemStatus";
import { StoryQueryRouter } from "@/storybook/StoryProviders";
import { HostCard } from "./SystemStatusPage";

const GB = 1024 * 1024 * 1024;

function snapshot(cpuPct: number, memUsedGb: number, memTotalGb: number): SystemHostSnapshot {
  return {
    sampled_at: new Date().toISOString(),
    interval_s: 10,
    machine: {
      cpu_pct: cpuPct,
      mem_used: memUsedGb * GB,
      mem_total: memTotalGb * GB,
      disk_used: 410,
      disk_total: 1000,
      load1: null,
    },
    processes: [],
    runner_count: 1,
    sampler_cpu_ms: 4,
    monitor_rss_delta: 0,
  };
}

const DEFAULT_LINES = { heavy: 4, new_session: 5 };

function host(id: string, name: string, fields: Partial<SystemStatusHost>): SystemStatusHost {
  return {
    host_id: id,
    owner: "admin",
    name,
    state: "online",
    since: 0,
    last_snapshot: snapshot(24, 21, 32),
    last_runner_count: 1,
    estimate: false,
    reason: null,
    lines: DEFAULT_LINES,
    lines_source: "default",
    queued_children: 0,
    mem_pct: 66,
    ...fields,
  };
}

const HOSTS: SystemStatusHost[] = [
  host("host_mac", "build-mac", {
    load_per_core: 0.93,
    load_equiv: 11.1,
    cpu_count: 12,
    level: "normal",
  }),
  host("host_win", "win-desktop", {
    last_snapshot: snapshot(71, 40, 64),
    load_per_core: 4.31,
    load_equiv: 103.4,
    estimate: true,
    cpu_count: 24,
    level: "heavy_hold",
    reason: "load",
  }),
  host("host_linux", "linux-runner", {
    last_snapshot: snapshot(97, 58, 64),
    load_per_core: 6.2,
    load_equiv: 198.4,
    cpu_count: 32,
    level: "new_session_hold",
    reason: "load",
    lines: { heavy: 3, new_session: 4.5 },
    lines_source: "host",
    queued_children: 2,
  }),
  host("host_idle", "spare-mini", {
    last_snapshot: snapshot(3, 6, 16),
    load_per_core: 0.21,
    load_equiv: 1.7,
    cpu_count: 8,
    level: "spare",
  }),
  host("host_memory", "studio", {
    last_snapshot: snapshot(18, 59, 64),
    load_per_core: 0.6,
    load_equiv: 9.6,
    cpu_count: 16,
    level: "heavy_hold",
    reason: "memory",
    mem_pct: 92,
  }),
  host("host_old", "old-laptop", {
    state: "needs_update",
    load_per_core: null,
    load_equiv: null,
    cpu_count: null,
    level: "unknown",
  }),
];

function HostGrid({ isAdmin }: { isAdmin: boolean }) {
  return (
    <StoryQueryRouter
      seed={(client) => {
        for (const entry of HOSTS) {
          client.setQueryData(["system-status", "history", entry.host_id], []);
        }
      }}
    >
      <div className="grid w-[min(960px,calc(100vw-3rem))] gap-3 lg:grid-cols-2">
        {HOSTS.map((entry) => (
          <HostCard
            key={entry.host_id}
            host={entry}
            threshold={85}
            sessionTitles={new Map()}
            nowMs={Date.now()}
            isAdmin={isAdmin}
            defaultLines={DEFAULT_LINES}
          />
        ))}
      </div>
    </StoryQueryRouter>
  );
}

const meta = {
  title: "Pages/System status/Host cards",
  parameters: { layout: "fullscreen" },
} satisfies Meta;

export default meta;
type Story = StoryObj<typeof meta>;

export const AdminView: Story = { render: () => <HostGrid isAdmin /> };

export const MemberView: Story = { render: () => <HostGrid isAdmin={false} /> };
