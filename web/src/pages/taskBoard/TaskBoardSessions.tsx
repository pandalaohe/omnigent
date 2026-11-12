import {
  Fragment,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
  type ReactNode,
} from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  ActivityIcon,
  ArrowLeftRightIcon,
  ArrowUpRightIcon,
  BookOpenIcon,
  ChevronDownIcon,
  CircleCheckIcon,
  CodeXmlIcon,
  BugIcon,
  EyeIcon,
  HardDriveDownloadIcon,
  DatabaseBackupIcon,
  DraftingCompassIcon,
  FilePlusIcon,
  FileTextIcon,
  FlagTriangleRightIcon,
  FlaskConicalIcon,
  GitMergeIcon,
  HandIcon,
  HistoryIcon,
  ListChecksIcon,
  LogInIcon,
  LogOutIcon,
  NetworkIcon,
  QuoteIcon,
  RocketIcon,
  SearchIcon,
  SquareTerminalIcon,
  XIcon,
} from "lucide-react";
import { boardExitProps } from "@/lib/boardFrame";
import { Link } from "@/lib/routing";
import { BoardIdentityMark } from "./BoardIdentityMark";
import { TaskBoardDetail, taskTitle } from "./TaskBoardDetail";
import { TaskBoardTaskHover } from "./TaskBoardTaskHover";
import { BoardPager } from "./TaskBoardRoutes";
import "./taskBoardSessions.css";
import { taskIdentity, type BoardTask } from "./taskBoardData";
import { sessionObservation, type DisplaySession } from "./taskBoardDisplay";
import { useBoardSessionDetails, type BoardSession } from "./taskBoardNative";
import {
  archivedToday,
  boardSessionRows,
  recentSessionFacts,
  sessionGroups,
  type SessionRow,
} from "./taskBoardSessionData";
import type { SourceCache } from "./taskBoardSnapshotData";
import { formatDate, formatNumber, t } from "./taskBoardText";

