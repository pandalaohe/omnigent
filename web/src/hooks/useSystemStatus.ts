// Resource-monitor read path: the server's system-status view, per-target
// history and the admin-editable finding thresholds.
//
// Hand-written shapes that mirror `omnigent/server/system_status.py` (the hub's
// `view` / `history` payloads and the settings mapping) exactly. The sidebar
// summary is nudge-driven — the server pushes `system_status_changed` over the
// session-updates socket and this module refetches `summary=1`; there is no
// resident poll of its own. The full view polls only while a viewer keeps the
// page visible, which is also what renews the hosts' fast-sampling lease.
//
// `*_pct` values are percentages; byte fields are raw bytes.

import { useEffect, useRef, useSyncExternalStore } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";
import { useSessionUpdatesConnected } from "@/hooks/useSessionUpdatesConnected";

/** Worst visible finding level; `"ok"` when there are no findings. */
export type SystemStatusLevel = "ok" | "amber" | "red";

/** One threshold breach or host outage. Findings are always amber or red. */
export interface SystemStatusFinding {
  /** Stable id, e.g. `"host_1:cpu"`; the notice diff keys on it. */
  id: string;
  /** `"server"` or a host id. */
  target: string;
  /** `"cpu" | "mem" | "disk" | "5xx" | "offline"`. */
  kind: string;
  level: "amber" | "red";
  /** Wall-clock seconds when the condition started. */
  since: number;
  detail: string;
  /** Session with the most CPU in the latest minute, when known. */
  top_session: string | null;
}

/** `summary=1` payload: what the sidebar and the notice diff consume. */
export interface SystemStatusSummary {
  revision: number;
  level: SystemStatusLevel;
  findings: SystemStatusFinding[];
}

/** Machine-wide metrics from one host snapshot. */
export interface SystemMachineMetrics {
  cpu_pct: number;
  mem_used: number;
  mem_total: number;
  disk_used: number;
  disk_total: number;
  load1: number | null;
}

/** One omnigent-owned process in a host snapshot (`pid` 0 on folded rows). */
export interface SystemProcessRow {
  pid: number;
  ppid: number;
  name: string;
  role: string;
  session_id: string | null;
  cpu_pct: number;
  rss: number;
  /** Epoch seconds when the process started; absent from older hosts. */
  started_at?: number | null;
}

/** `host.resource_snapshot` as stored by the hub. */
export interface SystemHostSnapshot {
  sampled_at: string;
  interval_s: number;
  machine: SystemMachineMetrics;
  processes: SystemProcessRow[];
  runner_count: number;
  sampler_cpu_ms: number;
  monitor_rss_delta: number;
}

/** `"online" | "offline" | "needs_update"` inventory state of one host. */
export type SystemHostState = "online" | "offline" | "needs_update";

/**
 * Host-reported load level against its two lines; `"unknown"` when the host
 * sends no fresh reading (warmup, stale, disconnected, or too old to report).
 */
export type SystemHostLoadLevel =
  "spare" | "normal" | "heavy_hold" | "new_session_hold" | "unknown";

/** Per-core load lines: heavy work waits at `heavy`, new child sessions queue at `new_session`. */
export interface SystemLoadLinePair {
  heavy: number;
  new_session: number;
}

/** Where a host's effective lines come from. */
export type SystemLoadLinesSource = "host" | "default" | "factory";

/** One inventory row in the full view. */
export interface SystemStatusHost {
  host_id: string;
  owner: string | null;
  name: string;
  state: SystemHostState;
  /** Seconds when the state last changed; `null` for a never-connected host. */
  since: number | null;
  last_snapshot: SystemHostSnapshot | null;
  last_runner_count: number;
  // Host load; absent from servers that predate the load lines.
  /** 1-minute load divided by logical cores. */
  load_per_core?: number | null;
  /** The 1-minute load the per-core value divides; estimated on Windows. */
  load_equiv?: number | null;
  /** Whether `load_equiv` is an estimate (Windows has no native load average). */
  estimate?: boolean;
  cpu_count?: number | null;
  level?: SystemHostLoadLevel;
  /** What set the current hold: `"load"` or `"memory"`. */
  reason?: string | null;
  lines?: SystemLoadLinePair;
  lines_source?: SystemLoadLinesSource;
  /** Agent-created child sessions waiting for this host to drop below its new-session line. */
  queued_children?: number;
  mem_pct?: number | null;
}

/** One server process point; `req` / `err` are deltas since the prior tick. */
export interface SystemServerPoint {
  t: number;
  cpu: number;
  rss: number;
  in_flight: number;
  websockets: number;
  req: number;
  err: number;
  load1: number | null;
  disk_pct: number;
}

/** The server card; `null` for non-admins. */
export interface SystemServerStatus {
  state: "online";
  since: number;
  last_point: SystemServerPoint | null;
}

