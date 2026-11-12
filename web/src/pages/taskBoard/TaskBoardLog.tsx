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
  Play,
} from "lucide-react";
import { taskIdentity, type BoardTask } from "./taskBoardData";
import type { BoardSession } from "./taskBoardNative";
import { eventMother, localDay, type BoardEvent } from "./taskBoardActivityData";
import { boardGroupColors } from "./taskBoardSessionData";
import { BoardIdentityMark } from "./BoardIdentityMark";
import { t } from "./taskBoardText";
import {
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
export function TaskBoardLog({
  tasks,
  sessions,
  events,
  onSelect,
}: {
  tasks: BoardTask[];
  sessions: BoardSession[];
  events: BoardEvent[];
  onSelect: (task: BoardTask) => void;
}) {
  const [by, setBy] = useState("time"),
    [kind, setKind] = useState("all"),
    [paused, setPaused] = useState<BoardEvent[] | null>(null),
    [open, setOpen] = useState<string | null>(null),
    [page, setPage] = useState(0),
    [pages, setPages] = useState<Record<string, number>>({});
  const taskMap = new Map(tasks.map((task) => [taskIdentity(task), task])),
    colors = boardGroupColors(tasks);
  const rich = tasks.flatMap((task) =>
    (task["omnigent.ai:sourceDisplay"]?.events ?? []).map((event) => ({
      task,
      event,
      key: JSON.stringify([event.task.source, event.task.uid, event.id]),
    })),
  );
  const richMap = new Map(rich.map((r) => [r.key, r]));
  const combined = new Map(events.map((e) => [JSON.stringify([e.source, e.uid, e.id]), e]));
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
  const live = [...combined.values()].sort(
    (a, b) =>
      Date.parse(b.time) - Date.parse(a.time) ||
      (richMap.get(JSON.stringify([a.source, a.uid, a.id]))?.event.rank ?? 0) -
        (richMap.get(JSON.stringify([b.source, b.uid, b.id]))?.event.rank ?? 0),
  );
  const shown = paused ?? live,
    buffered = paused
      ? live.filter(
          (e) => !paused.some((p) => p.id === e.id && p.source === e.source && p.uid === e.uid),
        ).length
      : 0;
  const groups = new Map<string, { label: string; rows: BoardEvent[] }>();
  for (const event of shown) {
    const k = kinds[event.kind] ?? event.kind;
    if (
      kind !== "all" &&
      !(kind === "hand" ? ["open", "claim", "takeover", "release", "done"].includes(k) : kind === k)
    )
      continue;
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
    const label =
      by === "time"
        ? localDay(event.time)
        : by === "mother"
          ? (sessions.find((s) => s.id === root)?.title ??
            mother?.label ??
            t("activity.unassigned"))
          : (identity?.groupLabel ?? task?.["omnigent.ai:group"]?.name ?? event.source);
    if (!groups.has(key)) groups.set(key, { label, rows: [] });
    groups.get(key)!.rows.push(event);
  }
  const change = (setter: (v: string) => void) => (v: string) => {
    setter(v);
    setPage(0);
    setPages({});
    setOpen(null);
  };
  return (
    <div className="tb-log">
      <div className="l-bar">
        <ActivitySegments
          value={by}
          onChange={change(setBy)}
          items={["time", "mother", "line"].map((k) => [k, t(`g6.log.by.${k}`)])}
        />
        <ActivitySegments
          value={kind}
          onChange={change(setKind)}
          items={["all", "hand", "test", "commit"].map((k) => [k, t(`g6.log.kind.${k}`)])}
        />
        <button
          type="button"
          className={`l-play ${paused ? "on" : ""}`}
          onClick={() => setPaused(paused ? null : live)}
        >
          {paused ? <Play size={14} /> : <Pause size={14} />}
          <span>{t(paused ? "g6.resume" : "g6.pause")}</span>
          {buffered > 0 && <i className="l-n">{buffered}</i>}
        </button>
      </div>
      {[...groups].slice(page * 6, page * 6 + 6).map(([key, g]) => (
        <section className="l-g" key={key}>
          <h4>
            {g.label}
            <em>{g.rows.length}</em>
          </h4>
          <div className="l-rows">
            {g.rows.slice((pages[key] ?? 0) * 25, (pages[key] ?? 0) * 25 + 25).map((row) => {
              const id = JSON.stringify([row.source, row.uid, row.id]),
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
                        label={r?.host ?? row.source}
                        product={r?.agentId}
                        size={24}
                      />
                    </span>
                    <span
                      className={`lr-ev ev-${k === "test" || k === "done" ? "green" : k === "claim" ? "blue" : k === "takeover" ? "violet" : "neutral"} ${check ? "v-" + check.verdict : ""}`}
                      title={t("g6.event." + k)}
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
                          row.uid
                        )}
                      </b>
                      <span
                        className={`lt ${check ? "v-" + check.verdict : k === "commit" ? "mono" : ""}`}
                      >
                        {check
                          ? `${check.host} ${t("sessions.check." + check.verdict)}`
                          : k === "commit"
                            ? (commits[0]?.subject ?? row.label)
                            : `${r?.agentId ?? ""} ${row.label ?? t("g6.event." + k)}`}
                      </span>
                    </span>
                    <span className="lr-sub">
                      {r?.workSeconds ? activityDuration(r.workSeconds) : ""}
                      {check
                        ? `${check.count} · ${activityDuration(check.seconds ?? 0)} ${check.head ?? ""}`
                        : r?.host}
                      {commits.length > 0 && ` · ${commits.length} ${t("g6.commits")}`}
                    </span>
                    <span className="lr-line">
                      {d?.identity && (
                        <BoardIdentityMark
                          id={d.identity.groupId}
                          label={d.identity.groupBadge ?? d.identity.groupId}
                          size={18}
                        />
                      )}
                      <code>{d?.identity?.key ?? row.uid}</code>
                    </span>
                    <ChevronDown size={14} className="lr-chev" />
                  </div>
                  {open === id && (
                    <div className="lgd">
                      {check ? (
                        <div className="dc-tiles">
                          <div>
                            <small>{other?.host ?? t("g6.otherHost")}</small>
                            <b>
                              {other ? t("sessions.check." + other.verdict) : t("g6.noReceipt")}
                            </b>
                            {other && (
                              <small>
                                {other.count}
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
                                {commits.length} {t("g6.commits")}
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
      <ActivityPages page={page} total={groups.size} size={6} onChange={setPage} />
    </div>
  );
}