const icons: Record<string, typeof ActivityIcon> = {
  read: BookOpenIcon,
  plan: ListChecksIcon,
  network: NetworkIcon,
  search: SearchIcon,
  design: DraftingCompassIcon,
  code: CodeXmlIcon,
  debug: BugIcon,
  review: EyeIcon,
  report: FileTextIcon,
  test: FlaskConicalIcon,
  test2: FlaskConicalIcon,
  merge: GitMergeIcon,
  ship: RocketIcon,
  install: HardDriveDownloadIcon,
  you: HandIcon,
  you2: HandIcon,
  backup: DatabaseBackupIcon,
  backup2: DatabaseBackupIcon,
};
const eventIcons: Record<string, typeof ActivityIcon> = {
  open: FilePlusIcon,
  start: LogInIcon,
  transfer: ArrowLeftRightIcon,
  stop: LogOutIcon,
  outcome: CircleCheckIcon,
  check: FlaskConicalIcon,
  other: ActivityIcon,
};
const clock = (value: string) =>
  formatDate(Date.parse(value), { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
function duration(seconds?: number) {
  if (seconds == null || !Number.isFinite(seconds)) return t("sessions.unknown");
  const minutes = Math.max(1, Math.round(seconds / 60)),
    hours = Math.floor(seconds / 3600);
  return hours < 1
    ? `${minutes}m`
    : hours < 24
      ? `${hours}h${Math.round((seconds % 3600) / 60) ? ` ${Math.round((seconds % 3600) / 60)}m` : ""}`
      : `${Math.floor(hours / 24)}d ${hours % 24}h`;
}
function SessionCost({ session }: { session: DisplaySession }) {
  const cost = session.cost;
  if (!cost) return null;
  return (
    <span
      className={`tb-session-money ${cost.kind}`}
      title={[t(`sessions.cost.${cost.kind}.hint`), cost.sourceLabel].filter(Boolean).join(" · ")}
    >
      {cost.amount == null
        ? "—"
        : `$${formatNumber(cost.amount, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`}
      {cost.amount != null && <i>{t(`sessions.cost.${cost.kind}`)}</i>}
    </span>
  );
}
function SessionHover({ session, asOf }: { session: DisplaySession; asOf: string }) {
  const observation = sessionObservation(session, asOf);
  const tool = session.tool,
    liveTool =
      tool &&
      Date.parse(asOf) >= Date.parse(tool.startedAt) &&
      (tool.limitSeconds == null ||
        Date.parse(asOf) - Date.parse(tool.startedAt) < tool.limitSeconds * 1000);
  return (
    <>
      <div className="tb-session-hover-meta">
        <span>
          {[session.agentId || t("sessions.unknown"), session.model].filter(Boolean).join(" · ")}
        </span>
        <span>
          {session.status === "archived"
            ? t("sessions.archiveStamp", {
                time: session.archivedAt
                  ? formatDate(Date.parse(session.archivedAt), {
                      dateStyle: "medium",
                      timeStyle: "short",
                    })
                  : t("sessions.unknown"),
              })
            : `${t(`sessions.state.${observation.state}`)} · ${duration(
                observation.state === "running"
                  ? observation.runningSeconds
                  : observation.idleSeconds,
              )}`}
        </span>
      </div>
      {liveTool && (
        <p>
          <SquareTerminalIcon size={13} />
          {`${tool.label} (${tool.name}) · `}
          {duration((Date.parse(asOf) - Date.parse(tool.startedAt)) / 1000)}
          {tool.limitSeconds != null && ` / ${duration(tool.limitSeconds)}`}
        </p>
      )}
      {session.next && (
        <p>
          <FlagTriangleRightIcon size={13} />
          {session.next}
        </p>
      )}
      {session.context != null && (
        <div className="tb-session-context">
          <span>{t("sessions.context")}</span>
          <i>
            <b
              className={session.context > 0.85 ? "hot" : undefined}
              style={{ width: `${Math.max(0, Math.min(100, session.context * 100))}%` }}
            />
          </i>
          <em>{`${formatNumber(session.context * 100)}%`}</em>
        </div>
      )}
    </>
  );
}
function Steps({
  session,
  onHover,
}: {
  session: DisplaySession;
  onHover: (content: ReactNode) => void;
}) {
  const steps = session.steps ?? [];
  if (!steps.length) return null;
  const current = steps.findIndex((step) => step.state === "current"),
    compact = steps.length > 8;
  const focus = current < 0 ? steps.length - 1 : current;
  const position = (index: number) => (steps.length === 1 ? 0 : (index / (steps.length - 1)) * 100);
  const done = current < 0 ? 100 : position(current);
  return (
    <div
      className={`tb-session-track ${current >= 0 || compact ? "has-current" : ""}`}
      aria-label={t("sessions.steps")}
    >
      <i className="tb-session-track-base" />
      <i className="tb-session-track-done" style={{ width: `${done}%` }} />
      {steps.flatMap((step, index) => {
        if (compact && index !== focus) return [];
        const state = compact ? "current" : step.state,
          Icon = icons[step.kind] ?? BookOpenIcon;
        const tooltip = (
          <>
            <b>{step.label}</b>
            <small>
              {t("sessions.stepPosition", { index: index + 1, count: steps.length })}
              {" · "}
              {t(`sessions.step.${step.state}`)}
            </small>
            {step.state === "current" && <p>{session.now}</p>}
          </>
        );
        return [
          <button
            type="button"
            key={step.id}
            aria-label={t("sessions.stepLabel", {
              label: step.label,
              index: index + 1,
              count: steps.length,
              state: t(`sessions.step.${step.state}`),
            })}
            className={`tb-session-station ${state} ${step.kind.startsWith("you") ? "waiting" : ""}`}
            style={{ left: `${position(index)}%` }}
            onMouseEnter={(event) => {
              event.stopPropagation();
              onHover(tooltip);
            }}
            onFocus={() => onHover(tooltip)}
            onClick={(event) => {
              event.stopPropagation();
              onHover(tooltip);
            }}
          >
            <Icon size={state === "current" ? 15 : 13} />
            {state === "current" && (
              <em>
                {step.label}
                {compact ? ` · ${index + 1}/${steps.length}` : ""}
              </em>
            )}
          </button>,
        ];
      })}
    </div>
  );
}

function SessionLine({
  row,
  kind,
  expanded,
  onToggle,
  open,
  onOpen,
  asOf,
  onHover,
  onHoverOff,
  onPointer,
}: {
  row: SessionRow;
  kind: "root" | "child" | "archived";
  expanded?: boolean;
  onToggle?: () => void;
  open: boolean;
  onOpen: () => void;
  asOf: string;
  onHover: (content: ReactNode) => void;
  onHoverOff: () => void;
  onPointer: (x: number, y: number) => void;
}) {
  const { session, task, native } = row,
    observation = sessionObservation(session, asOf);
  const tool = session.tool,
    toolLive =
      tool &&
      observation.state === "running" &&
      Date.parse(asOf) >= Date.parse(tool.startedAt) &&
      (tool.limitSeconds == null ||
        Date.parse(asOf) - Date.parse(tool.startedAt) < tool.limitSeconds * 1000);
  const tag =
    observation.state === "waiting"
      ? t("session.waiting")
      : observation.state === "stale"
        ? t("sessions.staleFor", { duration: duration(observation.idleSeconds) })
        : toolLive
          ? t("sessions.toolFor", { duration: duration(observation.runningSeconds) })
          : "";
  const hoverContent = <SessionHover session={session} asOf={asOf} />;
  return (
    <div
      className={`tb-session-line ${kind} state-${observation.state} ${open ? "is-open" : ""}`}
      style={{ "--session-color": row.color } as CSSProperties}
      role="button"
      tabIndex={0}
      aria-expanded={open}
      aria-label={t("sessions.details", { title: session.label })}
      data-session-id={session.id}
      onClick={onOpen}
      onKeyDown={(event) => {
        if (event.target !== event.currentTarget) return;
        if (["Enter", " "].includes(event.key)) {
          event.preventDefault();
          onOpen();
        }
        if (event.key === "Escape") {
          onHoverOff();
          if (open) onOpen();
        }
      }}
      onMouseMove={(event) => onPointer(event.clientX, event.clientY)}
      onMouseEnter={(event) => {
        onPointer(event.clientX, event.clientY);
        onHover(hoverContent);
      }}
      onMouseLeave={onHoverOff}
      onFocus={(event) => {
        if (event.target !== event.currentTarget) return;
        const rect = event.currentTarget.getBoundingClientRect();
        onPointer(rect.left, rect.bottom);
        onHover(hoverContent);
      }}
      onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget)) onHoverOff();
      }}
    >
      <div className="tb-session-who">
        {kind === "root" && onToggle && (
          <button
            type="button"
            className="tb-session-fold"
            aria-expanded={expanded}
            aria-label={t(expanded ? "sessions.collapse" : "sessions.expand", {
              title: session.label,
            })}
            onClick={(event) => {
              event.stopPropagation();
              onToggle();
            }}
          >
            <ChevronDownIcon
              size={15}
              style={{ transform: expanded ? undefined : "rotate(-90deg)" }}
            />
          </button>
        )}
        <BoardIdentityMark
          id={session.agentId}
          product={session.agentId}
          label={session.agentId || session.label}
          neutral
          size={kind === "root" ? 34 : kind === "archived" ? 26 : 30}
        />
      </div>
      <div className="tb-session-title">
        <b>{session.label}</b>
        <small>
          {kind !== "archived" && task && (
            <i
              className={`tb-session-bullet ${(task["omnigent.ai:sourceDisplay"]?.identity?.groupBadge?.length ?? 0) > 1 ? "code" : ""}`}
              style={{ background: row.color }}
            >
              {task["omnigent.ai:sourceDisplay"]?.identity?.groupBadge ||
                task["omnigent.ai:group"]?.name?.slice(0, 1) ||
                "·"}
            </i>
          )}
          <span>
            {[
              task?.["omnigent.ai:sourceDisplay"]?.identity?.key ?? task?.["omnigent.ai:key"],
              kind === "archived" && session.archivedAt
                ? t("sessions.archivedAt", { time: clock(session.archivedAt) })
                : session.host,
            ]
              .filter(Boolean)
              .join(" · ")}
          </span>
          {tag && kind !== "archived" && <u className={observation.state}>{tag}</u>}
        </small>
      </div>
      {kind !== "archived" && (
        <div className="tb-session-steps">
          <Steps session={session} onHover={onHover} />
        </div>
      )}
      <div className="tb-session-now">
        {kind === "archived"
          ? session.last
          : session.need && observation.state === "waiting"
            ? session.need
            : session.questions?.length
              ? t("sessions.questions", { count: session.questions.length })
              : session.now}
      </div>
      <div className="tb-session-cost">
        <SessionCost session={session} />
      </div>
      <div className="tb-session-go">
        {native && (
          <Link
            {...boardExitProps()}
            to={`/c/${encodeURIComponent(session.id)}`}
            aria-label={t("sessions.open", { title: session.label })}
            onClick={(event) => event.stopPropagation()}
          >
            <ArrowUpRightIcon size={14} />
          </Link>
        )}
      </div>
    </div>
  );
}

