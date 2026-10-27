// The resource-monitor page (``/system``): the server process's own metrics,
// every visible host's machine metrics and 24 h CPU trend, and each host's
// omnigent process tree attributed to its session.
//
// A member receives `server: null` and only their own hosts — the page just
// renders what the permission-filtered response contains.

import { Fragment, memo, useEffect, useMemo, useState } from "react";
import { ChevronDownIcon, ChevronRightIcon } from "lucide-react";
import { PageScroll } from "@/components/PageScroll";
import { Sparkline } from "@/components/Sparkline";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { useIsAdmin } from "@/hooks/useIsAdmin";
import { useLoadedConversations } from "@/hooks/useSidebarData";
import { type HealthCheckNotice, useStartHealthCheck } from "@/hooks/useStartHealthCheck";
import {
  loadLinePairError,
  MAX_LOAD_LINE,
  parseLoadLine,
  useSystemHistory,
  useSystemStatus,
  useSystemStatusSettings,
  useUpdateSystemStatusSettings,
  type SystemHostLoadLevel,
  type SystemHostState,
  type SystemLoadLinePair,
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

function formatUptime(startedAt: number | null | undefined, nowS: number): string {
  if (startedAt === null || startedAt === undefined) return "—";
  const seconds = nowS - startedAt;
  if (seconds >= 86_400) {
    return `${Math.floor(seconds / 86_400)}d ${Math.floor((seconds % 86_400) / 3_600)}h`;
  }
  if (seconds >= 3_600) {
    return `${Math.floor(seconds / 3_600)}h ${Math.floor((seconds % 3_600) / 60)}m`;
  }
  if (seconds >= 60) return `${Math.floor(seconds / 60)}m`;
  return "<1m";
}

/** Whether a sample is older than its freshness window. */
function isStale(sampledAtMs: number, staleAfterS: number, nowMs: number): boolean {
  return (nowMs - sampledAtMs) / 1000 > staleAfterS;
}

/** Coarse sample-age label: seconds under a minute, then minutes, then hours. */
function formatAge(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds));
  if (total < 60) return `${total} s`;
  if (total < 3600) return `${Math.floor(total / 60)} min`;
  return `${Math.floor(total / 3600)} h`;
}

/** Coarse elapsed-span label for sparkline axes; never reads "0 min". */
function formatSpan(seconds: number): string {
  if (seconds >= 3600) return `${Math.floor(seconds / 3600)} h`;
  return `${Math.max(1, Math.floor(seconds / 60))} min`;
}