/** The monitor's own measured cost, shown in the page footer. */
export interface SystemMonitorOverhead {
  server: {
    cpu_pct: number;
    mem_estimate_bytes: number;
    estimated: boolean;
  };
  hosts: Record<string, { cpu_pct: number; rss_delta: number }>;
}

/** Full `GET /v1/system/status` payload. */
export interface SystemStatusView extends SystemStatusSummary {
  server: SystemServerStatus | null;
  hosts: SystemStatusHost[];
  monitor_overhead: SystemMonitorOverhead;
}

/** One host per-minute history point; `top` is `[session_id, cpu_pct]` pairs. */
export interface SystemHostHistoryPoint {
  t: number;
  cpu_avg: number;
  cpu_max: number;
  mem_used: number;
  mem_total: number;
  disk_pct: number;
  load1: number | null;
  top: [string, number][];
}

/** One server per-minute history point; `req` / `err` are tick deltas. */
export interface SystemServerHistoryPoint {
  t: number;
  cpu: number;
  rss: number;
  in_flight: number;
  websockets: number;
  req: number;
  err: number;
  load1: number | null;
  disk_pct: number;
}

/** The history endpoint returns target-specific points (see the two above). */
export type SystemHistoryPoint = SystemHostHistoryPoint | SystemServerHistoryPoint;

/** Ops target for the one-click health check; a null `prompt` means the shipped default. */
export interface HealthCheckSettings {
  project_id: string | null;
  host_id: string | null;
  prompt: string | null;
}

/** The current workspace's load lines: its default, per-host overrides and the queue switch. */
export interface SystemLoadLinesSettings {
  default: SystemLoadLinePair;
  hosts: Record<string, SystemLoadLinePair>;
  /** Whether agent-created child sessions queue on a host above its new-session line. */
  queue_enabled: boolean;
}

/** `GET|PUT /v1/system/settings` mapping. */
export interface SystemStatusSettings {
  cpu_pct: number;
  cpu_sustain_min: number;
  mem_pct: number;
  disk_pct: number;
  server_5xx_pct: number;
  health_check: HealthCheckSettings;
  /** Absent from servers that predate the load lines. */
  load_lines?: SystemLoadLinesSettings;
  /** Shipped prompt to show when `health_check.prompt` is null. */
  default_health_check_prompt: string;
}

/** The numeric fields, edited together by the thresholds form. */
export type SystemStatusThresholds = Pick<
  SystemStatusSettings,
  "cpu_pct" | "cpu_sustain_min" | "mem_pct" | "disk_pct" | "server_5xx_pct"
>;

/** A load-lines patch; a `null` host entry removes its override (follow the default). */
export interface SystemLoadLinesUpdate {
  default?: SystemLoadLinePair;
  hosts?: Record<string, SystemLoadLinePair | null>;
  queue_enabled?: boolean;
}

/** A partial settings PUT body; the server merges the groups it receives. */
export type SystemStatusSettingsUpdate = Partial<
  SystemStatusThresholds & { health_check: HealthCheckSettings; load_lines: SystemLoadLinesUpdate }
>;

/** Highest line the server accepts, per core. */
export const MAX_LOAD_LINE = 64;

/** A typed line value; blank reads as missing rather than 0. */
export function parseLoadLine(text: string): number {
  return text.trim() === "" ? Number.NaN : Number(text);
}

/** The server's rule for a line pair, checked before saving; `null` when valid. */
export function loadLinePairError(heavy: number, newSession: number): string | null {
  if (!Number.isFinite(heavy) || !Number.isFinite(newSession))
    return "Enter both lines as numbers.";
  if (heavy <= 0) return "The heavy-work line must be above 0.";
  if (heavy >= newSession) return "The heavy-work line must be below the new-session line.";
  if (newSession > MAX_LOAD_LINE) return `The new-session line can be at most ${MAX_LOAD_LINE}.`;
  return null;
}

const SUMMARY_QUERY_KEY = ["system-status", "summary"] as const;
const FULL_QUERY_KEY = ["system-status", "full"] as const;
const SETTINGS_QUERY_KEY = ["system-status", "settings"] as const;
const LIVE_INTERVAL_MS = 10_000;
const HISTORY_REFETCH_MS = 60_000;

const historyQueryKey = (target: string) => ["system-status", "history", target] as const;

/** OmnigentError bodies carry ``{"error": {"message"}}``; keep the status otherwise. */
async function errorMessage(res: Response): Promise<string> {
  let message = `${res.status} ${res.statusText}`.trim();
  try {
    const body = (await res.json()) as { error?: { message?: unknown } };
    if (typeof body.error?.message === "string") message = body.error.message;
  } catch {
    /* Non-JSON error body: the status fallback stands. */
  }
  return message;
}

async function fetchSystemStatusSummary(): Promise<SystemStatusSummary> {
  const res = await authenticatedFetch("/v1/system/status?summary=1");
  if (!res.ok) throw new Error(await errorMessage(res));
  return (await res.json()) as SystemStatusSummary;
}

