// The resource-monitor page (``/system``): the server process's own metrics,
// every visible host's machine metrics and 24 h CPU trend, and each host's
// omnigent process tree attributed to its session.
//
// A member receives `server: null` and only their own hosts — the page just
// renders what the permission-filtered response contains.

import {
  Fragment,
  memo,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
  type RefObject,
} from "react";
import { ChevronDownIcon, ChevronRightIcon } from "lucide-react";
import { PageScroll } from "@/components/PageScroll";
import { Sparkline } from "@/components/Sparkline";
import { CliRetentionFold } from "@/components/CliRetentionSettings";
import { MonitorSettingsDialog } from "@/components/MonitorSettings";
import { HostRestartControl } from "@/components/HostRestartControl";
import { HostTerminalLink } from "@/components/HostTerminalLink";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { useIsAdmin } from "@/hooks/useIsAdmin";
import { useHosts, type Host } from "@/hooks/useHosts";
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
  type SystemProcessRow,
  type SystemServerStatus,
  type SystemStatusSettings,
  type SystemStatusSettingsUpdate,
  type SystemStatusFinding,
  type SystemStatusHost,
} from "@/hooks/useSystemStatus";
import { Link, useSearchParams } from "@/lib/routing";
import { relativeTime } from "@/lib/relativeTime";
import { formatBytes } from "@/shell/fileStatusUtils";
import { cn } from "@/lib/utils";

function formatPct(value: number, digits = 1): string {
  return `${value.toFixed(digits)}%`;
}

