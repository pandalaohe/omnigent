import { taskIdentity, type BoardTask } from "./taskBoardData";
import { mergeDisplaySession, type DisplayCost } from "./taskBoardDisplay";
import type { BoardSession } from "./taskBoardNative";
import { accountDayAmounts, type SourceCache } from "./taskBoardSnapshotData";

export const DAY = 86400000;
export interface HistoryNode {
  id: string;
  from: number;
  to: number;
  agent: string;
  host?: string;
  session?: string;
  tokens?: number | null;
  cost?: DisplayCost;
}
export interface HistoryCard {
  task: BoardTask;
  id: string;
  source: string;
  group: string;
  groupLabel: string;
  first: number;
  end: number;
  outcome: string;
  nodes: HistoryNode[];
  usd: number | null;
  seconds: number;
  agents: string[];
}
export function historyCards(
  tasks: BoardTask[],
  sessions: BoardSession[],
  now: number,
  caches: Record<string, SourceCache> = {},
): HistoryCard[] {
  const native = new Map(sessions.map((s) => [s.id, s]));
  return tasks.flatMap((task) => {
    const d = task["omnigent.ai:sourceDisplay"],
      h = d?.historyState;
    const end = Date.parse(h?.end ?? h?.last ?? task.progressUpdated ?? task.updated ?? "");
    if (
      h?.pinned ||
      h?.future ||
      (!h?.end &&
        (!!h || (task.progress !== "completed" && task.progress !== "cancelled")) &&
        now - end <= DAY) ||
      !Number.isFinite(end)
    )
      return [];
    const nodes: HistoryNode[] =
      d?.nodeSelection !== "intervals" && d?.sessions?.length
        ? d.sessions.map((source) => {
            const s = mergeDisplaySession(source, native.get(source.id));
            return {
              id: s.id,
              from: Date.parse(s.createdAt),
              to: Date.parse(s.archivedAt ?? s.endedAt ?? s.lastActivityAt),
              agent: s.agentId,
              host: s.host,
              session: s.id,
              cost: s.cost,
              tokens: s.cost?.tokens,
            };
          })
        : (d?.intervals ?? []).map((n) => ({
            id: n.id,
            from: Date.parse(n.from),
            to: Date.parse(
              n.to ??
                caches[task["omnigent.ai:source"]]?.display.asOf ??
                new Date(now).toISOString(),
            ),
            agent: n.agentId,
            host: n.host,
            session: n.sessionId,
            tokens: n.tokens,
            cost: n.cost,
          }));
    const priced = nodes.filter((n) => n.cost?.amount != null);
    const group = d?.identity?.groupId ?? task["omnigent.ai:group"]?.id ?? "";
    return [
      {
        task,
        id: taskIdentity(task),
        source: task["omnigent.ai:source"],
        group,
        groupLabel: d?.identity?.groupLabel ?? task["omnigent.ai:group"]?.name ?? group,
        first: Date.parse(h?.first ?? task.created ?? "") || end,
        end,
        outcome:
          h?.outcome === "unknown"
            ? ""
            : (h?.outcome ?? (task.progress === "completed" ? "completed" : "")),
        nodes,
        usd: priced.length ? priced.reduce((sum, n) => sum + (n.cost?.amount ?? 0), 0) : null,
        seconds: nodes.reduce((sum, n) => sum + Math.max(0, (n.to - n.from) / 1000), 0),
        agents: [...new Set(nodes.map((n) => n.agent))],
      },
    ];
  });
}
export const historySort = (sort: string) => (a: HistoryCard, b: HistoryCard) =>
  sort === "cost"
    ? (b.usd ?? 0) - (a.usd ?? 0)
    : sort === "time"
      ? b.seconds - a.seconds
      : sort === "size"
        ? b.nodes.length - a.nodes.length
        : b.end - a.end;
export function historyRoll(cards: HistoryCard[]) {
  const nodes = cards.flatMap((c) => c.nodes),
    priced = nodes.filter((n) => n.cost?.amount != null),
    durationNodes = nodes.filter((n) => Number.isFinite(n.from) && Number.isFinite(n.to));
  return {
    n: cards.length,
    seconds: durationNodes.reduce(
      (sum, node) => sum + Math.max(0, (node.to - node.from) / 1000),
      0,
    ),
    durationNodes: durationNodes.length,
    nodes: nodes.length,
    unpriced: nodes.length - priced.length,
    usd: priced.length ? priced.reduce((sum, n) => sum + (n.cost?.amount ?? 0), 0) : null,
    latest: Math.max(0, ...cards.map((c) => c.end)),
    good: cards.filter((c) => ["completed", "merged"].includes(c.outcome)).length,
    muted: cards.filter((c) => ["dropped", "split"].includes(c.outcome)).length,
  };
}
export function accountTotals(caches: Record<string, SourceCache>, from: string, to: string) {
  const totals = new Map<string, { label: string; amount: number | null }>();
  const labels = new Map(
    Object.values(caches).flatMap((c) =>
      (c.display.accounts ?? []).map(
        (a) => [JSON.stringify([a.ref.namespace, a.ref.id]), a.label] as const,
      ),
    ),
  );
  for (const [key, value] of accountDayAmounts(caches)) {
    const [namespace, id, date] = JSON.parse(key) as string[];
    if (date < from || date > to) continue;
    const ref = JSON.stringify([namespace, id]),
      old = totals.get(ref);
    totals.set(ref, {
      label: labels.get(ref) ?? id,
      amount: value === null || old?.amount === null ? null : (old?.amount ?? 0) + value,
    });
  }
  return totals;
}

export const activityColors = [
  "#ee352e",
  "#0091c8",
  "#00933c",
  "#b933ad",
  "#ff6319",
  "#2f7fd4",
  "#d4972a",
  "#00a1a7",
  "#7c5cdb",
  "#df3c85",
  "#6cbe45",
  "#996633",
];