export function TaskBoardSessions({
  sessions,
  tasks,
  sourceCaches,
  onSelect,
  onView,
  inlineDetail,
}: {
  inlineDetail?: ReactNode;
  sessions: BoardSession[];
  tasks: BoardTask[];
  sourceCaches?: Record<string, SourceCache>;
  onSelect: (task: BoardTask) => void;
  onView: (view: "log" | "today") => void;
}) {
  const [page, setPage] = useState(0),
    [closed, setClosed] = useState<string[]>([]),
    [childPages, setChildPages] = useState<Record<string, number>>({});
  const [archiveOpen, setArchiveOpen] = useState(false),
    [archivePage, setArchivePage] = useState(0),
    [selected, setSelected] = useState<string | null>(null);
  const [relatedTask, setRelatedTask] = useState<BoardTask | null>(null);
  const [hover, setHover] = useState<ReactNode>(null),
    [pointer, setPointer] = useState({ x: 0, y: 0 });
  const [hoverEvent, setHoverEvent] = useState<string | null>(null);
  const [recentDetail, setRecentDetail] = useState<string | null>(null);
  const tooltipId = useId();
  const tooltip = useRef<HTMLDivElement>(null);
  const [hoverSize, setHoverSize] = useState({ width: 330, height: 180 });
  useLayoutEffect(() => {
    if (!hover || !tooltip.current) return;
    const { width, height } = tooltip.current.getBoundingClientRect();
    setHoverSize((old) => (old.width === width && old.height === height ? old : { width, height }));
  }, [hover]);
  const rowElements = useRef(new Map<string, HTMLDivElement>());
  const client = useQueryClient();
  const now = Date.now(),
    asOf = new Date(now).toISOString();
  const hydrated = sessions.map((session) => {
    const detail = client.getQueryData<BoardSession>(["taskBoard", "session", session.id]);
    return detail && (detail.updated_at ?? 0) >= (session.updated_at ?? 0)
      ? { ...session, ...detail }
      : session;
  });
  const rows = boardSessionRows(tasks, hydrated, sourceCaches);
  const groups = sessionGroups(rows, asOf),
    current = Math.min(page, Math.max(0, Math.ceil(groups.length / 6) - 1));
  const groupsOnPage = groups.slice(current * 6, (current + 1) * 6);
  const childPage = (group: (typeof groups)[number]) =>
    Math.min(
      childPages[group.root.session.id] ?? 0,
      Math.max(0, Math.ceil(group.children.length / 8) - 1),
    );
  const archived = archivedToday(rows, now),
    lastArchive = Math.max(0, Math.ceil(archived.length / 10) - 1),
    archiveCurrent = Math.min(archivePage, lastArchive);
  const visibleIds = groupsOnPage
    .flatMap((group) => [
      group.root,
      ...(closed.includes(group.root.session.id)
        ? []
        : group.children
            .slice(childPage(group) * 8, childPage(group) * 8 + 8)
            .map((child) => child.row)),
    ])
    .concat(archiveOpen ? archived.slice(archiveCurrent * 10, archiveCurrent * 10 + 10) : [])
    .flatMap((row) => (row.native ? [row.session.id] : []));
  // Keep cached observations subscribed when a transition moves a row off-page.
  const details = useBoardSessionDetails(
    sessions.map((session) => session.id),
    visibleIds,
  );
  const restore = () => {
    const id = selected;
    setSelected(null);
    setRelatedTask(null);
    if (id) rowElements.current.get(id)?.querySelector<HTMLElement>('[role="button"]')?.focus();
  };
  const renderRow = (row: SessionRow, kind: "root" | "child" | "archived", children?: number) => {
    const id = row.session.id,
      open = selected === id;
    return (
      <Fragment key={id}>
        <div
          ref={(element) => {
            if (element) rowElements.current.set(id, element);
            else rowElements.current.delete(id);
          }}
        >
          <SessionLine
            row={row}
            kind={kind}
            expanded={!closed.includes(id)}
            onToggle={
              kind === "root" && children
                ? () =>
                    setClosed((value) =>
                      value.includes(id) ? value.filter((key) => key !== id) : [...value, id],
                    )
                : undefined
            }
            open={open}
            onOpen={() => {
              setSelected(open ? null : id);
              setRelatedTask(null);
              setHover(null);
            }}
            asOf={row.asOf ?? asOf}
            onHover={(content) => {
              setHoverEvent(null);
              setHover(content);
            }}
            onHoverOff={() => setHover(null)}
            onPointer={(x, y) => setPointer({ x, y })}
          />
        </div>
        {open &&
          (row.task ? (
            <TaskBoardDetail
              key={taskIdentity(relatedTask ?? row.task)}
              task={relatedTask ?? row.task}
              selectedSessionId={relatedTask ? undefined : row.session.id}
              tasks={tasks}
              sessions={hydrated}
              sourceCache={sourceCaches?.[(relatedTask ?? row.task)["omnigent.ai:source"]]}
              onSelect={setRelatedTask}
              onClose={restore}
            />
          ) : (
            <section
              className="tb-session-detail"
              aria-label={t("sessions.details", { title: row.session.label })}
            >
              <button type="button" onClick={restore} aria-label={t("detail.close")}>
                <XIcon size={15} />
              </button>
              <h3>{row.session.title}</h3>
              <SessionHover session={row.session} asOf={row.asOf ?? asOf} />
              <p>{t("sessions.noTask")}</p>
            </section>
          ))}
      </Fragment>
    );
  };
  const recent = recentSessionFacts(tasks);
  return (
    <section className="tb-sessions" aria-label={t("view.sessions")}>
      {!rows.some((row) => row.session.status !== "archived") && (
        <p className="tb-empty">{t("sessions.empty")}</p>
      )}
      {groupsOnPage.map((group) => (
        <div className="tb-session-group" key={group.root.session.id}>
          {renderRow(group.root, "root", group.children.length)}
          {!closed.includes(group.root.session.id) && (
            <div className="tb-session-children">
              {group.children
                .slice(childPage(group) * 8, childPage(group) * 8 + 8)
                .map(({ row }) => renderRow(row, "child"))}
              <BoardPager
                page={childPage(group)}
                pages={Math.max(1, Math.ceil(group.children.length / 8))}
                onChange={(value) =>
                  setChildPages({ ...childPages, [group.root.session.id]: value })
                }
              />
            </div>
          )}
        </div>
      ))}
      <BoardPager
        page={current}
        pages={Math.max(1, Math.ceil(groups.length / 6))}
        onChange={(value) => {
          setPage(value);
          setSelected(null);
          setHover(null);
        }}
      />
      <button
        type="button"
        className="tb-session-archive"
        aria-expanded={archiveOpen}
        onClick={() => setArchiveOpen(!archiveOpen)}
      >
        <ChevronDownIcon
          size={15}
          style={{ transform: archiveOpen ? undefined : "rotate(-90deg)" }}
        />
        {t("sessions.archivedToday", { count: archived.length })}
      </button>
      {archiveOpen && (
        <div className="tb-session-archived">
          {archived
            .slice(archiveCurrent * 10, archiveCurrent * 10 + 10)
            .map((row) => renderRow(row, "archived"))}
          <BoardPager page={archiveCurrent} pages={lastArchive + 1} onChange={setArchivePage} />
        </div>
      )}
      {details.some((detail) => detail.isError) && (
        <p role="status">{t("sessions.detailsFailed")}</p>
      )}
      <div className="tb-session-recent">
        <section>
          <h4>
            <HistoryIcon size={14} />
            {t("sessions.recent")}
            <button type="button" onClick={() => onView("log")}>
              {t("sessions.allLog")}
            </button>
          </h4>
          {recent.events.map(({ event, task }) => {
            const Icon = eventIcons[event.kind] ?? ActivityIcon;
            const eventKey = JSON.stringify([taskIdentity(task), event.id]);
            const taskHover = <TaskBoardTaskHover task={task} tasks={tasks} />;
            const check =
              event.kind === "check"
                ? task["omnigent.ai:sourceDisplay"]?.checks?.find(
                    (item) => item.id === event.checkId,
                  )
                : undefined;
            return (
              <Fragment key={eventKey}>
                <button
                  type="button"
                  className="tb-recent-event"
                  aria-describedby={hover && hoverEvent === eventKey ? tooltipId : undefined}
                  onMouseEnter={(pointerEvent) => {
                    setHoverEvent(eventKey);
                    setPointer({ x: pointerEvent.clientX, y: pointerEvent.clientY });
                    setHover(taskHover);
                  }}
                  onMouseMove={(pointerEvent) =>
                    setPointer({ x: pointerEvent.clientX, y: pointerEvent.clientY })
                  }
                  onMouseLeave={() => setHover(null)}
                  onFocus={(focusEvent) => {
                    setHoverEvent(eventKey);
                    const rect = focusEvent.currentTarget.getBoundingClientRect();
                    setPointer({ x: rect.left, y: rect.top });
                    setHover(taskHover);
                  }}
                  onBlur={() => setHover(null)}
                  onKeyDown={(keyEvent) => {
                    if (keyEvent.key === "Escape") setHover(null);
                  }}
                  onClick={() => {
                    setHover(null);
                    setRecentDetail(eventKey);
                    onSelect(task);
                  }}
                >
                  <time dateTime={event.at}>{clock(event.at)}</time>
                  <BoardIdentityMark
                    id={event.agentId ?? event.host ?? ""}
                    product={event.agentId}
                    label={event.agentId ?? event.host ?? "?"}
                    neutral
                    size={24}
                  />
                  <i className={`tb-recent-kind ${event.kind} ${check?.verdict ?? ""}`}>
                    <Icon size={12} />
                  </i>
                  <span>
                    <b>{task["omnigent.ai:sourceDisplay"]?.identity?.label || taskTitle(task)}</b>{" "}
                    <small className={check?.verdict}>
                      {check
                        ? `${check.host} ${t(`sessions.check.${check.verdict}`)}`
                        : event.kind === "check"
                          ? event.label || t("sessions.check.unknown")
                          : event.label || t(`sessions.event.${event.kind}`)}
                    </small>
                  </span>
                  <em>
                    {check
                      ? t("sessions.checkCount", { count: check.count })
                      : event.workSeconds
                        ? duration(event.workSeconds)
                        : ""}
                  </em>
                </button>
                {recentDetail === eventKey && inlineDetail}
              </Fragment>
            );
          })}
        </section>
        <section>
          <h4>
            <QuoteIcon size={14} />
            {t("sessions.decisions")}
            <button type="button" onClick={() => onView("today")}>
              {t("sessions.seeToday")}
            </button>
          </h4>
          {recent.quotes.map(({ quote, task }) => (
            <Fragment key={JSON.stringify([taskIdentity(task), quote.id])}>
              <button
                type="button"
                className="tb-recent-quote"
                onClick={() => {
                  setRecentDetail(JSON.stringify([taskIdentity(task), quote.id]));
                  onSelect(task);
                }}
                title={quote.date || quote.at}
              >
                <blockquote>
                  {"「"}
                  {quote.text.slice(0, 58)}
                  {quote.text.length > 58 ? "…」" : "」"}
                  <cite>
                    {task["omnigent.ai:sourceDisplay"]?.identity?.label || taskTitle(task)}
                    {" · "}
                    {task["omnigent.ai:sourceDisplay"]?.identity?.key ||
                      task["omnigent.ai:key"] ||
                      task.uid}
                  </cite>
                </blockquote>
              </button>
              {recentDetail === JSON.stringify([taskIdentity(task), quote.id]) && inlineDetail}
            </Fragment>
          ))}
        </section>
      </div>
      {hover && (
        <div
          className="tb-session-hover"
          id={tooltipId}
          ref={tooltip}
          role="tooltip"
          style={{
            left:
              pointer.x + 18 + hoverSize.width > window.innerWidth - 10
                ? Math.max(10, pointer.x - hoverSize.width - 18)
                : pointer.x + 18,
            top: Math.max(10, Math.min(pointer.y + 14, window.innerHeight - hoverSize.height - 10)),
          }}
        >
          {hover}
        </div>
      )}
    </section>
  );
}