function formatLoad(load1: number | null): string {
  return load1 === null ? "No reading" : load1.toFixed(1);
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

function Metric({
  label,
  value,
  alert,
}: {
  label: string;
  value: string;
  /** The alert line that governs this metric, when it has one. */
  alert?: ReactNode;
}) {
  return (
    <div>
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="text-ui font-semibold tabular-nums">{value}</dd>
      {alert}
    </div>
  );
}

// The monitor's four alert lines. They are one server-wide value each (not a
// per-host one), which is why they are edited here once and not on a card.
type AlertField = "cpu_pct" | "mem_pct" | "disk_pct" | "server_5xx_pct";

const ALERT_SPECS: Record<AlertField, { metric: string; window?: string; sustained?: boolean }> = {
  cpu_pct: {
    metric: "CPU",
    window: "The whole sustained window has to stay above it; short spikes do not notify.",
    sustained: true,
  },
  mem_pct: { metric: "Memory", window: "Above it for 2 minutes in a row notifies." },
  disk_pct: { metric: "Disk", window: "The newest minute above it notifies." },
  server_5xx_pct: {
    metric: "5xx failure rate",
    window: "Over 5 minutes with at least 20 requests notifies.",
  },
};

/** A percentage as typed: 85, or 62.5, never 85.0. */
function formatThreshold(value: number): string {
  return String(Math.round(value * 100) / 100);
}

function alertError(percent: number, minutes: number | null): string | null {
  if (!Number.isFinite(percent) || percent <= 0 || percent > 100) {
    return "Enter a percentage above 0 and at most 100.";
  }
  if (minutes !== null && (!Number.isSafeInteger(minutes) || minutes < 1 || minutes > 1440)) {
    return "Enter the sustained window as whole minutes from 1 to 1440.";
  }
  return null;
}

/**
 * One alert line under its metric. The number is the button: opening it shows
 * the value and the sustained window, Enter saves, Esc leaves, and a value the
 * server would refuse stays here as a message instead of being sent.
 */
function MetricAlert({
  field,
  threshold,
  sustainMinutes,
}: {
  field: AlertField;
  threshold: number;
  sustainMinutes?: number;
}) {
  const spec = ALERT_SPECS[field];
  const update = useUpdateSystemStatusSettings();
  const [open, setOpen] = useState(false);
  // The stored number, not its rounded label: changing only the window must send
  // the percentage back exactly as it came.
  const [percent, setPercent] = useState(() => String(threshold));
  const [minutes, setMinutes] = useState(() =>
    sustainMinutes === undefined ? "" : String(sustainMinutes),
  );
  const [error, setError] = useState<string | null>(null);

  // Every open starts from what the server has, so a refused edit leaves no draft.
  useEffect(() => {
    if (open) return;
    setPercent(String(threshold));
    setMinutes(sustainMinutes === undefined ? "" : String(sustainMinutes));
    setError(null);
  }, [open, threshold, sustainMinutes]);

  const parsedPercent = percent.trim() === "" ? Number.NaN : Number(percent);
  const parsedMinutes = spec.sustained
    ? minutes.trim() === ""
      ? Number.NaN
      : Number(minutes)
    : null;
  const invalid = alertError(parsedPercent, parsedMinutes);

  const save = () => {
    if (invalid !== null) {
      setError(invalid);
      return;
    }
    setError(null);
    let patch: SystemStatusSettingsUpdate;
    if (spec.sustained) {
      patch = { cpu_pct: parsedPercent, cpu_sustain_min: parsedMinutes as number };
    } else if (field === "mem_pct") {
      patch = { mem_pct: parsedPercent };
    } else if (field === "disk_pct") {
      patch = { disk_pct: parsedPercent };
    } else {
      patch = { server_5xx_pct: parsedPercent };
    }
    update.mutate(patch, {
      onSuccess: () => setOpen(false),
      onError: (failure) => setError(failure.message),
    });
  };

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          data-testid={`alert-${field}`}
          aria-label={`Alert line for ${spec.metric}, currently ${formatThreshold(threshold)} percent`}
          className="mt-1 inline-flex min-h-6 items-center gap-1 rounded-sm text-xs text-amber-700 outline-none hover:underline focus-visible:ring-2 focus-visible:ring-ring dark:text-amber-400"
        >
          alert <span className="tabular-nums">{formatThreshold(threshold)}%</span>
        </button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-64">
        <p className="text-sm font-medium">Alert line for {spec.metric}</p>
        <form
          className="flex flex-col gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            save();
          }}
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              event.preventDefault();
              setOpen(false);
              return;
            }
            // Enter saves from a value field; on a button it would replace that
            // button's own action.
            if (event.key === "Enter" && event.target instanceof HTMLInputElement) {
              event.preventDefault();
              save();
            }
          }}
        >
          <label className="flex items-center justify-between gap-2 text-sm">
            Above
            <span className="flex items-center gap-1">
              <Input
                autoFocus
                type="number"
                min={0}
                max={100}
                step="any"
                aria-label={`Alert when ${spec.metric} is above, in percent`}
                className="h-9 w-20 text-right tabular-nums"
                value={percent}
                onChange={(event) => {
                  setPercent(event.target.value);
                  setError(null);
                }}
              />
              <span className="text-muted-foreground">%</span>
            </span>
          </label>
          {spec.sustained && (
            <label className="flex items-center justify-between gap-2 text-sm">
              For at least
              <span className="flex items-center gap-1">
                <Input
                  type="number"
                  min={1}
                  max={1440}
                  step={1}
                  aria-label="Sustained window, in minutes"
                  className="h-9 w-20 text-right tabular-nums"
                  value={minutes}
                  onChange={(event) => {
                    setMinutes(event.target.value);
                    setError(null);
                  }}
                />
                <span className="text-muted-foreground">min</span>
              </span>
            </label>
          )}
          <p className="text-xs text-muted-foreground">
            Every host shares this line. {spec.window}
          </p>
          {error !== null && (
            <p role="alert" className="text-xs text-destructive">
              {error}
            </p>
          )}
          <div className="flex items-center gap-2">
            <Button type="submit" size="sm" loading={update.isPending}>
              Save
            </Button>
          </div>
        </form>
      </PopoverContent>
    </Popover>
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
          <Link to="/system?monitor=1" className="underline">
            Open Monitor settings
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
  settings,
  nowMs,
}: {
  server: SystemServerStatus;
  threshold: number | null;
  /** The alert lines, known to admins only; absent leaves the metrics plain. */
  settings: SystemStatusSettings | null;
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
          <dl className="grid grid-cols-2 gap-x-3 gap-y-4 sm:grid-cols-3 lg:grid-cols-4">
            <Metric
              label="CPU"
              value={formatPct(point.cpu)}
              alert={
                settings !== null ? (
                  <MetricAlert
                    field="cpu_pct"
                    threshold={settings.cpu_pct}
                    sustainMinutes={settings.cpu_sustain_min}
                  />
                ) : undefined
              }
            />
            <Metric
              label="Memory"
              value={formatBytes(point.rss)}
              alert={
                settings !== null ? (
                  <MetricAlert field="mem_pct" threshold={settings.mem_pct} />
                ) : undefined
              }
            />
            <Metric
              label="Disk"
              value={formatPct(point.disk_pct)}
              alert={
                settings !== null ? (
                  <MetricAlert field="disk_pct" threshold={settings.disk_pct} />
                ) : undefined
              }
            />
            <Metric
              label="5xx failure rate"
              value={failureRate === null ? "No requests" : formatPct(failureRate)}
              alert={
                settings !== null ? (
                  <MetricAlert field="server_5xx_pct" threshold={settings.server_5xx_pct} />
                ) : undefined
              }
            />
            <Metric label="Load" value={formatLoad(point.load1)} />
            <Metric label="In-flight requests" value={String(point.in_flight)} />
            <Metric label="Open connections" value={String(point.websockets)} />
          </dl>
          {points.length > 0 && firstEntry !== undefined && (
            <>
              <Sparkline
                points={points}
                threshold={threshold}
                thresholdLabel={threshold === null ? undefined : `${formatThreshold(threshold)}%`}
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

/** Where a load value sits on the ruler, in percent of its width. */
function scalePosition(point: number, lines: SystemLoadLinePair): number {
  const max = lines.new_session * LOAD_SCALE_HEADROOM;
  return Math.min(100, Math.max(0, (point / max) * 100));
}

/** A tick label stays inside the ruler even when its line is at an edge. */
function tickPosition(point: number, lines: SystemLoadLinePair): number {
  return Math.min(96, Math.max(4, scalePosition(point, lines)));
}

/** Room between two tick labels when their lines sit close together, in px. */
const TICK_LABEL_GAP_PX = 34;

/**
 * Move tick labels apart until none is closer than `gap` to its neighbour while
 * keeping their order; the marks on the bar keep the true positions. Two lines
 * a few hundredths apart (4.9 and 5) would otherwise draw their numbers on top
 * of each other.
 */
export function fanOutLabels(positions: number[], gap: number, width: number): number[] {
  if (positions.length < 2 || width <= 0) return positions;
  const order = positions
    .map((position, index) => ({ position, index }))
    .sort((left, right) => left.position - right.position);
  const placed = order.map((entry) => entry.position);
  for (let index = 1; index < placed.length; index += 1) {
    const overlap = placed[index - 1] + gap - placed[index];
    if (overlap <= 0) continue;
    placed[index - 1] -= overlap / 2;
    placed[index] += overlap / 2;
  }
  // Keep every label's centre inside the ruler, then restore the gap the clamp
  // just squeezed, spreading leftwards from the right edge.
  const half = gap / 2;
  for (let index = 0; index < placed.length; index += 1) {
    placed[index] = Math.min(width - half, Math.max(half, placed[index]));
  }
  const last = placed.length - 1;
  if (placed[last] > width - half) placed[last] = width - half;
  for (let index = last; index > 0; index -= 1) {
    if (placed[index] - placed[index - 1] < gap) placed[index - 1] = placed[index] - gap;
  }
  const out = [...positions];
  order.forEach((entry, index) => {
    out[entry.index] = Math.max(0, placed[index]);
  });
  return out;
}

/** The ruler's own width, so tick labels can be laid out in real pixels. */
function useRulerWidth(ref: RefObject<HTMLDivElement | null>): number {
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const node = ref.current;
    if (node === null) return;
    const measure = () => setWidth(node.getBoundingClientRect().width);
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => observer.disconnect();
  }, [ref]);
  return width;
}

type LineName = "heavy" | "new_session";

const LINE_TITLES: Record<LineName, string> = {
  heavy: "Heavy work",
  new_session: "No new session",
};

/**
 * The editor behind one tick: this host's value for that line, Enter to save,
 * Esc to leave, and "Reset to default" once this host has lines of its own.
 * The pair has to satisfy the server's rule before it is sent.
 */
function HostLineEditor({
  host,
  line,
  lines,
  hasOwnLines,
  onClose,
}: {
  host: SystemStatusHost;
  line: LineName;
  lines: SystemLoadLinePair;
  hasOwnLines: boolean;
  onClose: () => void;
}) {
  const update = useUpdateSystemStatusSettings();
  const [text, setText] = useState(String(lines[line]));
  const [error, setError] = useState<string | null>(null);

  const save = () => {
    const typed = parseLoadLine(text);
    const pair: SystemLoadLinePair =
      line === "heavy"
        ? { heavy: typed, new_session: lines.new_session }
        : { heavy: lines.heavy, new_session: typed };
    const invalid = loadLinePairError(pair.heavy, pair.new_session);
    if (invalid !== null) {
      setError(invalid);
      return;
    }
    setError(null);
    update.mutate(
      { load_lines: { hosts: { [host.host_id]: pair } } },
      { onSuccess: onClose, onError: (failure) => setError(failure.message) },
    );
  };

  const resetToDefault = () => {
    setError(null);
    update.mutate(
      { load_lines: { hosts: { [host.host_id]: null } } },
      { onSuccess: onClose, onError: (failure) => setError(failure.message) },
    );
  };

  return (
    <form
      className="flex flex-col gap-2"
      data-testid={`host-line-form-${line}`}
      onSubmit={(event) => {
        event.preventDefault();
        save();
      }}
      onKeyDown={(event) => {
        if (event.key === "Escape") {
          event.preventDefault();
          onClose();
          return;
        }
        // Enter saves from the value field only, so a focused Reset keeps its
        // own action; the implicit submit is suppressed so nothing sends twice.
        if (event.key === "Enter" && event.target instanceof HTMLInputElement) {
          event.preventDefault();
          save();
        }
      }}
    >
      <p className="text-sm font-medium">{LINE_TITLES[line]} (per core)</p>
      <Input
        autoFocus
        type="number"
        min={0}
        max={MAX_LOAD_LINE}
        step="any"
        aria-label={`${LINE_TITLES[line]} (per core)`}
        className="h-9 w-24 text-right tabular-nums"
        value={text}
        onChange={(event) => {
          setText(event.target.value);
          setError(null);
        }}
      />
      <p className="text-xs text-muted-foreground">
        {line === "heavy"
          ? "At or above it, new heavy work waits on this host."
          : `Must be above Heavy work, at most ${String(MAX_LOAD_LINE)}. New child sessions wait here.`}
      </p>
      {error !== null && (
        <p role="alert" className="text-xs text-destructive">
          {error}
        </p>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <Button type="submit" size="sm" loading={update.isPending}>
          Save
        </Button>
        {hasOwnLines && (
          <Button type="button" size="sm" variant="ghost" onClick={resetToDefault}>
            Reset to default
          </Button>
        )}
      </div>
    </form>
  );
}

/**
 * The load ruler: the two lines as numbered ticks on a scale, each a button an
 * admin opens to change this host's value, the bands below them, and the
 * current load as a dot. A member sees the same numbers, unclickable.
 */
function LoadRuler({
  host,
  lines,
  value,
  isAdmin,
}: {
  host: SystemStatusHost;
  lines: SystemLoadLinePair;
  value: number | null;
  isAdmin: boolean;
}) {
  const [editing, setEditing] = useState<LineName | null>(null);
  const ticksRef = useRef<HTMLDivElement | null>(null);
  const width = useRulerWidth(ticksRef);
  const hasOwnLines = host.lines_source === "host";
  const lineNames = Object.keys(LINE_TITLES) as LineName[];
  const natural = lineNames.map((line) => (tickPosition(lines[line], lines) / 100) * width);
  const placed = width > 0 ? fanOutLabels(natural, TICK_LABEL_GAP_PX, width) : null;
  return (
    <div className="mt-2" data-testid="host-load-ruler">
      <div className="relative h-6" ref={ticksRef}>
        {placed !== null &&
          lineNames.map((line, index) => (
            // The mark keeps the value's own place when the label beside it has
            // to move over to clear its neighbour.
            <span
              key={line}
              aria-hidden="true"
              className="absolute bottom-0 h-2 w-px bg-current opacity-40"
              data-testid={`host-load-mark-${line}`}
              style={{ left: `${natural[index]}px` }}
            />
          ))}
        {lineNames.map((line, index) => {
          const tone =
            line === "heavy"
              ? "text-amber-700 dark:text-amber-400"
              : "text-red-700 dark:text-red-400";
          const label = String(lines[line]);
          return (
            <div
              key={line}
              className="absolute top-0 -translate-x-1/2"
              style={{
                left:
                  placed === null ? `${tickPosition(lines[line], lines)}%` : `${placed[index]}px`,
              }}
            >
              {isAdmin ? (
                <Popover
                  open={editing === line}
                  onOpenChange={(next) => setEditing(next ? line : null)}
                >
                  <PopoverTrigger asChild>
                    <button
                      type="button"
                      data-testid={`host-load-tick-${line}`}
                      aria-label={`${LINE_TITLES[line]} line, currently ${label} per core. Click to change it for this host.`}
                      className={cn(
                        "min-h-6 min-w-6 rounded-sm px-1 text-xs font-semibold tabular-nums outline-none hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring",
                        tone,
                      )}
                    >
                      {label}
                    </button>
                  </PopoverTrigger>
                  <PopoverContent align="center" side="top" className="w-64">
                    <HostLineEditor
                      host={host}
                      line={line}
                      lines={lines}
                      hasOwnLines={hasOwnLines}
                      onClose={() => setEditing(null)}
                    />
                  </PopoverContent>
                </Popover>
              ) : (
                <span
                  data-testid={`host-load-tick-${line}`}
                  className={cn("px-1 text-xs font-semibold tabular-nums", tone)}
                >
                  {label}
                </span>
              )}
            </div>
          );
        })}
      </div>
      <div
        role="img"
        aria-label={`${value === null ? "No current load" : `${value.toFixed(2)} per core`}; heavy work ${String(lines.heavy)}, no new session ${String(lines.new_session)}`}
        className="relative mt-1 h-2 rounded-full bg-muted"
        data-testid="host-load-scale"
      >
        <span
          className="absolute inset-y-0 left-0 rounded-l-full bg-emerald-500/25"
          style={{ width: `${scalePosition(Math.min(1, lines.heavy), lines)}%` }}
        />
        <span
          className="absolute inset-y-0 bg-amber-500/30"
          style={{
            left: `${scalePosition(lines.heavy, lines)}%`,
            width: `calc(${scalePosition(lines.new_session, lines)}% - ${scalePosition(lines.heavy, lines)}%)`,
          }}
        />
        <span
          className="absolute inset-y-0 right-0 rounded-r-full bg-red-500/30"
          style={{ left: `${scalePosition(lines.new_session, lines)}%` }}
        />
        <span
          className="absolute -top-1 h-4 w-0.5 -translate-x-1/2 bg-amber-600 dark:bg-amber-400"
          style={{ left: `${scalePosition(lines.heavy, lines)}%` }}
          data-testid="host-load-heavy-mark"
        />
        <span
          className="absolute -top-1 h-4 w-0.5 -translate-x-1/2 bg-red-600 dark:bg-red-400"
          style={{ left: `${scalePosition(lines.new_session, lines)}%` }}
          data-testid="host-load-new-session-mark"
        />
        {value !== null && (
          <span
            className="absolute top-1/2 size-3 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-card bg-foreground"
            style={{ left: `${scalePosition(value, lines)}%` }}
            data-testid="host-load-marker"
          />
        )}
      </div>
      <div className="mt-1 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
        <span className="inline-flex items-center gap-1">
          <span className="h-2.5 w-0.5 bg-amber-600 dark:bg-amber-400" aria-hidden="true" />
          Heavy work
        </span>
        <span className="inline-flex items-center gap-1">
          <span className="h-2.5 w-0.5 bg-red-600 dark:bg-red-400" aria-hidden="true" />
          No new session
        </span>
        {hasOwnLines && (
          <span
            className="inline-flex items-center gap-1 font-medium text-foreground"
            data-testid="host-load-own-lines"
          >
            <span className="size-1.5 shrink-0 rounded-full bg-pink-500" aria-hidden="true" />
            Own lines
          </span>
        )}
      </div>
    </div>
  );
}

/**
 * The Load row: load per core and what it divides, the host's level against its
 * two lines, the ruler, and queued child sessions.
 */
function HostLoad({ host, isAdmin }: { host: SystemStatusHost; isAdmin: boolean }) {
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
        {perCore === null ? (
          <span className="text-muted-foreground">No reading per core</span>
        ) : (
          <span className="font-semibold tabular-nums">{perCore.toFixed(2)} per core</span>
        )}{" "}
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
          <LoadRuler host={host} lines={lines} value={perCore} isAdmin={isAdmin} />
          {queued > 0 && (
            <p
              className="mt-1 text-xs text-amber-700 dark:text-amber-400"
              data-testid="host-load-queued"
            >
              {queued === 1 ? "1 child session queued" : `${queued} child sessions queued`} until
              this host drops below No new session
            </p>
          )}
        </>
      )}
    </div>
  );
}

