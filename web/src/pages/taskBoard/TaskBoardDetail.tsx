import { routeState } from "./taskBoardRouteModel";
import { useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  XIcon,
  ArrowUpRightIcon,
  HandIcon,
  ActivityIcon,
  CircleCheckIcon,
  PackageCheckIcon,
  CircleXIcon,
  BanIcon,
  ClockIcon,
  CircleDotIcon,
  SquareTerminalIcon,
} from "lucide-react";
import { Link } from "@/lib/routing";
import { useBoardCadence } from "./taskBoardPreferences";
import type { SourceCache } from "./taskBoardSnapshotData";
import {
  readBoardJson,
  safeSourceLink,
  taskIdentity,
  taskProgress,
  type BoardTask,
} from "./taskBoardData";
import { useBoardSessionDetails, type BoardSession } from "./taskBoardNative";
import { taskLocalTime, taskRelations } from "./taskBoardRouteData";
import { displayCost, sessionObservation, type TaskDisplay } from "./taskBoardDisplay";
import { boardSessionRows, boardGroupColors } from "./taskBoardSessionData";
import { formatDate, formatNumber, t } from "./taskBoardText";
import {
  activityAgentName,
  activityName,
  activityTaskName,
  activityTaskKey,
} from "./taskBoardPresentation";
import { BoardIdentityMark, BoardAgentLogo } from "./BoardIdentityMark";
import { TaskSourceDetails } from "./TaskSourceDetails";
import { TaskBoardFoldText } from "./TaskBoardFoldText";
import { TaskBoardActions, taskActions } from "./TaskBoardActions";
import "./taskBoardDetail.css";

export const taskTitle = (task: BoardTask) => activityTaskName(task);
export function BoardDate({
  value,
  observed,
  zone,
}: {
  value?: string;
  observed?: boolean;
  zone?: string;
}) {
  const time = value ? taskLocalTime(value, zone) : NaN;
  if (!value || !Number.isFinite(time)) return null;
  return (
    <time dateTime={value}>
      {formatDate(new Date(time), { dateStyle: "medium", timeStyle: "short" })}
      {observed && <span className="ml-1 text-muted-foreground">{t("detail.observed")}</span>}
    </time>
  );
}

function Tile({
  label,
  children,
  sub,
  verdict = "",
}: {
  label: string;
  children: ReactNode;
  sub?: ReactNode;
  verdict?: string;
}) {
  return (
    <div className={`tb-dc-tile ${verdict}`}>
      <span>{label}</span>
      <b>{children}</b>
      {sub && <small>{sub}</small>}
    </div>
  );
}
const detailStateIcons = {
  done: CircleCheckIcon,
  ready: PackageCheckIcon,
  running: ActivityIcon,
  user: HandIcon,
  failed: CircleXIcon,
  blocked: BanIcon,
  queued: ClockIcon,
  held: CircleDotIcon,
};
const duration = (seconds: number) => {
  const hours = Math.floor(seconds / 3600),
    minutes = Math.max(1, Math.round(seconds / 60));
  return hours < 1
    ? `${minutes}m`
    : hours >= 24
      ? `${Math.floor(hours / 24)}d ${hours % 24}h`
      : `${hours}h${Math.round((seconds % 3600) / 60) ? ` ${Math.round((seconds % 3600) / 60)}m` : ""}`;
};
const toolDuration = (seconds: number) => {
  const rounded = Math.round(seconds);
  if (rounded < 60) return t("detail.toolSeconds", { count: rounded });
  if (rounded < 3600) return t("detail.toolMinutes", { count: Math.round(rounded / 60) });
  const hours = Math.floor(rounded / 3600),
    minutes = Math.round((rounded % 3600) / 60);
  return hours >= 24
    ? t("detail.toolDays", { days: Math.floor(hours / 24), hours: hours % 24 })
    : t("detail.toolHours", {
        hours,
        minutes: minutes ? ` ${t("detail.toolMinutes", { count: minutes })}` : "",
      });
};
const Mark = ({ id, label = id }: { id: string; label?: string }) => (
  <BoardIdentityMark id={id} product={id} label={label} neutral size={18} />
);

