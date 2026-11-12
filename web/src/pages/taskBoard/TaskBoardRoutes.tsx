import {
  Fragment,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type PointerEvent as ReactPointerEvent,
} from "react";
import {
  ChevronDownIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  ChevronsDownUpIcon,
  ChevronsUpDownIcon,
  ClockIcon,
  CircleHelpIcon,
  MinusIcon,
  PlusIcon,
  RouteIcon,
  XIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Link } from "@/lib/routing";
import { boardExitProps } from "@/lib/boardFrame";
import { taskIdentity, type BoardTask, type TaskSource } from "./taskBoardData";
import type { BoardSchedule } from "./taskBoardNative";
import type { SourceCache } from "./taskBoardSnapshotData";
import {
  DAY,
  HOUR,
  MIN_SPAN,
  detailLevel,
  initialWindow,
  routeTicks,
  taskLocalTime,
  type RouteMode,
  type TimeWindow,
} from "./taskBoardRouteData";
import {
  clampMapWindow,
  mapDensity,
  mapOverview,
  mapGroups,
  mapHubMembers,
  mapRelations,
  mapScene,
  routeAttention,
  routeColors,
  routeMoment,
  routeQueued,
  routeState,
  stepMapWindow,
  type MapGroup,
  type MapRelation,
  type MapNode,
  type MapLane,
  type MapEdge,
} from "./taskBoardRouteModel";
import { TaskBoardDetail, taskTitle } from "./TaskBoardDetail";
import { TaskBoardSearch } from "./TaskBoardSearch";
import { TaskBoardTaskHover } from "./TaskBoardTaskHover";
import { BoardIdentityMark } from "./BoardIdentityMark";
import { formatDate, formatNumber, t } from "./taskBoardText";
import "./taskBoardRoutes.css";

const emptyCaches: Record<string, SourceCache> = {};
type OpenPanel =
  | { kind: "task"; lane: string; task: BoardTask }
  | { kind: "cluster"; lane: string; tasks: BoardTask[] }
  | { kind: "edge"; lane: string; edge: MapEdge }
  | { kind: "hub"; lane: string; task: BoardTask };
interface Hover {
  node?: MapNode;
  edge?: MapEdge;
  x: number;
  y: number;
}
const badgeStyle = (color: string) => ({ "--rt-color": color }) as CSSProperties;
const momentWord = (time: number) => {
  const current = Date.now(),
    date = new Date(time),
    today = new Date(current),
    yesterday = new Date(current);
  yesterday.setDate(yesterday.getDate() - 1);
  const sameDay = (a: Date, b: Date) =>
    a.getFullYear() === b.getFullYear() &&
    a.getMonth() === b.getMonth() &&
    a.getDate() === b.getDate();
  const day = sameDay(date, today)
    ? ""
    : sameDay(date, yesterday)
      ? t("routes.yesterday")
      : formatDate(time, {
          ...(date.getFullYear() !== today.getFullYear() ? { year: "numeric" as const } : {}),
          month: "numeric",
          day: "numeric",
        });
  return `${day ? day + " " : ""}${formatDate(time, { hour: "2-digit", minute: "2-digit", hour12: false })}`;
};
const stateWord = (task: BoardTask) => t(`sessions.taskState.${routeState(task)}`);
const nodeLabel = (node: MapNode) =>
  node.tasks.length > 1
    ? t(node.queued ? "routes.queuedCount" : "routes.earlierCount", { count: node.tasks.length })
    : (node.tasks[0]["omnigent.ai:sourceDisplay"]?.identity?.label ?? taskTitle(node.tasks[0]));