export function HostCard({
  host,
  threshold,
  thresholdLabel,
  sessionTitles,
  nowMs,
  isAdmin = false,
  controlHost,
  ownedHost,
}: {
  host: SystemStatusHost;
  threshold: number | null;
  /** Label for the chart's alert line, when the alert is known. */
  thresholdLabel?: string;
  sessionTitles: Map<string, string>;
  nowMs: number;
  /** Shows the ruler's per-host line edit. */
  isAdmin?: boolean;
  /** Owned host from /v1/hosts, with current control capabilities. */
  controlHost?: Host;
  /** The same owner's row, for this card's idle-CLI fold. */
  ownedHost?: Host;
}) {
  const history = useSystemHistory(host.host_id);
  const isMobileViewport = useIsMobileViewport();
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
        {controlHost && (
          <div
            className={cn(
              "flex items-center gap-2 [&>button]:ml-0 max-md:[&>button]:min-h-10",
              isMobileViewport ? "w-full" : "ml-auto",
            )}
          >
            <HostTerminalLink host={controlHost} />
            <HostRestartControl host={controlHost} sessionTitles={sessionTitles} />
          </div>
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
      {(machine !== null || host.lines !== undefined) && <HostLoad host={host} isAdmin={isAdmin} />}
      {machine !== null && points.length > 0 && firstEntry !== undefined && (
        <>
          <Sparkline
            points={points}
            threshold={threshold}
            thresholdLabel={thresholdLabel}
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
              <p className="mb-1 text-sm text-muted-foreground">
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
      {ownedHost !== undefined && <CliRetentionFold host={ownedHost} />}
    </section>
  );
}

export function SystemStatusPage() {
  const isAdmin = useIsAdmin();
  const { data: ownedHosts } = useHosts();
  const nowMs = useSecondTick();
  // `?monitor=1` is how the old Settings link opens this panel; clearing it
  // means a reload does not reopen the panel.
  const [searchParams, setSearchParams] = useSearchParams();
  const monitorOpen = searchParams.get("monitor") === "1";
  const setMonitorOpen = (open: boolean) => {
    const next = new URLSearchParams(searchParams);
    if (open) next.set("monitor", "1");
    else next.delete("monitor");
    setSearchParams(next, { replace: true });
  };
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
  if (status.data === undefined) {
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
  const thresholdLabel = threshold === null ? undefined : `${formatThreshold(threshold)}%`;
  return (
    <PageScroll maxWidthClassName="max-w-5xl" contentClassName="px-8">
      <div className="mb-6 flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-medium">System status</h1>
        <div className="flex flex-wrap items-center gap-2">
          {isAdmin && (
            <Button
              variant="outline"
              className="shrink-0"
              componentId="system.monitor-settings"
              onClick={() => setMonitorOpen(true)}
            >
              Monitor settings
            </Button>
          )}
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
      </div>

      {isAdmin && healthCheck.notice !== null && (
        <HealthCheckNoticeBox notice={healthCheck.notice} />
      )}

      {status.isError && (
        <p role="alert" className="mb-4 text-ui text-destructive">
          Unable to refresh system status. Showing the last received data.
        </p>
      )}

      <FindingsBanner
        findings={data.findings}
        hostNames={hostNames}
        sessionTitles={sessionTitles}
      />

      {data.server !== null && (
        <div className="mt-4">
          <ServerCard
            server={data.server}
            threshold={threshold}
            settings={settings.data ?? null}
            nowMs={nowMs}
          />
        </div>
      )}

      <h2 className="mt-6 mb-2 text-ui font-semibold">Hosts</h2>
      {data.hosts.length === 0 ? (
        <p className="text-ui text-muted-foreground">No hosts connected.</p>
      ) : (
        <div className="grid gap-3 lg:grid-cols-2">
          {data.hosts.map((host) => {
            const ownedHost = ownedHosts?.find((owned) => owned.host_id === host.host_id);
            return (
              <HostCard
                key={host.host_id}
                host={host}
                threshold={threshold}
                thresholdLabel={thresholdLabel}
                sessionTitles={sessionTitles}
                nowMs={nowMs}
                isAdmin={isAdmin}
                controlHost={ownedHost}
                ownedHost={ownedHost}
              />
            );
          })}
        </div>
      )}

      <MonitorSettingsDialog
        open={monitorOpen}
        onOpenChange={setMonitorOpen}
        overhead={data.monitor_overhead}
        hostNames={hostNames}
      />
    </PageScroll>
  );
}