export function TaskBoardDetail({
  task,
  tasks,
  sessions,
  onSelect,
  onClose,
  sourceCache,
  selectedSessionId,
}: {
  task: BoardTask;
  tasks: BoardTask[];
  sessions: BoardSession[];
  onSelect: (task: BoardTask) => void;
  onClose: () => void;
  sourceCache?: SourceCache;
  selectedSessionId?: string;
}) {
  const cadence = useBoardCadence();
  const query = useQuery({
    queryKey: [
      "taskBoard",
      "item",
      task["omnigent.ai:source"],
      task.uid,
      task.updated,
      sourceCache?.pin?.id,
      sourceCache?.pin?.sourceGeneration,
    ],
    queryFn: () =>
      readBoardJson<BoardTask>(
        `/v1/me/tasks/item?${new URLSearchParams({ source: task["omnigent.ai:source"], uid: task.uid, ...(sourceCache?.pin?.id ? { snapshot: sourceCache.pin.id, source_generation: sourceCache.pin.sourceGeneration } : {}) })}`,
      ),
    enabled: !sourceCache || (!!sourceCache.pin && sourceCache.pin.phase !== "staging"),
    retry: false,
    staleTime: 30_000,
    refetchInterval: cadence,
  });
  const current = query.data ?? task;
  const display = current["omnigent.ai:sourceDisplay"];
  const ids = [
    ...new Set([
      ...(display?.sessions?.map((s) => s.id) ?? []),
      ...(current["omnigent.ai:workedBy"]?.map((s) => s.session) ?? []),
    ]),
  ];
  const legacyIds = new Set(current["omnigent.ai:workedBy"]?.map((span) => span.session) ?? []);
  const details = useBoardSessionDetails(ids.filter((id) => sessions.some((s) => s.id === id)));
  const byId = new Map(sessions.map((s) => [s.id, s]));
  details.forEach((detail) => {
    if (detail.data && (detail.data.updated_at ?? 0) >= (byId.get(detail.data.id)?.updated_at ?? 0))
      byId.set(detail.data.id, detail.data);
  });
  const groupColor = boardGroupColors(tasks).get(taskIdentity(current));
  const rows = boardSessionRows([current], [...byId.values()]).filter(
    (row) =>
      ids.includes(row.session.id) &&
      (row.task ? taskIdentity(row.task) === taskIdentity(current) : legacyIds.has(row.session.id)),
  );
  const linked = rows.map((row) => ({
    ...row.session,
    createdAt:
      row.session.createdAt ||
      current["omnigent.ai:workedBy"]?.find((span) => span.session === row.session.id)?.from ||
      "",
  }));
  const selected = linked.find((s) => s.id === selectedSessionId);
  const live = selected ?? linked.find((s) => s.status !== "archived");
  const asOf = sourceCache?.display.asOf ?? new Date(Date.now()).toISOString();
  const state = routeState(current);
  const StateIcon = detailStateIcons[state];
  const stamp = (value: string) =>
    formatDate(Date.parse(value), {
      ...(formatDate(Date.parse(value), { year: "numeric", month: "2-digit", day: "2-digit" }) ===
      formatDate(Date.parse(asOf), { year: "numeric", month: "2-digit", day: "2-digit" })
        ? {}
        : { month: "2-digit" as const, day: "2-digit" as const }),
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
    });
  const now = live?.now ?? display?.current;
  const need =
    live?.need ??
    (live?.questions?.length ? live.questions.map((q) => q.text).join("；") : undefined) ??
    display?.need ??
    current["omnigent.ai:waiting"]?.note;
  const tool = live?.tool;
  const toolClock = live && byId.has(live.id) ? new Date(Date.now()).toISOString() : asOf;
  const toolElapsed = tool
    ? (Date.parse(toolClock) - Date.parse(tool.startedAt)) / 1000
    : undefined;
  const next = live?.next ?? display?.next ?? current["omnigent.ai:next"];
  const actors = new Map(
    (display?.actors ?? []).map((a) => [a.agentId, activityAgentName(a.agentId, a.label)]),
  );
  linked.forEach((s) => {
    if (!actors.has(s.agentId)) actors.set(s.agentId, activityAgentName(s.agentId));
  });
  const last = display?.lastActivity ?? current.updated;
  const checks = display?.checks ?? [];
  const checkHosts = sourceCache?.display.checkHosts ?? [
    ...new Set(checks.map((check) => check.host)),
  ];
  const verdict = checks.some((c) => c.verdict === "failed")
    ? "failed"
    : checks.some((c) => c.verdict === "flaky")
      ? "flaky"
      : checks.length
        ? "passed"
        : "";
  const commits = [...(display?.commits ?? [])].sort((a, b) => b.at.localeCompare(a.at));
  const roll = displayCost({ version: 1, ...display, sessions: linked });
  const nodes =
    display?.nodeSelection !== "intervals" && linked.length ? linked : (display?.intervals ?? []);
  const intervals = display?.intervals?.length
    ? display.intervals
    : linked.map((s) => ({
        id: s.id,
        from: s.createdAt,
        to: sessionObservation(s, byId.has(s.id) ? new Date(Date.now()).toISOString() : asOf)
          .workEnd,
        agentId: s.agentId,
        live: s.status === "running",
      }));
  const validIntervals = intervals.filter(
    (s) => Number.isFinite(Date.parse(s.from)) && Number.isFinite(Date.parse(s.to ?? asOf)),
  );
  const start = Math.min(
    ...validIntervals.map((s) => Date.parse(s.from)),
    ...(display?.events ?? []).map((e) => Date.parse(e.at)),
  );
  const end = Date.parse(asOf),
    span = Math.max(1, end - start);
  const id = taskIdentity(current);
  type DetailRelation = Omit<NonNullable<TaskDisplay["relations"]>[number], "kind"> & {
    kind: "parent" | "dependency" | "include" | "next";
  };
  const relationMap = new Map<string, DetailRelation>(
    tasks.flatMap((item) =>
      (item["omnigent.ai:sourceDisplay"]?.relations ?? []).map(
        (edge) =>
          [
            JSON.stringify([
              edge.from.source,
              edge.from.uid,
              edge.to.source,
              edge.to.uid,
              edge.kind,
            ]),
            edge,
          ] as const,
      ),
    ),
  );
  for (const edge of taskRelations(tasks)) {
    const from = { source: edge.from["omnigent.ai:source"], uid: edge.from.uid };
    const to = {
      source: edge.to?.["omnigent.ai:source"] ?? edge.from["omnigent.ai:source"],
      uid: edge.to?.uid ?? edge.uid,
    };
    const key = JSON.stringify([from, to, edge.kind]);
    if (
      ![...relationMap.values()].some(
        (item) =>
          JSON.stringify(item.from) === JSON.stringify(from) &&
          JSON.stringify(item.to) === JSON.stringify(to) &&
          item.kind === edge.kind,
      )
    )
      relationMap.set(key, {
        id: key,
        from,
        to,
        kind: edge.kind,
        origin: "observed",
        provenance: t("detail.observedEdge"),
      });
  }
  const relations = [...relationMap.values()].filter(
    (e) =>
      JSON.stringify([e.from.source, e.from.uid]) === id ||
      JSON.stringify([e.to.source, e.to.uid]) === id,
  );
  const [page, setPage] = useState(0);
  const currentPage = Math.min(page, Math.max(0, Math.ceil(relations.length / 4) - 1));
  const quote = display?.quotes?.filter((q) => q.text.length > 8) ?? [];
  const others = linked.filter((s) => s.id !== selectedSessionId).slice(0, 3);
  const sourceLink = safeSourceLink(
    Object.values(current.links ?? {}).find((l) => l.rel === "alternate")?.href,
  );
  return (
    <section
      className={`tb-dc state-${state}`}
      aria-label={t("detail.title", { title: taskTitle(current) })}
    >
      <header className="tb-dc-header">
        <span className="tb-dc-pill">
          <StateIcon size={14} />
          {t(`detail.state.${state}`)}
        </span>
        <b>{taskTitle(current)}</b>
        {activityTaskKey(current) && <code>{activityTaskKey(current)}</code>}
        <span className="tb-dc-group">
          {display?.identity?.groupBadge && (
            <span
              className="tb-dc-group-badge"
              style={{ background: groupColor ?? "var(--primary)" }}
            >
              {activityName(display.identity.groupBadge) ?? t("g6.other")}
            </span>
          )}
          {activityName(display?.identity?.groupLabel, current["omnigent.ai:group"]?.name)}
        </span>
        <button type="button" aria-label={t("detail.close")} onClick={onClose}>
          <XIcon size={15} />
        </button>
      </header>
      <div className="tb-dc-body">
        <div className="tb-dc-main">
          {!selectedSessionId && (!!now || !need) && (
            <p className={`tb-dc-now ${!now ? "quiet" : ""}`}>
              {now ||
                t(
                  state === "done"
                    ? "detail.closedLead"
                    : state === "running"
                      ? "visual.runningLead"
                      : "detail.quietLead",
                )}
            </p>
          )}
          {tool &&
            toolElapsed !== undefined &&
            toolElapsed >= 0 &&
            (tool.limitSeconds == null || toolElapsed < tool.limitSeconds) && (
              <p
                className="tb-dc-tool"
                title={
                  t("detail.toolLine", {
                    label: tool.label,
                    name: tool.name,
                    elapsed: toolDuration(toolElapsed),
                  }) +
                  (tool.limitSeconds == null
                    ? ""
                    : t("detail.toolLimit", { limit: toolDuration(tool.limitSeconds) }))
                }
              >
                <SquareTerminalIcon size={14} />
                {t("detail.toolLine", {
                  label: tool.label,
                  name: tool.name,
                  elapsed: toolDuration(toolElapsed),
                })}
                {tool.limitSeconds == null
                  ? ""
                  : t("detail.toolLimit", { limit: toolDuration(tool.limitSeconds) })}
              </p>
            )}
          {need && (
            <div className="tb-dc-need">
              <HandIcon size={16} />
              <TaskBoardFoldText key={need} value={need} />
            </div>
          )}
          {next && (
            <div className="tb-dc-next">
              <em>{t("detail.next")}</em>
              <p>
                <TaskBoardFoldText key={next} value={next} />
              </p>
            </div>
          )}
          <div className="tb-dc-tiles">
            {actors.size > 0 && (
              <Tile label={t("detail.handlers")} sub={live?.host}>
                <>
                  <span className="tb-dc-who">
                    {[...actors].map(([agent]) => (
                      <BoardAgentLogo key={agent} id={agent} />
                    ))}
                  </span>
                  {actors.size === 1
                    ? [...actors.values()][0]
                    : t("detail.actorCount", { count: actors.size })}
                </>
              </Tile>
            )}
            {last && (
              <Tile label={t("detail.lastMovement")} sub={stamp(last)}>
                {t("detail.ago", { time: duration(Math.max(60, (end - Date.parse(last)) / 1000)) })}
              </Tile>
            )}
            {checks.length > 0 && (
              <Tile
                label={t("detail.tests")}
                verdict={verdict}
                sub={
                  checks.length > 0 &&
                  checkHosts.map((host) => {
                    const check = checks.find((item) => item.host === host);
                    return (
                      <span key={host}>
                        {host}{" "}
                        {check ? (
                          <>
                            {t(`sessions.check.${check.verdict}`)}{" "}
                            {t("detail.checkCount", { count: check.count })}
                          </>
                        ) : (
                          t("detail.noCheckRecord")
                        )}
                        <br />
                      </span>
                    );
                  })
                }
              >
                {t(`sessions.check.${verdict}`)}
              </Tile>
            )}
            {commits.length > 0 && (
              <Tile label={t("detail.commits")} sub={<code>{commits[0].sha}</code>}>
                {t("detail.commitCount", { count: commits.length })}
              </Tile>
            )}
            {roll.amount !== null && (
              <Tile
                label={t("detail.cost")}
                sub={<>{t("detail.segmentCount", { count: nodes.length })}</>}
              >
                <span
                  className={`tb-dc-money ${roll.kind}`}
                  title={[
                    t(`sessions.cost.${roll.kind}.hint`),
                    ...new Set(
                      nodes.flatMap((node) =>
                        node.cost?.sourceLabel ? [node.cost.sourceLabel] : [],
                      ),
                    ),
                    roll.unpriced ? t("detail.unpriced", { count: roll.unpriced }) : "",
                  ]
                    .filter(Boolean)
                    .join(" · ")}
                >
                  <span>
                    {"$"}
                    {formatNumber(roll.amount, {
                      minimumFractionDigits: 2,
                      maximumFractionDigits: 2,
                    })}
                  </span>
                  <i>{t(`sessions.cost.${roll.kind}`)}</i>
                </span>
              </Tile>
            )}
          </div>
        </div>
        {(validIntervals.length > 0 || relations.length > 0) && (
          <div className="tb-dc-side">
            {validIntervals.length > 0 && (
              <section>
                <h5>{t("detail.workTimeline")}</h5>
                <div className="tb-dc-track">
                  {validIntervals.map((s) => {
                    const liveSegment = s.to === null || ("live" in s && s.live);
                    const to = s.to ?? asOf;
                    return (
                      <i
                        key={s.id}
                        className={`tb-dc-segment ag-${s.agentId}${liveSegment ? " live" : ""}`}
                        style={{
                          left: `${((Date.parse(s.from) - start) / span) * 100}%`,
                          width: `${Math.max(0.8, ((Date.parse(to) - Date.parse(s.from)) / span) * 100)}%`,
                        }}
                        title={`${activityAgentName(s.agentId, actors.get(s.agentId))} ${stamp(s.from)} → ${liveSegment ? t("detail.now") : stamp(to)} (${duration(Math.max(0, (Date.parse(to) - Date.parse(s.from)) / 1000))})`}
                      />
                    );
                  })}
                </div>
                <div className="tb-dc-axis">
                  <span>{stamp(new Date(start).toISOString())}</span>
                  <span>
                    {[...new Set(validIntervals.map((s) => s.agentId))].map((agent) => (
                      <BoardAgentLogo key={agent} id={agent} size={16} compact />
                    ))}
                    {t("detail.segmentCount", { count: validIntervals.length })}
                  </span>
                  <span>{t("detail.now")}</span>
                </div>
              </section>
            )}
            {relations.length > 0 && (
              <section>
                <h5>{t("detail.upstreamDownstream")}</h5>
                {relations.slice(currentPage * 4, currentPage * 4 + 4).map((edge) => {
                  const fromHere = JSON.stringify([edge.from.source, edge.from.uid]) === id;
                  const ref = fromHere ? edge.to : edge.from;
                  const target = tasks.find(
                    (item) => taskIdentity(item) === JSON.stringify([ref.source, ref.uid]),
                  );
                  const kind =
                    edge.kind === "parent"
                      ? fromHere
                        ? "childTask"
                        : "parentTask"
                      : edge.kind === "next"
                        ? fromHere
                          ? "successor"
                          : "predecessor"
                        : edge.kind === "include"
                          ? fromHere
                            ? "includes"
                            : "includedBy"
                          : fromHere
                            ? "blocking"
                            : "dependsOn";
                  const targetState =
                    target?.["omnigent.ai:sourceDisplay"]?.state ??
                    (target && ["completed", "cancelled"].includes(taskProgress(target))
                      ? "done"
                      : "queued");
                  const TargetIcon = detailStateIcons[targetState];
                  return (
                    <div className="tb-dc-relation" key={edge.id}>
                      <em>{t(`relation.${kind}`)}</em>
                      {target ? (
                        <button type="button" onClick={() => onSelect(target)}>
                          <TargetIcon size={12} />
                          {activityTaskKey(target) && <b>{activityTaskKey(target)}</b>}
                          <span>{taskTitle(target)}</span>
                        </button>
                      ) : (
                        <span>{t("g6.untitledTask")}</span>
                      )}
                      {edge.kind === "parent" && edge.since ? (
                        <small className="tb-dc-parent-since" title={edge.provenance}>
                          {t("detail.attachedAt", { date: edge.since.slice(0, 10) })}
                        </small>
                      ) : (
                        <small className={edge.origin} title={edge.provenance}>
                          {t(edge.origin === "manual" ? "detail.manual" : "detail.observedEdge")}
                          {edge.origin === "observed" && ` · ${edge.provenance}`}
                        </small>
                      )}
                    </div>
                  );
                })}
                {relations.length > 4 && (
                  <div className="tb-dc-pages">
                    <button
                      type="button"
                      disabled={currentPage === 0}
                      onClick={() => setPage(currentPage - 1)}
                    >
                      {t("pager.previous")}
                    </button>
                    <span>
                      {currentPage + 1}
                      {"/"}
                      {Math.ceil(relations.length / 4)}
                    </span>
                    <button
                      type="button"
                      disabled={(currentPage + 1) * 4 >= relations.length}
                      onClick={() => setPage(currentPage + 1)}
                    >
                      {t("pager.next")}
                    </button>
                  </div>
                )}
              </section>
            )}
          </div>
        )}
      </div>
      {(quote.length > 0 || commits.length > 0 || others.length > 0) && (
        <footer className="tb-dc-footer">
          {quote.length > 0 && (
            <section>
              <h5>{t("detail.words")}</h5>
              <blockquote>
                {"「"}
                {quote[0].text.slice(0, 96)}
                {quote[0].text.length > 96 ? "…" : ""}
                {"」"}
                {(quote[0].date || quote.length > 1) && (
                  <cite>
                    {quote[0].date}
                    {quote.length > 1 && (
                      <>
                        {" "}
                        {"· "}
                        {sourceLink ? (
                          <a href={sourceLink} target="_blank" rel="noopener noreferrer">
                            {t("detail.moreQuotes", { count: quote.length - 1 })}
                          </a>
                        ) : (
                          t("detail.moreQuotes", { count: quote.length - 1 })
                        )}
                      </>
                    )}
                  </cite>
                )}
              </blockquote>
            </section>
          )}
          {commits.length > 0 && (
            <section>
              <h5>{t("detail.recentCommits")}</h5>
              <ul className="tb-dc-commits">
                {commits.slice(0, 3).map((c) => (
                  <li key={c.id}>
                    <code>{c.sha}</code>
                    <span title={c.subject}>{c.subject}</span>
                  </li>
                ))}
              </ul>
            </section>
          )}
          {others.length > 0 && (
            <section>
              <h5>{t("detail.linkedSessions")}</h5>
              <ul className="tb-dc-sessions">
                {others.map((s) => (
                  <li key={s.id}>
                    <Mark id={s.agentId} />
                    <span>{s.title}</span>
                    <small>
                      {t(
                        `sessions.state.${sessionObservation(s, byId.has(s.id) ? new Date(Date.now()).toISOString() : asOf).state}`,
                      )}
                    </small>
                    {byId.has(s.id) && (
                      <Link
                        to={`/c/${encodeURIComponent(s.id)}`}
                        target="_blank"
                        rel="noopener noreferrer"
                        aria-label={t("sessions.open", { title: s.title })}
                      >
                        <ArrowUpRightIcon size={14} />
                      </Link>
                    )}
                  </li>
                ))}
              </ul>
            </section>
          )}
        </footer>
      )}
      <TaskSourceDetails
        display={display}
        hasParent={relations.some((edge) => edge.kind === "parent")}
      />
      {(sourceLink || taskActions(current["omnigent.ai:actions"]).length > 0) && (
        <div className="tb-dc-actions">
          <TaskBoardActions actions={taskActions(current["omnigent.ai:actions"])} />
          {sourceLink && (
            <a href={sourceLink} target="_blank" rel="noopener noreferrer">
              {t("source.open")}
            </a>
          )}
        </div>
      )}
      {query.isError && (
        <div role="status">
          {t("detail.historyFailed")}
          <button type="button" onClick={() => void query.refetch()}>
            {t("board.retry")}
          </button>
        </div>
      )}
    </section>
  );
}
