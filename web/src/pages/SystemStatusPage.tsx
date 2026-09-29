// The resource-monitor page (``/system``): the server process's own metrics,
// every visible host's machine metrics and 24 h CPU trend, and each host's
// omnigent process tree attributed to its session.
//
// The page never renders the health-check surface (a later task). A member
// receives `server: null` and only their own hosts — the page just renders
// what the permission-filtered response contains.

import { Fragment, useMemo, useState } from "react";
import { ChevronDownIcon, ChevronRightIcon } from "lucide-react";
import { PageScroll } from "@/components/PageScroll";
import { Sparkline } from "@/components/Sparkline";
import { useIsAdmin } from "@/hooks/useIsAdmin";
import { useLoadedConversations } from "@/hooks/useSidebarData";
import {
  useSystemHistory,
  useSystemStatus,
  useSystemStatusSettings,
  type SystemHostState,
  type SystemMonitorOverhead,
  type SystemProcessRow,
  type SystemServerStatus,
  type SystemStatusFinding,
  type SystemStatusHost,
} from "@/hooks/useSystemStatus";
import { Link } from "@/lib/routing";
import { relativeTime } from "@/lib/relativeTime";
import { formatBytes } from "@/shell/fileStatusUtils";
import { cn } from "@/lib/utils";

function formatPct(value: number, digits = 1): string {
  return `${value.toFixed(digits)}%`;
}

function formatLoad(load1: number | null): string {
  return load1 === null ? "—" : load1.toFixed(1);
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="text-ui font-semibold tabular-nums">{value}</dd>
    </div>
  );
}

const STATE_STYLES: Record<SystemHostState, string> = {
  online: "bg-emerald-500/10 text-emerald-700 dark:text-emerald-400",
  offline: "bg-muted text-muted-foreground",
  needs_update: "bg-amber-500/10 text-amber-700 dark:text-amber-400",
};

const STATE_LABELS: Record<SystemHostState, string> = {
  online: "Online",
  offline: "Offline",
  needs_update: "Needs update",
};

function StatePill({ state }: { state: SystemHostState }) {
  return (
    <span
      className={cn("rounded-full px-2 py-0.5 text-xs font-medium", STATE_STYLES[state])}
      data-testid={`host-state-${state}`}
    >
      {STATE_LABELS[state]}
    </span>
  );
}

/** Session title when the sidebar has loaded the row, else the raw id. */
function sessionLabel(sessionId: string, sessionTitles: Map<string, string>): string {
  return sessionTitles.get(sessionId) || sessionId;
}

function FindingsBanner({
  findings,
  hostNames,
  sessionTitles,
}: {
  findings: SystemStatusFinding[];
  hostNames: Map<string, string>;
  sessionTitles: Map<string, string>;
}) {
  if (findings.length === 0) {
    return (
      <div className="flex items-center gap-2 rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-ui text-emerald-700 dark:text-emerald-400">
        <span className="size-2 shrink-0 rounded-full bg-emerald-500" aria-hidden="true" />
        All systems normal.
      </div>
    );
  }
  return (
    <ul className="flex flex-col gap-2" data-testid="system-status-findings">
      {findings.map((finding) => (
        <li
          key={finding.id}
          className={cn(
            "flex items-start gap-2 rounded-lg border px-3 py-2 text-ui",
            finding.level === "red"
              ? "border-red-500/30 bg-red-500/10 text-red-700 dark:text-red-400"
              : "border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-400",
          )}
        >
          <span
            className={cn(
              "mt-1 size-2 shrink-0 rounded-full",
              finding.level === "red" ? "bg-red-500" : "bg-amber-500",
            )}
            aria-hidden="true"
          />
          <span className="min-w-0">
            <span className="font-medium">
              {finding.target === "server"
                ? "Server"
                : (hostNames.get(finding.target) ?? finding.target)}
            </span>{" "}
            {finding.detail}
            {finding.top_session !== null && (
              <>
                {" · "}
                <Link to={`/c/${finding.top_session}`} className="underline">
                  {sessionLabel(finding.top_session, sessionTitles)}
                </Link>
              </>
            )}
          </span>
        </li>
      ))}
    </ul>
  );
}

