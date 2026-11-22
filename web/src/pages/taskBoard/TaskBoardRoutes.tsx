import { Segmented } from "@/components/ui/segmented";
import { boardSourceColors } from "./taskBoardVisual";
import {
  Fragment,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
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
  HandIcon,
  CircleCheckIcon,
  CircleXIcon,
  CircleDotIcon,
  BanIcon,
  PackageCheckIcon,
  ActivityIcon,
  PinIcon,
  Rows3Icon,
  Rows4Icon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { Link } from "@/lib/routing";
import { boardExitProps } from "@/lib/boardFrame";
import { taskIdentity, type BoardTask, type TaskSource } from "./taskBoardData";
import type { BoardSchedule, BoardSession } from "./taskBoardNative";
import type { SourceCache } from "./taskBoardSnapshotData";
import {
  DAY,
  HOUR,
  MIN_SPAN,
  detailLevel,
  initialWindow,
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
  routeMoment,
  routeQueued,
  routeState,
  stepMapWindow,
  mapAxisTicks,
  routeLabelLines,
  placeMapLabels,
  type MapGroup,
  type MapRelation,
  type MapNode,
  type MapLane,
  type MapEdge,
} from "./taskBoardRouteModel";
import { TaskBoardDetail, taskTitle } from "./TaskBoardDetail";
import { TaskBoardTaskHover } from "./TaskBoardTaskHover";
import { TaskBoardRouteFocus } from "./TaskBoardRouteFocus";
import { TaskBoardRoutePins } from "./TaskBoardRoutePins";
import {
  routeWorthShowing,
  useRouteViewSettings,
  useStableList,
  type RoutePins,
} from "./taskBoardRouteView";
import { BoardIdentityMark } from "./BoardIdentityMark";
import { formatDate, formatNumber, t } from "./taskBoardText";
import "./taskBoardRoutes.css";

const emptyCaches: Record<string, SourceCache> = {};
const emptySessions: BoardSession[] = [];
type OpenPanel =
  | { kind: "task"; lane: string; anchor: string; task: BoardTask }
  | { kind: "cluster"; lane: string; tasks: BoardTask[] }
  | { kind: "edge"; lane: string; edge: MapEdge }
  | { kind: "hub"; lane: string; task: BoardTask };
/** Hover keeps identities only; each render resolves them against the current scene. */
interface Hover {
  node?: string;
  edge?: string;
  x: number;
  y: number;
}
const nodeKey = (node: MapNode) => (node.tasks.length ? taskIdentity(node.tasks[0]) : node.id);
const currentNode = (nodes: MapNode[], key?: string) =>
  key === undefined
    ? undefined
    : nodes.find((node) =>
        node.tasks.length ? node.tasks.some((task) => taskIdentity(task) === key) : node.id === key,
      );
const currentEdge = (edges: MapEdge[], relation?: string) =>
  relation === undefined
    ? undefined
    : edges.find((edge) => edge.relations.some((item) => item.id === relation));
const currentHub = (nodes: MapNode[], hub: string | null) =>
  hub !== null && nodes.some((node) => node.hub && taskIdentity(node.tasks[0]) === hub);
const badgeStyle = (color: string) => ({ "--rt-color": color }) as CSSProperties;
const dayKey = (time: number) => {
  const date = new Date(time);
  return `${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
};
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
      : sameDay(date, new Date(current + DAY))
        ? t("routes.tomorrow")
        : (current - time > 300 * DAY ? date.getFullYear() + "-" : "") + dayKey(time);
  return `${day ? day + " " : ""}${formatDate(time, { hour: "2-digit", minute: "2-digit", hour12: false })}`;
};
const stateWord = (task: BoardTask) => t(`sessions.taskState.${routeState(task)}`);
const nodeLabel = (node: MapNode) =>
  node.tasks.length > 1
    ? t(
        node.timeCluster
          ? "routes.stops"
          : node.queued
            ? "routes.queuedCount"
            : "routes.earlierCount",
        { count: node.tasks.length },
      )
    : (node.tasks[0]["omnigent.ai:sourceDisplay"]?.identity?.label ?? taskTitle(node.tasks[0]));
function Station({
  tasks,
  extent = 0,
  time = false,
}: {
  tasks: BoardTask[];
  extent?: number;
  time?: boolean;
}) {
  if (tasks.length > 1)
    return (
      <span
        style={{ width: extent + 29 }}
        className={`rt-capsule${tasks.every(routeQueued) ? " queued" : time && tasks.some((task) => routeState(task) === "failed") ? " hot" : ""}`}
      >
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
        <RouteAgentMark
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
const taskKey = (task: BoardTask) =>
  task["omnigent.ai:sourceDisplay"]?.identity?.key ?? task["omnigent.ai:key"] ?? task.uid;
function RouteAgentMark(props: Parameters<typeof BoardIdentityMark>[0]) {
  return props.product === "claude" ? (
    <span className="tb-identity-mark tb-brand-claude" aria-hidden="true">
      <svg viewBox="0 0 24 24" width={props.size} height={props.size}>
        <path
          fill="currentColor"
          fillRule="evenodd"
          clipRule="evenodd"
          d="M20.998 10.949H24v3.102h-3v3.028h-1.487V20H18v-2.921h-1.487V20H15v-2.921H9V20H7.488v-2.921H6V20H4.487v-2.921H3V14.05H0V10.95h3V5h17.998v5.949zM6 10.949h1.488V8.102H6v2.847zm10.51 0H18V8.102h-1.49v2.847z"
        />
      </svg>
    </span>
  ) : (
    <BoardIdentityMark {...props} />
  );
}
function PhoneStation({ task }: { task: BoardTask }) {
  const state = routeState(task),
    Icon = {
      running: ActivityIcon,
      user: HandIcon,
      done: CircleCheckIcon,
      failed: CircleXIcon,
      ready: PackageCheckIcon,
      held: CircleDotIcon,
      queued: ClockIcon,
      blocked: BanIcon,
    }[state],
    session = task["omnigent.ai:sourceDisplay"]?.sessions?.find(
      (item) => item.status === "running",
    );
  return (
    <i className={`rt-phone-icon state-${state}`}>
      {state === "running" && session ? (
        <RouteAgentMark
          id={session.agentId}
          label={session.agentId}
          product={session.agentId}
          size={16}
        />
      ) : (
        <Icon size={14} />
      )}
    </i>
  );
}
const measureLabel = (() => {
  let context: CanvasRenderingContext2D | null = null;
  const measured = new Map<string, number>();
  return (text: string, size: number, weight: number) => {
    const font = 'ui-sans-serif,system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif',
      key = font + size + weight + text;
    if (measured.has(key)) return measured.get(key)!;
    if (typeof document !== "undefined" && !navigator.userAgent.includes("jsdom"))
      context ??= document.createElement("canvas").getContext("2d");
    if (context) context.font = `${weight} ${size}px ${font}`;
    const width = context
      ? context.measureText(text).width
      : [...text].reduce((n, char) => n + size * (char.charCodeAt(0) > 127 ? 1 : 0.56), 0);
    measured.set(key, width);
    return width;
  };
})();
const maxFlows = 12;
/** Pause looping motion while the tab is hidden. */
function usePageHidden() {
  const [hidden, setHidden] = useState(() => typeof document !== "undefined" && document.hidden);
  useEffect(() => {
    const update = () => setHidden(document.hidden);
    document.addEventListener("visibilitychange", update);
    return () => document.removeEventListener("visibilitychange", update);
  }, []);
  return hidden;
}
const noMarks = { changed: new Set<string>(), added: new Set<string>() };
/** Cards whose state changed or that appeared since the previous refresh, marked for a moment. */
function useStateMarks(tasks: BoardTask[]) {
  const [marks, setMarks] = useState(noMarks),
    seen = useRef<Map<string, string> | null>(null);
  useEffect(() => {
    const before = seen.current,
      next = new Map(tasks.map((task) => [taskIdentity(task), routeState(task)]));
    seen.current = next;
    if (!before) return;
    const changed = new Set<string>(),
      added = new Set<string>();
    for (const [id, state] of next)
      if (!before.has(id)) added.add(id);
      else if (before.get(id) !== state) changed.add(id);
    if (!changed.size && !added.size) return;
    setMarks({ changed, added });
    const timer = setTimeout(() => setMarks(noMarks), 2400);
    return () => clearTimeout(timer);
  }, [tasks]);
  return marks;
}
/** One compact label line, cut with an ellipsis at `max` pixels. */
function clipLabel(text: string, max: number) {
  if (measureLabel(text, 12, 700) <= max) return text;
  let low = 1,
    high = text.length;
  while (low < high) {
    const middle = Math.ceil((low + high) / 2);
    if (measureLabel(text.slice(0, middle) + "…", 12, 700) <= max) low = middle;
    else high = middle - 1;
  }
  return text.slice(0, low) + "…";
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
          <p>
            <button
              type="button"
              onClick={() => onSelect(relation.from)}
              aria-label={taskKey(relation.from)}
            >
              <code>{taskKey(relation.from)}</code> {taskTitle(relation.from)}
            </button>
          </p>
          <p className="to">
            <button
              type="button"
              onClick={() => onSelect(relation.to)}
              aria-label={taskKey(relation.to)}
            >
              <code>{taskKey(relation.to)}</code> {taskTitle(relation.to)}
            </button>
          </p>
          <small>
            {relation.label ??
              (relation.labelKey
                ? t(relation.labelKey)
                : t(`routes.direction.${relation.kind}`, {
                    from: taskKey(relation.from),
                    to: taskKey(relation.to),
                  }))}
          </small>
          <small>
            <i className={`rt-origin ${relation.origin ?? "reported"}`}>
              {relation.origin
                ? t(`routes.origin.${relation.origin}`)
                : t("routes.relationReported")}
            </i>{" "}
            {relation.origin === "manual"
              ? t("routes.manualSource", { source: relation.provenance ?? "" })
              : relation.since
                ? t("routes.attachedAt", { date: relation.since.slice(0, 10) })
                : t("routes.relationUndated")}
          </small>
        </div>
      ))}
    </div>
  );
}
export function TaskBoardRoutes({
  sessions = emptySessions,
  tasks: suppliedTasks,
  sources: suppliedSources,
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
  sessions?: BoardSession[];
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
  // The page filters on every render; equal contents keep one identity so layouts stay cached.
  const tasks = useStableList(suppliedTasks),
    sources = useStableList(suppliedSources);
  const [settings, updateSettings] = useRouteViewSettings(),
    compact = settings.rows === "compact",
    pins = settings.pins;
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
    groups = useMemo(
      () => mapGroups(tasks, sources, caches, now, boardHours),
      [tasks, sources, caches, now, boardHours],
    ),
    relations = useMemo(() => mapRelations(tasks), [tasks]);
  const relationCounts = useMemo(() => {
    const counts = new Map<string, { outgoing: number; incoming: number }>();
    for (const relation of relations) {
      const from = taskIdentity(relation.from),
        to = taskIdentity(relation.to);
      if (!counts.has(from)) counts.set(from, { outgoing: 0, incoming: 0 });
      if (!counts.has(to)) counts.set(to, { outgoing: 0, incoming: 0 });
      counts.get(from)!.outgoing++;
      counts.get(to)!.incoming++;
    }
    return counts;
  }, [relations]);
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
    [hoverStore] = useState(createHoverStore);
  const [focusOpen, setFocusOpen] = useState(false),
    [pinsOpen, setPinsOpen] = useState(false);
  const open = useMemo<OpenPanel | null>(() => {
    if (!selection) return null;
    const byId = new Map(tasks.map((task) => [taskIdentity(task), task]));
    if (selection.kind === "task" || selection.kind === "hub") {
      const task = byId.get(taskIdentity(selection.task));
      return task && (selection.kind !== "task" || byId.has(selection.anchor))
        ? { ...selection, task }
        : null;
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
    detailTrigger = useRef<HTMLElement | null>(null),
    overviewRef = useRef<HTMLDivElement>(null),
    axisRef = useRef<HTMLDivElement>(null),
    panelRef = useRef<HTMLDivElement>(null),
    svgRef = useRef<SVGSVGElement>(null),
    plotRef = useRef<HTMLDivElement>(null);
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
          const at =
            Date.parse(task["omnigent.ai:sourceDisplay"]?.historyState?.first ?? "") ||
            routeMoment(task, now);
          return at === undefined ? [] : [at];
        }),
        ...Object.values(caches).flatMap((cache) =>
          (cache.display.closedDays ?? []).map((day) =>
            taskLocalTime(`${day.date}T00:00:00`, cache.display.timeZone),
          ),
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
  const sourceColors = useMemo(() => boardSourceColors(tasks), [tasks]);
  const sourceColor = useCallback(
    (id: string) => sourceColors.get(id) ?? "var(--muted-foreground)",
    [sourceColors],
  );
  const nowOn = view.from <= now && view.to >= now;
  // Discrete window jumps ease from the previous window; drags and wheel pans follow the pointer.
  const easeFrom = useRef<TimeWindow | null>(null);
  const moveWindow = (next: TimeWindow, ease = false) => {
    if (ease) easeFrom.current = view;
    setView(clampMapWindow(next, oldest, now));
    setOpen(null);
    hoverStore.set({ hover: null });
  };
  const zoom = useCallback(
    (direction: number) => {
      setView((current) => {
        const next = stepMapWindow(current, direction, oldest, now);
        if (next !== current) easeFrom.current = current;
        return next;
      });
      setOpen(null);
      hoverStore.set({ hover: null });
    },
    [oldest, now, hoverStore],
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
  // The attention view keeps rows with something to look at; long time windows stay a history view.
  const attentionView = settings.scope === "attention" && !(mode === "time" && level === "segment");
  const worth = useMemo(
    () => new Set(tasks.filter((task) => routeWorthShowing(task, now, pins)).map(taskIdentity)),
    [tasks, now, pins],
  );
  const selectedGroups = useMemo(
    () =>
      groups
        .filter((group) => !focus.length || focus.includes(group.id))
        .map((group) => ({
          ...group,
          tasks:
            mode === "order"
              ? // Pins, and recent changes in the attention view, outrank the board window.
                pins.groups.includes(group.id)
                ? group.tasks
                : group.tasks.filter(
                    (task) =>
                      pins.tasks.includes(taskIdentity(task)) ||
                      (attentionView && worth.has(taskIdentity(task))) ||
                      ((task["omnigent.ai:sourceDisplay"]?.historyState?.active !== false ||
                        routeAttention(task)) &&
                        (routeState(task) !== "done" ||
                          !(task.progressUpdated ?? task["omnigent.ai:closedSeen"]) ||
                          Date.parse((task.progressUpdated ?? task["omnigent.ai:closedSeen"])!) >=
                            now - boardHours * HOUR)),
                  )
              : level === "segment"
                ? group.tasks
                : group.tasks.filter((task) =>
                    routeQueued(task)
                      ? nowOn && level === "station"
                      : (routeMoment(task, now) ?? 0) >= view.from &&
                        (routeMoment(task, now) ?? 0) <= view.to,
                  ),
        }))
        .filter((group) => group.tasks.length > 0),
    [groups, focus, mode, now, boardHours, level, nowOn, view, pins, attentionView, worth],
  );
  const important = useCallback(
    (task: BoardTask) => routeAttention(task) || (attentionView && worth.has(taskIdentity(task))),
    [worth, attentionView],
  );
  const groupWorth = useCallback(
    (group: MapGroup) =>
      pins.groups.includes(group.id) || group.tasks.some((task) => worth.has(taskIdentity(task))),
    [pins.groups, worth],
  );
  const isExpanded = useCallback(
    (source: string) =>
      expanded[source] ??
      allExpanded ??
      ((mode === "order" || level === "station") &&
        (attentionView
          ? selectedGroups.some((group) => group.source === source && groupWorth(group))
          : groups.some((group) => group.source === source && group.tasks.some(routeAttention)))),
    [expanded, allExpanded, mode, level, groups, attentionView, selectedGroups, groupWorth],
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
    let top = 0,
      folded = 0;
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
      const historical = lane.header
        ? groups.filter((group) => group.source === lane.source).flatMap((group) => group.tasks)
        : (groups.find((group) => group.id === lane.id)?.tasks ?? lane.tasks);
      lanes.push({
        ...lane,
        top,
        nodes: [],
        count: countTasks(lane.tasks, lane.collapsed ? lane.source : undefined),
        historyStart: Math.min(...historical.map((task) => routeMoment(task, now) ?? Infinity)),
      });
      top += lane.height;
      if (open?.lane === lane.id) top += panelHeight;
    };
    for (const source of sourceSlice) {
      const candidates = selectedGroups.filter((group) => group.source === source);
      const live = (group: MapGroup) =>
        attentionView
          ? groupWorth(group)
          : pins.groups.includes(group.id) ||
            group.tasks.some((task) => pins.tasks.includes(taskIdentity(task))) ||
            (mode === "order"
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
                  ));
      const active = candidates.filter(live),
        inactive = candidates.filter((group) => !live(group)),
        list = quiet[source] ? [...active, ...inactive] : active;
      const sourceTasks = candidates.flatMap((group) => group.tasks),
        opened = isExpanded(source);
      if (!sourceTasks.length && !caches[source]?.display.closedDays?.length) continue;
      if (attentionView && !active.length) {
        folded++;
        continue;
      }
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
        summary: (() => {
          const all = groups
            .filter((group) => group.source === source)
            .flatMap((group) => group.tasks)
            .filter((task) => task["omnigent.ai:sourceDisplay"]?.historyState?.active !== false);
          const counts = ["running", "user", "failed"].flatMap((state) => {
            const count = all.filter((task) => routeState(task) === state).length;
            return count ? [t(`routes.summary.${state}`, { count })] : [];
          });
          return counts.join(" · ") || t("routes.groupCount", { count: candidates.length });
        })(),
        header: true,
        collapsed: !opened,
        height: opened
          ? phone || compact
            ? 34
            : 40
          : phone
            ? 74
            : level === "segment" && mode === "time"
              ? compact
                ? 44
                : 54
              : compact
                ? 46
                : 58,
      });
      if (!opened) continue;
      const page = Math.min(groupPages[source] ?? 0, Math.max(0, Math.ceil(list.length / 10) - 1));
      for (const group of list.slice(page * 10, (page + 1) * 10))
        append({
          ...group,
          height:
            mode === "order"
              ? compact
                ? 80
                : 136
              : level === "station"
                ? phone
                  ? 150
                  : compact
                    ? 80
                    : 136
                : level === "phase"
                  ? phone
                    ? 96
                    : compact
                      ? 60
                      : 84
                  : phone
                    ? 66
                    : compact
                      ? 36
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
          total: list.length,
          page,
          pages: Math.ceil(list.length / 10),
          height: 40,
        });
    }
    const scene = mapScene(
      lanes,
      mode,
      view,
      width,
      now,
      level,
      relations,
      hubs,
      caches,
      (node) => {
        if (node.timeCluster) {
          const times = node.tasks.map((task) => routeMoment(task, now)!),
            first = Math.min(...times),
            last = Math.max(...times),
            hm = (time: number) =>
              formatDate(time, { hour: "2-digit", minute: "2-digit", hour12: false });
          const day = (time: number) => formatDate(time, { month: "2-digit", day: "2-digit" });
          if (compact) return { lines: [t("routes.stops", { count: node.tasks.length })], sub: "" };
          return {
            lines: [t("routes.stops", { count: node.tasks.length })],
            sub:
              level === "station"
                ? `${hm(first)}–${hm(last)}`
                : day(first) === day(last)
                  ? day(first)
                  : `${day(first)}–${day(last)}`,
          };
        }
        const task = node.tasks[0];
        if (compact)
          return {
            lines: [
              node.tasks.length > 1
                ? mode === "time" && node.queued
                  ? t("routes.queuedMore")
                  : nodeLabel(node)
                : clipLabel(nodeLabel(node), 150),
            ],
            sub: "",
          };
        return {
          lines:
            node.tasks.length > 1
              ? [mode === "time" && node.queued ? t("routes.queuedMore") : nodeLabel(node)]
              : routeLabelLines(nodeLabel(node)),
          sub:
            node.tasks.length === 1
              ? mode === "time" && routeState(task) === "user"
                ? t("sessions.taskState.user")
                : !routeQueued(task) &&
                    !["running", "user"].includes(routeState(task)) &&
                    routeMoment(task, now)
                  ? momentWord(routeMoment(task, now)!)
                  : ""
              : "",
        };
      },
      measureLabel,
      { compact, important },
    );
    return { ...scene, height: top + scene.grown, folded };
  }, [
    attentionView,
    groupWorth,
    pins,
    important,
    compact,
    selectedGroups,
    groups,
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
  // Flow dashes mark the segment a live session is on, capped like the final's busiest screen.
  const flows = useMemo(() => {
    const out: { id: string; lane: string; x1: number; x2: number; y: number }[] = [];
    for (const lane of model.lanes)
      lane.nodes.slice(1).forEach((node, index) => {
        const previous = lane.nodes[index],
          live = mode === "order" ? previous : node;
        if (
          out.length < maxFlows &&
          !node.density &&
          live.tasks.length === 1 &&
          routeState(live.tasks[0]) === "running"
        )
          out.push({ id: "flow" + node.id, lane: lane.id, x1: previous.x1, x2: node.x, y: node.y });
      });
    return out;
  }, [model, mode]);
  const hidden = usePageHidden(),
    marks = useStateMarks(tasks);
  const x = (time: number) =>
      gutter + ((time - view.from) / (view.to - view.from)) * (right - gutter),
    axis = mapAxisTicks(view, right - gutter),
    ticks = mode === "time" ? axis.ticks : [];
  const tickText = (date: Date, step: number) =>
    step < DAY
      ? date.getHours() === 0
        ? dayKey(+date) === dayKey(now)
          ? t("routes.today")
          : dayKey(+date) === dayKey(now - DAY)
            ? t("routes.yesterday")
            : dayKey(+date) === dayKey(now + DAY)
              ? t("routes.tomorrow")
              : dayKey(+date)
        : formatDate(date, { hour: "2-digit", minute: "2-digit", hour12: false })
      : step < 28 * DAY
        ? dayKey(+date)
        : date.getMonth() === 0
          ? t("routes.year", { year: date.getFullYear() })
          : `${String(date.getFullYear()).slice(2)}-${String(date.getMonth() + 1).padStart(2, "0")}`;
  const nowX = Math.max(40, Math.min(x(now), width - 46));
  const shading: { from: number; to: number }[] = [];
  if (mode === "time" && axis.step < 2 * DAY) {
    const day = new Date(view.from);
    day.setHours(0, 0, 0, 0);
    for (; +day < view.to; day.setDate(day.getDate() + 1)) {
      const number = Math.floor((+day - day.getTimezoneOffset() * 60000) / DAY);
      if (number % 2) continue;
      const end = new Date(day);
      end.setDate(end.getDate() + 1);
      const from = Math.max(gutter, x(+day)),
        to = Math.min(right, x(+end), nowOn ? x(now) : Infinity);
      if (to > from) shading.push({ from, to });
    }
  }
  const scheduledVisible =
    mode === "time" && level === "station" && nowOn
      ? future.filter((item) => item.at > now && item.at >= view.from && item.at <= view.to)
      : [];
  const scheduleGroups = scheduledVisible.reduce<(typeof scheduledVisible)[]>((out, item) => {
    const previous = out.at(-1);
    if (previous && x(item.at) - x(previous.at(-1)!.at) < 92) previous.push(item);
    else out.push([item]);
    return out;
  }, []);
  const scheduleLabels: MapNode[] = scheduleGroups.map((items) => ({
    id: items[0].id,
    lane: "scheduled",
    tasks: [],
    x: x(items[0].at),
    x1: Math.max(x(items.at(-1)!.at), x(items[0].at) + (items.length > 1 ? 2 : 0)),
    y: model.height + (phone ? 96 : 88) / 2 + (phone ? 8 : 7),
    top: model.height,
    height: phone ? 96 : 88,
    color: "var(--foreground)",
    queued: false,
    scheduled: true,
    lines: items.length > 1 ? [t("routes.items", { count: items.length })] : [items[0].label],
    sub:
      items.length > 1
        ? `${formatDate(items[0].at, { hour: "2-digit", minute: "2-digit", hour12: false })}–${formatDate(items.at(-1)!.at, { hour: "2-digit", minute: "2-digit", hour12: false })}`
        : momentWord(items[0].at),
  }));
  if (scheduleLabels.length)
    placeMapLabels(
      scheduleLabels,
      [
        { a: { x: x(now), y: 0 }, b: { x: x(now), y: 99999 }, half: 1 },
        {
          a: { x: x(now), y: scheduleLabels[0].y },
          b: { x: Math.max(...scheduleLabels.map((node) => node.x1)), y: scheduleLabels[0].y },
          half: 1.5,
        },
      ],
      [
        {
          id: "scheduled",
          source: "",
          name: "",
          badge: "",
          color: "",
          tasks: [],
          height: phone ? 96 : 88,
          top: model.height,
          nodes: [],
        },
      ],
      width,
      phone ? 4 : 168,
      measureLabel,
    );
  const openEdge = open?.kind === "edge" ? open.edge : null,
    openHub = open?.kind === "hub" ? taskIdentity(open.task) : null;
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
    if (!document.activeElement?.closest(".tb-dc"))
      detailTrigger.current = document.activeElement as HTMLElement;
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
        : { kind: "task", lane: group.id, anchor: taskIdentity(task), task },
    );
    hoverStore.set({ hover: null });
  };
  const selectPanelTask = (task: BoardTask) => {
    setOpen((current) =>
      current
        ? {
            kind: "task",
            lane: current.lane,
            anchor: current.kind === "task" ? current.anchor : taskIdentity(task),
            task,
          }
        : null,
    );
    hoverStore.set({ hover: null });
  };
  const closeDetail = () => {
    setOpen(null);
    detailTrigger.current?.focus();
  };
  const openNode = (node: MapNode) => {
    if (node.density) {
      const span = Math.max(MIN_SPAN, (node.density.to - node.density.from) * 2);
      moveWindow({ from: node.density.from - span / 4, to: node.density.from + span * 0.75 }, true);
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
      moveWindow({ from, to: Math.max(to, from + MIN_SPAN) }, true);
    } else {
      setOpen({ kind: "cluster", lane: node.lane, tasks: node.tasks });
      setPanelPage(0);
    }
    hoverStore.set({ hover: null });
  };
  const edgeIntent = useRef<ReturnType<typeof setTimeout>>(undefined),
    shownScene = useRef(model);
  useEffect(() => () => clearTimeout(edgeIntent.current), []);
  const showHover = (value: Omit<Hover, "x" | "y">, element: Element) => {
    const rect = element.getBoundingClientRect();
    hoverStore.set({ hover: { ...value, x: rect.left + rect.width / 2, y: rect.bottom + 8 } });
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
    const from = easeFrom.current;
    easeFrom.current = null;
    if (
      !from ||
      mode !== "time" ||
      globalThis.matchMedia?.("(prefers-reduced-motion: reduce)").matches
    )
      return;
    // Draw the new window where the old one stood, then let it settle (compositor-only transform).
    const scale = (view.to - view.from) / (from.to - from.from),
      shift =
        gutter +
        ((view.from - from.from) / (from.to - from.from)) * (right - gutter) -
        scale * gutter;
    if (scale < 1 / 8 || scale > 8 || Math.abs(shift) > 2 * width) return;
    for (const element of [svgRef.current, plotRef.current, axisRef.current])
      element?.animate?.(
        [{ transform: `translateX(${shift}px) scaleX(${scale})` }, { transform: "none" }],
        { duration: 260, easing: "cubic-bezier(.2, .7, .2, 1)" },
      );
  }, [view, mode, gutter, right, width]);
  // Connector hover dims the map through one class; the map itself never re-renders on hover.
  // A refreshed scene drops hover whose station, connector or hub is gone.
  useLayoutEffect(() => {
    shownScene.current = model;
    const { hover, hub } = hoverStore.get();
    if (
      hover &&
      !(hover.edge ? currentEdge(model.edges, hover.edge) : currentNode(model.nodes, hover.node))
    )
      hoverStore.set({ hover: null });
    if (hub && !currentHub(model.nodes, hub)) hoverStore.set({ hub: null });
    const sync = () =>
      svgRef.current?.classList.toggle(
        "rt-edge-hover",
        !!currentEdge(model.edges, hoverStore.get().hover?.edge),
      );
    sync();
    return hoverStore.subscribe(sync);
  }, [hoverStore, model]);
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
          hoverStore.set({ hover: null });
        }
      };
      element.addEventListener("wheel", wheel, { passive: false });
      cleanups.push(() => element.removeEventListener("wheel", wheel));
    }
    return () => cleanups.forEach((cleanup) => cleanup());
  }, [mode, width, oldest, now, zoom, hoverStore]);
  const pointerDown = (event: ReactPointerEvent<HTMLDivElement>, kind: "map" | "overview") => {
    if (
      mode !== "time" ||
      (kind === "map" && (event.target as Element).closest("button,a,[role=button]"))
    )
      return;
    const target = event.target as HTMLElement,
      handle = target.closest(".rt-overview-window")?.getBoundingClientRect();
    const edge =
      handle &&
      event.clientX > handle.left + handle.width * 0.25 &&
      event.clientX < handle.right - handle.width * 0.25
        ? "move"
        : (target.dataset.edge ?? "move");
    pointers.current.set(event.pointerId, { x: event.clientX, y: event.clientY });
    pointer.current = {
      kind,
      edge,
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
    if (key === "0") {
      easeFrom.current = view;
      setView(initialWindow(now));
      setOpen(null);
      return;
    }
    if (key === "+" || key === "-") zoom(key === "+" ? -1 : 1);
    else {
      const delta = (key === "ArrowLeft" ? -1 : 1) * (view.to - view.from) * 0.1;
      moveWindow({ from: view.from + delta, to: view.to + delta }, true);
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
      {t(
        quiet[lane.source]
          ? attentionView
            ? "routes.hideCalm"
            : "routes.hideQuiet"
          : attentionView
            ? "routes.showCalm"
            : "routes.showQuiet",
        { count: lane.quiet ?? 0 },
      )}
    </button>
  );
  const groupPager = (lane: MapLane) => (
    <BoardPager
      page={lane.page ?? 0}
      pages={lane.pages ?? 1}
      total={lane.total}
      onChange={(value) => {
        setGroupPages((current) => ({ ...current, [lane.source]: value }));
        setOpen(null);
      }}
    />
  );
  const panelLane = model.lanes.find((lane) => lane.id === open?.lane);
  const renderPanel = () =>
    open && panelLane ? (
      <div
        ref={panelRef}
        className="rt-panel"
        style={{
          top: panelLane.top + panelLane.height,
        }}
      >
        {open.kind === "task" ? (
          <TaskBoardDetail
            key={taskIdentity(open.task)}
            task={open.task}
            tasks={tasks}
            sessions={sessions}
            sourceCache={caches[open.task["omnigent.ai:source"]]}
            onSelect={selectPanelTask}
            onClose={closeDetail}
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
              <RelationFacts relations={open.edge.relations} onSelect={selectPanelTask} />
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
                                  <button type="button" onClick={() => selectPanelTask(task)}>
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
                            <button type="button" onClick={() => selectPanelTask(task)}>
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
    <div
      className={`tb-routes rt-routes${compact ? " rt-compact" : ""}${hidden ? " rt-paused" : ""}`}
      data-route-level={mode === "order" ? "order" : level}
    >
      <div className="rt-toolbar">
        <Segmented
          className="rt-segment"
          label={t("routes.mode")}
          value={mode}
          onChange={(value) => {
            onMode(value);
            setOpen(null);
            hoverStore.set({ hover: null, hub: null });
          }}
          options={(["order", "time"] as const).map((value) => ({
            value,
            label: (
              <>
                {value === "order" ? <RouteIcon size={15} /> : <ClockIcon size={15} />}
                {t(`routes.${value}`)}
              </>
            ),
          }))}
        />
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
                          true,
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
          <Segmented
            className="rt-segment"
            label={t("routes.scope")}
            value={settings.scope}
            onChange={(scope) => {
              updateSettings({ scope });
              setOpen(null);
            }}
            options={(["attention", "all"] as const).map((value) => ({
              value,
              label: t(`routes.scope.${value}`),
            }))}
          />
          <button
            type="button"
            className="rt-focus-toggle rt-pins-toggle"
            aria-pressed={pinsOpen}
            aria-label={t("routes.pinsTitle")}
            onClick={() => setPinsOpen(!pinsOpen)}
          >
            <PinIcon size={14} />
            {t("routes.add")}
            {pins.groups.length + pins.tasks.length > 0 && (
              <em>{pins.groups.length + pins.tasks.length}</em>
            )}
          </button>
          <button
            type="button"
            className="rt-focus-toggle rt-rows-toggle"
            aria-pressed={compact}
            aria-label={t(compact ? "routes.rowsFinal" : "routes.rowsCompact")}
            title={t(compact ? "routes.rowsFinal" : "routes.rowsCompact")}
            onClick={() => {
              updateSettings({ rows: compact ? "final" : "compact" });
              setOpen(null);
            }}
          >
            {compact ? <Rows3Icon size={14} /> : <Rows4Icon size={14} />}
            <span>{t("routes.rowsCompact")}</span>
          </button>
          <button
            type="button"
            className="rt-focus-toggle"
            aria-pressed={focusOpen}
            onClick={() => setFocusOpen(!focusOpen)}
          >
            {t("routes.focus")}
            {focus.length > 0 && <em>{focus.length}</em>}
          </button>
          <TooltipProvider>
            <Tooltip>
              <TooltipTrigger asChild>
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
                  {allExpanded ? (
                    <ChevronsDownUpIcon size={16} />
                  ) : (
                    <ChevronsUpDownIcon size={16} />
                  )}
                </button>
              </TooltipTrigger>
              <TooltipContent>
                {t(allExpanded ? "routes.collapseAll" : "routes.expandAll")}
              </TooltipContent>
            </Tooltip>
          </TooltipProvider>
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
      <div className="rt-legend rt-legend-always" aria-label={t("routes.legend")}>
        {["running", "user", "failed", "ready", "held", "queued"].map((state) => {
          const count = tasks.filter((task) => routeState(task) === state).length;
          return (
            <span key={state} className={count ? "" : "zero"}>
              <i className={`rt-symbol rt-${state}`} />
              {t(`sessions.taskState.${state}`)} <b>{count}</b>
            </span>
          );
        })}
        <span>
          <i className="rt-line-key done" />
          {t("visual.donePath")}
        </span>
        <span>
          <i className="rt-line-key queued" />
          {t("sessions.taskState.queued")}
        </span>
        <span>
          <i className="rt-line-key running" />
          {t("visual.runningPath")}
        </span>
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
          <p>{t("routes.attentionHelp")}</p>
          <p>{t("routes.gestureHelp")}</p>
        </section>
      )}
      {focusOpen && (
        <TaskBoardRouteFocus
          groups={groups}
          selected={focus}
          sourceName={sourceName}
          sourceColor={sourceColor}
          onClose={() => setFocusOpen(false)}
          onChange={(ids) => {
            onFocus(ids);
            setSourcePage(0);
            setOpen(null);
          }}
        />
      )}
      {pinsOpen && (
        <TaskBoardRoutePins
          groups={groups}
          pins={pins}
          sourceName={sourceName}
          onClose={() => setPinsOpen(false)}
          onChange={(next: RoutePins) => {
            updateSettings({ pins: next });
            setOpen(null);
          }}
        />
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
                y={32 - Math.max(2, Math.sqrt(bucket.count / overviewMax) * 30)}
                width={Math.max(1, width / 240 - 0.6)}
                height={bucket.count ? Math.max(2, Math.sqrt(bucket.count / overviewMax) * 30) : 0}
                rx="1"
              />
            ))}
            {mapAxisTicks({ from: oldest, to: now }, width, width < 500 ? 84 : 70)
              .ticks.filter(
                (date) =>
                  ((+date - oldest) / (now - oldest)) * width > 22 &&
                  ((+date - oldest) / (now - oldest)) * width < width - 22,
              )
              .map((date) => (
                <text key={+date} x={((+date - oldest) / (now - oldest)) * width} y="43">
                  {tickText(
                    date,
                    mapAxisTicks({ from: oldest, to: now }, width, width < 500 ? 84 : 70).step,
                  )}
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
            hoverStore.set({ hover: null, hub: null });
          }
          if (
            mode === "time" &&
            event.target === event.currentTarget &&
            ["ArrowLeft", "ArrowRight", "+", "-", "0"].includes(event.key)
          ) {
            event.preventDefault();
            mapKey(event.key);
          }
        }}
      >
        {mode === "time" && (
          <div ref={axisRef} className="rt-axis">
            {ticks
              .filter(
                (date) =>
                  x(+date) >= gutter + 4 &&
                  x(+date) <= right - 6 &&
                  (!nowOn || Math.abs(x(+date) - nowX) >= 66),
              )
              .map((date) => (
                <span
                  className={`${axis.step < DAY ? (date.getHours() === 0 ? "major" : "") : date.getDate() === 1 ? "major" : ""} ${+date > now ? "future" : ""}`}
                  key={+date}
                  style={{ left: x(+date) }}
                >
                  {tickText(date, axis.step)}
                </span>
              ))}
            {nowOn ? (
              <span className="rt-now-chip" style={{ left: nowX }}>
                {t("routes.now")}{" "}
                {formatDate(now, { hour: "2-digit", minute: "2-digit", hour12: false })}
              </span>
            ) : (
              <button
                className="rt-back-now"
                type="button"
                onClick={() => {
                  easeFrom.current = view;
                  setView(initialWindow(now));
                  setOpen(null);
                }}
              >
                {t("routes.backNow")}
              </button>
            )}
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
                    <i className="rt-bullet round">{band.badge}</i>
                    <span>
                      <b>{band.name}</b>
                      <small>
                        {t("board.tasks", { count: band.count ?? band.tasks.length })}
                        {" · "}
                        {band.summary}
                      </small>
                    </span>
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
                              <small>{t("board.tasks", { count: lane.tasks.length })}</small>
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
                                      <PhoneStation task={task} />
                                      <span>
                                        <b>{taskTitle(task)}</b>
                                        <small>
                                          {stateWord(task)}
                                          {!routeQueued(task) &&
                                          !["running", "user"].includes(routeState(task)) &&
                                          routeMoment(task, now)
                                            ? ` · ${momentWord(routeMoment(task, now)!)}`
                                            : ""}
                                          {!!relationCounts.get(taskIdentity(task))?.outgoing &&
                                            ` · ${t("routes.connectedTo", { count: relationCounts.get(taskIdentity(task))!.outgoing })}`}
                                          {!!relationCounts.get(taskIdentity(task))?.incoming &&
                                            ` · ${t("routes.connectedFrom", { count: relationCounts.get(taskIdentity(task))!.incoming })}`}
                                        </small>
                                      </span>
                                    </button>
                                    {open?.kind === "task" &&
                                      open.anchor === taskIdentity(task) && (
                                        <TaskBoardDetail
                                          key={taskIdentity(open.task)}
                                          task={open.task}
                                          tasks={tasks}
                                          sessions={sessions}
                                          sourceCache={caches[open.task["omnigent.ai:source"]]}
                                          onSelect={selectPanelTask}
                                          onClose={closeDetail}
                                        />
                                      )}
                                  </li>
                                </Fragment>
                              ))}
                            </ol>
                            <BoardPager
                              page={page}
                              pages={Math.ceil(sorted.length / 8)}
                              total={sorted.length}
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
            style={{ height: model.height + (scheduledVisible.length ? (phone ? 96 : 88) : 0) }}
          >
            <svg
              ref={svgRef}
              className={`rt-svg${openEdge ? " rt-edge-focus" : ""}${currentEdge(model.edges, hoverStore.get().hover?.edge) ? " rt-edge-hover" : ""}`}
              width={width}
              height={model.height + (scheduledVisible.length ? (phone ? 96 : 88) : 0)}
              aria-label={t("routes.map")}
            >
              {mode === "time" && nowOn && view.to - view.from <= 2 * DAY && (
                <rect
                  className="rt-future-bg"
                  x={x(now)}
                  y={0}
                  width={Math.max(0, right - x(now) + 14)}
                  height="100%"
                />
              )}
              {shading.map((band) => (
                <rect
                  key={band.from}
                  className="rt-day-band"
                  x={band.from}
                  width={band.to - band.from}
                  height="100%"
                />
              ))}
              {model.edges
                .filter((edge) => edge.long && !edge.hub)
                .map((edge) => (
                  <path
                    key={edge.id}
                    className="rt-connection rt-long"
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
                    className={`rt-grid${axis.step < DAY ? (date.getHours() === 0 ? " major" : "") : date.getDate() === 1 ? " major" : ""}`}
                  />
                ))}
              {mode === "time" && nowOn && (
                <line x1={x(now)} x2={x(now)} y1="0" y2="100%" className="rt-now" />
              )}
              {model.edges
                .filter((edge) => !edge.long && !edge.hub)
                .map((edge) => (
                  <g key={edge.id}>
                    <path d={edge.path} fill="none" stroke="var(--card-solid)" strokeWidth="9" />
                    <path
                      className="rt-connection"
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
                  {mode === "time" &&
                    lane.nodes.some((node) => !node.queued && !node.density) &&
                    (() => {
                      // Wrapped rows below carry only waiting stations; the rail runs on the first.
                      const past = lane.nodes.filter(
                        (node) => !node.queued && !node.density && node.top === lane.top,
                      );
                      const earliest = lane.historyStart ?? Infinity;
                      return (
                        <line
                          className="rt-rail"
                          x1={earliest < view.from ? gutter : past[0].x}
                          x2={past.at(-1)!.x1}
                          y1={past[0].y}
                          y2={past[0].y}
                          stroke={lane.color}
                          strokeWidth={lane.collapsed ? 7 : level === "station" ? 11 : 9}
                          strokeLinecap="round"
                        />
                      );
                    })()}
                  {lane.nodes
                    .slice(1)
                    .filter((node) => !node.density && (mode === "order" || node.queued))
                    .map((node) => (
                      <line
                        key={node.id}
                        className="rt-rail"
                        x1={(() => {
                          const previous = lane.nodes[lane.nodes.indexOf(node) - 1];
                          return previous?.top === node.top ? previous.x1 : node.x;
                        })()}
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
                  {lane.nodes.some((node) => node.density) && (
                    <line
                      x1={lane.nodes[0].x}
                      x2={lane.nodes.at(-1)!.x1}
                      y1={lane.nodes[0].y}
                      y2={lane.nodes[0].y}
                      stroke={lane.color}
                      strokeWidth={2.5}
                      opacity={0.4}
                    />
                  )}
                  {flows
                    .filter((flow) => flow.lane === lane.id)
                    .map((flow) => (
                      <line
                        key={flow.id}
                        className="rt-flow"
                        x1={flow.x1}
                        x2={flow.x2}
                        y1={flow.y}
                        y2={flow.y}
                        stroke="var(--card-solid)"
                        strokeWidth={2.5}
                        strokeDasharray="3 9"
                        strokeLinecap="round"
                      />
                    ))}
                </g>
              ))}
              {model.edges
                .filter((edge) => !edge.hub && edge.badge)
                .map((edge) => (
                  <g className="rt-edge-count" key={"count" + edge.id}>
                    <circle
                      cx={edge.badge!.x}
                      cy={edge.badge!.y}
                      r={9}
                      fill="var(--card-solid)"
                      stroke={edge.from.color}
                      strokeWidth={2}
                    />
                    <text x={edge.badge!.x} y={edge.badge!.y + 4} textAnchor="middle">
                      {edge.relations.length}
                    </text>
                  </g>
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
                    onMouseEnter={(event) => {
                      // Thin connectors cross the whole map: only a pointer that rests on one
                      // dims the rest, so sweeping across them does not repaint the map.
                      const target = event.currentTarget;
                      clearTimeout(edgeIntent.current);
                      edgeIntent.current = setTimeout(() => {
                        // A refresh meanwhile may have removed this connector or its relation.
                        const relation = edge.relations[0].id;
                        if (target.isConnected && currentEdge(shownScene.current.edges, relation))
                          showHover({ edge: relation }, target);
                      }, 120);
                    }}
                    onMouseLeave={() => {
                      clearTimeout(edgeIntent.current);
                      hoverStore.set({ hover: null });
                    }}
                    onFocus={(event) =>
                      showHover({ edge: edge.relations[0].id }, event.currentTarget)
                    }
                    onBlur={() => hoverStore.set({ hover: null })}
                    onClick={() => {
                      setOpen((current) =>
                        current?.kind === "edge" && current.edge.id === edge.id
                          ? null
                          : { kind: "edge", lane: edge.from.lane, edge },
                      );
                      hoverStore.set({ hover: null });
                    }}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        setOpen({ kind: "edge", lane: edge.from.lane, edge });
                        hoverStore.set({ hover: null });
                      }
                    }}
                  />
                ))}
            </svg>
            <RouteHighlights
              store={hoverStore}
              nodes={model.nodes}
              edges={model.edges}
              hubs={hubs}
              openEdge={openEdge && (currentEdge(model.edges, openEdge.relations[0]?.id) ?? null)}
              openHub={openHub}
              width={width}
              height={model.height + (scheduledVisible.length ? (phone ? 96 : 88) : 0)}
            />
            {model.lanes.map((lane) => (
              <div
                key={lane.id}
                className={`rt-lane${lane.header ? " rt-source" : ""}${lane.header && !lane.collapsed ? " thin" : ""}${lane.quiet ? " quiet" : ""}`}
                style={
                  {
                    ...badgeStyle(lane.color),
                    "--rt-band-color": sourceColor(lane.source),
                    top: lane.top,
                    height: lane.height,
                  } as CSSProperties
                }
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
                        <i className="rt-bullet round">{lane.badge}</i>
                        <span>
                          <b>{lane.name}</b>
                          <small>
                            <span>
                              {t("board.tasks", { count: lane.count ?? lane.tasks.length })}
                            </span>
                            <span className="rt-summary">
                              {lane.tasks.filter((task) => routeState(task) === "running").length >
                                0 && (
                                <em className="running">
                                  {t("sessions.taskState.running")}{" "}
                                  {
                                    lane.tasks.filter((task) => routeState(task) === "running")
                                      .length
                                  }
                                </em>
                              )}
                              {lane.tasks.filter((task) => routeState(task) === "user").length >
                                0 && (
                                <em className="user">
                                  {t("sessions.taskState.user")}{" "}
                                  {lane.tasks.filter((task) => routeState(task) === "user").length}
                                </em>
                              )}
                            </span>
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
            <div className="rt-plot" ref={plotRef}>
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
                      onMouseEnter={(event) =>
                        showHover({ node: nodeKey(node) }, event.currentTarget)
                      }
                      onMouseLeave={() => hoverStore.set({ hover: null })}
                      onClick={() => openNode(node)}
                    />
                  ) : (
                    <>
                      <button
                        type="button"
                        className={`rt-stop${node.tasks.some((task) => marks.changed.has(taskIdentity(task))) ? " rt-changed" : ""}${node.tasks.every((task) => marks.added.has(taskIdentity(task))) ? " rt-new" : ""}`}
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
                          showHover({ node: nodeKey(node) }, event.currentTarget);
                          if (
                            node.tasks.length === 1 &&
                            hubs.get(taskIdentity(node.tasks[0]))?.size
                          )
                            hoverStore.set({ hub: taskIdentity(node.tasks[0]) });
                        }}
                        onMouseLeave={() => {
                          hoverStore.set({ hover: null, hub: null });
                        }}
                        onFocus={(event) => showHover({ node: nodeKey(node) }, event.currentTarget)}
                        onBlur={() => hoverStore.set({ hover: null })}
                        onClick={() => openNode(node)}
                      >
                        <Station
                          tasks={node.tasks}
                          extent={node.x1 - node.x}
                          time={mode === "time"}
                        />
                      </button>
                      {node.label && (
                        <span
                          className={`rt-label${node.hub ? " hub" : ""} rt-label-${node.tasks.length === 1 ? routeState(node.tasks[0]) : "cluster"}`}
                          style={{ left: node.label.x, top: node.label.y, width: node.label.width }}
                        >
                          <b>
                            {node.lines?.map((line, index) => (
                              <Fragment key={line}>
                                {index > 0 && <br />}
                                {line}
                              </Fragment>
                            ))}
                          </b>
                          {node.sub && <small>{node.sub}</small>}
                        </span>
                      )}
                      {node.tasks.length === 1 &&
                        (hubs.get(taskIdentity(node.tasks[0]))?.size ?? 0) > 0 && (
                          <button
                            type="button"
                            className="rt-hub-badge"
                            style={{ left: node.x + 16, top: node.y - 25 }}
                            aria-label={t("routes.hubCount", {
                              count: hubs.get(taskIdentity(node.tasks[0]))!.size,
                            })}
                            onMouseEnter={() =>
                              hoverStore.set({ hub: taskIdentity(node.tasks[0]) })
                            }
                            onMouseLeave={() => hoverStore.set({ hub: null })}
                            onFocus={() => hoverStore.set({ hub: taskIdentity(node.tasks[0]) })}
                            onBlur={() => hoverStore.set({ hub: null })}
                            onClick={() => {
                              setOpen((current) =>
                                current?.kind === "hub" &&
                                taskIdentity(current.task) === taskIdentity(node.tasks[0])
                                  ? null
                                  : { kind: "hub", lane: node.lane, task: node.tasks[0] },
                              );
                              hoverStore.set({ hover: null });
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
            </div>
            {scheduledVisible.length > 0 && (
              <div className="rt-future" style={{ top: model.height, height: phone ? 96 : 88 }}>
                <div className="rt-lane-heading">
                  <i className="rt-bullet round">{"◆"}</i>
                  <span>
                    <b>{t("routes.scheduledTitle")}</b>
                    <small>{t("routes.items", { count: scheduledVisible.length })}</small>
                  </span>
                </div>
                <svg className="rt-svg" width={width} height={phone ? 96 : 88} aria-hidden="true">
                  <line
                    x1={x(now)}
                    x2={Math.max(...scheduledVisible.map((item) => x(item.at)))}
                    y1={(phone ? 96 : 88) / 2 + (phone ? 8 : 7)}
                    y2={(phone ? 96 : 88) / 2 + (phone ? 8 : 7)}
                    stroke="var(--muted-foreground)"
                    strokeWidth={2.5}
                  />
                </svg>
                {scheduleGroups.map((items) => {
                  const first = x(items[0].at),
                    last = Math.max(x(items.at(-1)!.at), first + (items.length > 1 ? 2 : 0)),
                    center = (first + last) / 2,
                    cy = (phone ? 96 : 88) / 2 + (phone ? 8 : 7);
                  return (
                    <Fragment key={items[0].id}>
                      <details className="rt-future-cluster" style={{ left: center, top: cy }}>
                        <summary
                          aria-label={
                            items.length > 1
                              ? t("routes.futureCount", { count: items.length })
                              : items[0].label
                          }
                        >
                          {items.length > 1 ? (
                            <span className="rt-capsule" style={{ width: last - first + 29 }}>
                              {items.length}
                            </span>
                          ) : (
                            <span className="rt-symbol rt-scheduled" />
                          )}
                        </summary>
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
                              ) : item.task ? (
                                <button
                                  type="button"
                                  data-route-task={item.task.uid}
                                  onClick={() => selectTask(item.task!)}
                                >
                                  {item.label}
                                </button>
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
                      {scheduleLabels.find((node) => node.id === items[0].id)?.label &&
                        (() => {
                          const node = scheduleLabels.find((label) => label.id === items[0].id)!;
                          return (
                            <span
                              className="rt-label"
                              style={{
                                left: node.label!.x,
                                top: node.label!.y - model.height,
                                width: node.label!.width,
                              }}
                            >
                              <b>
                                {node.lines!.map((line, index) => (
                                  <Fragment key={line}>
                                    {index > 0 && <br />}
                                    {line}
                                  </Fragment>
                                ))}
                              </b>
                              <small>{node.sub}</small>
                            </span>
                          );
                        })()}
                    </Fragment>
                  );
                })}
              </div>
            )}
            {renderPanel()}
          </div>
        )}
        {attentionView && model.folded > 0 && (
          <button
            type="button"
            className="rt-folded"
            onClick={() => {
              updateSettings({ scope: "all" });
              setOpen(null);
            }}
          >
            <ChevronDownIcon size={14} />
            {t("routes.foldedSources", { count: model.folded })}
          </button>
        )}
        {!model.lanes.length && !scheduledVisible.length && (
          <div className="tb-empty">
            {tasks.length ? (
              <p>{t(attentionView && model.folded ? "routes.attentionEmpty" : "search.empty")}</p>
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
        total={sourceIds.length}
        onChange={(value) => {
          setSourcePage(value);
          setOpen(null);
        }}
      />
      {!phone && (
        <RouteTooltip
          store={hoverStore}
          nodes={model.nodes}
          edges={model.edges}
          tasks={tasks}
          onSelect={selectTask}
        />
      )}
    </div>
  );
}

type HoverStore = ReturnType<typeof createHoverStore>;
/** Pointer hover lives outside React state so hovering re-renders only the tooltip and highlights. */
function createHoverStore() {
  let state: { hover: Hover | null; hub: string | null } = { hover: null, hub: null };
  const listeners = new Set<() => void>();
  return {
    get: () => state,
    subscribe(listener: () => void) {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    set(patch: Partial<typeof state>) {
      const next = { ...state, ...patch };
      if (next.hover === state.hover && next.hub === state.hub) return;
      state = next;
      listeners.forEach((listener) => listener());
    },
  };
}
const useHover = (store: HoverStore) => useSyncExternalStore(store.subscribe, store.get);
/** Active connector, hub fan-out and their endpoint rings, drawn over the map. */
function RouteHighlights({
  store,
  nodes,
  edges,
  hubs,
  openEdge,
  openHub,
  width,
  height,
}: {
  store: HoverStore;
  nodes: MapNode[];
  edges: MapEdge[];
  hubs: Map<string, Set<string>>;
  openEdge: MapEdge | null;
  openHub: string | null;
  width: number;
  height: number;
}) {
  const { hover, hub } = useHover(store);
  const activeEdge = currentEdge(edges, hover?.edge) ?? openEdge,
    activeHub = (currentHub(nodes, hub) ? hub : null) ?? openHub;
  if (!activeEdge && !activeHub) return null;
  const ringed = activeEdge
    ? nodes.filter((node) => node.id === activeEdge.from.id || node.id === activeEdge.to.id)
    : nodes.filter((node) =>
        node.tasks.some((task) => hubs.get(activeHub!)?.has(taskIdentity(task))),
      );
  return (
    <svg className="rt-svg rt-highlights" width={width} height={height} aria-hidden="true">
      {activeEdge && (
        <path
          className={`rt-connection${activeEdge.long ? " rt-long" : ""} active`}
          d={activeEdge.path}
          strokeDasharray={
            activeEdge.relations.every((relation) => relation.kind === "dependency")
              ? "10 8"
              : undefined
          }
        />
      )}
      {!activeEdge &&
        edges
          .filter(
            (edge) => edge.hub && edge.from.tasks.some((task) => taskIdentity(task) === activeHub),
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
      {ringed.map((node) => (
        <ellipse
          key={node.id}
          cx={(node.x + node.x1) / 2}
          cy={node.y}
          rx={(node.x1 - node.x) / 2 + 21}
          ry="21"
          className="rt-endpoint"
        />
      ))}
    </svg>
  );
}
function RouteTooltip({
  store,
  nodes,
  edges,
  tasks,
  onSelect,
}: {
  store: HoverStore;
  nodes: MapNode[];
  edges: MapEdge[];
  tasks: BoardTask[];
  onSelect: (task: BoardTask) => void;
}) {
  const { hover } = useHover(store);
  const edge = currentEdge(edges, hover?.edge),
    node = edge ? undefined : currentNode(nodes, hover?.node);
  const ref = useRef<HTMLDivElement>(null),
    [size, setSize] = useState({ width: 340, height: 240 });
  useLayoutEffect(() => {
    const rect = ref.current?.getBoundingClientRect();
    if (rect) setSize({ width: rect.width, height: rect.height });
  }, [hover, edge, node]);
  if (!hover || !(edge || node)) return null;
  return (
    <div
      ref={ref}
      role="tooltip"
      className="tb-session-hover rt-tooltip"
      style={{
        left: Math.max(8, Math.min(globalThis.innerWidth - size.width - 8, hover.x + 12)),
        top: Math.max(8, Math.min(globalThis.innerHeight - size.height - 8, hover.y)),
      }}
    >
      {edge ? (
        <>
          <b>
            {t(edge.relations.length > 1 ? "routes.relationCount" : "routes.relationTitle", {
              count: edge.relations.length,
            })}
          </b>
          <RelationFacts relations={edge.relations.slice(0, 4)} onSelect={onSelect} />
        </>
      ) : node?.density ? (
        <>
          <b>{t("routes.stops", { count: node.density.count })}</b>
          <p>
            {momentWord(node.density.from)}
            {" — "}
            {momentWord(node.density.to)}
          </p>
        </>
      ) : node?.tasks.length === 1 ? (
        <TaskBoardTaskHover task={node.tasks[0]} tasks={tasks} />
      ) : (
        <>
          <b>{t("routes.stops", { count: node?.tasks.length ?? 0 })}</b>
          <ul>
            {node?.tasks.slice(0, 6).map((task) => (
              <li key={taskIdentity(task)}>{taskTitle(task)}</li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}

export function BoardPager({
  page,
  pages,
  onChange,
  total,
  formatValue = String,
}: {
  page: number;
  pages: number;
  onChange: (page: number) => void;
  total?: number;
  formatValue?: (value: number) => string;
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
      <span>{t("pager.page", { page: formatValue(page + 1), pages: formatValue(pages) })}</span>
      <Button
        size="icon-sm"
        variant="ghost"
        disabled={page + 1 >= pages}
        onClick={() => onChange(page + 1)}
        aria-label={t("pager.next")}
      >
        <ChevronRightIcon className="size-4" />
      </Button>
      {total !== undefined && (
        <small>{t("routes.pagerTotal", { count: formatValue(total) })}</small>
      )}
    </nav>
  );
}
