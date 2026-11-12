import { useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import {
  ChevronDownIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  ClockIcon,
  FocusIcon,
  RouteIcon,
  RotateCcwIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Link } from "@/lib/routing";
import type { BoardSchedule } from "./taskBoardNative";
import { taskIdentity, taskProgress, type BoardTask, type TaskSource } from "./taskBoardData";
import {
  changeWindow,
  DAY,
  detailLevel,
  HOUR,
  initialWindow,
  MIN_SPAN,
  routeLayout,
  routeRows,
  routeTicks,
  parentRelationCounts,
  taskRelations,
  taskTime,
  type RouteEdge,
  type RouteMode,
  type RouteNode,
  type TimeWindow,
} from "./taskBoardRouteData";
import { TaskBoardSearch } from "./TaskBoardSearch";
import { taskTitle } from "./TaskBoardDetail";
import { TaskProgressIcon } from "./TaskProgressIcon";
import { formatDate, formatNumber, t } from "./taskBoardText";

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
}) {
  const [window, setWindow] = useState(initialWindow);
  const [width, setWidth] = useState(900);
  const [closed, setClosed] = useState<string[]>([]);
  const [page, setPage] = useState(0);
  const [groupPages, setGroupPages] = useState<Record<string, number>>({});
  const [phonePages, setPhonePages] = useState<Record<string, number>>({});
  const [hover, setHover] = useState<string | null>(null);
  const [hub, setHub] = useState<string | null>(null);
  const [clusterIds, setCluster] = useState<string[] | null>(null);
  const [clusterPage, setClusterPage] = useState(0);
  const [edgeKey, setEdgeKey] = useState<string | null>(null);
  const [hoverEdgeKey, setHoverEdgeKey] = useState<string | null>(null);
  const container = useRef<HTMLDivElement>(null);
  const drag = useRef<{ x: number; from: number; to: number } | null>(null);
  const touches = useRef(new Map<number, { x: number; y: number }>());
  const pinch = useRef<{ distance: number; window: TimeWindow } | null>(null);
  const now = Date.now();
  const allRows = useMemo(() => routeRows(tasks, sources), [tasks, sources]);
  const sourceName = (id: string) =>
    aliases[id] || sources.find((source) => source.id === id)?.name || id;
  const shown = allRows.filter((row) => !focus.length || focus.includes(row.id));
  const sourceIds = [...new Set(shown.map((row) => row.source))];
  const pages = Math.max(1, Math.ceil(sourceIds.length / 12)),
    currentPage = Math.min(page, pages - 1);
  const visibleSources = new Set(sourceIds.slice(currentPage * 12, (currentPage + 1) * 12));
  const rows = shown.filter((row) => {
    if (!visibleSources.has(row.source)) return false;
    if (closed.includes(row.source)) return true;
    const siblings = shown.filter((other) => other.source === row.source);
    const groupPage = Math.min(
      groupPages[row.source] ?? 0,
      Math.max(0, Math.ceil(siblings.length / 10) - 1),
    );
    const position = siblings.findIndex((other) => other.id === row.id);
    return position >= groupPage * 10 && position < (groupPage + 1) * 10;
  });
  const mapped = rows.flatMap((row, index) => {
    if (!closed.includes(row.source)) return [row];
    if (index && rows[index - 1].source === row.source) return [];
    return [
      {
        ...row,
        id: `source:${row.source}`,
        name: sourceName(row.source),
        tasks: rows.filter((other) => other.source === row.source).flatMap((other) => other.tasks),
      },
    ];
  });
  const label = (node: RouteNode) =>
    node.tasks.length > 1
      ? t("routes.stops", { count: node.tasks.length })
      : taskTitle(node.tasks[0]).slice(0, 23) + (taskTitle(node.tasks[0]).length > 23 ? "…" : "");
  const layout = routeLayout(mapped, mode, window, width, now, label);
  const clusterTasks = tasks.filter((task) => clusterIds?.includes(taskIdentity(task)));
  const currentClusterPage = Math.min(
    clusterPage,
    Math.max(0, Math.ceil(clusterTasks.length / 12) - 1),
  );
  const phonePage = (id: string, count: number) =>
    Math.min(phonePages[id] ?? 0, Math.max(0, Math.ceil(count / 12) - 1));
  const relationKey = (edge: RouteEdge) =>
    JSON.stringify(
      edge.relations.map((relation) => [
        taskIdentity(relation.from),
        relation.to ? taskIdentity(relation.to) : relation.uid,
        relation.kind,
      ]),
    );
  const activeEdge = layout.edges.find((edge) => relationKey(edge) === (edgeKey ?? hoverEdgeKey));
  const edgeLabel = (edge: RouteEdge) =>
    edge.relations
      .map((relation) =>
        t("routes.edge", {
          from: taskTitle(relation.from),
          to: relation.to ? taskTitle(relation.to) : relation.uid,
          kind: t(`relation.${relation.kind}`),
        }),
      )
      .join("; ");
  const ticks = mode === "time" ? routeTicks(window, width - (width < 600 ? 28 : 210)) : [];
  const relations = useMemo(() => taskRelations(tasks), [tasks]);
  const parentCounts = useMemo(() => parentRelationCounts(tasks, relations), [tasks, relations]);
  const childCount = (node: RouteNode) =>
    node.tasks.reduce((count, task) => count + (parentCounts.get(task) ?? 0), 0);
  const nodeActive = (node: RouteNode) => hub === node.id || hover === node.id;
  const showEdge = (edge: (typeof layout.edges)[number]) => !edge.hub || nodeActive(edge.from);
  const litChildren = new Set([
    ...layout.edges
      .filter((edge) => edge.hub && nodeActive(edge.from))
      .flatMap((edge) => edge.targets?.map((node) => node.id) ?? [edge.to.id]),
    ...(activeEdge
      ? [activeEdge.from.id, ...(activeEdge.targets ?? [activeEdge.to]).map((node) => node.id)]
      : []),
  ]);
  const openNode = (node: RouteNode) => {
    if (node.tasks.length === 1) {
      onSelect(node.tasks[0]);
      if (childCount(node) >= 8) setHub((current) => (current === node.id ? null : node.id));
      return;
    }
    if (mode === "time" && window.to - window.from > MIN_SPAN + 1) {
      const from = Math.min(...node.tasks.map((task) => taskTime(task, now)));
      const to = Math.max(...node.tasks.map((task) => taskTime(task, now)));
      const span = Math.max(MIN_SPAN, to - from + 80 * 60_000);
      setWindow({ from: from - 40 * 60_000, to: from - 40 * 60_000 + span });
    } else {
      setCluster(node.tasks.map(taskIdentity));
      setClusterPage(0);
    }
  };
  const hasRows = mapped.length > 0;
  useEffect(() => {
    const element = container.current;
    if (!element) return;
    const measure = () => setWidth(element.clientWidth || 900);
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [hasRows]);
  useEffect(() => {
    const element = container.current;
    if (!element || mode !== "time") return;
    const wheel = (event: WheelEvent) => {
      event.preventDefault();
      setWindow((current) =>
        changeWindow(current, event.deltaX || event.deltaY, event.ctrlKey || event.metaKey),
      );
    };
    element.addEventListener("wheel", wheel, { passive: false });
    return () => element.removeEventListener("wheel", wheel);
  }, [mode, hasRows]);
  const gutter = width < 600 ? 14 : 190;
  const x = (time: number) =>
    gutter + ((time - window.from) / (window.to - window.from)) * (width - gutter - 28);
  const future =
    mode === "time"
      ? scheduled.filter(
          (task) =>
            task.state === "active" &&
            task.next_run_at &&
            Date.parse(task.next_run_at) >= window.from &&
            Date.parse(task.next_run_at) <= window.to,
        )
      : [];
  const graphHeight = layout.height + (future.length ? 90 : 0);
  const reset = () => {
    setWindow(initialWindow());
    setHub(null);
    setCluster(null);
    setEdgeKey(null);
    setHoverEdgeKey(null);
  };
  return (
    <div className="tb-routes">
      <div className="tb-route-toolbar">
        <div className="tb-segment" aria-label={t("routes.mode")}>
          <Button
            size="sm"
            variant={mode === "order" ? "secondary" : "ghost"}
            aria-pressed={mode === "order"}
            onClick={() => {
              onMode("order");
              setCluster(null);
            }}
          >
            <RouteIcon className="size-4" />
            {t("routes.order")}
          </Button>
          <Button
            size="sm"
            variant={mode === "time" ? "secondary" : "ghost"}
            aria-pressed={mode === "time"}
            onClick={() => {
              onMode("time");
              setCluster(null);
            }}
          >
            <ClockIcon className="size-4" />
            {t("routes.time")}
          </Button>
        </div>
        <div className="flex items-center gap-2">
          <TaskBoardSearch
            label={t("routes.focus")}
            options={allRows.map((row) => ({
              id: row.id,
              label: row.name || t("group.default"),
              hint: sourceName(row.source),
            }))}
            selected={focus}
            onChange={(ids) => {
              onFocus(ids);
              setPage(0);
            }}
          />
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label={t("routes.reset")}
            onClick={() => {
              onFocus([]);
              setClosed([]);
              setPage(0);
              reset();
            }}
          >
            <RotateCcwIcon className="size-4" />
          </Button>
        </div>
      </div>
      {focus.length > 0 && (
        <p className="mb-3 flex items-center gap-2 text-xs text-muted-foreground">
          <FocusIcon className="size-3.5" />
          {t("routes.focused", { count: focus.length })}
        </p>
      )}
      {mode === "time" && (
        <div className="tb-time-controls">
          {[12, 24, 72, 168, 720, 2160, 8760].map((hours) => (
            <Button
              key={hours}
              size="sm"
              variant="ghost"
              onClick={() =>
                setWindow({
                  from: now - (hours === 12 ? 6 : hours) * HOUR,
                  to: now + (hours === 12 ? 6 : 24) * HOUR,
                })
              }
            >
              {t(hours < 48 ? "routes.hours" : "routes.days", {
                count: hours < 48 ? hours : hours / 24,
              })}
            </Button>
          ))}
          <Button size="sm" variant="ghost" onClick={reset}>
            {t("routes.now")}
          </Button>
          <span className="ml-auto text-xs text-muted-foreground">
            {t(`routes.${detailLevel(window.to - window.from)}`)}
          </span>
        </div>
      )}
      {!tasks.length ? (
        <div className="tb-empty">
          <h2>{t("board.empty")}</h2>
          <p>{t("board.connect")}</p>
          <code className="mt-3 block break-all text-xs">{t("board.endpoint")}</code>
          <a href="https://www.rfc-editor.org/rfc/rfc8984.html" target="_blank" rel="noreferrer">
            {t("sources.docs")}
          </a>
        </div>
      ) : !mapped.length ? (
        <p className="tb-empty">{t("search.empty")}</p>
      ) : (
        <>
          <div
            ref={container}
            className={`tb-map tb-map-${mode}`}
            tabIndex={0}
            role="region"
            aria-label={t("view.routes")}
            onKeyDown={(event) => {
              if (mode !== "time" || event.target !== event.currentTarget) return;
              if (["ArrowLeft", "ArrowRight", "+", "-"].includes(event.key)) {
                event.preventDefault();
                setWindow((current) =>
                  changeWindow(
                    current,
                    event.key === "ArrowLeft"
                      ? -120
                      : event.key === "ArrowRight"
                        ? 120
                        : event.key === "+"
                          ? -120
                          : 120,
                    ["+", "-"].includes(event.key),
                  ),
                );
              }
            }}
            onPointerDown={(event) => {
              if (mode !== "time" || (event.target as Element).closest("button,[role=button],a"))
                return;
              drag.current = { x: event.clientX, ...window };
              touches.current.set(event.pointerId, { x: event.clientX, y: event.clientY });
              if (touches.current.size === 2) {
                const [a, b] = [...touches.current.values()];
                pinch.current = { distance: Math.hypot(b.x - a.x, b.y - a.y), window };
              }
              event.currentTarget.setPointerCapture(event.pointerId);
            }}
            onPointerMove={(event) => {
              if (touches.current.has(event.pointerId))
                touches.current.set(event.pointerId, { x: event.clientX, y: event.clientY });
              if (touches.current.size === 2 && pinch.current) {
                const [a, b] = [...touches.current.values()],
                  distance = Math.hypot(b.x - a.x, b.y - a.y);
                const start = pinch.current;
                const span = Math.max(
                  MIN_SPAN,
                  ((start.window.to - start.window.from) * start.distance) / Math.max(1, distance),
                );
                setWindow({ from: start.window.to - span, to: start.window.to });
                return;
              }
              if (!drag.current) return;
              const start = drag.current;
              setWindow(
                changeWindow(
                  { from: start.from, to: start.to },
                  ((start.x - event.clientX) * 800) / width,
                  false,
                ),
              );
            }}
            onPointerUp={(event) => {
              touches.current.delete(event.pointerId);
              pinch.current = null;
              drag.current = null;
            }}
            onPointerCancel={(event) => {
              touches.current.delete(event.pointerId);
              pinch.current = null;
              drag.current = null;
            }}
          >
            {mode === "time" && (
              <div className="tb-axis" aria-hidden="true">
                {ticks.map((date) => (
                  <span key={+date} style={{ left: x(+date) }}>
                    {formatDate(date, {
                      month: "short",
                      day: "numeric",
                      ...(window.to - window.from < 3 * DAY ? { hour: "numeric" as const } : {}),
                    })}
                  </span>
                ))}
              </div>
            )}
            <div className="tb-route-graph" style={{ height: graphHeight }}>
              <svg
                width={width}
                height={graphHeight}
                className="tb-map-svg"
                aria-label={t("routes.map")}
              >
                <g className="tb-long-edges">
                  {layout.edges
                    .filter((edge) => edge.long && showEdge(edge))
                    .map((edge) => (
                      <path
                        key={relationKey(edge)}
                        d={edge.path}
                        className={
                          nodeActive(edge.from) || nodeActive(edge.to) || activeEdge === edge
                            ? "tb-edge-active"
                            : ""
                        }
                        stroke={`var(--tb-line-${edge.from.row.color})`}
                        strokeWidth="1.2"
                        opacity=".2"
                        fill="none"
                        strokeDasharray={edge.kind === "dependency" ? "6 5" : undefined}
                      />
                    ))}
                </g>
                {mode === "time" && (
                  <g className="tb-grid" aria-hidden="true">
                    {ticks.map((date) => (
                      <line key={+date} x1={x(+date)} x2={x(+date)} y1="0" y2={layout.height} />
                    ))}
                    {now >= window.from && now <= window.to && (
                      <line
                        x1={x(now)}
                        x2={x(now)}
                        y1="0"
                        y2={layout.height}
                        className="tb-now-line"
                      />
                    )}
                  </g>
                )}
                <g className="tb-edges">
                  {layout.edges
                    .filter((edge) => !edge.long && showEdge(edge))
                    .map((edge) => (
                      <path
                        key={relationKey(edge)}
                        d={edge.path}
                        stroke={`var(--tb-line-${edge.from.row.color})`}
                        strokeWidth={edge.kind === "parent" ? 2 : 3}
                        strokeDasharray={edge.kind === "dependency" ? "6 5" : undefined}
                        fill="none"
                      />
                    ))}
                </g>
                <g className="tb-rails">
                  {layout.rails.map((rail) => (
                    <line
                      key={rail.row.id}
                      x1={rail.x}
                      x2={rail.x1}
                      y1={rail.y}
                      y2={rail.y}
                      stroke={`var(--tb-line-${rail.row.color})`}
                      strokeWidth="7"
                      strokeLinecap="round"
                      opacity={
                        detailLevel(window.to - window.from) === "segment" && mode === "time"
                          ? 0.4
                          : 1
                      }
                    />
                  ))}
                </g>
                {layout.edges
                  .filter((edge) => edge.hub && showEdge(edge))
                  .map((edge) => (
                    <g key={JSON.stringify([edge.from.id, edge.to.row.id])}>
                      <rect
                        x={(edge.from.x + edge.to.x) / 2 - 12}
                        y={(edge.from.y + edge.to.y) / 2 - 10}
                        width="24"
                        height="20"
                        rx="7"
                        fill="var(--background)"
                        stroke={`var(--tb-line-${edge.from.row.color})`}
                      />
                      <text
                        x={(edge.from.x + edge.to.x) / 2}
                        y={(edge.from.y + edge.to.y) / 2 + 4}
                        textAnchor="middle"
                        fontSize="12"
                        fill="var(--foreground)"
                      >
                        {edge.targets?.length ?? 1}
                      </text>
                    </g>
                  ))}
                <g className="tb-edge-hits">
                  {layout.edges.filter(showEdge).map((edge) => (
                    <path
                      key={relationKey(edge)}
                      d={edge.path}
                      fill="none"
                      stroke="transparent"
                      strokeWidth="16"
                      pointerEvents="stroke"
                      role="button"
                      tabIndex={0}
                      aria-label={edgeLabel(edge)}
                      onMouseEnter={() => setHoverEdgeKey(relationKey(edge))}
                      onMouseLeave={() => setHoverEdgeKey(null)}
                      onFocus={() => setHoverEdgeKey(relationKey(edge))}
                      onBlur={() => setHoverEdgeKey(null)}
                      onClick={() =>
                        setEdgeKey((current) =>
                          current === relationKey(edge) ? null : relationKey(edge),
                        )
                      }
                      onKeyDown={(event) => {
                        if (event.key === "Enter" || event.key === " ") {
                          event.preventDefault();
                          setEdgeKey((current) =>
                            current === relationKey(edge) ? null : relationKey(edge),
                          );
                        }
                      }}
                    >
                      <title>{edgeLabel(edge)}</title>
                    </path>
                  ))}
                </g>
                {layout.nodes.map((node) => (
                  <g
                    key={node.id}
                    role="button"
                    tabIndex={0}
                    aria-label={
                      node.tasks.length > 1
                        ? t("routes.openCluster", { count: node.tasks.length })
                        : taskTitle(node.tasks[0])
                    }
                    className="tb-station"
                    onClick={() => openNode(node)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        openNode(node);
                      }
                    }}
                    onMouseEnter={() => setHover(node.id)}
                    onMouseLeave={() => setHover(null)}
                    onFocus={() => setHover(node.id)}
                    onBlur={() => setHover(null)}
                  >
                    <title>
                      {node.tasks.length > 1
                        ? node.tasks.slice(0, 6).map(taskTitle).join("\n")
                        : [
                            taskTitle(node.tasks[0]),
                            node.tasks[0]["omnigent.ai:sourceState"],
                            node.tasks[0]["omnigent.ai:next"],
                          ]
                            .filter(Boolean)
                            .join("\n")}
                    </title>
                    {node.density ? (
                      <rect
                        x={node.x}
                        y={node.y - Math.min(28, 8 + Math.sqrt(node.tasks.length) * 4)}
                        width={Math.max(3, node.x1 - node.x - 2)}
                        height={Math.min(56, 16 + Math.sqrt(node.tasks.length) * 8)}
                        rx="3"
                        fill={`var(--tb-line-${node.row.color})`}
                        opacity=".7"
                      />
                    ) : (
                      <>
                        <rect
                          x={node.x - 16}
                          y={node.y - 16}
                          width={Math.max(32, node.x1 - node.x + 32)}
                          height="32"
                          rx="16"
                          fill="var(--background)"
                          stroke={`var(--tb-line-${node.row.color})`}
                          strokeWidth={litChildren.has(node.id) ? 5 : 3}
                        />
                        {node.tasks.length > 1 ? (
                          <text
                            x={(node.x + node.x1) / 2}
                            y={node.y + 5}
                            textAnchor="middle"
                            fill="var(--foreground)"
                            fontSize="13"
                            fontWeight="700"
                          >
                            {formatNumber(node.tasks.length)}
                          </text>
                        ) : (
                          <foreignObject x={node.x - 8} y={node.y - 8} width="16" height="16">
                            <TaskProgressIcon
                              progress={taskProgress(node.tasks[0])}
                              waiting={!!node.tasks[0]["omnigent.ai:waiting"]}
                            />
                          </foreignObject>
                        )}
                        {childCount(node) >= 8 && (
                          <g>
                            <rect
                              x={node.x + 12}
                              y={node.y - 31}
                              width="28"
                              height="20"
                              rx="7"
                              fill="var(--muted)"
                            />
                            <text
                              x={node.x + 26}
                              y={node.y - 17}
                              textAnchor="middle"
                              fontSize="11"
                              fill="var(--foreground)"
                            >
                              {childCount(node)}
                            </text>
                          </g>
                        )}
                      </>
                    )}
                  </g>
                ))}
                {future.slice(0, 12).map((task) => (
                  <g key={task.id}>
                    <path
                      d={`M${x(Date.parse(task.next_run_at!))} ${layout.height + 35}l8 8-8 8-8-8z`}
                      fill="var(--background)"
                      stroke="var(--foreground)"
                      strokeWidth="2"
                    />
                    <foreignObject
                      x={Math.max(
                        gutter + 4,
                        Math.min(width - 150, x(Date.parse(task.next_run_at!)) - 64),
                      )}
                      y={layout.height + 56}
                      width="140"
                      height="30"
                    >
                      <Link
                        to={`/tasks/${encodeURIComponent(task.id)}`}
                        className="block truncate text-xs underline"
                        title={task.name}
                      >
                        {task.name}
                      </Link>
                    </foreignObject>
                  </g>
                ))}
              </svg>
              {!!future.length && (
                <div className="tb-lane-heading" style={{ top: layout.height, height: 90 }}>
                  <h3>{t("schedule.title")}</h3>
                  <span>{t("board.tasks", { count: future.length })}</span>
                </div>
              )}
              {layout.headers.map((header) => (
                <div className="tb-source-heading" key={header.source} style={{ top: header.y }}>
                  <button
                    type="button"
                    onClick={() =>
                      setClosed((current) =>
                        current.includes(header.source)
                          ? current.filter((id) => id !== header.source)
                          : [...current, header.source],
                      )
                    }
                    aria-expanded={!closed.includes(header.source)}
                  >
                    <ChevronDownIcon
                      className={`size-3.5 ${closed.includes(header.source) ? "-rotate-90" : ""}`}
                    />
                    <b>{sourceName(header.source)}</b>
                    <span>
                      {t("board.tasks", {
                        count: tasks.filter((task) => task["omnigent.ai:source"] === header.source)
                          .length,
                      })}
                    </span>
                  </button>
                </div>
              ))}
              {layout.rails.map((rail) => (
                <div
                  key={rail.row.id}
                  className="tb-lane-heading"
                  style={
                    {
                      top: rail.y - layout.rowHeight / 2,
                      height: layout.rowHeight,
                      "--tb-line": `var(--tb-line-${rail.row.color})`,
                    } as CSSProperties
                  }
                >
                  <h3>{rail.row.name || t("group.default")}</h3>
                  <span>{t("board.tasks", { count: rail.row.tasks.length })}</span>
                </div>
              ))}
              {layout.nodes
                .filter((node) => node.label)
                .map((node) => (
                  <button
                    type="button"
                    key={node.id}
                    className="tb-map-label"
                    style={{
                      left: node.label!.x,
                      top: node.label!.y,
                      width: node.label!.w,
                      height: node.label!.h,
                    }}
                    onClick={() => openNode(node)}
                    title={node.tasks.length === 1 ? taskTitle(node.tasks[0]) : undefined}
                  >
                    {node.labelText}
                  </button>
                ))}
            </div>
            <div className="tb-route-phone">
              {mapped.map((row) => (
                <section key={row.id} aria-label={sourceName(row.source)}>
                  <h2>{sourceName(row.source)}</h2>
                  <h3>{row.name || t("group.default")}</h3>
                  <ol>
                    {row.tasks
                      .slice(
                        phonePage(row.id, row.tasks.length) * 12,
                        (phonePage(row.id, row.tasks.length) + 1) * 12,
                      )
                      .map((task) => (
                        <li key={taskIdentity(task)}>
                          <button type="button" onClick={() => onSelect(task)}>
                            <TaskProgressIcon
                              progress={taskProgress(task)}
                              waiting={!!task["omnigent.ai:waiting"]}
                            />
                            <span>
                              {taskTitle(task)}
                              {task["omnigent.ai:key"] && <small>{task["omnigent.ai:key"]}</small>}
                            </span>
                          </button>
                          {relations
                            .filter(
                              (edge) => edge.to && taskIdentity(edge.from) === taskIdentity(task),
                            )
                            .map((edge) => (
                              <button
                                type="button"
                                key={JSON.stringify([taskIdentity(edge.from), edge.uid, edge.kind])}
                                className="tb-phone-relation"
                                onClick={() => onSelect(edge.to!)}
                              >
                                {t(`relation.${edge.kind}`)}
                                <ChevronRightIcon className="size-3" />
                                {taskTitle(edge.to!)}
                              </button>
                            ))}
                        </li>
                      ))}
                  </ol>
                  <BoardPager
                    page={phonePage(row.id, row.tasks.length)}
                    pages={Math.ceil(row.tasks.length / 12)}
                    onChange={(value) =>
                      setPhonePages((current) => ({ ...current, [row.id]: value }))
                    }
                  />
                </section>
              ))}
            </div>
          </div>
          {activeEdge && edgeKey && (
            <section className="tb-cluster" aria-label={t("detail.relations")}>
              <header>
                <b>{t(`relation.${activeEdge.kind}`)}</b>
                <Button size="sm" variant="ghost" onClick={() => setEdgeKey(null)}>
                  {t("detail.close")}
                </Button>
              </header>
              <ul>
                {[
                  ...new Map(
                    activeEdge.relations.flatMap((relation) =>
                      [relation.from, ...(relation.to ? [relation.to] : [])].map(
                        (task) => [taskIdentity(task), task] as const,
                      ),
                    ),
                  ).values(),
                ].map((task) => (
                  <li key={taskIdentity(task)}>
                    <button type="button" onClick={() => onSelect(task)}>
                      <TaskProgressIcon progress={taskProgress(task)} />
                      <span>{taskTitle(task)}</span>
                    </button>
                  </li>
                ))}
              </ul>
            </section>
          )}
          {clusterTasks.length > 0 && (
            <section className="tb-cluster" aria-label={t("routes.clusterTitle")}>
              <header>
                <b>{t("routes.stops", { count: clusterTasks.length })}</b>
                <Button size="sm" variant="ghost" onClick={() => setCluster(null)}>
                  {t("detail.close")}
                </Button>
              </header>
              <ul>
                {clusterTasks
                  .slice(currentClusterPage * 12, (currentClusterPage + 1) * 12)
                  .map((task) => (
                    <li key={taskIdentity(task)}>
                      <button type="button" onClick={() => onSelect(task)}>
                        <TaskProgressIcon progress={taskProgress(task)} />
                        <span>{taskTitle(task)}</span>
                      </button>
                    </li>
                  ))}
              </ul>
              <BoardPager
                page={currentClusterPage}
                pages={Math.ceil(clusterTasks.length / 12)}
                onChange={setClusterPage}
              />
            </section>
          )}
          {[...visibleSources].map((source) => {
            const siblings = shown.filter((row) => row.source === source);
            return !closed.includes(source) && siblings.length > 10 ? (
              <section key={source} className="tb-group-pager">
                <span>{sourceName(source)}</span>
                <BoardPager
                  page={Math.min(groupPages[source] ?? 0, Math.ceil(siblings.length / 10) - 1)}
                  pages={Math.ceil(siblings.length / 10)}
                  onChange={(value) =>
                    setGroupPages((current) => ({ ...current, [source]: value }))
                  }
                />
              </section>
            ) : null;
          })}
          <BoardPager page={currentPage} pages={pages} onChange={setPage} />
        </>
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
