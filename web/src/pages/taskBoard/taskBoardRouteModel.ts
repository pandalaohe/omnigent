import { graphlib, layout as layoutGraph } from "@dagrejs/dagre";
import { timeHour, timeDay, timeMonday, timeMonth, timeYear } from "d3-time";
import { taskIdentity, taskProgress, type BoardTask, type TaskSource } from "./taskBoardData";
import type { TaskDisplay } from "./taskBoardDisplay";
import {
  DAY,
  HOUR,
  MIN_SPAN,
  initialWindow,
  taskLocalTime,
  taskRelations,
  type TimeWindow,
} from "./taskBoardRouteData";
import type { SourceCache } from "./taskBoardSnapshotData";

export const routeColors = [
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
export type RouteState = NonNullable<TaskDisplay["state"]>;
/** A card runs only while a session works on it: a running source session or an open work span. */
export const routeLive = (task: BoardTask) =>
  !!task["omnigent.ai:sourceDisplay"]?.sessions?.some((session) => session.status === "running") ||
  !!task["omnigent.ai:workedBy"]?.some((span) => !span.to);
export const routeState = (task: BoardTask): RouteState =>
  task["omnigent.ai:sourceDisplay"]?.state ??
  (task["omnigent.ai:waiting"]
    ? task["omnigent.ai:waiting"].on === "user"
      ? "user"
      : "blocked"
    : taskProgress(task) === "in-process"
      ? routeLive(task)
        ? "running"
        : "held"
      : ["completed", "cancelled"].includes(taskProgress(task))
        ? "done"
        : taskProgress(task) === "failed"
          ? "failed"
          : "queued");
export const routeAttention = (task: BoardTask) =>
  ["running", "user", "failed"].includes(routeState(task));
export const routeQueued = (task: BoardTask) => ["queued", "blocked"].includes(routeState(task));
export function routeMoment(task: BoardTask, now: number): number | undefined {
  if (["running", "user"].includes(routeState(task))) return now;
  const display = task["omnigent.ai:sourceDisplay"];
  const supplied = [
    display?.historyState?.end ?? display?.historyState?.last,
    display?.lastActivity,
    task.progressUpdated,
    task["omnigent.ai:closedSeen"],
  ].flatMap((value) => (value && Number.isFinite(Date.parse(value)) ? [Date.parse(value)] : []));
  if (supplied.length) return Math.max(...supplied);
  const fallback = task.updated ?? task.created;
  return fallback && Number.isFinite(Date.parse(fallback)) ? Date.parse(fallback) : undefined;
}
export interface MapGroup {
  id: string;
  source: string;
  name: string;
  badge: string;
  color: string;
  tasks: BoardTask[];
}
export function mapGroups(
  tasks: BoardTask[],
  sources: TaskSource[],
  caches: Record<string, SourceCache>,
  now = Date.now(),
  boardHours = 24,
): MapGroup[] {
  const keyed = new Map<string, MapGroup>();
  for (const task of tasks) {
    const source = task["omnigent.ai:source"],
      identity = task["omnigent.ai:sourceDisplay"]?.identity,
      groupId = identity?.groupId ?? task["omnigent.ai:group"]?.id ?? "",
      id = JSON.stringify([source, groupId]);
    if (!keyed.has(id)) {
      const supplied = caches[source]?.display.groups?.find((group) => group.id === groupId);
      let name = (
        supplied?.label ??
        identity?.groupLabel ??
        task["omnigent.ai:group"]?.name ??
        groupId
      )
        .split(/[—:：;；,，(（]/)[0]
        .trim();
      if (name.toLocaleLowerCase().startsWith(source.toLocaleLowerCase()))
        name = name.slice(source.length).trim() || name;
      keyed.set(id, {
        id,
        source,
        name,
        badge: supplied?.badge ?? identity?.groupBadge ?? groupId,
        color: "",
        tasks: [],
      });
    }
    keyed.get(id)!.tasks.push(task);
  }
  const groups = orderMapGroups([...keyed.values()], tasks, sources),
    used = new Map<string, Set<number>>(),
    positions = new Map(groups.map((group, index) => [group.id, index]));
  const colorOrder = [...groups].sort((a, b) => {
    const rank = (group: MapGroup) => {
      const onBoard = group.tasks.some((task) => {
        const closed = task.progressUpdated ?? task["omnigent.ai:closedSeen"];
        return (
          (task["omnigent.ai:sourceDisplay"]?.historyState?.active !== false ||
            routeAttention(task)) &&
          (routeState(task) !== "done" || !closed || Date.parse(closed) >= now - boardHours * HOUR)
        );
      });
      return onBoard
        ? (caches[group.source]?.display.groups?.find(
            (item) => JSON.stringify([group.source, item.id]) === group.id,
          )?.rank ?? Number.MAX_SAFE_INTEGER)
        : Number.MAX_SAFE_INTEGER;
    };
    const liveSession = (group: MapGroup) =>
      group.tasks.some((task) =>
        task["omnigent.ai:sourceDisplay"]?.sessions?.some(
          (session) => session.status === "running",
        ),
      );
    return (
      Number(liveSession(b)) - Number(liveSession(a)) ||
      rank(a) - rank(b) ||
      positions.get(a.id)! - positions.get(b.id)!
    );
  });
  for (const group of colorOrder) {
    const id = JSON.parse(group.id)[1] as string,
      seed = `${group.source}/${id}`;
    let hash = 0;
    for (let i = 0; i < seed.length; i++) hash = (hash * 31 + seed.charCodeAt(i)) >>> 0;
    const taken = used.get(group.source) ?? new Set<number>();
    let index = hash % routeColors.length;
    for (let step = 0; step < routeColors.length; step++) {
      const candidate = (hash + step) % routeColors.length;
      if (!taken.has(candidate)) {
        index = candidate;
        break;
      }
    }
    group.color = routeColors[index];
    taken.add(index);
    used.set(group.source, taken);
    group.tasks.sort(
      (a, b) =>
        (a["omnigent.ai:sourceDisplay"]?.identity?.rank ?? Number.MAX_SAFE_INTEGER) -
          (b["omnigent.ai:sourceDisplay"]?.identity?.rank ?? Number.MAX_SAFE_INTEGER) ||
        (a.created ?? "").localeCompare(b.created ?? ""),
    );
  }
  return groups;
}
const compareText = (a: string, b: string) => (a < b ? -1 : a > b ? 1 : 0);
let linkedOrder: { structure: string; order: [string, string[]][] } | undefined;
/** Order linked groups by source block: dagre seeds the order, a bounded swap search refines it. */
function arrangeLinkedGroups(
  members: [string, string[]][],
  links: { pair: [string, string]; weight: number }[],
): [string, string[]][] {
  const positions = new Map<string, number>(),
    count = members.reduce((sum, [, ids]) => sum + ids.length, 0);
  if (count > 0 && count <= 150) {
    // Longest-path ranking keeps dagre's ordering phase, at a fifth of network-simplex's cost here.
    const graph = new graphlib.Graph({ compound: true })
      .setGraph({ rankdir: "LR", nodesep: 10, ranksep: 40, ranker: "longest-path" })
      .setDefaultEdgeLabel(() => ({}));
    for (const [source, ids] of members) {
      graph.setNode(`source:${source}`, {});
      for (const id of ids) {
        graph.setNode(id, { width: 100, height: 20 });
        graph.setParent(id, `source:${source}`);
      }
    }
    links.forEach(({ pair: [a, b], weight }, index) => {
      const joint = `joint:${index}`;
      graph.setNode(joint, { width: 4, height: 4 });
      graph.setEdge(a, joint, { weight });
      graph.setEdge(b, joint, { weight });
    });
    layoutGraph(graph);
    for (const [, ids] of members)
      for (const id of ids) positions.set(id, graph.node(id).y as number);
  }
  const mean = (ids: string[]) =>
    positions.size ? ids.reduce((sum, id) => sum + positions.get(id)!, 0) / ids.length : 0;
  const arranged = [...members]
    .sort((a, b) => mean(a[1]) - mean(b[1]))
    .map(([source, ids]) => ({
      source,
      rows: [...ids].sort((a, b) => (positions.get(a) ?? 0) - (positions.get(b) ?? 0)),
    }));
  const slot = new Map(members.flatMap(([, ids]) => ids).map((id, index) => [id, index])),
    ends = links.map(({ pair: [a, b] }) => [slot.get(a)!, slot.get(b)!]),
    weights = Float64Array.from(links, (link) => link.weight),
    at = new Int32Array(slot.size),
    starts = new Int32Array(links.length),
    stops = new Int32Array(links.length);
  const cost = () => {
    let row = 0;
    for (const source of arranged) for (const id of source.rows) at[slot.get(id)!] = row++;
    let score = 0;
    for (let i = 0; i < ends.length; i++) {
      const x = at[ends[i][0]],
        y = at[ends[i][1]];
      starts[i] = Math.min(x, y);
      stops[i] = Math.max(x, y);
      score += weights[i] * Math.max(0, stops[i] - starts[i] - 1);
    }
    for (let i = 0; i < ends.length; i++)
      for (let j = i + 1; j < ends.length; j++) {
        const a = starts[i],
          b = stops[i],
          c = starts[j],
          d = stops[j];
        if ((a < c && c < b && b < d) || (c < a && a < d && d < b))
          score += 2 * weights[i] * weights[j];
      }
    return score;
  };
  let best = cost(),
    changed = true;
  const started = performance.now();
  while (changed && performance.now() - started < 300) {
    changed = false;
    for (let i = 0; i < arranged.length; i++)
      for (let j = i + 1; j < arranged.length; j++) {
        [arranged[i], arranged[j]] = [arranged[j], arranged[i]];
        const next = cost();
        if (next < best) {
          best = next;
          changed = true;
        } else [arranged[i], arranged[j]] = [arranged[j], arranged[i]];
      }
    for (const source of arranged)
      for (let i = 0; i + 1 < source.rows.length; i++) {
        [source.rows[i], source.rows[i + 1]] = [source.rows[i + 1], source.rows[i]];
        const next = cost();
        if (next < best) {
          best = next;
          changed = true;
        } else [source.rows[i], source.rows[i + 1]] = [source.rows[i + 1], source.rows[i]];
      }
  }
  return arranged.map(({ source, rows }) => [source, rows]);
}
function orderMapGroups(
  groups: MapGroup[],
  tasks: BoardTask[],
  sourceMetadata: TaskSource[],
): MapGroup[] {
  const groupByTask = new Map(
    groups.flatMap((group) => group.tasks.map((task) => [taskIdentity(task), group.id] as const)),
  );
  const attentionCounts = new Map(
      groups.map((group) => [group.id, group.tasks.filter(routeAttention).length]),
    ),
    recentTimes = new Map(
      groups.map((group) => [
        group.id,
        Math.max(
          0,
          ...group.tasks.map((task) => {
            const display = task["omnigent.ai:sourceDisplay"];
            return (
              Date.parse(
                display
                  ? (display.historyState?.end ?? display.historyState?.last ?? "")
                  : (task.updated ?? task.created ?? ""),
              ) || 0
            );
          }),
        ),
      ]),
    );
  const attention = (group: MapGroup) => attentionCounts.get(group.id)!;
  const recent = (group: MapGroup) => recentTimes.get(group.id)!;
  const bySource = new Map<string, MapGroup[]>();
  for (const group of groups)
    bySource.set(group.source, [...(bySource.get(group.source) ?? []), group]);
  const priority = (a: MapGroup, b: MapGroup) =>
    attention(b) - attention(a) || recent(b) - recent(a);
  const sources = [...bySource]
    .map(([source, rows]) => ({
      source,
      rows: rows.sort(priority),
      attention: rows.reduce((sum, row) => sum + attention(row), 0),
      last: Math.max(0, ...rows.map(recent)),
    }))
    .sort(
      (a, b) =>
        b.attention - a.attention ||
        b.last - a.last ||
        sourceMetadata.findIndex((source) => source.id === a.source) -
          sourceMetadata.findIndex((source) => source.id === b.source),
    );
  const hubMembers = new Map(
    tasks.flatMap((task) => {
      const members = task["omnigent.ai:sourceDisplay"]?.hub?.members;
      return members
        ? [
            [
              taskIdentity(task),
              new Set(members.map((ref) => JSON.stringify([ref.source, ref.uid]))),
            ] as const,
          ]
        : [];
    }),
  );
  // Many relations join the same two groups: lay out and score each group pair once, weighted.
  const pairs = new Map<string, { pair: [string, string]; weight: number }>();
  for (const edge of mapRelations(tasks)) {
    const from = taskIdentity(edge.from),
      to = taskIdentity(edge.to);
    if (hubMembers.get(from)?.has(to)) continue;
    const a = groupByTask.get(from),
      b = groupByTask.get(to);
    if (!a || !b || a === b) continue;
    const pair: [string, string] = a < b ? [a, b] : [b, a],
      key = JSON.stringify(pair),
      known = pairs.get(key);
    if (known) known.weight++;
    else pairs.set(key, { pair, weight: 1 });
  }
  const links = [...pairs.values()].sort((a, b) =>
      compareText(a.pair.join("\n"), b.pair.join("\n")),
    ),
    linked = new Set(links.flatMap((link) => link.pair)),
    members = sources
      .flatMap((source) => {
        const ids = source.rows.flatMap((group) => (linked.has(group.id) ? [group.id] : []));
        return ids.length ? [[source.source, ids.sort(compareText)] as [string, string[]]] : [];
      })
      .sort((a, b) => compareText(a[0], b[0]));
  // The linked order depends on structure only, so refreshes that change no link reuse it.
  const structure = JSON.stringify([members, links]);
  if (linkedOrder?.structure !== structure)
    linkedOrder = { structure, order: arrangeLinkedGroups(members, links) };
  const byId = new Map(groups.map((group) => [group.id, group]));
  const arranged = linkedOrder.order.map(([source, ids]) => ({
    source,
    rows: ids.map((id) => byId.get(id)!),
  }));
  const attentionCost = (list: typeof arranged) =>
    list.reduce(
      (sum, source, index) =>
        sum + (sources.find((item) => item.source === source.source)?.attention ?? 0) * index,
      0,
    );
  if (attentionCost([...arranged].reverse()) < attentionCost(arranged)) arranged.reverse();
  const insertRest = (source: (typeof sources)[number], linkedRows: MapGroup[]) => {
    const rest = source.rows.filter((group) => !linkedRows.includes(group));
    return [
      ...rest.filter((group) => attention(group) > 0),
      ...linkedRows,
      ...rest.filter((group) => !attention(group)),
    ];
  };
  const connected = new Set(arranged.map((source) => source.source));
  return [
    ...sources
      .filter((source) => source.attention && !connected.has(source.source))
      .flatMap((source) => insertRest(source, [])),
    ...arranged.flatMap((source) =>
      insertRest(
        sources.find((item) => item.source === source.source)!,
        source.rows,
      ),
    ),
    ...sources
      .filter((source) => !source.attention && !connected.has(source.source))
      .flatMap((source) => insertRest(source, [])),
  ];
}

export interface MapRelation {
  id: string;
  from: BoardTask;
  to: BoardTask;
  kind: "parent" | "dependency" | "include";
  label?: string;
  origin?: "observed" | "manual";
  provenance?: string;
  since?: string;
  rank?: number;
  labelKey?: "relation.next";
}
export function mapRelations(tasks: BoardTask[]): MapRelation[] {
  const byId = new Map(tasks.map((task) => [taskIdentity(task), task]));
  const out = new Map<string, MapRelation>();
  for (const task of tasks)
    for (const relation of task["omnigent.ai:sourceDisplay"]?.relations ?? []) {
      const from = byId.get(JSON.stringify([relation.from.source, relation.from.uid])),
        to = byId.get(JSON.stringify([relation.to.source, relation.to.uid]));
      if (!from || !to) continue;
      const id = JSON.stringify([taskIdentity(from), taskIdentity(to), relation.kind]);
      out.set(id, { ...relation, id, from, to });
    }
  for (const relation of taskRelations(tasks)) {
    if (!relation.to) continue;
    const kind = relation.kind === "next" ? "include" : relation.kind,
      id = JSON.stringify([taskIdentity(relation.from), taskIdentity(relation.to), kind]);
    if (!out.has(id))
      out.set(id, {
        id,
        from: relation.from,
        to: relation.to,
        kind,
        ...(relation.kind === "next" ? { labelKey: "relation.next" as const } : {}),
      });
  }
  return [...out.values()].sort(
    (a, b) => (a.rank ?? Number.MAX_SAFE_INTEGER) - (b.rank ?? Number.MAX_SAFE_INTEGER),
  );
}
export function mapHubMembers(task: BoardTask, tasks: BoardTask[]): BoardTask[] {
  const members = task["omnigent.ai:sourceDisplay"]?.hub?.members;
  if (!members) return [];
  const byId = new Map(tasks.map((item) => [taskIdentity(item), item]));
  return [...new Set(members.map((ref) => JSON.stringify([ref.source, ref.uid])))].flatMap((id) =>
    byId.has(id) ? [byId.get(id)!] : [],
  );
}
export interface MapFold {
  tasks: BoardTask[];
  queued: boolean;
}
const urgency: Partial<Record<RouteState, number>> = { running: 0, user: 1, failed: 2, blocked: 3 };
/** Important cards first: running, waiting on you, failed, blocked, then the most recent. */
const byUrgency = (tasks: BoardTask[]) =>
  tasks
    .map((task, index) => ({ task, index }))
    .sort(
      (a, b) =>
        (urgency[routeState(a.task)] ?? 9) - (urgency[routeState(b.task)] ?? 9) ||
        b.index - a.index,
    )
    .map((item) => item.task);
/**
 * Fit a row into `room` stops. Important cards stay stations; the rest keep the newest finished and
 * the next queued in turn, and whatever does not fit folds in place into counted capsules.
 */
export function orderStops(
  tasks: BoardTask[],
  room: number,
  collapsed = false,
  important: (task: BoardTask) => boolean = routeAttention,
): MapFold[] {
  const fold = (items: BoardTask[]) => ({ tasks: items, queued: items.every(routeQueued) });
  const past = tasks.filter((task) => !routeQueued(task)),
    queued = tasks.filter(routeQueued);
  if (collapsed) {
    const shown = new Set(byUrgency(past.filter(important)).slice(0, Math.max(0, room - 2)));
    return [
      past.filter((task) => !shown.has(task)),
      ...past.filter((task) => shown.has(task)).map((task) => [task]),
      queued,
    ]
      .filter((items) => items.length)
      .map(fold);
  }
  if (tasks.length <= room) return tasks.map((task) => fold([task]));
  const finished = past.filter((task) => !important(task)).reverse(),
    next = queued.filter((task) => !important(task)),
    candidates = byUrgency(tasks.filter(important));
  for (let i = 0; i < Math.max(finished.length, next.length); i++)
    candidates.push(...(next[i] ? [next[i]] : []), ...(finished[i] ? [finished[i]] : []));
  // Entries = kept stations + runs of folded cards; keeping a card changes that by 0, 1 or 2.
  const index = new Map(tasks.map((task, position) => [task, position])),
    kept = new Array<boolean>(tasks.length).fill(false);
  let entries = 1;
  for (const task of candidates) {
    const at = index.get(task)!,
      left = at > 0 && !kept[at - 1],
      right = at + 1 < tasks.length && !kept[at + 1],
      change = left && right ? 2 : left || right ? 1 : 0;
    if (entries + change > room) continue;
    kept[at] = true;
    entries += change;
  }
  const out: MapFold[] = [];
  let run: BoardTask[] = [];
  tasks.forEach((task, at) => {
    if (!kept[at]) {
      run.push(task);
      return;
    }
    if (run.length) out.push(fold(run));
    run = [];
    out.push(fold([task]));
  });
  if (run.length) out.push(fold(run));
  return out;
}
export const routePresetHours = [12, 48, 96, 192, 744, 2184, 8784] as const;
export function clampMapWindow(view: TimeWindow, oldest: number, now: number): TimeWindow {
  const maximum = Math.max(2 * DAY, now - oldest + DAY),
    span = Math.min(maximum, Math.max(MIN_SPAN, view.to - view.from));
  const future = span <= 2 * DAY ? span / 2 : Math.max(DAY, span * 0.02);
  const from = Math.max(oldest - 3 * HOUR, Math.min(now + future - span, view.from));
  return { from, to: from + span };
}
export function stepMapWindow(
  view: TimeWindow,
  direction: number,
  oldest: number,
  now: number,
): TimeWindow {
  const span = view.to - view.from,
    hasNow = view.from <= now && view.to >= now;
  const rungs = [
    ...routePresetHours.map((hours) => hours * HOUR),
    Math.max(2 * DAY, now - oldest + DAY),
  ]
    .filter((value, index, list) => list.indexOf(value) === index)
    .sort((a, b) => a - b);
  if (direction < 0 && hasNow && span < 2 * DAY * 0.95) return view;
  if (direction < 0 && hasNow && span <= 2 * DAY * 1.05) return initialWindow(now);
  const next =
    direction > 0
      ? rungs.find((value) => value > span * 1.05)
      : [...rungs].reverse().find((value) => value < span * 0.95);
  if (!next) return view;
  if (hasNow && Math.abs(next - 2 * DAY) < HOUR) return initialWindow(now);
  return clampMapWindow({ from: view.to - next, to: view.to }, oldest, now);
}
export interface MapDensity {
  from: number;
  to: number;
  count: number;
  tasks: BoardTask[];
}
export function mapAxisTicks(view: TimeWindow, width: number, minPx = 72) {
  const rungs: [ReturnType<typeof timeHour.every>, number][] = [
    [timeHour.every(1), HOUR],
    [timeHour.every(2), 2 * HOUR],
    [timeHour.every(3), 3 * HOUR],
    [timeHour.every(6), 6 * HOUR],
    [timeHour.every(12), 12 * HOUR],
    [timeDay, DAY],
    [timeDay.every(2), 2 * DAY],
    [timeMonday, 7 * DAY],
    [timeMonth, 30.4 * DAY],
    [timeMonth.every(2), 61 * DAY],
    [timeMonth.every(3), 91 * DAY],
    [timeMonth.every(6), 182 * DAY],
    [timeYear, 365 * DAY],
    [timeYear.every(2), 730 * DAY],
  ];
  const [interval, step] =
    rungs.find(([, duration]) => (duration * width) / (view.to - view.from) >= minPx) ??
    rungs.at(-1)!;
  return { ticks: interval!.range(new Date(view.from), new Date(view.to + 1)), step };
}
export function mapDensity(
  tasks: BoardTask[],
  view: TimeWindow,
  width: number,
  now: number,
  cache?: SourceCache,
): MapDensity[] {
  const interval =
    ([
      [timeDay, 1],
      [timeDay.every(2)!, 2],
      [timeMonday, 7],
      [timeMonth, 30.4],
      [timeMonth.every(3)!, 91],
      [timeMonth.every(6)!, 182],
      [timeYear, 365],
    ].find(([, days]) => width / ((view.to - view.from) / DAY / (days as number)) >= 4.5)?.[0] as
      typeof timeDay | undefined) ?? timeYear;
  const buckets = new Map<number, MapDensity>();
  const put = (at: number, count: number, task?: BoardTask) => {
    if (
      !Number.isFinite(at) ||
      at > now ||
      at < +timeDay.floor(new Date(view.from)) ||
      at >= +timeDay.offset(timeDay.floor(new Date(view.to)), 1)
    )
      return;
    const from = +interval.floor(new Date(at)),
      to = +interval.offset(new Date(from), 1);
    const bucket = buckets.get(from) ?? { from, to, count: 0, tasks: [] };
    bucket.count += count;
    if (task) bucket.tasks.push(task);
    buckets.set(from, bucket);
  };
  if (cache?.display.closedDays?.length) {
    for (const day of cache.display.closedDays)
      put(taskLocalTime(`${day.date}T00:00:00`, cache.display.timeZone), day.count);
  } else
    for (const task of tasks) {
      const historical = task["omnigent.ai:sourceDisplay"]?.historyState;
      const at = historical
        ? Date.parse(historical.end ?? historical.last)
        : routeMoment(task, now);
      if (at !== undefined && !routeQueued(task)) put(at, 1, task);
    }
  return [...buckets.values()].sort((a, b) => a.from - b.from);
}
export function mapOverview(
  tasks: BoardTask[],
  caches: Record<string, SourceCache>,
  view: TimeWindow,
  width: number,
  now: number,
): MapDensity[] {
  void width;
  const days: number[] = [];
  for (let day = timeDay.floor(new Date(view.from)); +day <= now; day = timeDay.offset(day, 1))
    days.push(+day);
  const counts = new Map<number, number>();
  const sources = new Set([
    ...tasks.map((task) => task["omnigent.ai:source"]),
    ...Object.keys(caches),
  ]);
  for (const source of sources)
    for (const bucket of mapDensity(
      tasks.filter((task) => task["omnigent.ai:source"] === source),
      view,
      Number.MAX_SAFE_INTEGER,
      now,
      caches[source],
    )) {
      counts.set(bucket.from, (counts.get(bucket.from) ?? 0) + bucket.count);
    }
  const prefix = [0];
  for (const day of days) prefix.push(prefix.at(-1)! + (counts.get(day) ?? 0));
  return Array.from({ length: 240 }, (_, index) => ({
    from: view.from + (index / 240) * (now - view.from),
    to: view.from + ((index + 1) / 240) * (now - view.from),
    count:
      prefix[Math.floor(((index + 1) * days.length) / 240)] -
      prefix[Math.floor((index * days.length) / 240)],
    tasks: [],
  }));
}
export function roundedRoutePath(points: { x: number; y: number }[], radius = 16) {
  let path = `M${points[0].x} ${points[0].y}`;
  for (let i = 1; i < points.length - 1; i++) {
    const point = points[i],
      previous = points[i - 1],
      next = points[i + 1],
      a = Math.hypot(point.x - previous.x, point.y - previous.y),
      b = Math.hypot(next.x - point.x, next.y - point.y);
    if (!a || !b) continue;
    const r = Math.min(radius, a / 2, b / 2);
    path += ` L${point.x + ((previous.x - point.x) / a) * r} ${point.y + ((previous.y - point.y) / a) * r} Q${point.x} ${point.y} ${point.x + ((next.x - point.x) / b) * r} ${point.y + ((next.y - point.y) / b) * r}`;
  }
  return path + ` L${points.at(-1)!.x} ${points.at(-1)!.y}`;
}

export interface MapLane {
  id: string;
  source: string;
  name: string;
  badge: string;
  color: string;
  tasks: BoardTask[];
  height: number;
  top: number;
  header?: boolean;
  collapsed?: boolean;
  quiet?: number;
  pager?: boolean;
  page?: number;
  pages?: number;
  nodes: MapNode[];
  count?: number;
  summary?: string;
  total?: number;
  historyStart?: number;
}
export interface MapNode {
  id: string;
  lane: string;
  tasks: BoardTask[];
  x: number;
  x1: number;
  y: number;
  top: number;
  height: number;
  color: string;
  queued: boolean;
  label?: { x: number; y: number; width: number };
  lines?: string[];
  sub?: string;
  timeCluster?: boolean;
  hub?: boolean;
  scheduled?: boolean;
  density?: MapDensity;
}
export interface MapEdge {
  id: string;
  from: MapNode;
  to: MapNode;
  path: string;
  long: boolean;
  hub: boolean;
  relations: MapRelation[];
  points: { x: number; y: number }[];
  badge?: { x: number; y: number };
}
export function routeLabelLines(text: string): string[] {
  const tokens = text.match(/[A-Za-z0-9.\-_/]+|\s+|./g) ?? [];
  const cost = (token: string) =>
    /^[A-Za-z0-9.\-_/]/.test(token) ? token.length * 0.56 : /^\s/.test(token) ? 0.3 : 1;
  const total = tokens.reduce((sum, token) => sum + cost(token), 0);
  if (total <= 8.5) return [text];
  let best = 1,
    score = Infinity,
    acc = 0;
  tokens.forEach((token, index) => {
    acc += cost(token);
    if (index === tokens.length - 1 || /^[，、。）:]/.test(tokens[index + 1])) return;
    let next = Math.max(acc, total - acc);
    if (/^[，、]/.test(token) || /^\s/.test(token)) next -= 0.8;
    if (Math.min(acc, total - acc) < 2) next += 9;
    if (next < score) {
      score = next;
      best = index + 1;
    }
  });
  return [tokens.slice(0, best).join("").trim(), tokens.slice(best).join("").trim()];
}
interface RoutePoint {
  x: number;
  y: number;
}
interface RouteBox extends RoutePoint {
  w: number;
  h: number;
}
export function routeSegmentHits(
  a: RoutePoint,
  b: RoutePoint,
  box: RouteBox,
  pad: number,
): boolean {
  let low = 0,
    high = 1;
  const dx = b.x - a.x,
    dy = b.y - a.y;
  for (const [p, q] of [
    [-dx, a.x - box.x + pad],
    [dx, box.x + box.w + pad - a.x],
    [-dy, a.y - box.y + pad],
    [dy, box.y + box.h + pad - a.y],
  ]) {
    if (p === 0) {
      if (q < 0) return false;
      continue;
    }
    const ratio = q / p;
    if (p < 0) {
      if (ratio > high) return false;
      low = Math.max(low, ratio);
    } else {
      if (ratio < low) return false;
      high = Math.min(high, ratio);
    }
  }
  return true;
}
export const routeNodeRadius = (node: MapNode) =>
  node.scheduled
    ? node.x1 > node.x
      ? 15
      : 9
    : node.tasks.length > 1
      ? 15
      : node.tasks[0] && routeState(node.tasks[0]) === "running"
        ? node.hub
          ? 19
          : 17
        : node.tasks[0] && routeState(node.tasks[0]) === "user"
          ? 15
          : node.tasks[0] && routeState(node.tasks[0]) === "failed"
            ? 14
            : node.queued
              ? 10
              : 12;
export function placeMapLabels(
  nodes: MapNode[],
  segs: { a: RoutePoint; b: RoutePoint; half: number }[],
  lanes: MapLane[],
  width: number,
  gutter: number,
  measure: (text: string, size: number, weight: number) => number,
  compact = false,
) {
  const labelled = nodes.filter((node) => node.lines?.length),
    placed: RouteBox[] = [];
  const boxes = labelled.map((node) => {
    const radius = routeNodeRadius(node),
      half = (node.x1 - node.x) / 2 + radius;
    return {
      x: (node.x + node.x1) / 2 - half - 2,
      y: node.y - radius - 2,
      w: half * 2 + 4,
      h: radius * 2 + 4,
    };
  });
  labelled.forEach((node) => {
    if (node.hub) boxes.push({ x: node.x + 14, y: node.y - 27, w: 28, h: 22 });
  });
  const touches = (a: RouteBox, b: RouteBox, pad: number) =>
    a.x < b.x + b.w + pad &&
    b.x < a.x + a.w + pad &&
    a.y < b.y + b.h + pad &&
    b.y < a.y + a.h + pad;
  const rank = (node: MapNode) =>
    node.hub
      ? 0
      : (node.tasks.length === 1
          ? { running: 1, user: 2, failed: 3, ready: 4, held: 5, done: 5, queued: 6, blocked: 6 }[
              routeState(node.tasks[0])
            ]
          : 7) + (node.tasks.length > 1 ? 1 : 0);
  [...labelled]
    .sort((a, b) => rank(a) - rank(b) || b.x - a.x)
    .forEach((node) => {
      const w =
        Math.ceil(
          Math.max(
            ...node.lines!.map((line) =>
              measure(line, (node.hub ? 14 : 13) - (compact ? 1 : 0), 700),
            ),
            node.sub ? measure(node.sub, 12, 400) : 0,
          ),
        ) + 2;
      const h = node.lines!.length * (compact ? 15 : 17) + (node.sub ? 15 : 0),
        gap = routeNodeRadius(node) + 4,
        cx = (node.x + node.x1) / 2,
        lane = lanes.find((item) => item.id === node.lane)!;
      let found: RouteBox | undefined,
        best = Infinity;
      for (const dy of [-(gap + h), gap])
        for (const dx of [
          0,
          10,
          -10,
          22,
          -22,
          36,
          -36,
          w / 2 + 4,
          -(w / 2 + 4),
          w / 2 + 20,
          -(w / 2 + 20),
        ]) {
          const box = { x: cx + dx - w / 2, y: node.y + dy, w, h };
          if (
            box.x < gutter + 4 ||
            box.x + w > width - 2 ||
            box.y < lane.top + (width <= 620 ? 26 : 0) + 2 ||
            box.y + h > lane.top + lane.height - 2
          )
            continue;
          const cost = Math.abs(dx) + (dy > 0 ? 6 : 0);
          if (
            cost < best &&
            !segs.some((segment) =>
              routeSegmentHits(segment.a, segment.b, box, segment.half + 3),
            ) &&
            !placed.some((other) => touches(box, other, 4)) &&
            !boxes.some((other) => touches(box, other, 1))
          ) {
            best = cost;
            found = box;
          }
        }
      if (found) {
        node.label = { x: found.x, y: found.y, width: found.w };
        placed.push(found);
      }
    });
}
export function mapScene(
  lanes: MapLane[],
  mode: "order" | "time",
  view: TimeWindow,
  width: number,
  now: number,
  level: "station" | "phase" | "segment",
  relations: MapRelation[],
  hubs: Map<string, Set<string>>,
  caches: Record<string, SourceCache>,
  label: (node: MapNode) => string | { lines: string[]; sub: string },
  measure: (text: string, size: number, weight: number) => number = (text, size) =>
    text.length * size * 0.56,
  options: { compact?: boolean; important?: (task: BoardTask) => boolean } = {},
) {
  const phone = width <= 620;
  if (phone && mode === "order") return { nodes: [] as MapNode[], edges: [] as MapEdge[], lanes };
  const gutter = phone ? 14 : 208,
    right = width - (mode === "order" ? 58 : 14),
    nodes: MapNode[] = [];
  const items = new Map(
    lanes
      .filter((lane) => !lane.quiet && !lane.pager && (!lane.header || lane.collapsed))
      .map((lane) => [
        lane.id,
        orderStops(
          lane.tasks,
          Math.max(3, Math.floor((right - gutter) / 104) + 1),
          lane.collapsed,
          options.important,
        ),
      ]),
  );
  const maxStops = Math.max(3, ...[...items.values()].map((stops) => stops.length)),
    gap = Math.min(176, (right - gutter) / Math.max(1, maxStops - 1));
  const maxDensity = Math.max(
    1,
    ...lanes.flatMap((lane) =>
      mapDensity(
        lane.tasks,
        view,
        width,
        now,
        lane.collapsed ? caches[lane.source] : undefined,
      ).map((bucket) => bucket.count),
    ),
  );
  for (const lane of lanes) {
    lane.nodes = [];
    if (lane.quiet || lane.pager || (lane.header && !lane.collapsed)) continue;
    const y = lane.top + lane.height / 2 + (phone && mode === "time" ? 12 : 0);
    const add = (tasks: BoardTask[], x: number, x1 = x, density?: MapDensity) => {
      const node: MapNode = {
        id: `${lane.id}:${nodes.length}`,
        lane: lane.id,
        tasks,
        x,
        x1,
        y,
        top: lane.top,
        height: lane.height,
        color: lane.color,
        queued: tasks.length > 0 && tasks.every(routeQueued),
        density,
        hub: tasks.length === 1 && (hubs.get(taskIdentity(tasks[0]))?.size ?? 0) > 0,
      };
      nodes.push(node);
      lane.nodes.push(node);
      return node;
    };
    if (mode === "order") {
      (items.get(lane.id) ?? []).forEach((item, index) => {
        const at = gutter + index * gap;
        add(item.tasks, at, item.tasks.length > 1 ? at + 2 : at);
      });
      continue;
    }
    if (level === "segment") {
      for (const bucket of mapDensity(
        lane.tasks,
        view,
        width - gutter,
        now,
        lane.collapsed ? caches[lane.source] : undefined,
      )) {
        const x =
            gutter +
            Math.max(0, (bucket.from - view.from) / (view.to - view.from)) * (right - gutter),
          x1 =
            gutter +
            Math.min(1, (bucket.to - view.from) / (view.to - view.from)) * (right - gutter);
        const node = add(bucket.tasks, x, x1, bucket);
        node.height = Math.max(3, Math.sqrt(bucket.count / maxDensity) * (lane.height - 14));
      }
      continue;
    }
    const timed = lane.tasks
      .filter(
        (task) =>
          !routeQueued(task) &&
          routeMoment(task, now) !== undefined &&
          routeMoment(task, now)! >= view.from &&
          routeMoment(task, now)! <= view.to,
      )
      .sort((a, b) => routeMoment(a, now)! - routeMoment(b, now)!);
    const live = byUrgency(timed.filter((task) => ["running", "user"].includes(routeState(task)))),
      rest = timed.filter((task) => !live.includes(task));
    const x = (time: number) =>
      gutter + ((time - view.from) / (view.to - view.from)) * (right - gutter);
    const minimum = lane.collapsed ? 34 : level === "station" ? 60 : 84;
    const pinnedX = Math.min(right - 8, x(now)),
      pinnedGap = lane.collapsed ? 38 : 50,
      // Live stops stack left of now; those that would run past the row start share one capsule.
      liveRoom = Math.max(1, Math.floor((pinnedX - gutter - minimum) / pinnedGap) + 1),
      pinned: BoardTask[][] =
        live.length > liveRoom
          ? [live.slice(liveRoom - 1), ...live.slice(0, liveRoom - 1).map((task) => [task])]
          : live.map((task) => [task]),
      limit = pinned.length ? pinnedX - (pinned.length - 1) * pinnedGap - minimum : Infinity;
    const clusters: { x: number; end: number; tasks: BoardTask[] }[] = [];
    for (const task of rest) {
      const px = Math.min(limit, x(routeMoment(task, now)!)),
        previous = clusters.at(-1);
      if (previous && px - previous.end < minimum) {
        previous.tasks.push(task);
        previous.end = px;
      } else clusters.push({ x: px, end: px, tasks: [task] });
    }
    for (const cluster of clusters) {
      const node = add(
        cluster.tasks,
        cluster.x,
        cluster.tasks.length > 1 ? Math.max(cluster.end, cluster.x + 2) : cluster.x,
      );
      node.timeCluster = cluster.tasks.length > 1;
    }
    pinned.forEach((stop, index) => {
      const at = pinnedX - (pinned.length - 1 - index) * pinnedGap;
      add(stop, at, stop.length > 1 ? at + 2 : at);
    });
    if (view.from <= now && view.to >= now && level === "station") {
      const queue = lane.tasks.filter(routeQueued),
        end = Math.max(x(now), ...lane.nodes.map((node) => node.x1)),
        first = end + (lane.collapsed ? 34 : 44),
        gapQ = Math.min(132, Math.max(84, (right - 40 - end - 44) / Math.max(1, queue.length)));
      const fit = lane.collapsed
        ? first < right - 16
          ? 1
          : 0
        : Math.max(0, Math.floor((right - 26 - first) / gapQ) + 1);
      const shown =
        queue.length > fit || lane.collapsed ? queue.slice(0, Math.max(0, fit - 1)) : queue;
      shown.forEach((task, index) => add([task], first + index * gapQ));
      const more = queue.slice(shown.length);
      if (more.length && fit > 0) {
        const at = first + shown.length * gapQ;
        add(more, at, more.length > 1 ? at + 2 : at);
      }
    }
    lane.nodes.sort((a, b) => a.x - b.x);
  }
  const positions = new Map(
      nodes.flatMap((node) => node.tasks.map((task) => [taskIdentity(task), node] as const)),
    ),
    edgeMap = new Map<string, MapEdge>();
  let offset = 0;
  if (!(mode === "time" && level === "segment") && !(mode === "order" && phone))
    for (const relation of relations) {
      const a = positions.get(taskIdentity(relation.from)),
        b = positions.get(taskIdentity(relation.to));
      if (!a || !b || a === b || a.lane === b.lane) continue;
      const hub = hubs.get(taskIdentity(relation.from))?.has(taskIdentity(relation.to)) ?? false,
        key = JSON.stringify([a.id, b.id, hub]),
        existing = edgeMap.get(key);
      if (existing) {
        existing.relations.push(relation);
        continue;
      }
      const rowA = lanes
          .filter((lane) => (!lane.header || lane.collapsed) && !lane.quiet && !lane.pager)
          .findIndex((lane) => lane.id === a.lane),
        rowB = lanes
          .filter((lane) => (!lane.header || lane.collapsed) && !lane.quiet && !lane.pager)
          .findIndex((lane) => lane.id === b.lane);
      const long = Math.abs(rowA - rowB) >= 2 || Math.abs(a.x - b.x) > width * 0.3,
        dy = b.y - a.y,
        sign = Math.sign(dy) || 1,
        dx = Math.sign(b.x - a.x) || 1,
        middle = a.y + sign * (a.height / 2 - 2 + offset * 7),
        d1 = Math.abs(middle - a.y),
        d2 = Math.abs(b.y - middle),
        rail = a.x + (a.x1 - a.x) / 2 + 26 + ((offset + 1) % 3) * 7,
        step = Math.min(30, Math.abs(dy) / 3);
      const points =
        long || hub
          ? [a, b]
          : Math.abs(a.x - b.x) < 10
            ? [a, { x: rail, y: a.y + sign * step }, { x: rail, y: b.y - sign * step }, b]
            : Math.abs(a.x - b.x) <= Math.abs(dy) * 1.3
              ? [a, b]
              : [a, { x: a.x + dx * d1, y: middle }, { x: b.x - dx * d2, y: middle }, b];
      offset = (offset + 1) % 3;
      edgeMap.set(key, {
        id: key,
        from: a,
        to: b,
        path: roundedRoutePath(points),
        points,
        long,
        hub,
        relations: [relation],
      });
    }
  const edges = [...edgeMap.values()];
  for (const edge of edges) {
    if (edge.relations.length > 1) {
      const index = Math.floor(edge.points.length / 2),
        a = edge.points[index],
        b = edge.points[index - 1];
      edge.badge = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
    }
  }
  const segments: { a: RoutePoint; b: RoutePoint; half: number }[] = [];
  for (const lane of lanes) {
    const railNodes = lane.nodes.filter((node) => !node.density),
      thick = lane.collapsed ? 7 : mode === "time" && level === "phase" ? 9 : 11;
    if (mode === "time" && railNodes.length) {
      const past = railNodes.filter((node) => !node.queued),
        first = past[0],
        last = past.at(-1);
      if (first && last) {
        const earliest =
          lane.historyStart ??
          Math.min(
            ...lane.tasks
              .map((task) =>
                Date.parse(
                  task["omnigent.ai:sourceDisplay"]?.historyState?.end ??
                    task["omnigent.ai:sourceDisplay"]?.historyState?.last ??
                    "",
                ),
              )
              .filter(Number.isFinite),
          );
        segments.push({
          a: { x: earliest < view.from ? gutter : first.x, y: first.y },
          b: { x: last.x1, y: last.y },
          half: thick / 2,
        });
      }
    }
    railNodes.slice(1).forEach((node, index) =>
      segments.push({
        a: { x: railNodes[index].x1, y: node.y },
        b: { x: node.x, y: node.y },
        half: node.queued ? 2.5 : mode === "order" ? thick / 2 + 1 : thick / 2,
      }),
    );
  }
  for (const edge of edges) {
    if (!edge.long && !edge.hub)
      for (let i = 1; i < edge.points.length; i++)
        segments.push({ a: edge.points[i - 1], b: edge.points[i], half: 3 });
  }
  if (mode === "time" && view.from <= now && now <= view.to) {
    const at = gutter + ((now - view.from) / (view.to - view.from)) * (right - gutter);
    segments.push({ a: { x: at, y: 0 }, b: { x: at, y: 99999 }, half: 1 });
  }
  for (const node of nodes) {
    if (node.density || lanes.find((lane) => lane.id === node.lane)?.header) continue;
    const value = label(node);
    if (typeof value === "string") {
      node.lines = routeLabelLines(value);
      node.sub = "";
    } else {
      node.lines = value.lines;
      node.sub = value.sub;
    }
    if (
      mode === "time" &&
      level !== "station" &&
      node.tasks.length === 1 &&
      !routeAttention(node.tasks[0])
    )
      node.lines = [];
  }
  placeMapLabels(
    nodes,
    segments,
    lanes,
    width,
    mode === "order" ? 168 : phone ? 4 : 168,
    measure,
    options.compact,
  );
  return { nodes, edges, lanes };
}