function Station({ tasks }: { tasks: BoardTask[] }) {
  if (tasks.length > 1)
    return (
      <span className={`rt-capsule${tasks.every(routeQueued) ? " queued" : ""}`}>
        {formatNumber(tasks.length)}
      </span>
    );
  const task = tasks[0],
    state = routeState(task),
    session = task["omnigent.ai:sourceDisplay"]?.sessions?.find(
      (item) => item.status === "running",
    );
  return (
    <span className={`rt-symbol rt-${state}`}>
      {state === "running" && session ? (
        <BoardIdentityMark
          id={session.agentId}
          label={session.agentId}
          product={session.agentId}
          size={22}
        />
      ) : state === "failed" ? (
        <XIcon size={13} />
      ) : null}
    </span>
  );
}
function RelationFacts({
  relations,
  onSelect,
}: {
  relations: MapRelation[];
  onSelect: (task: BoardTask) => void;
}) {
  return (
    <div className="rt-relation-facts">
      {relations.map((relation) => (
        <div key={relation.id}>
          <button type="button" onClick={() => onSelect(relation.from)}>
            {relation.from["omnigent.ai:sourceDisplay"]?.identity?.key ??
              relation.from["omnigent.ai:key"] ??
              relation.from.uid}
          </button>
          <span>{relation.label ?? t(relation.labelKey ?? `relation.${relation.kind}`)}</span>
          <button type="button" onClick={() => onSelect(relation.to)}>
            {relation.to["omnigent.ai:sourceDisplay"]?.identity?.key ??
              relation.to["omnigent.ai:key"] ??
              relation.to.uid}
          </button>
          <small>
            {relation.origin ? t(`routes.origin.${relation.origin}`) : t("routes.relationReported")}
            {relation.provenance ? ` · ${relation.provenance}` : ""}
            {" · "}
            {relation.since ? momentWord(Date.parse(relation.since)) : t("routes.relationUndated")}
          </small>
        </div>
      ))}
    </div>
  );
}
export function TaskBoardRoutes({
  tasks,
  sources,
  aliases,
  mode,
  onMode,
  focus,
  onFocus,
  onSelect,
  scheduled,
  sourceCaches = emptyCaches,
  boardHours = 24,
  lodStation = 2,
  lodPhase = 14,
}: {
  tasks: BoardTask[];
  sources: TaskSource[];
  aliases: Record<string, string>;
  mode: RouteMode;
  onMode: (mode: RouteMode) => void;
  focus: string[];
  onFocus: (ids: string[]) => void;
  onSelect: (task: BoardTask) => void;
  scheduled: BoardSchedule[];
  sourceCaches?: Record<string, SourceCache>;
  boardHours?: number;
  lodStation?: number;
  lodPhase?: number;
}) {
  const caches = useMemo(
    () =>
      Object.fromEntries(
        Object.entries(sourceCaches).filter(([id]) => sources.some((source) => source.id === id)),
      ),
    [sourceCaches, sources],
  );
  const now = useMemo(() => {
      void tasks;
      return Date.now();
    }, [tasks]),
    groups = useMemo(() => mapGroups(tasks, sources, caches), [tasks, sources, caches]),
    relations = useMemo(() => mapRelations(tasks), [tasks]);
  const hubs = useMemo(
    () =>
      new Map(
        tasks
          .filter((task) => task["omnigent.ai:sourceDisplay"]?.hub)
          .map((task) => [
            taskIdentity(task),
            new Set(mapHubMembers(task, tasks).map(taskIdentity)),
          ]),
      ),
    [tasks],
  );
  const [view, setView] = useState(() => initialWindow(now)),
    [width, setWidth] = useState(1100);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({}),
    [allExpanded, setAllExpanded] = useState<boolean | null>(null),
    [quiet, setQuiet] = useState<Record<string, boolean>>({});
  const [sourcePage, setSourcePage] = useState(0),
    [groupPages, setGroupPages] = useState<Record<string, number>>({}),
    [phonePages, setPhonePages] = useState<Record<string, number>>({});
  const [selection, setOpen] = useState<OpenPanel | null>(null),
    [panelPage, setPanelPage] = useState(0),
    [panelHeight, setPanelHeight] = useState(240),
    [help, setHelp] = useState(false),
    [hover, setHover] = useState<Hover | null>(null),
    [hoverHub, setHoverHub] = useState<string | null>(null);
  const open = useMemo<OpenPanel | null>(() => {
    if (!selection) return null;
    const byId = new Map(tasks.map((task) => [taskIdentity(task), task]));
    if (selection.kind === "task" || selection.kind === "hub") {
      const task = byId.get(taskIdentity(selection.task));
      return task ? { ...selection, task } : null;
    }
    if (selection.kind === "cluster") {
      const updated = selection.tasks.flatMap((task) =>
        byId.has(taskIdentity(task)) ? [byId.get(taskIdentity(task))!] : [],
      );
      return updated.length ? { ...selection, tasks: updated } : null;
    }
    const updated = selection.edge.relations.flatMap((relation) => {
      const from = byId.get(taskIdentity(relation.from)),
        to = byId.get(taskIdentity(relation.to));
      return from && to ? [{ ...relation, from, to }] : [];
    });
    return updated.length
      ? { ...selection, edge: { ...selection.edge, relations: updated } }
      : null;
  }, [selection, tasks]);
  const mapRef = useRef<HTMLDivElement>(null),
    overviewRef = useRef<HTMLDivElement>(null),
    axisRef = useRef<HTMLDivElement>(null),
    panelRef = useRef<HTMLDivElement>(null),
    tooltipRef = useRef<HTMLDivElement>(null);
  const [tooltipSize, setTooltipSize] = useState({ width: 340, height: 240 });
  const pointer = useRef<{
      kind: "map" | "overview";
      edge: string;
      x: number;
      view: TimeWindow;
    } | null>(null),
    pointers = useRef(new Map<number, { x: number; y: number }>()),
    pinch = useRef<{ distance: number; view: TimeWindow } | null>(null),
    wheelAt = useRef(0),
    dragged = useRef(false);
  const phone = width <= 620,
    level = detailLevel(view.to - view.from, lodStation, lodPhase),
    gutter = phone ? 14 : 208,
    right = width - (mode === "order" ? 58 : 14);
  const oldest = useMemo(
    () =>
      Math.min(
        now - DAY,
        ...tasks.flatMap((task) => {
          const at = routeMoment(task, now);
          return at === undefined ? [] : [at];
        }),
        ...Object.values(caches).flatMap((cache) =>
          (cache.display.closedDays ?? []).map((day) => Date.parse(day.date)),
        ),
      ),
    [tasks, caches, now],
  );
  const sourceName = useCallback(
    (id: string) =>
      aliases[id] ||
      caches[id]?.display.identity?.label ||
      sources.find((source) => source.id === id)?.name ||
      id,
    [aliases, caches, sources],
  );
  const sourceBadge = useCallback(
    (id: string) => caches[id]?.display.identity?.badge ?? sourceName(id).slice(0, 1).toUpperCase(),
    [caches, sourceName],
  );
  const sourceColor = useCallback(
    (id: string) =>
      routeColors[
        Math.max(
          0,
          sources.findIndex((source) => source.id === id),
        ) % routeColors.length
      ],
    [sources],
  );
  const nowOn = view.from <= now && view.to >= now;
  const moveWindow = (next: TimeWindow) => {
    setView(clampMapWindow(next, oldest, now));
    setOpen(null);
    setHover(null);
  };
  const zoom = useCallback(
    (direction: number) => {
      setView((current) => stepMapWindow(current, direction, oldest, now));
      setOpen(null);
      setHover(null);
    },
    [oldest, now],
  );
  const isExpanded = useCallback(
    (source: string) =>
      expanded[source] ??
      allExpanded ??
      ((mode === "order" || level === "station") &&
        groups.some((group) => group.source === source && group.tasks.some(routeAttention))),
    [expanded, allExpanded, mode, level, groups],
  );
  const future = useMemo(() => {
    const items = new Map<
      string,
      {
        id: string;
        label: string;
        at: number;
        sourceLabel?: string;
        native?: boolean;
        task?: BoardTask;
      }
    >();
    for (const item of scheduled)
      if (item.state === "active" && item.next_run_at)
        items.set(item.id, {
          id: item.id,
          label: item.name,
          at: Date.parse(item.next_run_at),
          native: true,
        });
    for (const task of tasks) {
      const planned = task.start ?? task.due;
      if (!planned || !routeQueued(task)) continue;
      const at = taskLocalTime(planned, task.timeZone);
      if (at > now)
        items.set(`task:${taskIdentity(task)}`, {
          id: `task:${taskIdentity(task)}`,
          label: taskTitle(task),
          at,
          task,
        });
    }
    for (const cache of Object.values(caches))
      for (const item of cache.display.future ?? []) {
        const native = items.get(item.id);
        items.set(item.id, {
          id: item.id,
          label: item.label,
          at: Date.parse(item.at),
          sourceLabel: item.sourceLabel,
          ...(native ? { native: true } : {}),
        });
      }
    return [...items.values()]
      .filter((item) => Number.isFinite(item.at))
      .sort((a, b) => a.at - b.at);
  }, [scheduled, caches, tasks, now]);
  const selectedGroups = useMemo(
    () =>
      groups
        .filter((group) => !focus.length || focus.includes(group.id))
        .map((group) => ({
          ...group,
          tasks:
            mode === "order"
              ? group.tasks.filter(
                  (task) =>
                    (task["omnigent.ai:sourceDisplay"]?.historyState?.active !== false ||
                      routeAttention(task)) &&
                    (routeState(task) !== "done" ||
                      !(task.progressUpdated ?? task["omnigent.ai:closedSeen"]) ||
                      Date.parse((task.progressUpdated ?? task["omnigent.ai:closedSeen"])!) >=
                        now - boardHours * HOUR),
                )
              : group.tasks,
        }))
        .filter((group) => mode !== "order" || group.tasks.length > 0),
    [groups, focus, mode, now, boardHours],
  );
  const sourceIds = [
    ...new Set([
      ...selectedGroups.map((group) => group.source),
      ...(mode === "time" && level === "segment"
        ? sources
            .filter((source) => caches[source.id]?.display.closedDays?.length)
            .map((source) => source.id)
        : []),
    ]),
  ];
  const pageCount = Math.max(1, Math.ceil(sourceIds.length / 12)),
    currentPage = Math.min(sourcePage, pageCount - 1);
  const sourceSliceKey = JSON.stringify(sourceIds.slice(currentPage * 12, (currentPage + 1) * 12));
  const sourceSlice = useMemo<string[]>(() => JSON.parse(sourceSliceKey), [sourceSliceKey]);
  const model = useMemo(() => {
    const lanes: MapLane[] = [];
    let top = 0;
    const countTasks = (items: BoardTask[], source?: string) =>
      mode === "order"
        ? items.length
        : level === "segment"
          ? mapDensity(items, view, width, now, source ? caches[source] : undefined).reduce(
              (sum, bucket) => sum + bucket.count,
              0,
            )
          : items.filter((task) =>
              routeQueued(task)
                ? nowOn && level === "station"
                : routeMoment(task, now)! >= view.from && routeMoment(task, now)! <= view.to,
            ).length;
    const append = (lane: Omit<MapLane, "top" | "nodes">) => {
      lanes.push({
        ...lane,
        top,
        nodes: [],
        count: countTasks(lane.tasks, lane.collapsed ? lane.source : undefined),
      });
      top += lane.height;
      if (open?.lane === lane.id) top += panelHeight;
    };
    for (const source of sourceSlice) {
      const candidates = selectedGroups.filter((group) => group.source === source);
      const live = (group: MapGroup) =>
        mode === "order"
          ? group.tasks.some(
              (task) =>
                routeAttention(task) ||
                (!routeQueued(task) &&
                  (routeMoment(task, now) === undefined ||
                    routeMoment(task, now)! >= now - boardHours * HOUR)),
            )
          : level === "segment"
            ? mapDensity(group.tasks, view, width, now).some((bucket) => bucket.count > 0)
            : group.tasks.some(
                (task) =>
                  !routeQueued(task) &&
                  (routeMoment(task, now) ?? 0) >= view.from &&
                  (routeMoment(task, now) ?? 0) <= view.to,
              );
      const active = candidates.filter(live),
        inactive = candidates.filter((group) => !live(group)),
        list = quiet[source] ? [...active, ...inactive] : active;
      const sourceTasks = candidates.flatMap((group) => group.tasks),
        opened = isExpanded(source);
      if (!sourceTasks.length && !caches[source]?.display.closedDays?.length) continue;
      if (
        mode === "time" &&
        level !== "segment" &&
        !active.length &&
        !(level === "station" && nowOn && sourceTasks.some(routeQueued))
      )
        continue;
      append({
        id: `source:${source}`,
        source,
        name: sourceName(source),
        badge: sourceBadge(source),
        color: sourceColor(source),
        tasks: sourceTasks,
        header: true,
        collapsed: !opened,
        height: opened
          ? phone
            ? 34
            : 40
          : phone
            ? 74
            : level === "segment" && mode === "time"
              ? 54
              : 58,
      });
      if (!opened) continue;
      const page = Math.min(groupPages[source] ?? 0, Math.max(0, Math.ceil(list.length / 10) - 1));
      for (const group of list.slice(page * 10, (page + 1) * 10))
        append({
          ...group,
          height:
            mode === "order"
              ? 136
              : level === "station"
                ? phone
                  ? 150
                  : 136
                : level === "phase"
                  ? phone
                    ? 96
                    : 84
                  : phone
                    ? 66
                    : 46,
        });
      if (inactive.length)
        append({
          id: `quiet:${source}`,
          source,
          name: "",
          badge: "",
          color: sourceColor(source),
          tasks: [],
          quiet: inactive.length,
          height: 32,
        });
      if (list.length > 10)
        append({
          id: `pager:${source}`,
          source,
          name: "",
          badge: "",
          color: sourceColor(source),
          tasks: [],
          pager: true,
          page,
          pages: Math.ceil(list.length / 10),
          height: 40,
        });
    }
    return {
      ...mapScene(lanes, mode, view, width, now, level, relations, hubs, caches, nodeLabel),
      height: top,
    };
  }, [
    selectedGroups,
    sourceSlice,
    sourceName,
    sourceBadge,
    sourceColor,
    isExpanded,
    nowOn,
    quiet,
    groupPages,
    mode,
    level,
    view,
    width,
    open?.lane,
    panelHeight,
    boardHours,
    caches,
    now,
    phone,
    relations,
    hubs,
  ]);
  const x = (time: number) =>
      gutter + ((time - view.from) / (view.to - view.from)) * (right - gutter),
    ticks = mode === "time" ? routeTicks(view, right - gutter) : [];
  const scheduledVisible =
    mode === "time" && level === "station" && nowOn
      ? future.filter((item) => item.at > now && item.at >= view.from && item.at <= view.to)
      : [];
  const scheduleGroups = scheduledVisible.reduce<(typeof scheduledVisible)[]>((out, item) => {
    const previous = out.at(-1);
    if (previous && x(item.at) - x(previous.at(-1)!.at) < 48) previous.push(item);
    else out.push([item]);
    return out;
  }, []);
  const activeEdge = hover?.edge ?? (open?.kind === "edge" ? open.edge : null),
    activeHub = hoverHub ?? (open?.kind === "hub" ? taskIdentity(open.task) : null);
  const highlighted = new Set(
    activeEdge
      ? [activeEdge.from.id, activeEdge.to.id]
      : activeHub
        ? model.nodes
            .filter((node) =>
              node.tasks.some((task) => hubs.get(activeHub)?.has(taskIdentity(task))),
            )
            .map((node) => node.id)
        : [],
  );
  const panelTasks =
    open?.kind === "cluster"
      ? open.tasks
      : open?.kind === "hub"
        ? mapHubMembers(open.task, tasks)
        : [];
  const overview = useMemo(
      () => mapOverview(tasks, caches, { from: oldest, to: now }, width, now),
      [tasks, caches, oldest, now, width],
    ),
    overviewMax = Math.max(1, ...overview.map((bucket) => bucket.count));
  const selectTask = (task: BoardTask) => {
    const group = groups.find((item) =>
      item.tasks.some((candidate) => taskIdentity(candidate) === taskIdentity(task)),
    );
    if (!group) {
      onSelect(task);
      return;
    }
    setExpanded((current) => ({ ...current, [group.source]: true }));
    setQuiet((current) => ({ ...current, [group.source]: true }));
    setOpen((current) =>
      current?.kind === "task" && taskIdentity(current.task) === taskIdentity(task)
        ? null
        : { kind: "task", lane: group.id, task },
    );
    setHover(null);
  };
  const openNode = (node: MapNode) => {
    if (node.density) {
      const span = Math.max(MIN_SPAN, (node.density.to - node.density.from) * 2);
      moveWindow({ from: node.density.from - span / 4, to: node.density.from + span * 0.75 });
      return;
    }
    if (node.tasks.length === 1) {
      selectTask(node.tasks[0]);
      return;
    }
    if (mode === "time" && view.to - view.from > MIN_SPAN + 1) {
      const times = node.tasks.flatMap((task) => routeMoment(task, now) ?? []);
      const from = Math.min(...times) - 40 * 60_000,
        to = Math.max(...times) + 40 * 60_000;
      moveWindow({ from, to: Math.max(to, from + MIN_SPAN) });
    } else {
      setOpen({ kind: "cluster", lane: node.lane, tasks: node.tasks });
      setPanelPage(0);
    }
    setHover(null);
  };
  const showHover = (value: Omit<Hover, "x" | "y">, element: Element) => {
    const rect = element.getBoundingClientRect();
    setHover({ ...value, x: rect.left + rect.width / 2, y: rect.bottom + 8 });
  };
  useEffect(() => {
    const element = mapRef.current;
    if (!element) return;
    const measure = () => setWidth(element.clientWidth || 1100);
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  useLayoutEffect(() => {
    const element = panelRef.current;
    if (!element) return;
    const measure = () => setPanelHeight(element.getBoundingClientRect().height);
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [selection]);
  useLayoutEffect(() => {
    const element = tooltipRef.current;
    if (element) {
      const rect = element.getBoundingClientRect();
      setTooltipSize({ width: rect.width, height: rect.height });
    }
  }, [hover]);
  useEffect(() => {
    if (mode !== "time") return;
    const cleanups: (() => void)[] = [];
    for (const [element, kind] of [
      [mapRef.current, "map"],
      [overviewRef.current, "overview"],
      [axisRef.current, "axis"],
    ] as const) {
      if (!element) continue;
      const wheel = (event: WheelEvent) => {
        if (
          kind === "map" &&
          !event.ctrlKey &&
          !event.metaKey &&
          Math.abs(event.deltaY) >= Math.abs(event.deltaX)
        )
          return;
        event.preventDefault();
        event.stopPropagation();
        if (event.ctrlKey || event.metaKey) {
          if (performance.now() - wheelAt.current < 160) return;
          wheelAt.current = performance.now();
          zoom(event.deltaY > 0 ? 1 : -1);
        } else {
          const delta = event.deltaX || event.deltaY;
          setView((current) => {
            const shift =
              (delta / (width || 1)) *
              (kind === "overview" ? now - oldest : current.to - current.from);
            return clampMapWindow(
              { from: current.from + shift, to: current.to + shift },
              oldest,
              now,
            );
          });
          setOpen(null);
          setHover(null);
        }
      };
      element.addEventListener("wheel", wheel, { passive: false });
      cleanups.push(() => element.removeEventListener("wheel", wheel));
    }
    return () => cleanups.forEach((cleanup) => cleanup());
  }, [mode, width, oldest, now, zoom]);
  const pointerDown = (event: ReactPointerEvent<HTMLDivElement>, kind: "map" | "overview") => {
    if (
      mode !== "time" ||
      (kind === "map" && (event.target as Element).closest("button,a,[role=button]"))
    )
      return;
    pointers.current.set(event.pointerId, { x: event.clientX, y: event.clientY });
    pointer.current = {
      kind,
      edge: (event.target as HTMLElement).dataset.edge ?? "move",
      x: event.clientX,
      view,
    };
    if (pointers.current.size === 2) {
      const [a, b] = [...pointers.current.values()];
      pinch.current = { distance: Math.hypot(a.x - b.x, a.y - b.y), view };
    }
    event.currentTarget.setPointerCapture(event.pointerId);
    dragged.current = false;
  };
  const pointerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!pointer.current) return;
    if (pointers.current.has(event.pointerId))
      pointers.current.set(event.pointerId, { x: event.clientX, y: event.clientY });
    if (pinch.current && pointers.current.size === 2) {
      const [a, b] = [...pointers.current.values()],
        distance = Math.hypot(a.x - b.x, a.y - b.y),
        ratio = distance / pinch.current.distance;
      if (ratio > 1.25 || ratio < 0.8) {
        const next = stepMapWindow(pinch.current.view, ratio > 1 ? -1 : 1, oldest, now);
        moveWindow(next);
        pinch.current = { distance, view: next };
      }
      return;
    }
    const start = pointer.current,
      dx = event.clientX - start.x;
    if (Math.abs(dx) > 4) dragged.current = true;
    const delta =
        ((start.kind === "overview" ? now - oldest : -(start.view.to - start.view.from)) * dx) /
        width,
      next =
        start.edge === "left"
          ? { from: Math.min(start.view.from + delta, start.view.to - MIN_SPAN), to: start.view.to }
          : start.edge === "right"
            ? {
                from: start.view.from,
                to: Math.max(start.view.to + delta, start.view.from + MIN_SPAN),
              }
            : { from: start.view.from + delta, to: start.view.to + delta };
    moveWindow(next);
  };
  const pointerEnd = (event: ReactPointerEvent<HTMLDivElement>) => {
    pointers.current.delete(event.pointerId);
    pointer.current = null;
    pinch.current = null;
  };
  const mapKey = (key: string) => {
    if (key === "+" || key === "-") zoom(key === "+" ? -1 : 1);
    else {
      const delta = (key === "ArrowLeft" ? -1 : 1) * (view.to - view.from) * 0.1;
      moveWindow({ from: view.from + delta, to: view.to + delta });
    }
  };
  const changeSource = (lane: MapLane) => {
    setExpanded((current) => ({ ...current, [lane.source]: !!lane.collapsed }));
    setOpen(null);
  };
  const quietButton = (lane: MapLane) => (
    <button
      type="button"
      className="rt-quiet"
      onClick={() => setQuiet((current) => ({ ...current, [lane.source]: !current[lane.source] }))}
    >
      <ChevronDownIcon size={14} />
      {t(quiet[lane.source] ? "routes.hideQuiet" : "routes.showQuiet", { count: lane.quiet ?? 0 })}
    </button>
  );
  const groupPager = (lane: MapLane) => (
    <BoardPager
      page={lane.page ?? 0}
      pages={lane.pages ?? 1}
      onChange={(value) => {
        setGroupPages((current) => ({ ...current, [lane.source]: value }));
        setOpen(null);
      }}
    />
  );
  const renderPanel = () =>
    open ? (
      <div
        ref={panelRef}
        className="rt-panel"
        style={{
          top:
            (model.lanes.find((lane) => lane.id === open.lane)?.top ?? 0) +
            (model.lanes.find((lane) => lane.id === open.lane)?.height ?? 0),
        }}
      >
        {open.kind === "task" ? (
          <TaskBoardDetail
            task={open.task}
            tasks={tasks}
            sessions={[]}
            sourceCache={caches[open.task["omnigent.ai:source"]]}
            onSelect={selectTask}
            onClose={() => setOpen(null)}
          />
        ) : (
          <section
            aria-label={t(
              open.kind === "edge"
                ? "detail.relations"
                : open.kind === "hub"
                  ? "routes.hubTitle"
                  : "routes.clusterTitle",
            )}
          >
            <header>
              <b>
                {t(
                  open.kind === "edge"
                    ? "detail.relations"
                    : open.kind === "hub"
                      ? "routes.hubCount"
                      : "routes.stops",
                  { count: panelTasks.length },
                )}
              </b>
              <button type="button" onClick={() => setOpen(null)} aria-label={t("detail.close")}>
                <XIcon size={16} />
              </button>
            </header>
            {open.kind === "edge" ? (
              <RelationFacts relations={open.edge.relations} onSelect={selectTask} />
            ) : (
              <>
                {open.kind === "hub" && (
                  <div className="rt-hub-groups">
                    {[...new Set(panelTasks.map((task) => task["omnigent.ai:source"]))]
                      .sort(
                        (a, b) =>
                          panelTasks.filter((task) => task["omnigent.ai:source"] === b).length -
                          panelTasks.filter((task) => task["omnigent.ai:source"] === a).length,
                      )
                      .map((source) => {
                        const children = panelTasks.filter(
                          (task) => task["omnigent.ai:source"] === source,
                        );
                        return (
                          <section key={source}>
                            <h4 style={badgeStyle(sourceColor(source))}>
                              <i className="rt-bullet">{sourceBadge(source)}</i>
                              {sourceName(source)}
                              <small>{children.length}</small>
                            </h4>
                            <div className="rt-hub-counts">
                              {[
                                "running",
                                "user",
                                "failed",
                                "queued",
                                "blocked",
                                "held",
                                "done",
                                "ready",
                              ].map((state) => {
                                const count = children.filter(
                                  (task) => routeState(task) === state,
                                ).length;
                                return count ? (
                                  <span key={state}>
                                    <i className={`rt-symbol rt-${state}`} />
                                    {t(`sessions.taskState.${state}`)} {count}
                                  </span>
                                ) : null;
                              })}
                            </div>
                            <ul>
                              {children.slice(0, 5).map((task) => (
                                <li key={taskIdentity(task)}>
                                  <button type="button" onClick={() => selectTask(task)}>
                                    <code>
                                      {task["omnigent.ai:sourceDisplay"]?.identity?.key ?? task.uid}
                                    </code>
                                    <span>{taskTitle(task)}</span>
                                  </button>
                                </li>
                              ))}
                            </ul>
                            {children.length > 5 && (
                              <small>
                                {t("routes.moreChildren", { count: children.length - 5 })}
                              </small>
                            )}
                          </section>
                        );
                      })}
                  </div>
                )}
                {open.kind === "cluster" && (
                  <>
                    <ul>
                      {panelTasks
                        .slice(
                          Math.min(panelPage, Math.max(0, Math.ceil(panelTasks.length / 12) - 1)) *
                            12,
                          (Math.min(panelPage, Math.max(0, Math.ceil(panelTasks.length / 12) - 1)) +
                            1) *
                            12,
                        )
                        .map((task) => (
                          <li key={taskIdentity(task)}>
                            <button type="button" onClick={() => selectTask(task)}>
                              <code>
                                {task["omnigent.ai:sourceDisplay"]?.identity?.key ?? task.uid}
                              </code>
                              <span>{taskTitle(task)}</span>
                            </button>
                          </li>
                        ))}
                    </ul>
                    <BoardPager
                      page={Math.min(panelPage, Math.max(0, Math.ceil(panelTasks.length / 12) - 1))}
                      pages={Math.ceil(panelTasks.length / 12)}
                      onChange={setPanelPage}
                    />
                  </>
                )}
              </>
            )}
          </section>
        )}
      </div>
    ) : null;
  return (
    <div className="tb-routes rt-routes" data-route-level={mode === "order" ? "order" : level}>
      <div className="rt-toolbar">
        <div className="tb-segment" role="group" aria-label={t("routes.mode")}>
          {(["order", "time"] as const).map((value) => (
            <Button
              key={value}
              size="sm"
              variant={mode === value ? "secondary" : "ghost"}
              aria-pressed={mode === value}
              onClick={() => {
                onMode(value);
                setOpen(null);
                setHover(null);
              }}
            >
              {value === "order" ? <RouteIcon size={15} /> : <ClockIcon size={15} />}{" "}
              {t(`routes.${value}`)}
            </Button>
          ))}
        </div>
        <div className="rt-controls">
          {mode === "time" && (
            <>
              <div className="rt-presets" role="group" aria-label={t("routes.timeWindow")}>
                {[12, 24, 72, 168, 720, 2160, 8760, 0].map((hours) => {
                  const span =
                    hours === 12
                      ? 12 * HOUR
                      : hours === 24
                        ? 48 * HOUR
                        : hours
                          ? hours * HOUR + DAY
                          : now - oldest + DAY;
                  return (
                    <button
                      key={hours}
                      type="button"
                      disabled={hours > 24 && hours * HOUR > (now - oldest) * 0.9}
                      aria-pressed={Math.abs(view.to - view.from - span) < span * 0.04}
                      onClick={() =>
                        moveWindow(
                          hours === 12
                            ? { from: now - 6 * HOUR, to: now + 6 * HOUR }
                            : hours === 24
                              ? initialWindow(now)
                              : { from: hours ? now - hours * HOUR : oldest, to: now + DAY },
                        )
                      }
                    >
                      {hours === 0
                        ? t("routes.allTime")
                        : hours === 12
                          ? "12H"
                          : hours === 24
                            ? "24H"
                            : hours === 8760
                              ? "1Y"
                              : `${hours / 24}D`}
                    </button>
                  );
                })}
              </div>
              <div className="rt-zoom">
                <button type="button" aria-label={t("routes.zoomOut")} onClick={() => zoom(1)}>
                  <MinusIcon size={15} />
                </button>
                <button
                  type="button"
                  aria-label={t("routes.zoomIn")}
                  disabled={
                    JSON.stringify(stepMapWindow(view, -1, oldest, now)) === JSON.stringify(view)
                  }
                  onClick={() => zoom(-1)}
                >
                  <PlusIcon size={15} />
                </button>
              </div>
            </>
          )}
          <TaskBoardSearch
            label={t("routes.focus")}
            options={groups.map((group) => ({
              id: group.id,
              label: group.name || t("group.default"),
              hint: sourceName(group.source),
            }))}
            selected={focus}
            onChange={(ids) => {
              onFocus(ids);
              setSourcePage(0);
              setOpen(null);
            }}
          />
          <button
            type="button"
            className="rt-icon"
            aria-label={t(allExpanded ? "routes.collapseAll" : "routes.expandAll")}
            onClick={() => {
              setExpanded({});
              setAllExpanded(!allExpanded);
              setOpen(null);
            }}
          >
            {allExpanded ? <ChevronsDownUpIcon size={16} /> : <ChevronsUpDownIcon size={16} />}
          </button>
          <button
            type="button"
            className="rt-icon"
            aria-label={t("routes.help")}
            aria-expanded={help}
            onClick={() => setHelp(!help)}
          >
            <CircleHelpIcon size={16} />
          </button>
        </div>
      </div>
      {help && (
        <section className="rt-help" aria-label={t("routes.help")}>
          <b>
            {t(mode === "order" ? "routes.orderHelpTitle" : "routes.timeHelpTitle", {
              level: t(`routes.${level}`),
            })}
          </b>
          <p>
            {t(mode === "order" ? "routes.orderHelp" : "routes.timeHelp", {
              hours: boardHours,
              station: lodStation,
              phase: lodPhase,
            })}
          </p>
          <div className="rt-legend">
            {["running", "user", "failed", "ready", "held", "queued"].map((state) => (
              <span key={state}>
                <i className={`rt-symbol rt-${state}`} />
                {t(`sessions.taskState.${state}`)}
              </span>
            ))}
          </div>
          <p>{t("routes.gestureHelp")}</p>
        </section>
      )}
      {mode === "time" && (
        <div
          ref={overviewRef}
          className="rt-overview"
          role="region"
          tabIndex={0}
          aria-label={t("routes.overview")}
          onClickCapture={(event) => {
            if (dragged.current) {
              event.preventDefault();
              event.stopPropagation();
              dragged.current = false;
            }
          }}
          onPointerDown={(event) => pointerDown(event, "overview")}
          onPointerMove={pointerMove}
          onPointerUp={pointerEnd}
          onPointerCancel={pointerEnd}
          onKeyDown={(event) => {
            if (["ArrowLeft", "ArrowRight", "+", "-"].includes(event.key)) {
              event.preventDefault();
              mapKey(event.key);
            }
          }}
        >
          <svg width="100%" height="46" aria-hidden="true">
            {overview.map((bucket) => (
              <rect
                key={bucket.from}
                x={((bucket.from - oldest) / (now - oldest)) * width}
                y={32 - Math.sqrt(bucket.count / overviewMax) * 26}
                width={Math.max(1, ((bucket.to - bucket.from) / (now - oldest)) * width - 1)}
                height={Math.sqrt(bucket.count / overviewMax) * 26}
                rx="1"
              />
            ))}
            {routeTicks({ from: oldest, to: now }, width).map((date) => (
              <text key={+date} x={((+date - oldest) / (now - oldest)) * width} y="44">
                {formatDate(date, { month: "short", day: "numeric" })}
              </text>
            ))}
          </svg>
          <div
            className="rt-overview-window"
            style={{
              left: Math.min(
                Math.max(0, ((view.from - oldest) / (now - oldest)) * width),
                width -
                  Math.max(
                    10,
                    Math.min(
                      width,
                      ((Math.min(now, view.to) - Math.max(oldest, view.from)) / (now - oldest)) *
                        width,
                    ),
                  ),
              ),
              width: Math.max(
                10,
                Math.min(
                  width,
                  ((Math.min(now, view.to) - Math.max(oldest, view.from)) / (now - oldest)) * width,
                ),
              ),
            }}
          >
            {(["left", "right"] as const).map((edge) => (
              <span
                key={edge}
                data-edge={edge}
                className={`rt-grip ${edge}`}
                role="slider"
                tabIndex={0}
                aria-label={t(edge === "left" ? "routes.windowStart" : "routes.windowEnd")}
                aria-valuemin={edge === "left" ? oldest : view.from + MIN_SPAN}
                aria-valuemax={edge === "left" ? view.to - MIN_SPAN : now + DAY}
                aria-valuenow={edge === "left" ? view.from : view.to}
                onKeyDown={(event) => {
                  if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
                    event.preventDefault();
                    event.stopPropagation();
                    const delta =
                      (event.key === "ArrowLeft" ? -1 : 1) * (view.to - view.from) * 0.1;
                    moveWindow(
                      edge === "left"
                        ? { from: Math.min(view.from + delta, view.to - MIN_SPAN), to: view.to }
                        : { from: view.from, to: Math.max(view.to + delta, view.from + MIN_SPAN) },
                    );
                  }
                }}
              />
            ))}
          </div>
        </div>
      )}
      {mode === "time" && (
        <div className="rt-window-word">
          <span>
            {momentWord(view.from)}
            {" — "}
            {momentWord(view.to)}
            {" · "}
            {t(`routes.${level}`)}
          </span>
          <button
            type="button"
            onClick={() => {
              setView(initialWindow(now));
              setOpen(null);
            }}
          >
            {t("routes.now")}
          </button>
        </div>
      )}
      <div
        ref={mapRef}
        className={`rt-map rt-${mode}${phone ? " rt-phone" : ""}`}
        role="region"
        aria-label={t("view.routes")}
        tabIndex={0}
        data-window-from={view.from}
        data-window-to={view.to}
        onPointerDown={(event) => pointerDown(event, "map")}
        onPointerMove={pointerMove}
        onPointerUp={pointerEnd}
        onPointerCancel={pointerEnd}
        onClickCapture={(event) => {
          if (dragged.current) {
            event.preventDefault();
            event.stopPropagation();
            dragged.current = false;
          }
        }}
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            setOpen(null);
            setHover(null);
            setHoverHub(null);
          }
          if (
            mode === "time" &&
            event.target === event.currentTarget &&
            ["ArrowLeft", "ArrowRight", "+", "-"].includes(event.key)
          ) {
            event.preventDefault();
            mapKey(event.key);
          }
        }}
      >
        {mode === "time" && (
          <div ref={axisRef} className="rt-axis">
            {ticks.map((date) => (
              <span key={+date} style={{ left: x(+date) }}>
                {formatDate(date, {
                  month: "short",
                  day: "numeric",
                  ...(view.to - view.from < 3 * DAY ? { hour: "numeric" as const } : {}),
                })}
              </span>
            ))}
          </div>
        )}
        {mode === "order" && phone ? (
          <div className="rt-phone-order">
            {model.lanes
              .filter((lane) => lane.header)
              .map((band) => (
                <section key={band.id} style={badgeStyle(band.color)}>
                  <button
                    type="button"
                    className="rt-phone-source"
                    aria-expanded={!band.collapsed}
                    onClick={() => changeSource(band)}
                  >
                    <ChevronDownIcon size={15} />
                    <i className="rt-bullet">{band.badge}</i>
                    <b>{band.name}</b>
                    <small>{t("board.tasks", { count: band.count ?? band.tasks.length })}</small>
                  </button>
                  {!band.collapsed &&
                    model.lanes
                      .filter(
                        (lane) =>
                          lane.source === band.source && !lane.header && !lane.quiet && !lane.pager,
                      )
                      .map((lane) => {
                        const sorted = [
                            ...lane.tasks.filter(routeAttention),
                            ...lane.tasks.filter((task) => !routeAttention(task)).reverse(),
                          ],
                          page = Math.min(
                            phonePages[lane.id] ?? 0,
                            Math.max(0, Math.ceil(sorted.length / 8) - 1),
                          );
                        return (
                          <section
                            key={lane.id}
                            className="rt-phone-lane"
                            style={badgeStyle(lane.color)}
                          >
                            <h3>
                              <i className="rt-bullet">{lane.badge}</i>
                              {lane.name}
                              <small>{lane.tasks.length}</small>
                            </h3>
                            <ol>
                              {sorted.slice(page * 8, (page + 1) * 8).map((task) => (
                                <Fragment key={taskIdentity(task)}>
                                  <li>
                                    <button
                                      aria-label={taskTitle(task)}
                                      data-route-task={task.uid}
                                      type="button"
                                      onClick={() => selectTask(task)}
                                    >
                                      <Station tasks={[task]} />
                                      <span>
                                        <b>{taskTitle(task)}</b>
                                        <small>
                                          {stateWord(task)}
                                          {!routeQueued(task) &&
                                          !["running", "user"].includes(routeState(task)) &&
                                          routeMoment(task, now)
                                            ? ` · ${momentWord(routeMoment(task, now)!)}`
                                            : ""}
                                        </small>
                                      </span>
                                    </button>
                                    {open?.kind === "task" &&
                                      taskIdentity(open.task) === taskIdentity(task) && (
                                        <TaskBoardDetail
                                          task={task}
                                          tasks={tasks}
                                          sessions={[]}
                                          sourceCache={caches[task["omnigent.ai:source"]]}
                                          onSelect={selectTask}
                                          onClose={() => setOpen(null)}
                                        />
                                      )}
                                  </li>
                                </Fragment>
                              ))}
                            </ol>
                            <BoardPager
                              page={page}
                              pages={Math.ceil(sorted.length / 8)}
                              onChange={(value) =>
                                setPhonePages((current) => ({ ...current, [lane.id]: value }))
                              }
                            />
                          </section>
                        );
                      })}
                  {!band.collapsed &&
                    model.lanes
                      .filter((lane) => lane.source === band.source && lane.quiet)
                      .map((lane) => <Fragment key={lane.id}>{quietButton(lane)}</Fragment>)}
                  {!band.collapsed &&
                    model.lanes
                      .filter((lane) => lane.source === band.source && lane.pager)
                      .map((lane) => <Fragment key={lane.id}>{groupPager(lane)}</Fragment>)}
                </section>
              ))}
          </div>
        ) : (
          <div
            className="rt-scene"
            style={{ height: model.height + (scheduledVisible.length ? 100 : 0) }}
          >
            <svg
              className={`rt-svg${activeEdge ? " rt-edge-focus" : ""}`}
              width={width}
              height={model.height + (scheduledVisible.length ? 100 : 0)}
              aria-label={t("routes.map")}
            >
              {model.edges
                .filter((edge) => edge.long && !edge.hub)
                .map((edge) => (
                  <path
                    key={edge.id}
                    className={`rt-connection rt-long${activeEdge?.id === edge.id ? " active" : ""}`}
                    d={edge.path}
                    stroke={edge.from.color}
                    strokeDasharray={
                      edge.relations.every((relation) => relation.kind === "dependency")
                        ? "10 8"
                        : undefined
                    }
                  />
                ))}
              {mode === "time" &&
                ticks.map((date) => (
                  <line
                    key={+date}
                    x1={x(+date)}
                    x2={x(+date)}
                    y1="0"
                    y2={model.height}
                    className="rt-grid"
                  />
                ))}
              {mode === "time" && nowOn && (
                <line x1={x(now)} x2={x(now)} y1="0" y2={model.height} className="rt-now" />
              )}
              {model.edges
                .filter((edge) => !edge.long && !edge.hub)
                .map((edge) => (
                  <g key={edge.id}>
                    <path d={edge.path} fill="none" stroke="var(--card-solid)" strokeWidth="9" />
                    <path
                      className={`rt-connection${activeEdge?.id === edge.id ? " active" : ""}`}
                      d={edge.path}
                      stroke={edge.from.color}
                      strokeWidth={
                        edge.relations.every((relation) => relation.kind === "parent") ? 3 : 4.5
                      }
                      strokeDasharray={
                        edge.relations.every((relation) => relation.kind === "dependency")
                          ? "10 8"
                          : undefined
                      }
                    />
                  </g>
                ))}
              {model.lanes.map((lane) => (
                <g key={lane.id}>
                  {lane.nodes
                    .slice(1)
                    .filter((node) => !node.density)
                    .map((node, index) => (
                      <line
                        key={node.id}
                        x1={lane.nodes[index].x1}
                        x2={node.x}
                        y1={node.y}
                        y2={node.y}
                        stroke={node.color}
                        strokeWidth={node.queued ? 4 : lane.collapsed ? 7 : 11}
                        strokeDasharray={node.queued ? "1 10" : undefined}
                        strokeLinecap="round"
                      />
                    ))}
                  {lane.nodes
                    .filter((node) => node.density)
                    .map((node) => (
                      <rect
                        key={node.id}
                        x={node.x}
                        y={node.y - node.height / 2}
                        width={Math.max(1, node.x1 - node.x - 0.8)}
                        height={node.height}
                        fill={node.color}
                      />
                    ))}
                </g>
              ))}
              {model.edges
                .filter(
                  (edge) =>
                    edge.hub && edge.from.tasks.some((task) => taskIdentity(task) === activeHub),
                )
                .map((edge) => (
                  <g key={edge.id} className="rt-hub-edge">
                    <path d={edge.path} stroke={edge.from.color} />
                    <circle
                      cx={edge.from.x + (edge.to.x - edge.from.x) * 0.72}
                      cy={edge.from.y + (edge.to.y - edge.from.y) * 0.72}
                      r="10"
                      stroke={edge.from.color}
                    />
                    <text
                      x={edge.from.x + (edge.to.x - edge.from.x) * 0.72}
                      y={edge.from.y + (edge.to.y - edge.from.y) * 0.72 + 4}
                      textAnchor="middle"
                    >
                      {edge.relations.length}
                    </text>
                  </g>
                ))}
              {model.nodes
                .filter((node) => highlighted.has(node.id))
                .map((node) => (
                  <ellipse
                    key={node.id}
                    cx={(node.x + node.x1) / 2}
                    cy={node.y}
                    rx={(node.x1 - node.x) / 2 + 21}
                    ry="21"
                    className="rt-endpoint"
                  />
                ))}
              {model.edges
                .filter((edge) => !edge.hub)
                .map((edge) => (
                  <path
                    key={edge.id}
                    className="rt-edge-hit"
                    d={edge.path}
                    role="button"
                    tabIndex={0}
                    aria-label={edge.relations
                      .map((relation) =>
                        t("routes.edge", {
                          from: taskTitle(relation.from),
                          to: taskTitle(relation.to),
                          kind: t(relation.labelKey ?? `relation.${relation.kind}`),
                        }),
                      )
                      .join("; ")}
                    onMouseEnter={(event) => showHover({ edge }, event.currentTarget)}
                    onMouseLeave={() => setHover(null)}
                    onFocus={(event) => showHover({ edge }, event.currentTarget)}
                    onBlur={() => setHover(null)}
                    onClick={() => {
                      setOpen((current) =>
                        current?.kind === "edge" && current.edge.id === edge.id
                          ? null
                          : { kind: "edge", lane: edge.from.lane, edge },
                      );
                      setHover(null);
                    }}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        setOpen({ kind: "edge", lane: edge.from.lane, edge });
                        setHover(null);
                      }
                    }}
                  />
                ))}
            </svg>
            {model.lanes.map((lane) => (
              <div
                key={lane.id}
                className={`rt-lane${lane.header ? " rt-source" : ""}${lane.header && !lane.collapsed ? " thin" : ""}${lane.quiet ? " quiet" : ""}`}
                style={{ ...badgeStyle(lane.color), top: lane.top, height: lane.height }}
              >
                {lane.quiet ? (
                  quietButton(lane)
                ) : lane.pager ? (
                  groupPager(lane)
                ) : (
                  <div className="rt-lane-heading">
                    {lane.header ? (
                      <button
                        type="button"
                        aria-expanded={!lane.collapsed}
                        onClick={() => changeSource(lane)}
                      >
                        <ChevronDownIcon size={14} className={lane.collapsed ? "-rotate-90" : ""} />
                        <i className="rt-bullet">{lane.badge}</i>
                        <span>
                          <b>{lane.name}</b>
                          <small>
                            {t("board.tasks", { count: lane.count ?? lane.tasks.length })}
                          </small>
                        </span>
                      </button>
                    ) : (
                      <>
                        <i className="rt-bullet">{lane.badge}</i>
                        <span>
                          <h3>{lane.name || t("group.default")}</h3>
                          <small>
                            {t("board.tasks", { count: lane.count ?? lane.tasks.length })}
                          </small>
                        </span>
                      </>
                    )}
                  </div>
                )}
              </div>
            ))}
            {model.nodes.map((node) => (
              <Fragment key={node.id}>
                {node.density ? (
                  <button
                    type="button"
                    className="rt-density-hit"
                    style={{
                      left: node.x,
                      top: node.y - 22,
                      width: Math.max(5, node.x1 - node.x),
                      height: 44,
                    }}
                    aria-label={t("routes.densityCount", {
                      count: node.density.count,
                      from: momentWord(node.density.from),
                      to: momentWord(node.density.to),
                    })}
                    onMouseEnter={(event) => showHover({ node }, event.currentTarget)}
                    onMouseLeave={() => setHover(null)}
                    onClick={() => openNode(node)}
                  />
                ) : (
                  <>
                    <button
                      type="button"
                      className="rt-stop"
                      data-route-task={node.tasks.length === 1 ? node.tasks[0].uid : undefined}
                      style={{
                        left: (node.x + node.x1) / 2,
                        top: node.y,
                        ...badgeStyle(node.color),
                      }}
                      aria-label={
                        node.tasks.length > 1
                          ? t("routes.openCluster", { count: node.tasks.length })
                          : nodeLabel(node)
                      }
                      onMouseEnter={(event) => {
                        showHover({ node }, event.currentTarget);
                        if (node.tasks.length === 1 && hubs.get(taskIdentity(node.tasks[0]))?.size)
                          setHoverHub(taskIdentity(node.tasks[0]));
                      }}
                      onMouseLeave={() => {
                        setHover(null);
                        setHoverHub(null);
                      }}
                      onFocus={(event) => showHover({ node }, event.currentTarget)}
                      onBlur={() => setHover(null)}
                      onClick={() => openNode(node)}
                    >
                      <Station tasks={node.tasks} />
                    </button>
                    {node.label && (
                      <span
                        className={`rt-label rt-label-${node.tasks.length === 1 ? routeState(node.tasks[0]) : "cluster"}`}
                        style={{ left: node.label.x, top: node.label.y, width: node.label.width }}
                      >
                        <b>{nodeLabel(node)}</b>
                        {node.tasks.length === 1 &&
                          !routeQueued(node.tasks[0]) &&
                          !["running", "user"].includes(routeState(node.tasks[0])) &&
                          routeMoment(node.tasks[0], now) && (
                            <small>{momentWord(routeMoment(node.tasks[0], now)!)}</small>
                          )}
                      </span>
                    )}
                    {node.tasks.length === 1 &&
                      (hubs.get(taskIdentity(node.tasks[0]))?.size ?? 0) > 0 && (
                        <button
                          type="button"
                          className="rt-hub-badge"
                          style={{ left: node.x + 16, top: node.y - 31 }}
                          aria-label={t("routes.hubCount", {
                            count: hubs.get(taskIdentity(node.tasks[0]))!.size,
                          })}
                          onMouseEnter={() => setHoverHub(taskIdentity(node.tasks[0]))}
                          onMouseLeave={() => setHoverHub(null)}
                          onFocus={() => setHoverHub(taskIdentity(node.tasks[0]))}
                          onBlur={() => setHoverHub(null)}
                          onClick={() => {
                            setOpen((current) =>
                              current?.kind === "hub" &&
                              taskIdentity(current.task) === taskIdentity(node.tasks[0])
                                ? null
                                : { kind: "hub", lane: node.lane, task: node.tasks[0] },
                            );
                            setHover(null);
                            setPanelPage(0);
                          }}
                        >
                          {hubs.get(taskIdentity(node.tasks[0]))!.size}
                        </button>
                      )}
                  </>
                )}
              </Fragment>
            ))}
            {scheduledVisible.length > 0 && (
              <div className="rt-future" style={{ top: model.height, height: 100 }}>
                <div className="rt-lane-heading">
                  <i className="rt-bullet">{"◆"}</i>
                  <b>{t("schedule.title")}</b>
                </div>
                {scheduleGroups.map((items) => (
                  <div
                    className="rt-future-stop"
                    key={items[0].id}
                    style={{
                      left: Math.min(width - 70, Math.max(gutter + 18, x(items[0].at))),
                      top: 34,
                    }}
                  >
                    <span className="rt-symbol rt-scheduled">
                      {items.length > 1 ? items.length : null}
                    </span>
                    {items.length === 1 && items[0].native ? (
                      <Link {...boardExitProps()} to={`/tasks/${encodeURIComponent(items[0].id)}`}>
                        {items[0].label}
                      </Link>
                    ) : items.length === 1 ? (
                      items[0].task ? (
                        <button
                          type="button"
                          data-route-task={items[0].task.uid}
                          onClick={() => selectTask(items[0].task!)}
                        >
                          {items[0].label}
                        </button>
                      ) : (
                        <span title={items[0].sourceLabel}>{items[0].label}</span>
                      )
                    ) : (
                      <details className="rt-future-cluster">
                        <summary>{t("routes.futureCount", { count: items.length })}</summary>
                        <ul>
                          {items.map((item) => (
                            <li key={item.id}>
                              {item.native ? (
                                <Link
                                  {...boardExitProps()}
                                  to={`/tasks/${encodeURIComponent(item.id)}`}
                                >
                                  {item.label}
                                </Link>
                              ) : (
                                <b>{item.label}</b>
                              )}
                              <small>
                                {momentWord(item.at)}
                                {" · "}
                                {item.sourceLabel}
                              </small>
                            </li>
                          ))}
                        </ul>
                      </details>
                    )}
                    <small>{momentWord(items[0].at)}</small>
                  </div>
                ))}
              </div>
            )}
            {renderPanel()}
          </div>
        )}
        {!model.lanes.length && !scheduledVisible.length && (
          <div className="tb-empty">
            {tasks.length ? (
              <p>{t("search.empty")}</p>
            ) : (
              <>
                <h2>{t("board.empty")}</h2>
                <Link {...boardExitProps()} to="/settings/general#task-board-settings">
                  {t("sources.openSettings")}
                </Link>
              </>
            )}
          </div>
        )}
      </div>
      <BoardPager
        page={currentPage}
        pages={pageCount}
        onChange={(value) => {
          setSourcePage(value);
          setOpen(null);
        }}
      />
      {hover && !phone && (
        <div
          ref={tooltipRef}
          role="tooltip"
          className="tb-session-hover rt-tooltip"
          style={{
            left: Math.max(
              8,
              Math.min(globalThis.innerWidth - tooltipSize.width - 8, hover.x + 12),
            ),
            top: Math.max(8, Math.min(globalThis.innerHeight - tooltipSize.height - 8, hover.y)),
          }}
        >
          {hover.edge ? (
            <>
              <b>{t("routes.relationTitle")}</b>
              <RelationFacts relations={hover.edge.relations} onSelect={selectTask} />
            </>
          ) : hover.node?.density ? (
            <>
              <b>{t("routes.stops", { count: hover.node.density.count })}</b>
              <p>
                {momentWord(hover.node.density.from)}
                {" — "}
                {momentWord(hover.node.density.to)}
              </p>
            </>
          ) : hover.node?.tasks.length === 1 ? (
            <TaskBoardTaskHover task={hover.node.tasks[0]} tasks={tasks} />
          ) : (
            <>
              <b>{t("routes.stops", { count: hover.node?.tasks.length ?? 0 })}</b>
              <ul>
                {hover.node?.tasks.slice(0, 6).map((task) => (
                  <li key={taskIdentity(task)}>{taskTitle(task)}</li>
                ))}
              </ul>
            </>
          )}
        </div>
      )}
    </div>
  );
}

export function BoardPager({
  page,
  pages,
  onChange,
}: {
  page: number;
  pages: number;
  onChange: (page: number) => void;
}) {
  if (pages <= 1) return null;
  return (
    <nav className="tb-pager" aria-label={t("pager.title")}>
      <Button
        size="icon-sm"
        variant="ghost"
        disabled={page === 0}
        onClick={() => onChange(page - 1)}
        aria-label={t("pager.previous")}
      >
        <ChevronLeftIcon className="size-4" />
      </Button>
      <span>{t("pager.page", { page: page + 1, pages })}</span>
      <Button
        size="icon-sm"
        variant="ghost"
        disabled={page + 1 >= pages}
        onClick={() => onChange(page + 1)}
        aria-label={t("pager.next")}
      >
        <ChevronRightIcon className="size-4" />
      </Button>
    </nav>
  );
}