function ServerCard({
  server,
  threshold,
}: {
  server: SystemServerStatus;
  threshold: number | null;
}) {
  const history = useSystemHistory("server");
  const point = server.last_point;
  // The server target's points differ from a host's: CPU is `cpu`, not `cpu_avg`.
  const points = (history.data ?? []).map((entry) => ("cpu" in entry ? entry.cpu : entry.cpu_avg));
  const failureRate = point !== null && point.req > 0 ? (point.err / point.req) * 100 : null;
  return (
    <section className="rounded-lg border bg-card p-4" data-testid="system-status-server-card">
      <div className="mb-3 flex items-center gap-2">
        <h2 className="text-ui font-semibold">Server</h2>
        <span className="rounded-full bg-emerald-500/10 px-2 py-0.5 text-xs font-medium text-emerald-700 dark:text-emerald-400">
          Online
        </span>
      </div>
      {point === null ? (
        <p className="text-sm text-muted-foreground">Waiting for the first metrics tick.</p>
      ) : (
        <>
          <dl className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
            <Metric label="CPU" value={formatPct(point.cpu)} />
            <Metric label="Memory" value={formatBytes(point.rss)} />
            <Metric label="Load" value={formatLoad(point.load1)} />
            <Metric label="In-flight requests" value={String(point.in_flight)} />
            <Metric
              label="5xx failure rate"
              value={failureRate === null ? "—" : formatPct(failureRate)}
            />
            <Metric label="Open connections" value={String(point.websockets)} />
          </dl>
          {points.length > 0 && (
            <Sparkline
              points={points}
              threshold={threshold}
              label="Server CPU, last 24 hours"
              className="mt-3"
            />
          )}
        </>
      )}
    </section>
  );
}

interface ProcessNode {
  row: SystemProcessRow;
  key: string;
  children: ProcessNode[];
  /** Own CPU plus every descendant's, matching the row's rendered value. */
  cpu: number;
  rss: number;
}

/** Nest rows by ``ppid``; roots are rows whose parent is not in the payload. */
function buildProcessTree(processes: SystemProcessRow[]): ProcessNode[] {
  const nodes: ProcessNode[] = processes.map((row, index) => ({
    row,
    key: String(index),
    children: [],
    cpu: row.cpu_pct,
    rss: row.rss,
  }));
  // Folded rows carry pid 0, so they can never be anyone's parent.
  const byPid = new Map<number, ProcessNode>();
  for (const node of nodes) {
    if (node.row.role !== "folded") byPid.set(node.row.pid, node);
  }
  const roots: ProcessNode[] = [];
  for (const node of nodes) {
    const parent = byPid.get(node.row.ppid);
    if (parent !== undefined && parent !== node) parent.children.push(node);
    else roots.push(node);
  }
  const accumulate = (node: ProcessNode): void => {
    for (const child of node.children) {
      accumulate(child);
      node.cpu += child.cpu;
      node.rss += child.rss;
    }
    node.children.sort((a, b) => b.row.cpu_pct - a.row.cpu_pct);
  };
  for (const root of roots) accumulate(root);
  roots.sort((a, b) => b.row.cpu_pct - a.row.cpu_pct);
  return roots;
}

