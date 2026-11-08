import dagre from "@dagrejs/dagre";
import { bisector } from "d3-array";
import { timeDay, timeHour, timeMonday, timeMonth, timeYear } from "d3-time";
import {
  groupIdentity,
  isOpenTask,
  taskIdentity,
  taskProgress,
  type BoardTask,
  type TaskSource,
} from "./taskBoardData";

export const HOUR = 3_600_000;
export const DAY = 24 * HOUR;
export const MIN_SPAN = 12 * HOUR;
export interface TimeWindow {
  from: number;
  to: number;
}
export type RouteMode = "order" | "time";
export type DetailLevel = "station" | "phase" | "segment";
export const detailLevel = (span: number): DetailLevel =>
  span <= 2 * DAY ? "station" : span <= 14 * DAY ? "phase" : "segment";
export const initialWindow = (now = Date.now()): TimeWindow => ({ from: now - DAY, to: now + DAY });
export function changeWindow(window: TimeWindow, delta: number, zoom: boolean): TimeWindow {
  const span = window.to - window.from;
  if (zoom) {
    const next = Math.max(
      MIN_SPAN,
      Math.min(10 * 365 * DAY, span * Math.exp(Math.max(-1, Math.min(1, delta / 500)))),
    );
    return { from: window.to - next, to: window.to };
  }
  const shift = (delta / 800) * span;
  return { from: window.from + shift, to: window.to + shift };
}
export function taskTime(task: BoardTask, now: number): number {
  if (task["omnigent.ai:waiting"] || taskProgress(task) === "in-process") return now;
  if (isOpenTask(task)) {
    const planned = task.start ?? task.due;
    if (planned) {
      const time = taskLocalTime(planned, task.timeZone);
      if (time > now) return time;
    }
  }
  const value =
    task.progressUpdated ?? task["omnigent.ai:closedSeen"] ?? task.created ?? task.updated;
  return value ? Date.parse(value) : now;
}

/** JSCalendar's local date/time carries its zone separately from the wall-clock string. */
export function taskLocalTime(value: string, zone?: string): number {
  if (!zone || /(?:Z|[+-]\d\d:\d\d)$/.test(value)) return Date.parse(value);
  const expected = Date.parse(`${value}Z`);
  if (!Number.isFinite(expected)) return NaN;
  const formatter = new Intl.DateTimeFormat("en-US", {
    timeZone: zone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
  });
  let result = expected;
  for (let pass = 0; pass < 2; pass++) {
    const parts = Object.fromEntries(
      formatter.formatToParts(result).map((part) => [part.type, part.value]),
    );
    const wall = Date.UTC(
      +parts.year,
      +parts.month - 1,
      +parts.day,
      +parts.hour,
      +parts.minute,
      +parts.second,
    );
    result += expected - wall;
  }
  return result;
}
export interface RouteRow {
  id: string;
  source: string;
  name: string;
  tasks: BoardTask[];
  color: number;
}
export interface TaskRelation {
  from: BoardTask;
  to?: BoardTask;
  uid: string;
  kind: "parent" | "next" | "dependency";
}

export function taskRelations(tasks: BoardTask[]): TaskRelation[] {
  const bySource = new Map(tasks.map((task) => [taskIdentity(task), task]));
  const byUid = new Map<string, BoardTask[]>();
  tasks.forEach((task) => byUid.set(task.uid, [...(byUid.get(task.uid) ?? []), task]));
  const edges = new Map<string, TaskRelation>();
  for (const task of tasks)
    for (const [uid, { relation }] of Object.entries(task.relatedTo ?? {})) {
      const target =
        bySource.get(JSON.stringify([task["omnigent.ai:source"], uid])) ??
        (byUid.get(uid)?.length === 1 ? byUid.get(uid)![0] : undefined);
      for (const [key, enabled] of Object.entries(relation)) {
        if (!enabled) continue;
        const kind = ["parent", "child"].includes(key)
          ? "parent"
          : ["next", "first"].includes(key)
            ? "next"
            : key === "omnigent.ai:depends-on"
              ? "dependency"
              : undefined;
        if (!kind) continue;
        const reverse = ["parent", "first", "omnigent.ai:depends-on"].includes(key);
        const from = reverse && target ? target : task;
        const to = reverse && target ? task : target;
        const edge: TaskRelation = { from, to, uid, kind };
        edges.set(JSON.stringify([taskIdentity(from), to ? taskIdentity(to) : uid, kind]), edge);
      }
    }
  return [...edges.values()];
}

function score(ids: string[], links: [string, string][]): number {
  const positions = new Map(ids.map((id, index) => [id, index]));
  return links.reduce(
    (sum, [a, b]) => sum + Math.abs((positions.get(a) ?? 0) - (positions.get(b) ?? 0)),
    0,
  );
}

