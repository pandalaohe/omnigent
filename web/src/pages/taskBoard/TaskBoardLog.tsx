import { Fragment, useState, type CSSProperties } from "react";
import {
  ArrowRightLeft,
  ChevronDown,
  CircleCheck,
  Clock,
  FilePlus2,
  FlaskConical,
  GitBranch,
  LogIn,
  LogOut,
  Pause,
  Network,
  Route,
  Play,
} from "lucide-react";
import { taskIdentity, type BoardTask } from "./taskBoardData";
import type { BoardSession } from "./taskBoardNative";
import { eventMother, localDay, type BoardEvent } from "./taskBoardActivityData";
import { boardGroupColors } from "./taskBoardSessionData";
import { BoardIdentityMark } from "./BoardIdentityMark";
import { TaskBoardFoldText } from "./TaskBoardFoldText";
import { activityHistoryValue } from "./taskBoardPresentation";
import { t } from "./taskBoardText";
import {
  activityNumber,
  activityAgentName,
  activityEventLabel,
  activityFieldName,
  activitySourceName,
  activityTaskName,
  ActivityPages,
  ActivitySegments,
  activityDuration,
  activityStamp,
} from "./TaskBoardActivityParts";

const kinds: Record<string, string> = {
  start: "claim",
  transfer: "takeover",
  stop: "release",
  outcome: "done",
  check: "test",
  created: "open",
  closed: "done",
  parent: "relation",
};
const icons = {
  open: FilePlus2,
  claim: LogIn,
  takeover: ArrowRightLeft,
  release: LogOut,
  done: CircleCheck,
  test: FlaskConical,
  commit: GitBranch,
};
type LogCategory = "created" | "done" | "relation" | "changes" | "activity";
interface LogRow {
  event: BoardEvent;
  category: LogCategory;
  changeBreakdown?: string[];
  changes?: BoardEvent[];
}
const logCategory = (kind: string): LogCategory => {
  if (["field", "assignee", "other"].includes(kind)) return "changes";
  if (["open", "created"].includes(kind)) return "created";
  if (["outcome", "done", "closed"].includes(kind)) return "done";
  if (["relation", "parent"].includes(kind)) return "relation";
  return "activity";
};
export function TaskBoardLog({
  tasks,
  sessions,
  events,
  sourceLabels,
  onSelect,
}: {
  tasks: BoardTask[];
  sessions: BoardSession[];
  events: BoardEvent[];
  sourceLabels?: Record<string, string>;
  onSelect: (task: BoardTask) => void;
}) {
  const [by, setBy] = useState("time"),
    [kind, setKind] = useState("all"),
    [paused, setPaused] = useState<LogRow[] | null>(null),
    [open, setOpen] = useState<string | null>(null),
    [page, setPage] = useState(0),
    [pages, setPages] = useState<Record<string, number>>({});
  const taskMap = new Map(tasks.map((task) => [taskIdentity(task), task])),
    colors = boardGroupColors(tasks);
  const roots = new Map(
    tasks.flatMap((task) =>
      (task["omnigent.ai:sourceDisplay"]?.sessions ?? []).map(
        (session) => [session.id, session] as const,
      ),
    ),
  );
  const rich = tasks.flatMap((task) =>
    (task["omnigent.ai:sourceDisplay"]?.events ?? []).map((event) => ({
      task,
      event,
      key: JSON.stringify([event.task.source, event.task.uid, event.id]),
    })),
  );
  const richMap = new Map(rich.map((r) => [r.key, r]));
  const supplied = new Set(
    tasks
      .filter((task) => task["omnigent.ai:sourceDisplay"]?.events !== undefined)
      .map(taskIdentity),
  );
  const combined = new Map(
    events
      .filter((event) => !supplied.has(JSON.stringify([event.source, event.uid])))
      .map((e) => [JSON.stringify([e.source, e.uid, e.id]), e]),
  );
  rich.forEach(({ event, key }) =>
    combined.set(key, {
      id: event.id,
      time: event.at,
      source: event.task.source,
      uid: event.task.uid,
      kind: kinds[event.kind] ?? event.kind,
      label: event.label,
    }),
  );
  const rawLive = [...combined.values()].sort(
    (a, b) =>
      Date.parse(b.time) - Date.parse(a.time) ||
      (richMap.get(JSON.stringify([a.source, a.uid, a.id]))?.event.rank ?? 0) -
        (richMap.get(JSON.stringify([b.source, b.uid, b.id]))?.event.rank ?? 0),
  );
  const changeGroups = new Map<
    string,
    { event: BoardEvent; kinds: Map<string, number>; changes: BoardEvent[] }
  >();
  const live: LogRow[] = [];
  for (const event of rawLive) {
    if (logCategory(event.kind) !== "changes") {
      live.push({ event, category: logCategory(event.kind) });
      continue;
    }
    const key = JSON.stringify([event.source, event.uid, localDay(event.time)]);
    const current = changeGroups.get(key) ?? {
      event,
      kinds: new Map<string, number>(),
      changes: [],
    };
    if (Date.parse(event.time) > Date.parse(current.event.time)) current.event = event;
    current.changes.push(event);
    const label = activityFieldName(
      event.field ?? event.kind,
      taskMap.get(JSON.stringify([event.source, event.uid])),
    );
    current.kinds.set(label, (current.kinds.get(label) ?? 0) + 1);
    changeGroups.set(key, current);
  }
  for (const { event, kinds: breakdown, changes } of changeGroups.values())
    live.push({
      event: {
        ...event,
        id: `changes:${event.source}:${event.uid}:${localDay(event.time)}`,
        kind: "other",
        label: t("g6.changeSummary", { count: [...breakdown.values()].reduce((a, b) => a + b, 0) }),
      },
      category: "changes",
      changeBreakdown: [...breakdown].map(([label, count]) =>
        t("g6.changeBreakdown", { label, count }),
      ),
      changes,
    });
  live.sort((a, b) => Date.parse(b.event.time) - Date.parse(a.event.time));
  const shown = paused ?? live,
    pausedKeys = new Set(
      (paused ?? [])
        .flatMap(({ event, changes }) => changes ?? [event])
        .map((event) => JSON.stringify([event.source, event.uid, event.id])),
    );
  const buffered = paused
    ? rawLive.filter(
        (event) => !pausedKeys.has(JSON.stringify([event.source, event.uid, event.id])),
      ).length
    : 0;
  const groups = new Map<
    string,
    { label: string; subtitle?: string; agent?: string; rows: LogRow[] }
  >();
  for (const item of shown) {
    const event = item.event;
    if (kind !== "all" && item.category !== kind) continue;
    const task = taskMap.get(JSON.stringify([event.source, event.uid])),
      mother = eventMother(event, task, sessions),
      identity = task?.["omnigent.ai:sourceDisplay"]?.identity;
    const root = richMap.get(JSON.stringify([event.source, event.uid, event.id]))?.event
      .rootSessionId;
    const key =
      by === "time"
        ? localDay(event.time)
        : by === "mother"
          ? (root ?? mother?.id ?? "none")
          : JSON.stringify([event.source, identity?.groupId ?? task?.["omnigent.ai:group"]?.id]);
    const rootSession = root ? roots.get(root) : mother ? roots.get(mother.id) : undefined;
    const day = localDay(event.time);
    const label =
      by === "time"
        ? day === localDay(Date.now())
          ? t("view.today")
          : day === localDay(Date.now() - 86400000)
            ? t("routes.yesterday")
            : day.slice(5)
        : by === "mother"
          ? (rootSession?.label ??
            sessions.find((s) => s.id === root)?.title ??
            (mother?.label !== mother?.id ? mother?.label : undefined) ??
            t("activity.unassigned"))
          : (identity?.groupLabel ??
            task?.["omnigent.ai:group"]?.name ??
            activitySourceName(event.source, task, sourceLabels));
    if (!groups.has(key))
      groups.set(key, {
        label,
        subtitle: by === "time" ? day.slice(5) : rootSession?.title,
        agent: by === "mother" ? rootSession?.agentId : undefined,
        rows: [],
      });
    groups.get(key)!.rows.push(item);
  }
  const change = (setter: (v: string) => void) => (v: string) => {
    setter(v);
    setPaused(null);
    setPage(0);
    setPages({});
    setOpen(null);
  };
  return (
    <div className="tb-log">
      <div className="l-bar">
        <ActivitySegments
          value={by}
          small={false}
          onChange={change(setBy)}
          items={["time", "mother", "line"].map((k) => [
            k,
            t(`g6.log.by.${k}`),
            k === "time" ? (
              <Clock key={k} size={13} />
            ) : k === "mother" ? (
              <Network key={k} size={13} />
            ) : (
              <Route key={k} size={13} />
            ),
          ])}
        />
        <ActivitySegments
          value={kind}
          small={false}
          onChange={change(setKind)}
          items={["all", "created", "done", "relation", "changes", "activity"].map((k) => [
            k,
            t(`g6.log.kind.${k}`),
          ])}
        />
        <button
          type="button"
          className={`l-play ${paused ? "on" : ""}`}
          disabled={by !== "time"}
          onClick={() => setPaused(paused ? null : live)}
        >
          {paused ? <Play size={14} /> : <Pause size={14} />}
          <span>{t(paused ? "g6.resume" : "g6.pause")}</span>
          {buffered > 0 && <i className="l-n">{activityNumber(buffered)}</i>}
        </button>
      </div>
      {[...groups]
        .slice(
          Math.min(page, Math.max(0, Math.ceil(groups.size / 6) - 1)) * 6,
          Math.min(page, Math.max(0, Math.ceil(groups.size / 6) - 1)) * 6 + 6,
        )
        .map(([key, g]) => (
          <section className="l-g" key={key}>
            <h4>
              {g.agent && (
                <BoardIdentityMark
                  id={g.agent}
                  product={g.agent}
                  label={activityAgentName(g.agent)}
                  size={18}
                />
              )}
              {g.label}
              {g.subtitle && <small>{g.subtitle}</small>}
              <em>{activityNumber(g.rows.length)}</em>
            </h4>
            <div className="l-rows">
              {g.rows.slice((pages[key] ?? 0) * 25, (pages[key] ?? 0) * 25 + 25).map((item) => {
                const row = item.event,
                  id = JSON.stringify([row.source, row.uid, row.id]),
                  task = taskMap.get(JSON.stringify([row.source, row.uid])),
                  d = task?.["omnigent.ai:sourceDisplay"],
                  r = richMap.get(id)?.event,
                  k = kinds[row.kind] ?? row.kind,
                  Icon = icons[k as keyof typeof icons] ?? Clock,
                  check = d?.checks?.find((c) => c.id === r?.checkId),
                  commits = (d?.commits ?? []).filter(
                    (c) =>
                      r?.commitIds?.includes(c.id) ||
                      (r?.from &&
                        Date.parse(c.at) >= Date.parse(r.from) &&
                        Date.parse(c.at) <= Date.parse(row.time)),
                  );
                const other = d?.checks
                  ?.filter((c) => c.host !== check?.host)
                  .sort((a, b) => Date.parse(b.at) - Date.parse(a.at))[0];
                return (
                  <Fragment key={id}>
                    <div
                      role="button"
                      tabIndex={0}
                      aria-expanded={open === id}
                      className={`lr lr-${k} ${open === id ? "is-open" : ""}`}
                      style={
                        {
                          "--lc": task ? colors.get(taskIdentity(task)) : undefined,
                        } as CSSProperties
                      }
                      onClick={() => {
                        setPaused(paused ?? live);
                        setOpen(open === id ? null : id);
                      }}
                      onKeyDown={(e) => {
                        if (e.target !== e.currentTarget) return;
                        if (e.key === "Enter" || e.key === " ") {
                          e.preventDefault();
                          setPaused(paused ?? live);
                          setOpen(open === id ? null : id);
                        }
                      }}
                    >
                      <span className="lr-t">{activityStamp(Date.parse(row.time), true)}</span>
                      <span className="lr-who">
                        <BoardIdentityMark
                          id={r?.agentId ?? r?.host ?? row.source}
                          label={
                            activityAgentName(r?.agentId, r?.host) ??
                            activitySourceName(row.source, task, sourceLabels)
                          }
                          product={r?.agentId}
                          size={24}
                        />
                      </span>
                      <span
                        className={`lr-ev ev-${k === "test" || k === "done" ? "green" : k === "claim" ? "blue" : k === "takeover" ? "violet" : "neutral"} ${check ? "v-" + check.verdict : ""}`}
                        title={activityEventLabel(k)}
                      >
                        <Icon size={14} />
                      </span>
                      <span className="lr-main">
                        <b>
                          {task ? (
                            <button
                              type="button"
                              onClick={(e) => {
                                e.stopPropagation();
                                setPaused(paused ?? live);
                                onSelect(task);
                              }}
                            >
                              {activityTaskName(task)}
                            </button>
                          ) : (
                            t("g6.untitledTask")
                          )}
                        </b>
                        <span
                          className={`lt ${check ? "v-" + check.verdict : k === "commit" ? "mono" : ""}`}
                        >
                          {item.changeBreakdown?.length
                            ? `${row.label}: ${item.changeBreakdown.join(" · ")}`
                            : check
                              ? `${check.host} ${t("sessions.check." + check.verdict)}`
                              : k === "commit"
                                ? (commits[0]?.subject ?? row.label ?? row.note)
                                : `${activityAgentName(r?.agentId ?? row.actor, d?.actors?.find((a) => a.agentId === r?.agentId)?.label)} ${activityHistoryValue(activityEventLabel(k, row.label, row.note) ?? "")}`}
                        </span>
                      </span>
                      <span className="lr-sub">
                        {r?.workSeconds ? activityDuration(r.workSeconds) : ""}
                        {check
                          ? `${activityNumber(check.count)} · ${activityDuration(check.seconds ?? 0)} ${check.head ?? ""}`
                          : r?.host}
                        {commits.length > 0 &&
                          ` · ${activityNumber(commits.length)} ${t("g6.commits")}`}
                      </span>
                      <span className="lr-line">
                        {d?.identity && (
                          <span
                            style={{
                              display: "inline-grid",
                              placeItems: "center",
                              minWidth: 18,
                              height: 18,
                              padding: "0 5px",
                              borderRadius: 6,
                              background: task
                                ? colors.get(taskIdentity(task))
                                : "var(--secondary)",
                              color: "white",
                              fontSize: 10,
                              fontWeight: 700,
                            }}
                          >
                            {d.identity.groupBadge ?? d.identity.groupId}
                          </span>
                        )}
                        <span>{activitySourceName(row.source, task, sourceLabels)}</span>
                      </span>
                      <ChevronDown size={14} className="lr-chev" />
                    </div>
                    {open === id && (
                      <div className="lgd">
                        {item.changeBreakdown?.length ? (
                          <div className="lgd-changes">
                            <p>{item.changeBreakdown.join(" · ")}</p>
                            <ol>
                              {item.changes
                                ?.slice((pages[id] ?? 0) * 25, (pages[id] ?? 0) * 25 + 25)
                                .map((event) => (
                                  <li key={event.id}>
                                    <time>{activityStamp(Date.parse(event.time), true)}</time>
                                    {" · "}
                                    <b>{activityFieldName(event.field ?? event.kind, task)}</b>
                                    {event.label && (
                                      <span>
                                        {" · "}
                                        {activityHistoryValue(event.label)}
                                      </span>
                                    )}
                                    {event.actor && (
                                      <span>
                                        {" · "}
                                        {activityAgentName(event.actor)}
                                      </span>
                                    )}
                                    {event.from !== undefined && (
                                      <div>
                                        <small>{t("g6.changedFrom")}</small>
                                        <TaskBoardFoldText
                                          value={activityHistoryValue(event.from)}
                                        />
                                      </div>
                                    )}
                                    {event.to !== undefined && (
                                      <div>
                                        <small>{t("g6.changedTo")}</small>
                                        <TaskBoardFoldText value={activityHistoryValue(event.to)} />
                                      </div>
                                    )}
                                    {event.note && (
                                      <span>
                                        {" · "}
                                        {activityHistoryValue(event.note)}
                                      </span>
                                    )}
                                  </li>
                                ))}
                            </ol>
                            <ActivityPages
                              page={pages[id] ?? 0}
                              total={item.changes?.length ?? 0}
                              size={25}
                              onChange={(p) => setPages({ ...pages, [id]: p })}
                            />
                          </div>
                        ) : check ? (
                          <div className="dc-tiles">
                            <div>
                              <small>{other?.host ?? t("g6.otherHost")}</small>
                              <b>
                                {other ? t("sessions.check." + other.verdict) : t("g6.noReceipt")}
                              </b>
                              {other && (
                                <small>
                                  {activityNumber(other.count)}
                                  {"·"}
                                  {activityStamp(Date.parse(other.at))}
                                </small>
                              )}
                            </div>
                            <div>
                              <small>{t("g6.finishedAt")}</small>
                              <b>{activityStamp(Date.parse(row.time))}</b>
                            </div>
                          </div>
                        ) : (
                          <>
                            {r?.from && (
                              <div className="dc-tiles">
                                <div>{activityStamp(Date.parse(r.from))}</div>
                                <div>{activityStamp(Date.parse(row.time))}</div>
                                <div>
                                  {activityNumber(commits.length)} {t("g6.commits")}
                                </div>
                              </div>
                            )}
                            <ul className="lgd-c">
                              {commits.slice(0, 4).map((c) => (
                                <li key={c.id}>
                                  <code>{c.sha}</code>
                                  <em>{activityStamp(Date.parse(c.at), true)}</em>
                                  <span>{c.subject}</span>
                                </li>
                              ))}
                            </ul>
                          </>
                        )}
                      </div>
                    )}
                  </Fragment>
                );
              })}
              <ActivityPages
                page={pages[key] ?? 0}
                total={g.rows.length}
                size={25}
                onChange={(p) => setPages({ ...pages, [key]: p })}
              />
            </div>
          </section>
        ))}
      {!groups.size && <p>{t("activity.empty")}</p>}
      <ActivityPages page={page} total={groups.size} size={6} onChange={setPage} />
    </div>
  );
}