function ProcessRows({
  nodes,
  sessionTitles,
  depth = 0,
}: {
  nodes: ProcessNode[];
  sessionTitles: Map<string, string>;
  depth?: number;
}) {
  return (
    <>
      {nodes.map((node) => (
        <Fragment key={node.key}>
          <tr data-depth={depth}>
            <td className="py-1 pr-3" style={{ paddingLeft: `${depth * 16 + 4}px` }}>
              <span className="font-mono text-xs">{node.row.name}</span>
            </td>
            <td className="py-1 pr-3 text-xs text-muted-foreground">
              {node.row.session_id !== null ? (
                <Link to={`/c/${node.row.session_id}`} className="underline">
                  {sessionLabel(node.row.session_id, sessionTitles)}
                </Link>
              ) : (
                "—"
              )}
            </td>
            <td className="py-1 pr-3 text-right tabular-nums">{formatPct(node.cpu)}</td>
            <td className="py-1 text-right tabular-nums">{formatBytes(node.rss)}</td>
          </tr>
          {node.children.length > 0 && (
            <ProcessRows nodes={node.children} sessionTitles={sessionTitles} depth={depth + 1} />
          )}
        </Fragment>
      ))}
    </>
  );
}

function ProcessTree({
  processes,
  sessionTitles,
}: {
  processes: SystemProcessRow[];
  sessionTitles: Map<string, string>;
}) {
  const roots = buildProcessTree(processes);
  return (
    <table className="w-full" data-testid="process-tree">
      <thead>
        <tr className="text-xs text-muted-foreground">
          <th className="py-1 pr-3 text-left font-medium">Process</th>
          <th className="py-1 pr-3 text-left font-medium">Session</th>
          <th className="py-1 pr-3 text-right font-medium">CPU</th>
          <th className="py-1 text-right font-medium">Memory</th>
        </tr>
      </thead>
      <tbody>
        <ProcessRows nodes={roots} sessionTitles={sessionTitles} />
      </tbody>
    </table>
  );
}

function HostCard({
  host,
  threshold,
  sessionTitles,
}: {
  host: SystemStatusHost;
  threshold: number | null;
  sessionTitles: Map<string, string>;
}) {
  const history = useSystemHistory(host.host_id);
  const [processesOpen, setProcessesOpen] = useState(false);
  const snapshot = host.last_snapshot;
  const machine = snapshot?.machine ?? null;
  const points = (history.data ?? []).map((entry) =>
    "cpu_avg" in entry ? entry.cpu_avg : entry.cpu,
  );
  const name = host.name || host.host_id;
  return (
    <section className="rounded-lg border bg-card p-4" data-testid={`host-card-${host.host_id}`}>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <h2 className="text-ui font-semibold">{name}</h2>
        <StatePill state={host.state} />
      </div>
      {host.state === "offline" && (
        <p className="mb-3 text-sm text-muted-foreground">
          {host.since === null
            ? "Never connected."
            : `Last seen ${relativeTime(host.since * 1000)} ago.`}
        </p>
      )}
      {host.state === "needs_update" && (
        <p className="mb-3 text-sm text-muted-foreground">Update host to see resources.</p>
      )}
      {machine !== null && (
        <>
          <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Metric label="CPU" value={formatPct(machine.cpu_pct)} />
            <Metric
              label="Memory"
              value={`${formatBytes(machine.mem_used)} / ${formatBytes(machine.mem_total)}`}
            />
            <Metric
              label="Disk"
              value={
                machine.disk_total > 0
                  ? formatPct((machine.disk_used / machine.disk_total) * 100)
                  : "—"
              }
            />
            <Metric label="Load" value={formatLoad(machine.load1)} />
          </dl>
          {points.length > 0 && (
            <Sparkline
              points={points}
              threshold={threshold}
              label={`${name} CPU, last 24 hours`}
              className="mt-3"
            />
          )}
        </>
      )}
      {snapshot !== null && snapshot.processes.length > 0 && (
        <div className="mt-3 border-t pt-2">
          <button
            type="button"
            className="flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
            aria-expanded={processesOpen}
            onClick={() => setProcessesOpen((open) => !open)}
          >
            {processesOpen ? (
              <ChevronDownIcon className="size-3.5" />
            ) : (
              <ChevronRightIcon className="size-3.5" />
            )}
            {processesOpen ? "Hide processes" : "View processes"}
          </button>
          {processesOpen && (
            <div className="mt-2">
              <p className="mb-1 text-xs text-muted-foreground">
                omnigent processes · parent rows include their children · sorted by CPU
              </p>
              <ProcessTree processes={snapshot.processes} sessionTitles={sessionTitles} />
            </div>
          )}
        </div>
      )}
    </section>
  );
}