/** Port of the accepted map's compound dagre order and adjacent-swap correction. */
export function routeRows(tasks: BoardTask[], sources: TaskSource[]): RouteRow[] {
  const groups = new Map<string, RouteRow>();
  for (const task of tasks) {
    const id = groupIdentity(task),
      source = task["omnigent.ai:source"];
    if (!groups.has(id))
      groups.set(id, {
        id,
        source,
        name: task["omnigent.ai:group"]?.name ?? task["omnigent.ai:group"]?.id ?? "",
        tasks: [],
        color:
          Math.max(
            0,
            sources.findIndex((candidate) => candidate.id === source),
          ) % 5,
      });
    groups.get(id)!.tasks.push(task);
  }
  for (const row of groups.values())
    row.tasks.sort(
      (a, b) =>
        (a.created ?? "").localeCompare(b.created ?? "") ||
        (a["omnigent.ai:key"] ?? a.uid).localeCompare(b["omnigent.ai:key"] ?? b.uid),
    );
  const rows = [...groups.values()];
  const links: [string, string][] = taskRelations(tasks)
    .filter((edge) => edge.to && groupIdentity(edge.from) !== groupIdentity(edge.to))
    .map((edge) => [groupIdentity(edge.from), groupIdentity(edge.to!)]);
  const linked = new Set(links.flat());
  let order = new Map<string, number>();
  if (linked.size > 0 && linked.size <= 150) {
    const graph = new dagre.graphlib.Graph({ compound: true })
      .setGraph({ rankdir: "LR", nodesep: 10, ranksep: 40 })
      .setDefaultEdgeLabel(() => ({}));
    for (const row of rows.filter((candidate) => linked.has(candidate.id))) {
      graph.setNode(`source:${row.source}`, {});
      graph.setNode(row.id, { width: 100, height: 20 });
      graph.setParent(row.id, `source:${row.source}`);
    }
    links.forEach(([a, b], index) => {
      const joint = `joint:${index}`;
      graph.setNode(joint, { width: 4, height: 4 });
      graph.setEdge(a, joint);
      graph.setEdge(b, joint);
    });
    dagre.layout(graph);
    order = new Map([...linked].map((id) => [id, graph.node(id).y as number]));
  }
  const sourceOrder = new Map<string, number>();
  for (const source of sources) {
    const children = rows.filter((row) => row.source === source.id);
    const values = children.flatMap((row) => (order.has(row.id) ? [order.get(row.id)!] : []));
    sourceOrder.set(
      source.id,
      values.length
        ? values.reduce((sum, value) => sum + value, 0) / values.length
        : sources.indexOf(source) * 1000,
    );
  }
  rows.sort(
    (a, b) =>
      (sourceOrder.get(a.source) ?? 0) - (sourceOrder.get(b.source) ?? 0) ||
      (order.get(a.id) ?? 1e9) - (order.get(b.id) ?? 1e9),
  );
  let best = score(
    rows.map((row) => row.id),
    links,
  );
  for (let pass = 0; pass < 3; pass++)
    for (let i = 0; i + 1 < rows.length; i++) {
      if (rows[i].source !== rows[i + 1].source) continue;
      [rows[i], rows[i + 1]] = [rows[i + 1], rows[i]];
      const next = score(
        rows.map((row) => row.id),
        links,
      );
      if (next < best) best = next;
      else [rows[i], rows[i + 1]] = [rows[i + 1], rows[i]];
    }
  return rows;
}

export interface Point {
  x: number;
  y: number;
}
export interface Rect extends Point {
  w: number;
  h: number;
}
export interface RouteNode extends Point {
  id: string;
  row: RouteRow;
  tasks: BoardTask[];
  x1: number;
  label?: Rect;
  labelText?: string;
  sub?: string;
  density?: boolean;
}
export interface RouteEdge {
  from: RouteNode;
  to: RouteNode;
  relations: TaskRelation[];
  kind: TaskRelation["kind"];
  long: boolean;
  hub: boolean;
  path: string;
  targets?: RouteNode[];
}
export interface RouteLayout {
  nodes: RouteNode[];
  edges: RouteEdge[];
  rails: { row: RouteRow; y: number; x: number; x1: number }[];
  headers: { source: string; y: number }[];
  height: number;
  rowHeight: number;
}

