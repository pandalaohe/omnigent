import { t } from "./taskBoardText";
import type { BoardSession } from "./taskBoardNative";

export interface TaskRef {
  source: string;
  uid: string;
}
export interface DisplayCost {
  amount: number | null;
  kind: "actual" | "estimate";
  tokens?: number | null;
  sourceLabel?: string;
}
export interface DisplayTool {
  id: string;
  name: string;
  label: string;
  startedAt: string;
  limitSeconds?: number;
}
export interface DisplaySession {
  nativeStatus?: string;
  id: string;
  task: TaskRef;
  parentId?: string;
  role: "root" | "child" | "peer";
  agentId: string;
  label: string;
  title: string;
  host?: string;
  model?: string;
  effort?: string;
  status: "running" | "idle" | "archived";
  createdAt: string;
  lastActivityAt: string;
  runningSince?: string;
  runningSeconds?: number;
  idleSeconds?: number;
  archivedAt?: string;
  endedAt?: string;
  now?: string;
  next?: string;
  need?: string;
  last?: string;
  context?: number;
  compactAt?: number;
  tool?: DisplayTool;
  questions?: { id: string; text: string }[];
  steps?: { id: string; kind: string; label: string; state: "done" | "current" | "future" }[];
  cost?: DisplayCost;
}
export interface DisplayInterval {
  id: string;
  from: string;
  to: string | null;
  agentId: string;
  host?: string;
  sessionId?: string;
  label?: string;
  origin: "event" | "session";
  tokens?: number | null;
  cost?: DisplayCost;
}
export interface TaskDisplay {
  version: 1;
  identity?: {
    key: string;
    label: string;
    groupId: string;
    groupLabel?: string;
    groupBadge?: string;
    rank?: number;
  };
  current?: string;
  need?: string;
  next?: string;
  state?: "done" | "ready" | "running" | "user" | "failed" | "blocked" | "queued" | "held";
  lastActivity?: string | null;
  badges?: {
    id: string;
    label: string;
    tone?: "neutral" | "info" | "success" | "warning" | "danger";
  }[];
  rows?: { id: string; label: string; value: string }[];
  links?: { id: string; label: string; href: string }[];
  actors?: { id: string; agentId: string; label: string; host?: string }[];
  sessions?: DisplaySession[];
  intervals?: DisplayInterval[];
  nodeSelection?: "sessions-first" | "intervals";
  checks?: {
    id: string;
    host: string;
    verdict: "passed" | "failed" | "flaky";
    count: number;
    at: string;
    seconds?: number;
    head?: string;
    sourceLabel?: string;
  }[];
  quotes?: {
    id: string;
    text: string;
    task: TaskRef;
    date?: string;
    at?: string;
    rank?: number;
    sourceLabel?: string;
  }[];
  commits?: { id: string; sha: string; subject: string; at: string }[];
  events?: {
    id: string;
    at: string;
    kind: "open" | "start" | "transfer" | "stop" | "outcome" | "check" | "commit" | "other";
    task: TaskRef;
    label?: string;
    from?: string;
    workSeconds?: number;
    checkId?: string;
    commitIds?: string[];
    outcome?: string;
    rank?: number;
    agentId?: string;
    host?: string;
    rootSessionId?: string;
    sourceLabel?: string;
  }[];
  relations?: {
    id: string;
    from: TaskRef;
    to: TaskRef;
    kind: "parent" | "dependency" | "include";
    label?: string;
    origin: "observed" | "manual";
    provenance: string;
    since?: string;
    rank?: number;
  }[];
  hub?: { scope: "explicit"; members: TaskRef[] };
  historyState?: {
    first: string;
    last: string;
    end?: string;
    outcome: "completed" | "merged" | "dropped" | "split" | "unknown";
    active: boolean;
    pinned: boolean;
    future: boolean;
    commitCount?: number;
  };
  omitted?: Record<string, number>;
}

const stamp = (value: number | null | undefined) =>
  value == null ? undefined : new Date(value * 1000).toISOString().replace(".000Z", "Z");

/** Replace the entire runtime tuple by full session id; preserve source attribution. */
export function mergeDisplaySession(source: DisplaySession, native?: BoardSession): DisplaySession {
  if (!native || native.id !== source.id) return source;
  const {
    status: _status,
    lastActivityAt: _last,
    runningSince: _since,
    runningSeconds: _running,
    idleSeconds: _idle,
    archivedAt: _archive,
    endedAt: _ended,
    tool: _tool,
    questions: _questions,
    need: _need,
    ...identity
  } = source;
  return {
    ...identity,
    nativeStatus: native.status,
    status: native.archived ? "archived" : native.status === "running" ? "running" : "idle",
    lastActivityAt: stamp(native.updated_at) ?? "",
    archivedAt: stamp(native.archived_at),
    runningSince: stamp(native.running_since),
    runningSeconds: native.running_seconds,
    idleSeconds: native.idle_seconds,
    endedAt: stamp(native.ended_at),
    tool: native.current_tool,
    questions: native.questions,
    need:
      native.status === "waiting" || (native.pending_elicitations_count ?? 0) > 0
        ? t("session.waiting")
        : native.need,
  };
}

export function sessionObservation(session: DisplaySession, asOf: string) {
  const at = Date.parse(asOf);
  const elapsed = (value?: string) =>
    value ? Math.max(0, (at - Date.parse(value)) / 1000) : undefined;
  const idle = session.idleSeconds ?? elapsed(session.lastActivityAt);
  const running = session.runningSeconds ?? elapsed(session.runningSince);
  const toolElapsed = elapsed(session.tool?.startedAt);
  const liveTool =
    session.tool &&
    toolElapsed !== undefined &&
    (session.tool.limitSeconds === undefined || toolElapsed < session.tool.limitSeconds);
  const state =
    session.nativeStatus === "failed"
      ? "failed"
      : session.status === "archived"
        ? "archived"
        : session.need || session.questions?.length
          ? "waiting"
          : session.status === "running" && (idle ?? 0) >= 1200 && !liveTool
            ? "stale"
            : session.status;
  return {
    state,
    idleSeconds: idle,
    runningSeconds: liveTool ? toolElapsed : running,
    workEnd:
      session.archivedAt ??
      (session.status === "running" ? asOf : (session.endedAt ?? session.lastActivityAt)),
    costEnd: session.archivedAt ?? session.endedAt ?? session.lastActivityAt,
  };
}

export function displayCost(display: TaskDisplay): DisplayCost & { unpriced: number } {
  const nodes =
    display.nodeSelection !== "intervals" && display.sessions?.length
      ? display.sessions
      : (display.intervals ?? []);
  let amount: number | null = null,
    kind: DisplayCost["kind"] = "actual",
    unpriced = 0;
  for (const node of nodes) {
    if (node.cost?.amount == null) {
      unpriced += 1;
      continue;
    }
    amount = (amount ?? 0) + node.cost.amount;
    if (node.cost.kind === "estimate") kind = "estimate";
  }
  return { amount, kind, unpriced };
}