/** Wall clock in ms, refreshed every second so freshness labels age visibly. */
function useSecondTick(): number {
  const [nowMs, setNowMs] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNowMs(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  return nowMs;
}

// The OS process name is "python" for most rows; show the role instead so the
// daemon, zygote, runner, harness and tmux rows stay distinguishable.
const PROCESS_ROLE_LABELS: Record<string, string> = {
  daemon: "host daemon",
  zygote: "runner zygote",
  runner: "runner",
  harness: "harness",
  tmux: "tmux",
};

function processLabel(row: SystemProcessRow): string {
  return PROCESS_ROLE_LABELS[row.role] ?? row.name;
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

// The server records one point per minute, so three missed points mean stale.
const SERVER_STALE_AFTER_S = 180;

/**
 * Sample-age label for a card header: muted while fresh, amber once the
 * sample is older than its window. The title is the sample's local time.
 */
function Freshness({
  sampledAtMs,
  staleAfterS,
  nowMs,
}: {
  sampledAtMs: number;
  staleAfterS: number;
  nowMs: number;
}) {
  const age = (nowMs - sampledAtMs) / 1000;
  const stale = isStale(sampledAtMs, staleAfterS, nowMs);
  return (
    <span
      data-testid="freshness"
      data-stale={stale ? "true" : undefined}
      title={new Date(sampledAtMs).toLocaleString()}
      className={cn(
        "ml-auto text-xs tabular-nums",
        stale ? "text-amber-700 dark:text-amber-400" : "text-muted-foreground",
      )}
    >
      {stale ? `Stale — last sample ${formatAge(age)} ago` : `Updated ${formatAge(age)} ago`}
    </span>
  );
}

/**
 * First-point / now labels under a sparkline. Points are spaced by index, so
 * no ticks in between: a middle label would lie over data gaps.
 */
function SparklineAxis({ firstT, nowMs }: { firstT: number; nowMs: number }) {
  return (
    <div
      className="mt-1 flex justify-between text-[10px] text-muted-foreground"
      data-testid="sparkline-axis"
    >
      <span>{formatSpan(nowMs / 1000 - firstT)} ago</span>
      <span>now</span>
    </div>
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
  onRunHealthCheck,
  healthCheckPending,
}: {
  findings: SystemStatusFinding[];
  hostNames: Map<string, string>;
  sessionTitles: Map<string, string>;
  /** Admin-only action; absent for members. */
  onRunHealthCheck?: () => void;
  healthCheckPending?: boolean;
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
    <div className="flex flex-col gap-2">
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
      {onRunHealthCheck !== undefined && (
        <div>
          <Button
            size="sm"
            variant="outline"
            loading={healthCheckPending}
            onClick={onRunHealthCheck}
          >
            Run health check
          </Button>
        </div>
      )}
    </div>
  );
}

function HealthCheckNoticeBox({ notice }: { notice: HealthCheckNotice }) {
  if (notice.kind === "error") {
    return (
      <div
        role="alert"
        className="mb-4 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-ui text-destructive"
      >
        {notice.message}
        {notice.sessionId !== undefined && (
          <>
            {" "}
            <Link to={`/c/${notice.sessionId}`} className="underline">
              Open the session
            </Link>
            .
          </>
        )}
      </div>
    );
  }
  return (
    <div className="mb-4 rounded-md border bg-muted/30 px-3 py-2 text-ui text-muted-foreground">
      {notice.message}
      {notice.kind === "unset" && (
        <>
          {" "}
          <Link to="/settings/system-status" className="underline">
            Open System status settings
          </Link>
          .
        </>
      )}
    </div>
  );
}

function ServerCard({
  server,
  threshold,
  nowMs,
}: {
  server: SystemServerStatus;
  threshold: number | null;
  nowMs: number;
}) {
  const history = useSystemHistory("server");
  const point = server.last_point;
  const entries = history.data ?? [];
  // The server target's points differ from a host's: CPU is `cpu`, not `cpu_avg`.
  const points = entries.map((entry) => ("cpu" in entry ? entry.cpu : entry.cpu_avg));
  const firstEntry = entries[0];
  const failureRate = point !== null && point.req > 0 ? (point.err / point.req) * 100 : null;
  const stale = point !== null && isStale(point.t * 1000, SERVER_STALE_AFTER_S, nowMs);
  return (
    <section className="rounded-lg border bg-card p-4" data-testid="system-status-server-card">
      <div className="mb-3 flex items-center gap-2">
        <h2 className="text-ui font-semibold">Server</h2>
        <span className="rounded-full bg-emerald-500/10 px-2 py-0.5 text-xs font-medium text-emerald-700 dark:text-emerald-400">
          Online
        </span>
        {point !== null && (
          <Freshness
            sampledAtMs={point.t * 1000}
            staleAfterS={SERVER_STALE_AFTER_S}
            nowMs={nowMs}
          />
        )}
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
          {points.length > 0 && firstEntry !== undefined && (
            <>
              <Sparkline
                points={points}
                threshold={threshold}
                label="Server CPU, last 24 hours"
                className="mt-3"
                latest={stale ? "stale" : "live"}
              />
              <SparklineAxis firstT={firstEntry.t} nowMs={nowMs} />
            </>
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
  nowS,
  depth = 0,
}: {
  nodes: ProcessNode[];
  sessionTitles: Map<string, string>;
  nowS: number;
  depth?: number;
}) {
  return (
    <>
      {nodes.map((node) => (
        <Fragment key={node.key}>
          <tr data-depth={depth}>
            <td className="py-1 pr-3" style={{ paddingLeft: `${depth * 16 + 4}px` }}>
              <span
                className="block truncate font-mono text-xs"
                title={
                  node.row.role === "folded"
                    ? node.row.name
                    : `${node.row.name} · pid ${node.row.pid}`
                }
              >
                {processLabel(node.row)}
              </span>
            </td>
            <td className="py-1 pr-3 text-xs text-muted-foreground">
              {node.row.session_id !== null ? (
                <Link
                  to={`/c/${node.row.session_id}`}
                  className="block truncate underline"
                  title={sessionLabel(node.row.session_id, sessionTitles)}
                >
                  {sessionLabel(node.row.session_id, sessionTitles)}
                </Link>
              ) : (
                "—"
              )}
            </td>
            <td className="py-1 pr-3 text-right whitespace-nowrap tabular-nums">
              {formatPct(node.cpu)}
            </td>
            <td className="py-1 pr-3 text-right whitespace-nowrap tabular-nums">
              {formatBytes(node.rss)}
            </td>
            <td className="py-1 text-right whitespace-nowrap tabular-nums">
              {formatUptime(node.row.started_at, nowS)}
            </td>
          </tr>
          {node.children.length > 0 && (
            <ProcessRows
              nodes={node.children}
              sessionTitles={sessionTitles}
              nowS={nowS}
              depth={depth + 1}
            />
          )}
        </Fragment>
      ))}
    </>
  );
}

// The page ticks every second; the tree re-renders on a new snapshot or a new minute.
const ProcessTree = memo(function ProcessTree({
  processes,
  sessionTitles,
  nowS,
}: {
  processes: SystemProcessRow[];
  sessionTitles: Map<string, string>;
  nowS: number;
}) {
  const roots = buildProcessTree(processes);
  return (
    <div className="max-h-80 overflow-auto" data-testid="process-tree-scroll">
      <table className="w-full table-fixed" data-testid="process-tree">
        <colgroup>
          <col />
          <col />
          <col className="w-14" />
          <col className="w-16" />
          <col className="w-14" />
        </colgroup>
        <thead>
          <tr className="text-xs text-muted-foreground">
            <th className="sticky top-0 z-10 bg-card-solid py-1 pr-3 text-left font-medium">
              Process
            </th>
            <th className="sticky top-0 z-10 bg-card-solid py-1 pr-3 text-left font-medium">
              Session
            </th>
            <th className="sticky top-0 z-10 bg-card-solid py-1 pr-3 text-right font-medium">
              CPU
            </th>
            <th className="sticky top-0 z-10 bg-card-solid py-1 pr-3 text-right font-medium">
              Memory
            </th>
            <th className="sticky top-0 z-10 bg-card-solid py-1 text-right font-medium">Uptime</th>
          </tr>
        </thead>
        <tbody>
          <ProcessRows nodes={roots} sessionTitles={sessionTitles} nowS={nowS} />
        </tbody>
      </table>
    </div>
  );
});

const LOAD_LEVEL_LABELS: Record<SystemHostLoadLevel, string> = {
  spare: "Spare capacity",
  normal: "Normal",
  heavy_hold: "Above heavy-work line",
  new_session_hold: "Above new-session line",
  unknown: "Incomplete data",
};

const LOAD_LEVEL_STYLES: Record<SystemHostLoadLevel, { dot: string; text: string }> = {
  spare: { dot: "bg-emerald-500", text: "text-emerald-700 dark:text-emerald-400" },
  normal: { dot: "bg-muted-foreground", text: "text-foreground" },
  heavy_hold: { dot: "bg-amber-500", text: "text-amber-700 dark:text-amber-400" },
  new_session_hold: { dot: "bg-red-500", text: "text-red-700 dark:text-red-400" },
  unknown: { dot: "bg-muted-foreground/40", text: "text-muted-foreground" },
};

const ESTIMATE_TITLE =
  "Estimated: this system has no native load average, so the load counts waiting threads plus the busy share of the cores.";

/** Room past the new-session line so a host above it still reads as a position. */
const LOAD_SCALE_HEADROOM = 1.5;

function hostLoadLevelLabel(host: SystemStatusHost, level: SystemHostLoadLevel): string {
  if (host.state === "needs_update") return "Host update needed";
  const label = LOAD_LEVEL_LABELS[level];
  if (level !== "heavy_hold" || host.reason !== "memory") return label;
  const memPct = host.mem_pct ?? null;
  return `${label} (memory${memPct === null ? "" : ` ${formatPct(memPct, 0)}`})`;
}

/** Bar from 0 to past the new-session line: both lines marked, the current load as a dot. */
function LoadScale({ value, lines }: { value: number | null; lines: SystemLoadLinePair }) {
  const max = lines.new_session * LOAD_SCALE_HEADROOM;
  const at = (point: number) => `${Math.min(100, Math.max(0, (point / max) * 100))}%`;
  const spareEnd = Math.min(1, lines.heavy);
  return (
    <div
      role="img"
      aria-label={`${value === null ? "No current load" : `${value.toFixed(2)} per core`}; heavy-work line ${String(lines.heavy)}, new-session line ${String(lines.new_session)}`}
      className="relative mt-2 h-2 rounded-full bg-muted"
      data-testid="host-load-scale"
    >
      <span
        className="absolute inset-y-0 left-0 rounded-l-full bg-emerald-500/25"
        style={{ width: at(spareEnd) }}
      />
      <span
        className="absolute inset-y-0 bg-amber-500/30"
        style={{
          left: at(lines.heavy),
          width: `calc(${at(lines.new_session)} - ${at(lines.heavy)})`,
        }}
      />
      <span
        className="absolute inset-y-0 right-0 rounded-r-full bg-red-500/30"
        style={{ left: at(lines.new_session) }}
      />
      <span
        className="absolute -top-1 h-4 w-0.5 -translate-x-1/2 bg-amber-600 dark:bg-amber-400"
        style={{ left: at(lines.heavy) }}
        data-testid="host-load-heavy-mark"
      />
      <span
        className="absolute -top-1 h-4 w-0.5 -translate-x-1/2 bg-red-600 dark:bg-red-400"
        style={{ left: at(lines.new_session) }}
        data-testid="host-load-new-session-mark"
      />
      {value !== null && (
        <span
          className="absolute top-1/2 size-3 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-card bg-foreground"
          style={{ left: at(value) }}
          data-testid="host-load-marker"
        />
      )}
    </div>
  );
}

/**
 * Admin edit of one host's lines. "Follow the default" removes the override;
 * the pair must satisfy the server's rule before it is sent.
 */
function HostLinesForm({
  host,
  lines,
  defaultLines,
  onClose,
}: {
  host: SystemStatusHost;
  lines: SystemLoadLinePair;
  defaultLines: SystemLoadLinePair | null;
  onClose: () => void;
}) {
  const update = useUpdateSystemStatusSettings();
  const [followDefault, setFollowDefault] = useState(host.lines_source !== "host");
  const [heavy, setHeavy] = useState(String(lines.heavy));
  const [newSession, setNewSession] = useState(String(lines.new_session));
  const [error, setError] = useState<string | null>(null);
  const shown = followDefault && defaultLines !== null ? defaultLines : null;
  const idPrefix = `host-lines-${host.host_id}`;

  return (
    <form
      className="mt-3 flex flex-col gap-3 rounded-md border bg-muted/30 p-3"
      data-testid={`host-lines-form-${host.host_id}`}
      onSubmit={(event) => {
        event.preventDefault();
        let pair: SystemLoadLinePair | null = null;
        if (!followDefault) {
          pair = { heavy: parseLoadLine(heavy), new_session: parseLoadLine(newSession) };
          const invalid = loadLinePairError(pair.heavy, pair.new_session);
          if (invalid !== null) {
            setError(invalid);
            return;
          }
        }
        setError(null);
        update.mutate(
          { load_lines: { hosts: { [host.host_id]: pair } } },
          { onSuccess: onClose, onError: (failure) => setError(failure.message) },
        );
      }}
    >
      <label className="flex items-center gap-2 text-sm">
        <Checkbox
          checked={followDefault}
          onCheckedChange={(checked) => setFollowDefault(checked === true)}
        />
        Follow the default{" "}
        {defaultLines !== null && (
          <span className="text-muted-foreground tabular-nums">
            ({String(defaultLines.heavy)} / {String(defaultLines.new_session)})
          </span>
        )}
      </label>
      <div className="grid grid-cols-2 gap-3">
        <label htmlFor={`${idPrefix}-heavy`} className="flex flex-col gap-1">
          <span className="text-xs font-medium">Heavy-work line (per core)</span>
          <Input
            id={`${idPrefix}-heavy`}
            type="number"
            min={0}
            max={MAX_LOAD_LINE}
            step="any"
            disabled={followDefault}
            value={shown === null ? heavy : String(shown.heavy)}
            onChange={(event) => setHeavy(event.target.value)}
          />
        </label>
        <label htmlFor={`${idPrefix}-new-session`} className="flex flex-col gap-1">
          <span className="text-xs font-medium">New-session line (per core)</span>
          <Input
            id={`${idPrefix}-new-session`}
            type="number"
            min={0}
            max={MAX_LOAD_LINE}
            step="any"
            disabled={followDefault}
            value={shown === null ? newSession : String(shown.new_session)}
            onChange={(event) => setNewSession(event.target.value)}
          />
        </label>
      </div>
      {error !== null && (
        <p role="alert" className="text-xs text-destructive">
          {error}
        </p>
      )}
      <div className="flex items-center gap-2">
        <Button type="submit" size="sm" loading={update.isPending}>
          Save
        </Button>
        <Button type="button" size="sm" variant="ghost" onClick={onClose}>
          Cancel
        </Button>
      </div>
    </form>
  );
}

/**
 * The Load row: load per core and what it divides, the host's level against its
 * two lines (text beside the colour), a scale marking both lines, queued child
 * sessions, and the admin's per-host edit.
 */
function HostLoad({
  host,
  isAdmin,
  defaultLines,
}: {
  host: SystemStatusHost;
  isAdmin: boolean;
  defaultLines: SystemLoadLinePair | null;
}) {
  const [editing, setEditing] = useState(false);
  const level = host.level ?? "unknown";
  const style = LOAD_LEVEL_STYLES[host.state === "needs_update" ? "unknown" : level];
  const perCore = host.load_per_core ?? null;
  const loadEquiv = host.load_equiv ?? null;
  const cores = host.cpu_count ?? null;
  const lines = host.lines;
  const queued = host.queued_children ?? 0;
  return (
    <div className="mt-3" data-testid={`host-load-${host.host_id}`}>
      <div className="text-xs text-muted-foreground">Load</div>
      <div className="text-ui">
        <span className="font-semibold tabular-nums">
          {perCore === null ? "—" : perCore.toFixed(2)}
        </span>{" "}
        <span className="text-muted-foreground">per core</span>{" "}
        {loadEquiv !== null && cores !== null && (
          <>
            <span
              className="text-muted-foreground tabular-nums"
              title={host.estimate ? ESTIMATE_TITLE : undefined}
              data-testid="host-load-equiv"
            >
              ({host.estimate ? "est. " : ""}
              {loadEquiv.toFixed(1)} / {cores} cores)
            </span>{" "}
          </>
        )}
        <span aria-hidden="true" className="text-muted-foreground">
          ·
        </span>{" "}
        <span
          className={cn("inline-flex items-center gap-1.5 font-medium", style.text)}
          data-testid="host-load-level"
          data-level={host.state === "needs_update" ? "needs_update" : level}
        >
          <span className={cn("size-2 shrink-0 rounded-full", style.dot)} aria-hidden="true" />
          {hostLoadLevelLabel(host, level)}
        </span>
      </div>
      {lines !== undefined && (
        <>
          <LoadScale value={perCore} lines={lines} />
          <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
            <span className="inline-flex items-center gap-1">
              <span className="h-2.5 w-0.5 bg-amber-600 dark:bg-amber-400" aria-hidden="true" />
              Heavy-work line {String(lines.heavy)}
            </span>
            <span className="inline-flex items-center gap-1">
              <span className="h-2.5 w-0.5 bg-red-600 dark:bg-red-400" aria-hidden="true" />
              New-session line {String(lines.new_session)}
            </span>
            <span data-testid="host-load-lines-source">
              {host.lines_source === "host" ? "Set for this host" : "Default"}
            </span>
            {isAdmin && !editing && (
              <Button
                type="button"
                size="xs"
                variant="ghost"
                className="ml-auto"
                onClick={() => setEditing(true)}
              >
                Edit lines
              </Button>
            )}
          </div>
          {queued > 0 && (
            <p
              className="mt-1 text-xs text-amber-700 dark:text-amber-400"
              data-testid="host-load-queued"
            >
              {queued === 1 ? "1 child session queued" : `${queued} child sessions queued`} until
              this host drops below its new-session line
            </p>
          )}
          {isAdmin && editing && (
            <HostLinesForm
              host={host}
              lines={lines}
              defaultLines={defaultLines}
              onClose={() => setEditing(false)}
            />
          )}
        </>
      )}
    </div>
  );
}

export function HostCard({
  host,
  threshold,
  sessionTitles,
  nowMs,
  isAdmin = false,
  defaultLines = null,
}: {
  host: SystemStatusHost;
  threshold: number | null;
  sessionTitles: Map<string, string>;
  nowMs: number;
  /** Shows the per-host line edit. */
  isAdmin?: boolean;
  /** The workspace default lines, known to admins only. */
  defaultLines?: SystemLoadLinePair | null;
}) {
  const history = useSystemHistory(host.host_id);
  const [processesOpen, setProcessesOpen] = useState(false);
  const snapshot = host.last_snapshot;
  const machine = snapshot?.machine ?? null;
  const entries = history.data ?? [];
  const points = entries.map((entry) => ("cpu_avg" in entry ? entry.cpu_avg : entry.cpu));
  const firstEntry = entries[0];
  const name = host.name || host.host_id;
  const sampledAtMs = snapshot === null ? Number.NaN : Date.parse(snapshot.sampled_at);
  const hasSample = snapshot !== null && Number.isFinite(sampledAtMs);
  const staleAfterS = snapshot === null ? 0 : 3 * snapshot.interval_s;
  const stale = hasSample && isStale(sampledAtMs, staleAfterS, nowMs);
  // Uptime labels change once a minute, so the memoized tree needs no second tick.
  const uptimeNowS = Math.floor(nowMs / 60_000) * 60;
  // Offline hosts say "Last seen …", so they carry no freshness label.
  const showFreshness = hasSample && host.state === "online";
  return (
    <section className="rounded-lg border bg-card p-4" data-testid={`host-card-${host.host_id}`}>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <h2 className="text-ui font-semibold">{name}</h2>
        <StatePill state={host.state} />
        {showFreshness && (
          <Freshness sampledAtMs={sampledAtMs} staleAfterS={staleAfterS} nowMs={nowMs} />
        )}
      </div>
      {host.state === "offline" && (
        <p className="mb-3 text-sm text-muted-foreground">
          {host.since === null
            ? "Never connected."
            : `Last seen ${relativeTime(host.since * 1000)} ago.`}
        </p>
      )}
      {host.state === "needs_update" && machine === null && (
        <p className="mb-3 text-sm text-muted-foreground">Update host to see resources.</p>
      )}
      {machine !== null && (
        <dl className="grid grid-cols-2 gap-3 sm:grid-cols-3">
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
        </dl>
      )}
      {/* Lines stay editable while a host is offline or has not sent resources yet. */}
      {(machine !== null || host.lines !== undefined) && (
        <HostLoad host={host} isAdmin={isAdmin} defaultLines={defaultLines} />
      )}
      {machine !== null && points.length > 0 && firstEntry !== undefined && (
        <>
          <Sparkline
            points={points}
            threshold={threshold}
            label={`${name} CPU, last 24 hours`}
            className="mt-3"
            latest={hasSample ? (stale ? "stale" : "live") : undefined}
          />
          <SparklineAxis firstT={firstEntry.t} nowMs={nowMs} />
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
              <ProcessTree
                processes={snapshot.processes}
                sessionTitles={sessionTitles}
                nowS={uptimeNowS}
              />
            </div>
          )}
        </div>
      )}
    </section>
  );
}

function MonitorOverheadFooter({
  overhead,
  hostNames,
}: {
  overhead: SystemMonitorOverhead;
  hostNames: Map<string, string>;
}) {
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
          {hostNames.get(hostId) ?? hostId} {formatPct(cost.cpu_pct, 2)} CPU ·{" "}
          {formatBytes(cost.rss_delta)} resident increase (upper bound)
        </span>
      ))}
    </div>
  );
}