async function fetchSystemStatus(live: boolean): Promise<SystemStatusView> {
  const res = await authenticatedFetch(live ? "/v1/system/status?live=1" : "/v1/system/status");
  if (!res.ok) throw new Error(await errorMessage(res));
  return (await res.json()) as SystemStatusView;
}

async function fetchSystemHistory(target: string): Promise<SystemHistoryPoint[]> {
  const res = await authenticatedFetch(`/v1/system/history?target=${encodeURIComponent(target)}`);
  if (!res.ok) throw new Error(await errorMessage(res));
  const body = (await res.json()) as { target: string; points: SystemHistoryPoint[] };
  return body.points;
}

async function fetchSystemStatusSettings(): Promise<SystemStatusSettings> {
  const res = await authenticatedFetch("/v1/system/settings");
  if (!res.ok) throw new Error(await errorMessage(res));
  return (await res.json()) as SystemStatusSettings;
}

function isDocumentVisible(): boolean {
  return typeof document === "undefined" || document.visibilityState === "visible";
}

function subscribeDocumentVisibility(onChange: () => void): () => void {
  document.addEventListener("visibilitychange", onChange);
  return () => document.removeEventListener("visibilitychange", onChange);
}

/** Whether the tab is visible; re-renders on `visibilitychange`. */
function useDocumentVisible(): boolean {
  return useSyncExternalStore(subscribeDocumentVisibility, isDocumentVisible, () => true);
}

/**
 * The sidebar summary — revision, overall level and findings.
 *
 * Fetched on mount and whenever the server nudges (`system_status_changed`),
 * with no refetch interval: idle users make zero polls. The socket drops
 * events sent while it is down, so a reconnect (false → true) invalidates the
 * summary to catch up.
 */
export function useSystemStatusSummary() {
  const queryClient = useQueryClient();
  const connected = useSessionUpdatesConnected();
  const wasConnected = useRef(connected);
  useEffect(() => {
    const reconnected = connected && !wasConnected.current;
    wasConnected.current = connected;
    if (reconnected) {
      void queryClient.invalidateQueries({ queryKey: SUMMARY_QUERY_KEY });
    }
  }, [connected, queryClient]);

  return useQuery({
    queryKey: SUMMARY_QUERY_KEY,
    queryFn: fetchSystemStatusSummary,
  });
}

/**
 * The full view.
 *
 * With ``live`` the query polls every 10 s while the tab is visible; hiding
 * the tab stops the poll (the host sampling lease then lapses back to 60 s),
 * and returning to it refetches at once so the lease renews without waiting a
 * full interval. The ``live=1`` query parameter marks the returned hosts as
 * watched by this viewer.
 */
export function useSystemStatus(options: { live: boolean }) {
  const visible = useDocumentVisible();
  const query = useQuery({
    queryKey: FULL_QUERY_KEY,
    queryFn: () => fetchSystemStatus(options.live && isDocumentVisible()),
    refetchInterval: options.live && visible ? LIVE_INTERVAL_MS : false,
  });
  const refetchRef = useRef(query.refetch);
  refetchRef.current = query.refetch;
  const wasVisible = useRef(visible);
  useEffect(() => {
    const becameVisible = visible && !wasVisible.current;
    wasVisible.current = visible;
    if (options.live && becameVisible) void refetchRef.current();
  }, [options.live, visible]);
  return query;
}

/** One target's 24 h per-minute points; refetched every minute while mounted. */
export function useSystemHistory(target: string | null, options: { enabled?: boolean } = {}) {
  const enabled = options.enabled ?? true;
  return useQuery({
    queryKey: historyQueryKey(target ?? ""),
    queryFn: () => fetchSystemHistory(target as string),
    enabled: enabled && target !== null,
    refetchInterval: HISTORY_REFETCH_MS,
  });
}

/** The finding thresholds (admin only; callers pass ``enabled`` once known). */
export function useSystemStatusSettings(options: { enabled?: boolean } = {}) {
  return useQuery({
    queryKey: SETTINGS_QUERY_KEY,
    queryFn: fetchSystemStatusSettings,
    enabled: options.enabled ?? true,
    staleTime: 5_000,
  });
}

/** PUT /v1/system/settings — validate and store the fields present in the body. */
export function useUpdateSystemStatusSettings() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (settings: SystemStatusSettingsUpdate): Promise<SystemStatusSettings> => {
      const res = await authenticatedFetch("/v1/system/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(settings),
      });
      if (!res.ok) throw new Error(await errorMessage(res));
      return (await res.json()) as SystemStatusSettings;
    },
    // Stay pending until the refetches land, so an editor reopened right after
    // a save starts from the saved lines rather than the previous ones.
    onSuccess: () =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: SETTINGS_QUERY_KEY }),
        // Host cards show each host's effective lines from the status view.
        queryClient.invalidateQueries({ queryKey: FULL_QUERY_KEY }),
      ]),
  });
}
