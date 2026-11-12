import { sourceHistory, type SourceCache } from "./taskBoardSnapshotData";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { timeMonday } from "d3-time";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { usageRangeQuery } from "@/pages/usageBoard/UsageRangeControl";
import type { UsageActivity } from "@/pages/usageBoard/usageActivityData";
import { Button } from "@/components/ui/button";
import { Link } from "@/lib/routing";
import { isOpenTask, readBoardJson, taskIdentity, type BoardTask } from "./taskBoardData";
import type { BoardSession } from "./taskBoardNative";
import { BoardDate, taskTitle } from "./TaskBoardDetail";
import { BoardPager } from "./TaskBoardRoutes";
import {
  eventMother,
  localDay,
  readBoardHistory,
  readLocalClosedCounts,
} from "./taskBoardActivityData";
import { formatDate, formatNumber, t } from "./taskBoardText";

export function TaskBoardActivity({
  view,
  tasks,
  sessions,
  onSelect,
  sourceCaches,
}: {
  sourceCaches?: Record<string, SourceCache>;
  view: "log" | "history" | "today";
  tasks: BoardTask[];
  sessions: BoardSession[];
  onSelect: (task: BoardTask) => void;
}) {
  const [group, setGroup] = useState("time"),
    [page, setPage] = useState(0),
    [month, setMonth] = useState(""),
    [hold, setHold] = useState("365");
  const history = useQuery({
    queryKey: ["taskBoard", "history"],
    queryFn: () => readBoardHistory(),
    enabled: !sourceCaches,
    retry: false,
    refetchInterval: () => (document.visibilityState === "visible" ? 60_000 : false),
  });
  const offset = -new Date().getTimezoneOffset();
  const counts = useQuery({
    queryKey: ["taskBoard", "closedCounts", offset],
    queryFn: () => readLocalClosedCounts(),
    enabled: !sourceCaches,
    retry: false,
    refetchInterval: () => (document.visibilityState === "visible" ? 60_000 : false),
  });
  const today = localDay(Date.now());
  const info = useServerInfo();
  const usage = useQuery({
    queryKey: ["taskBoard", "todayCost", today],
    enabled: view === "today" && info !== "loading" && !!info.usage_board_enabled,
    retry: false,
    queryFn: () =>
      readBoardJson<UsageActivity>(`/v1/me/usage/activity?${usageRangeQuery({ key: "today" })}`),
    refetchInterval: () => (document.visibilityState === "visible" ? 60_000 : false),
  });
  const byId = new Map(tasks.map((task) => [taskIdentity(task), task]));
  const cachedHistory = sourceCaches ? sourceHistory(sourceCaches) : undefined;
  const cachedCounts = sourceCaches
    ? Object.values(sourceCaches).flatMap((cache) => cache.counts)
    : undefined;
  const events = (cachedHistory ?? history.data ?? []).filter(
    (event) => view !== "today" || localDay(event.time) === today,
  );
  const weeks = new Map<string, number>(),
    months = new Map<string, number>();
  for (const day of cachedCounts ?? counts.data?.days ?? []) {
    const week = localDay(timeMonday.floor(new Date(`${day.day}T12:00:00`)));
    weeks.set(week, (weeks.get(week) ?? 0) + day.closed);
    months.set(day.day.slice(0, 7), (months.get(day.day.slice(0, 7)) ?? 0) + day.closed);
  }
  const closed = tasks.filter(
    (task) =>
      !isOpenTask(task) &&
      localDay(task.progressUpdated ?? task["omnigent.ai:closedSeen"] ?? "") === today,
  );
  const active = sessions.filter(
    (session) => session.updated_at && localDay(session.updated_at * 1000) === today,
  );
  const held = tasks.filter(
    (task) =>
      isOpenTask(task) ||
      (Date.parse(task.progressUpdated ?? task["omnigent.ai:closedSeen"] ?? "") >=
        Date.now() - Number(hold) * 86_400_000 &&
        (!month ||
          localDay(task.progressUpdated ?? task["omnigent.ai:closedSeen"] ?? "").startsWith(
            month,
          ))),
  );
  const groups = new Map<string, { label: string; entries: typeof events }>();
  for (const event of events) {
    const task = byId.get(JSON.stringify([event.source, event.uid]));
    const mother = group === "mother" ? eventMother(event, task, sessions) : undefined;
    const taskGroup = task?.["omnigent.ai:group"];
    const key = JSON.stringify(
      group === "mother"
        ? mother
          ? ["mother", mother.id]
          : [task ? "unassigned" : "notLoaded"]
        : group === "group"
          ? ["group", event.source, taskGroup?.id ?? null]
          : ["time", localDay(event.time)],
    );
    const label =
      group === "mother"
        ? task
          ? (mother?.label ?? t("activity.unassigned"))
          : t("activity.notLoaded")
        : group === "group"
          ? (taskGroup?.name ?? taskGroup?.id ?? event.source)
          : localDay(event.time);
    groups.set(key, { label, entries: [...(groups.get(key)?.entries ?? []), event] });
  }
  const pages = Math.max(1, Math.ceil((view === "history" ? held.length : events.length) / 30)),
    current = Math.min(page, pages - 1);
  let index = 0;
  return (
    <section className="tb-activity" aria-label={t(`view.${view}`)}>
      <div className="tb-section-toolbar">
        <h2>{t(`view.${view}`)}</h2>
        {view === "log" && (
          <label>
            {t("activity.group")}{" "}
            <select
              value={group}
              aria-label={t("activity.groupBy")}
              onChange={(e) => {
                setGroup(e.target.value);
                setPage(0);
              }}
            >
              {["time", "mother", "group"].map((id) => (
                <option key={id} value={id}>
                  {t(`activity.${id}`)}
                </option>
              ))}
            </select>
          </label>
        )}
        {view === "history" && (
          <label>
            {t("history.hold")}{" "}
            <select
              value={hold}
              onChange={(e) => {
                setHold(e.target.value);
                setPage(0);
              }}
            >
              {["7", "30", "90", "365"].map((days) => (
                <option key={days} value={days}>
                  {t("history.days", { count: Number(days) })}
                </option>
              ))}
            </select>
          </label>
        )}
      </div>
      {view === "history" ? (
        <>
          {counts.isError && (
            <p role="alert">
              {t("activity.countsFailed")}{" "}
              <Button onClick={() => void counts.refetch()}>{t("board.retry")}</Button>
            </p>
          )}
          <div className="tb-week-bars" aria-label={t("history.weeks")}>
            {[...weeks]
              .sort(([a], [b]) => a.localeCompare(b))
              .map(([week, count]) => (
                <button
                  key={week}
                  type="button"
                  title={t("history.week", { date: week, count })}
                  onClick={() => {
                    setMonth(
                      (cachedCounts ?? counts.data?.days ?? [])
                        .find(
                          (day) =>
                            localDay(timeMonday.floor(new Date(`${day.day}T12:00:00`))) === week,
                        )
                        ?.day.slice(0, 7) ?? "",
                    );
                    setPage(0);
                  }}
                >
                  <span
                    style={{
                      height: `${Math.max(4, (count / Math.max(...weeks.values(), 1)) * 70)}px`,
                    }}
                  />
                  <small>{formatNumber(count)}</small>
                  <time>
                    {formatDate(new Date(`${week}T12:00:00`), { month: "short", day: "numeric" })}
                  </time>
                </button>
              ))}
          </div>
          <label>
            {t("history.month")}{" "}
            <select
              value={month}
              onChange={(e) => {
                setMonth(e.target.value);
                setPage(0);
              }}
            >
              <option value="">{t("history.allMonths")}</option>
              {[...months]
                .sort(([a], [b]) => b.localeCompare(a))
                .map(([key, count]) => (
                  <option key={key} value={key}>
                    {t("history.monthCount", { month: key, count })}
                  </option>
                ))}
            </select>
          </label>
          {month && !held.some((task) => !isOpenTask(task)) && (
            <p className="tb-empty">{t("history.outsideHold")}</p>
          )}
          <ul className="tb-activity-list">
            {held.slice(current * 30, current * 30 + 30).map((task) => (
              <li key={taskIdentity(task)}>
                <button type="button" onClick={() => onSelect(task)}>
                  {taskTitle(task)}
                </button>
                <span>{t(`progress.${task.progress ?? "needs-action"}`)}</span>
                <BoardDate
                  value={task.progressUpdated ?? task["omnigent.ai:closedSeen"]}
                  observed={!task.progressUpdated && !!task["omnigent.ai:closedSeen"]}
                />
              </li>
            ))}
          </ul>
          {!held.length && <p className="tb-empty">{t("activity.empty")}</p>}
        </>
      ) : (
        <>
          {view === "today" && (
            <>
              <div className="tb-number-tiles">
                <div>
                  <dt>{t("today.closed")}</dt>
                  <dd>{formatNumber(closed.length)}</dd>
                </div>
                <div>
                  <dt>{t("today.sessions")}</dt>
                  <dd>{formatNumber(active.length)}</dd>
                </div>
                {[...new Set(events.map((e) => e.kind))].map((kind) => (
                  <div key={kind}>
                    <dt>
                      {t(`history.${kind}`) === `history.${kind}` ? kind : t(`history.${kind}`)}
                    </dt>
                    <dd>{formatNumber(events.filter((e) => e.kind === kind).length)}</dd>
                  </div>
                ))}
              </div>
              <ul className="tb-activity-list">
                {closed.map((task) => (
                  <li key={taskIdentity(task)}>
                    <button type="button" onClick={() => onSelect(task)}>
                      {taskTitle(task)}
                    </button>
                  </li>
                ))}
                {active.map((session) => (
                  <li key={session.id}>
                    <Link to={`/c/${encodeURIComponent(session.id)}`}>
                      {session.title || session.id}
                    </Link>
                  </li>
                ))}
              </ul>
            </>
          )}
          {view === "today" && usage.data && (
            <section>
              <h3>{t("today.cost")}</h3>
              <ul className="tb-activity-list">
                {usage.data.accounts.map((account) => (
                  <li key={account.account}>
                    <Link to={`/usage/board/${encodeURIComponent(account.account)}`}>
                      {account.account}
                    </Link>
                    <span>
                      {account.mark}
                      {account.totals.cost_usd === null
                        ? t("quota.unknown")
                        : formatNumber(account.totals.cost_usd, {
                            style: "currency",
                            currency: "USD",
                          })}
                    </span>
                  </li>
                ))}
              </ul>
            </section>
          )}
          {view === "today" && usage.isError && (
            <p role="alert">
              {t("today.costFailed")}{" "}
              <Button onClick={() => void usage.refetch()}>{t("board.retry")}</Button>
            </p>
          )}
          {history.isPending && <p role="status">{t("board.loading")}</p>}
          {history.isError && (
            <p role="alert">
              {t("activity.failed")}{" "}
              <Button onClick={() => void history.refetch()}>{t("board.retry")}</Button>
            </p>
          )}
          {!history.isPending && !history.isError && !events.length && (
            <p className="tb-empty">{t("activity.empty")}</p>
          )}
          {[...groups].map(([key, { label, entries }]) => {
            const visible = entries.filter(() => {
              const n = index++;
              return n >= current * 30 && n < current * 30 + 30;
            });
            return visible.length ? (
              <section key={key}>
                <h3>{label}</h3>
                <ul className="tb-activity-list">
                  {visible.map((event) => {
                    const task = byId.get(JSON.stringify([event.source, event.uid]));
                    return (
                      <li key={JSON.stringify([event.source, event.uid, event.id])}>
                        <BoardDate value={event.time} />
                        <div>
                          {task ? (
                            <button type="button" onClick={() => onSelect(task)}>
                              {taskTitle(task)}
                            </button>
                          ) : (
                            <span>{event.uid}</span>
                          )}
                          <p>
                            {event.label ||
                              (t(`history.${event.kind}`) === `history.${event.kind}`
                                ? event.kind
                                : t(`history.${event.kind}`))}
                          </p>
                          {event.note && <p>{event.note}</p>}
                        </div>
                      </li>
                    );
                  })}
                </ul>
              </section>
            ) : null;
          })}
        </>
      )}
      <BoardPager page={current} pages={pages} onChange={setPage} />
    </section>
  );
}