export function segmentHitsRect(a: Point, b: Point, r: Rect, pad = 3): boolean {
  let t0 = 0,
    t1 = 1;
  const dx = b.x - a.x,
    dy = b.y - a.y;
  for (const [p, q] of [
    [-dx, a.x - r.x + pad],
    [dx, r.x + r.w + pad - a.x],
    [-dy, a.y - r.y + pad],
    [dy, r.y + r.h + pad - a.y],
  ]) {
    if (p === 0) {
      if (q < 0) return false;
      continue;
    }
    const t = q / p;
    if (p < 0) {
      if (t > t1) return false;
      t0 = Math.max(t0, t);
    } else {
      if (t < t0) return false;
      t1 = Math.min(t1, t);
    }
  }
  return true;
}
const touches = (a: Rect, b: Rect, pad: number) =>
  a.x < b.x + b.w + pad && b.x < a.x + a.w + pad && a.y < b.y + b.h + pad && b.y < a.y + a.h + pad;
export function placeRouteLabels(
  nodes: RouteNode[],
  edges: RouteEdge[],
  width: number,
  rowHeight: number,
  gutter: number,
  label: (node: RouteNode) => string,
) {
  const occupied: Rect[] = nodes.map((node) => ({
    x: node.x - 18,
    y: node.y - 18,
    w: node.x1 - node.x + 36,
    h: 36,
  }));
  const placed: Rect[] = [];
  const priority = (node: RouteNode) =>
    node.tasks.some((task) => task["omnigent.ai:waiting"])
      ? 0
      : node.tasks.some((task) => taskProgress(task) === "in-process")
        ? 1
        : 2;
  for (const node of [...nodes].sort((a, b) => priority(a) - priority(b) || b.x - a.x)) {
    const text = label(node),
      w = Math.min(
        180,
        Math.max(
          40,
          [...text].reduce((n, char) => n + ((char.codePointAt(0) ?? 0) > 127 ? 13 : 7.6), 0),
        ),
      ),
      h = 30;
    const middle = (node.x + node.x1) / 2;
    let chosen: Rect | undefined;
    for (const dy of [-52, 22])
      for (const dx of [0, 10, -10, 22, -22, 36, -36, w / 2 + 4, -w / 2 - 4]) {
        const rect = { x: middle + dx - w / 2, y: node.y + dy, w, h };
        if (
          chosen ||
          rect.x < gutter + 4 ||
          rect.x + w > width - 4 ||
          rect.y < node.y - rowHeight / 2 + 5 ||
          rect.y + h > node.y + rowHeight / 2 - 5
        )
          continue;
        if (
          occupied.some((box) => touches(rect, box, 2)) ||
          placed.some((box) => touches(rect, box, 4))
        )
          continue;
        if (
          edges
            .filter((edge) => !edge.long && !edge.hub)
            .some((edge) => segmentHitsRect(edge.from, edge.to, rect, 5))
        )
          continue;
        chosen = rect;
      }
    if (chosen) {
      node.label = chosen;
      node.labelText = text;
      placed.push(chosen);
    }
  }
}