function MonitorOverheadFooter({ overhead }: { overhead: SystemMonitorOverhead }) {
  return (
    <div
      className="mt-6 flex flex-wrap items-center gap-x-4 gap-y-1 rounded-lg border bg-muted/30 px-4 py-3 text-sm"
      data-testid="system-status-overhead"
    >
      <span className="font-medium">Monitor overhead</span>
      <span>
        Server {formatPct(overhead.server.cpu_pct, 2)} CPU ·{" "}
        {formatBytes(overhead.server.mem_estimate_bytes)} memory (estimate)
      </span>
      {Object.entries(overhead.hosts).map(([hostId, cost]) => (
        <span key={hostId}>
          {hostId} {formatPct(cost.cpu_pct, 2)} CPU · {formatBytes(cost.rss_delta)} resident
          increase (upper bound)
        </span>
      ))}
    </div>
  );
}

export function SystemStatusPage() {
  const isAdmin = useIsAdmin();
  const status = useSystemStatus({ live: true });
  // The thresholds are admin-only; members never see the bytes line.
  const settings = useSystemStatusSettings({ enabled: isAdmin });
  // Titles ride the sidebar's already-loaded rows; no extra request.
  const { data: conversationsData } = useLoadedConversations();
  const sessionTitles = useMemo(() => {
    const titles = new Map<string, string>();
    for (const conversation of conversationsData?.pages.flatMap((page) => page.data) ?? []) {
      if (conversation.title) titles.set(conversation.id, conversation.title);
    }
    return titles;
  }, [conversationsData]);

  if (status.isLoading) {
    return (
      <PageScroll contentClassName="px-8">
        <p className="text-ui text-muted-foreground">Loading system status…</p>
      </PageScroll>
    );
  }
  if (status.isError || status.data === undefined) {
    return (
      <PageScroll contentClassName="px-8">
        <p
          role="alert"
          className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-ui text-destructive"
        >
          {status.error instanceof Error ? status.error.message : "Unable to load system status."}
        </p>
      </PageScroll>
    );
  }

  const data = status.data;
  const hostNames = new Map(data.hosts.map((host) => [host.host_id, host.name || host.host_id]));
  const threshold = settings.data?.cpu_pct ?? null;
  return (
    <PageScroll maxWidthClassName="max-w-5xl" contentClassName="px-8">
      <div className="mb-4">
        <h1 className="text-2xl font-semibold">System status</h1>
        <p className="mt-1 text-ui text-muted-foreground">
          Resources on this server and on every visible host, with omnigent's process tree
          attributed to its session.
        </p>
      </div>

      <FindingsBanner
        findings={data.findings}
        hostNames={hostNames}
        sessionTitles={sessionTitles}
      />

      {data.server !== null && (
        <div className="mt-4">
          <ServerCard server={data.server} threshold={threshold} />
        </div>
      )}

      <h2 className="mt-6 mb-2 text-ui font-medium">Hosts</h2>
      {data.hosts.length === 0 ? (
        <p className="text-ui text-muted-foreground">No hosts connected.</p>
      ) : (
        <div className="grid gap-3 lg:grid-cols-2">
          {data.hosts.map((host) => (
            <HostCard
              key={host.host_id}
              host={host}
              threshold={threshold}
              sessionTitles={sessionTitles}
            />
          ))}
        </div>
      )}

      <MonitorOverheadFooter overhead={data.monitor_overhead} />
    </PageScroll>
  );
}