export function SystemStatusPage() {
  const isAdmin = useIsAdmin();
  const nowMs = useSecondTick();
  const status = useSystemStatus({ live: true });
  // The thresholds are admin-only; members never see the bytes line.
  const settings = useSystemStatusSettings({ enabled: isAdmin });
  const healthCheck = useStartHealthCheck();
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
  const defaultLines = settings.data?.load_lines?.default ?? null;
  return (
    <PageScroll maxWidthClassName="max-w-5xl" contentClassName="px-8">
      <div className="mb-6 flex items-start justify-between gap-4">
        <div className="flex flex-col gap-1">
          <h1 className="text-2xl font-semibold">System status</h1>
          <p className="text-ui text-muted-foreground">
            Resources on this server and on every visible host, with omnigent's process tree
            attributed to its session.
          </p>
        </div>
        {isAdmin && (
          <Button
            className="shrink-0"
            loading={healthCheck.pending}
            componentId="system.health-check"
            onClick={() => void healthCheck.start()}
          >
            Run health check
          </Button>
        )}
      </div>

      {isAdmin && healthCheck.notice !== null && (
        <HealthCheckNoticeBox notice={healthCheck.notice} />
      )}

      <FindingsBanner
        findings={data.findings}
        hostNames={hostNames}
        sessionTitles={sessionTitles}
        onRunHealthCheck={isAdmin ? () => void healthCheck.start() : undefined}
        healthCheckPending={healthCheck.pending}
      />

      {data.server !== null && (
        <div className="mt-4">
          <ServerCard server={data.server} threshold={threshold} nowMs={nowMs} />
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
              nowMs={nowMs}
              isAdmin={isAdmin}
              defaultLines={defaultLines}
            />
          ))}
        </div>
      )}

      <MonitorOverheadFooter overhead={data.monitor_overhead} hostNames={hostNames} />
    </PageScroll>
  );
}