/** Station clusters, pinned live stops and pale cross-row spans follow the accepted drawing. */
export function routeLayout(
  rows: RouteRow[],
  mode: RouteMode,
  window: TimeWindow,
  width: number,
  now: number,
  label: (node: RouteNode) => string,
): RouteLayout {
  const rowHeight =
    detailLevel(window.to - window.from) === "station" || mode === "order" ? 136 : 92;
  const gutter = width < 600 ? 14 : 190,
    right = width - 28;
  const nodes: RouteNode[] = [],
    rails: RouteLayout["rails"] = [],
    headers: RouteLayout["headers"] = [];
  let top = 0,
    previousSource = "";
  rows.forEach((row, index) => {
    if (!index || previousSource !== row.source) {
      headers.push({ source: row.source, y: top });
      top += 40;
    }
    previousSource = row.source;
    const y = top + rowHeight / 2;
    top += rowHeight;
    const sorted =
      mode === "order"
        ? row.tasks
        : [...row.tasks].sort((a, b) => taskTime(a, now) - taskTime(b, now));
    const byTime = bisector((task: BoardTask) => taskTime(task, now));
    const visible =
      mode === "order"
        ? sorted
        : sorted.slice(byTime.left(sorted, window.from), byTime.right(sorted, window.to));
    const step = (right - gutter - 42) / Math.max(1, visible.length - 1);
    const clusters: RouteNode[] = [];
    const span = window.to - window.from,
      level = detailLevel(span);
    const minGap = level === "station" ? 60 : level === "phase" ? 84 : 40;
    if (mode === "time" && level === "segment") {
      const intervals = [timeDay, timeMonday, timeMonth, timeMonth.every(3)!, timeYear];
      const interval =
        intervals.find(
          (candidate) =>
            candidate.range(new Date(window.from), new Date(window.to)).length <=
            (right - gutter) / 32,
        ) ?? timeYear;
      const buckets = new Map<number, BoardTask[]>();
      for (const task of visible) {
        const key = +interval.floor(new Date(taskTime(task, now)));
        buckets.set(key, [...(buckets.get(key) ?? []), task]);
      }
      for (const [from, items] of buckets) {
        const to = +interval.offset(new Date(from), 1);
        const x = gutter + Math.max(0, (from - window.from) / span) * (right - gutter);
        const x1 = gutter + Math.min(1, (to - window.from) / span) * (right - gutter);
        clusters.push({
          id: `density:${row.id}:${from}`,
          row,
          tasks: items,
          x,
          x1,
          y,
          density: true,
        });
      }
      nodes.push(...clusters);
      rails.push({ row, y, x: gutter, x1: right });
      return;
    }
    const isPinned = (task: BoardTask) =>
      !!task["omnigent.ai:waiting"] || taskProgress(task) === "in-process";
    for (const [i, task] of visible.entries()) {
      const time = taskTime(task, now);
      const x =
        mode === "order"
          ? gutter + 22 + i * step
          : Math.max(
              gutter + 20,
              Math.min(right - 20, gutter + ((time - window.from) / span) * (right - gutter)),
            );
      const last = clusters.at(-1);
      if (
        last &&
        (mode === "order" ? step < 56 : x - last.x1 < minGap) &&
        !isPinned(task) &&
        !last.tasks.some(isPinned)
      ) {
        last.tasks.push(task);
        last.x1 = x;
      } else clusters.push({ id: taskIdentity(task), row, tasks: [task], x, x1: x, y });
    }
    const pinned = clusters.filter(
      (node) =>
        node.tasks.length === 1 &&
        (node.tasks[0]["omnigent.ai:waiting"] || taskProgress(node.tasks[0]) === "in-process"),
    );
    if (mode === "time")
      pinned.forEach((node, i) => {
        node.x = Math.max(
          gutter + 20,
          Math.min(right - 20, gutter + ((now - window.from) / span) * (right - gutter)) -
            (pinned.length - 1 - i) * 48,
        );
        node.x1 = node.x;
      });
    clusters.sort((a, b) => a.x - b.x);
    nodes.push(...clusters);
    rails.push({ row, y, x: clusters[0]?.x ?? gutter + 20, x1: clusters.at(-1)?.x1 ?? right - 20 });
  });
  const positions = new Map(
    nodes.flatMap((node) => node.tasks.map((task) => [taskIdentity(task), node] as const)),
  );
  const relations =
    mode === "time" && detailLevel(window.to - window.from) === "segment"
      ? []
      : taskRelations(rows.flatMap((row) => row.tasks));
  const kids = new Map<string, number>();
  relations
    .filter((edge) => edge.kind === "parent")
    .forEach((edge) =>
      kids.set(taskIdentity(edge.from), (kids.get(taskIdentity(edge.from)) ?? 0) + 1),
    );
  const edges: RouteEdge[] = [];
  for (const relation of relations) {
    const from = positions.get(taskIdentity(relation.from)),
      to = relation.to && positions.get(taskIdentity(relation.to));
    if (!from || !to || from === to) continue;
    const long = Math.abs(from.y - to.y) >= rowHeight * 2 || Math.abs(from.x - to.x) > width * 0.3;
    edges.push({
      from,
      to,
      relations: [relation],
      kind: relation.kind,
      long,
      hub: relation.kind === "parent" && (kids.get(taskIdentity(relation.from)) ?? 0) >= 8,
      path: `M${from.x} ${from.y}L${to.x} ${to.y}`,
    });
  }
  const grouped: RouteEdge[] = [];
  const hubRows = new Map<string, RouteEdge>();
  for (const edge of edges) {
    if (!edge.hub) {
      grouped.push(edge);
      continue;
    }
    const key = JSON.stringify([edge.from.id, edge.to.row.id]);
    const existing = hubRows.get(key);
    if (existing) {
      existing.targets!.push(edge.to);
      existing.relations.push(...edge.relations);
    } else {
      edge.targets = [edge.to];
      hubRows.set(key, edge);
      grouped.push(edge);
    }
  }
  if (mode !== "time" || detailLevel(window.to - window.from) !== "segment")
    placeRouteLabels(nodes, grouped, width, rowHeight, gutter, label);
  return { nodes, edges: grouped, rails, headers, height: Math.max(136, top), rowHeight };
}

/** Local calendar intervals preserve DST, week and month boundaries. */
export function routeTicks(window: TimeWindow, width: number): Date[] {
  const intervals = [
    timeHour.every(1)!,
    timeHour.every(3)!,
    timeHour.every(6)!,
    timeDay,
    timeMonday,
    timeMonth,
    timeMonth.every(3)!,
    timeYear,
  ];
  const interval =
    intervals.find(
      (candidate) =>
        candidate.range(new Date(window.from), new Date(window.to)).length <=
        Math.max(2, width / 90),
    ) ?? timeYear;
  return interval.range(interval.ceil(new Date(window.from)), new Date(window.to));
}
