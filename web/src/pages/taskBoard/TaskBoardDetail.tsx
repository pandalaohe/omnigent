import type { SourceCache } from "./taskBoardSnapshotData";
import { useQuery } from "@tanstack/react-query";
import { ExternalLinkIcon, XIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Link } from "@/lib/routing";
import {
  readBoardJson,
  safeSourceLink,
  taskIdentity,
  taskProgress,
  isOpenTask,
  type BoardTask,
} from "./taskBoardData";
import { useBoardSessionDetails, type BoardSession } from "./taskBoardNative";
import { taskRelations, taskLocalTime } from "./taskBoardRouteData";
import { formatDate, formatNumber, t } from "./taskBoardText";
import { TaskProgressIcon } from "./TaskProgressIcon";
import { TaskSourceDetails } from "./TaskSourceDetails";
import { TaskBoardActions, taskActions } from "./TaskBoardActions";

export const taskTitle = (task: BoardTask) =>
  task.title || task["omnigent.ai:key"] || task.uid || t("task.untitled");
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

export function TaskBoardDetail({
  task,
  tasks,
  sessions,
  onSelect,
  onClose,
  sourceCache,
}: {
  sourceCache?: SourceCache;
  task: BoardTask;
  tasks: BoardTask[];
  sessions: BoardSession[];
  onSelect: (task: BoardTask) => void;
  onClose: () => void;
}) {
  const query = useQuery({
    queryKey: ["taskBoard", "item", task["omnigent.ai:source"], task.uid, task.updated],
    queryFn: () =>
      readBoardJson<BoardTask>(
        `/v1/me/tasks/item?${new URLSearchParams({ source: task["omnigent.ai:source"], uid: task.uid })}`,
      ),
    enabled: !sourceCache,
    retry: false,
    staleTime: 30_000,
    refetchInterval: () => (document.visibilityState === "visible" ? 60_000 : false),
  });
  const current = sourceCache ? task : (query.data ?? task);
  const actions = taskActions(current["omnigent.ai:actions"]);
  const spans = current["omnigent.ai:workedBy"] ?? [];
  const ids = [...new Set(spans.map((span) => span.session))];
  const details = useBoardSessionDetails(ids);
  const byId = new Map(sessions.map((session) => [session.id, session]));
  details.forEach((detail) => {
    if (detail.data) byId.set(detail.data.id, detail.data);
  });
  const costs = ids
    .map((id) => byId.get(id)?.total_cost_usd)
    .filter((cost): cost is number => typeof cost === "number");
  const cost = costs.reduce((sum, value) => sum + value, 0);
  const links = Object.values(current.links ?? {});
  const edit = safeSourceLink(links.find((link) => link.rel === "alternate")?.href);
  const artifacts = Object.entries(current.links ?? {}).filter(
    ([, link]) => link.rel === "describedby",
  );
  const relations = taskRelations(tasks).filter(
    (edge) =>
      taskIdentity(edge.from) === taskIdentity(task) ||
      (edge.to && taskIdentity(edge.to) === taskIdentity(task)),
  );
  const history = [...(current["omnigent.ai:history"] ?? [])].sort((a, b) =>
    b.time.localeCompare(a.time),
  );
  const waiting = current["omnigent.ai:waiting"];
  return (
    <section className="tb-detail" aria-label={t("detail.title", { title: taskTitle(current) })}>
      <header className="flex items-start gap-3">
        <TaskProgressIcon progress={taskProgress(current)} waiting={!!waiting} />
        <div className="min-w-0 flex-1">
          <p className="mb-1 text-xs font-medium text-muted-foreground">
            {t(`progress.${taskProgress(current)}`)}
            {current["omnigent.ai:sourceState"] && (
              <span className="ml-2">{current["omnigent.ai:sourceState"]}</span>
            )}
          </p>
          <h3 className="break-words text-lg font-semibold">{taskTitle(current)}</h3>
          {current["omnigent.ai:key"] && (
            <code className="text-xs text-muted-foreground">{current["omnigent.ai:key"]}</code>
          )}
        </div>
        <Button variant="ghost" size="icon-sm" onClick={onClose} aria-label={t("detail.close")}>
          <XIcon className="size-4" />
        </Button>
      </header>
      <div className="tb-detail-grid">
        <div className="min-w-0 space-y-5">
          {current["omnigent.ai:resolution"] && (
            <div>
              <h4>{t("detail.resolution")}</h4>
              <p>{current["omnigent.ai:resolution"]}</p>
            </div>
          )}
          {waiting && (
            <div className="tb-wait-block">
              <h4>{t(`waiting.${waiting.on}`)}</h4>
              {waiting.note && <p>{waiting.note}</p>}
              <BoardDate value={waiting.since} />
            </div>
          )}
          {current["omnigent.ai:next"] && (
            <div>
              <h4>{t("detail.next")}</h4>
              <p className="whitespace-pre-wrap break-words">{current["omnigent.ai:next"]}</p>
            </div>
          )}
          {current.description && (
            <div>
              <h4>{t("detail.words")}</h4>
              <p className="whitespace-pre-wrap break-words">{current.description}</p>
            </div>
          )}
          {current.participants && Object.keys(current.participants).length > 0 && (
            <div>
              <h4>{t("detail.people")}</h4>
              <p>
                {Object.values(current.participants)
                  .flatMap((person) => (person.name ? [person.name] : []))
                  .join(" · ")}
              </p>
            </div>
          )}
          {current.keywords && Object.keys(current.keywords).length > 0 && (
            <div>
              <h4>{t("detail.tags")}</h4>
              <p>
                {Object.entries(current.keywords)
                  .filter(([, enabled]) => enabled)
                  .map(([word]) => word)
                  .join(" · ")}
              </p>
            </div>
          )}
          {!!current.recurrenceRules?.length && (
            <p className="text-xs text-muted-foreground">{t("detail.recurring")}</p>
          )}
          {Object.entries(current["omnigent.ai:sourceFields"] ?? {}).map(([key, field]) => (
            <div key={key}>
              <h4>{field.label}</h4>
              <p className="whitespace-pre-wrap break-words">{field.value}</p>
            </div>
          ))}
          {artifacts.length > 0 && (
            <div>
              <h4>{t("detail.artifacts")}</h4>
              <ul className="space-y-1">
                {artifacts.map(([linkId, link]) => {
                  const href = safeSourceLink(link.href);
                  return (
                    <li className="break-words" key={linkId}>
                      {href ? (
                        <a
                          href={href}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="underline underline-offset-2"
                        >
                          {link.title || link.href}
                        </a>
                      ) : (
                        <span>{link.href}</span>
                      )}
                    </li>
                  );
                })}
              </ul>
            </div>
          )}
          <div className="flex flex-wrap items-center gap-2">
            <TaskBoardActions actions={actions} />
            {edit ? (
              <a
                href={edit}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-2 text-sm underline underline-offset-2"
              >
                {t("source.open")}
                <ExternalLinkIcon className="size-3.5" />
              </a>
            ) : actions.length === 0 ? (
              <p className="text-sm text-muted-foreground">{t("source.noPage")}</p>
            ) : null}
          </div>
        </div>
        <div className="min-w-0 space-y-5">
          <dl className="tb-number-tiles">
            {typeof current.percentComplete === "number" && (
              <div>
                <dt>{t("detail.progress")}</dt>
                <dd>
                  {formatNumber(current.percentComplete / 100, {
                    style: "percent",
                    maximumFractionDigits: 0,
                  })}
                </dd>
              </div>
            )}
            {ids.length > 0 && (
              <div>
                <dt>{t("view.sessions")}</dt>
                <dd>{formatNumber(ids.length)}</dd>
              </div>
            )}
            {costs.length > 0 && (
              <div>
                <dt>{t(costs.length === ids.length ? "detail.atMost" : "detail.knownCost")}</dt>
                <dd>{formatNumber(cost, { style: "currency", currency: "USD" })}</dd>
              </div>
            )}
            {history.length > 0 && (
              <div>
                <dt>{t("detail.events")}</dt>
                <dd>{formatNumber(history.length)}</dd>
              </div>
            )}
          </dl>
          <dl className="space-y-2 text-xs text-muted-foreground">
            {current.created && (
              <div>
                <dt>{t("detail.created")}</dt>
                <dd>
                  <BoardDate value={current.created} />
                </dd>
              </div>
            )}
            {(current.progressUpdated || current["omnigent.ai:closedSeen"]) && (
              <div>
                <dt>{t(isOpenTask(current) ? "detail.stateChanged" : "detail.updated")}</dt>
                <dd>
                  <BoardDate
                    value={current.progressUpdated ?? current["omnigent.ai:closedSeen"]}
                    observed={!current.progressUpdated}
                  />
                </dd>
              </div>
            )}
            {current.due && (
              <div>
                <dt>{t("detail.due")}</dt>
                <dd>
                  <BoardDate value={current.due} zone={current.timeZone} />
                </dd>
              </div>
            )}
          </dl>
          {relations.length > 0 && (
            <div>
              <h4>{t("detail.relations")}</h4>
              <ul className="space-y-2">
                {relations.map((edge) => {
                  const target =
                    taskIdentity(edge.from) === taskIdentity(task) ? edge.to : edge.from;
                  return (
                    <li
                      key={JSON.stringify([
                        taskIdentity(edge.from),
                        edge.to ? taskIdentity(edge.to) : edge.uid,
                        edge.kind,
                      ])}
                      className="text-sm"
                    >
                      <span className="mr-2 text-xs text-muted-foreground">
                        {t(
                          edge.kind === "parent"
                            ? taskIdentity(edge.from) === taskIdentity(task)
                              ? "relation.childTask"
                              : "relation.parentTask"
                            : edge.kind === "next"
                              ? taskIdentity(edge.from) === taskIdentity(task)
                                ? "relation.successor"
                                : "relation.predecessor"
                              : taskIdentity(edge.from) === taskIdentity(task)
                                ? "relation.blocking"
                                : "relation.dependsOn",
                        )}
                      </span>
                      {target ? (
                        <button
                          type="button"
                          className="text-left underline underline-offset-2"
                          onClick={() => onSelect(target)}
                        >
                          {target["omnigent.ai:key"] ?? taskTitle(target)}
                        </button>
                      ) : (
                        <span>{t("relation.unresolved", { uid: edge.uid })}</span>
                      )}
                    </li>
                  );
                })}
              </ul>
            </div>
          )}
        </div>
      </div>
      {ids.length > 0 && (
        <div className="mt-5 border-t border-border pt-4">
          <h4>{t("detail.sessions")}</h4>
          <ul className="grid gap-2 sm:grid-cols-2">
            {ids.map((id) => (
              <li key={id}>
                <Link
                  to={`/c/${encodeURIComponent(id)}`}
                  className="block truncate text-sm underline underline-offset-2"
                >
                  {byId.get(id)?.title || id}
                </Link>
              </li>
            ))}
          </ul>
        </div>
      )}
      {query.isError && (
        <div role="status" className="mt-4 text-xs text-muted-foreground">
          {t("detail.historyFailed")}
          <Button size="sm" variant="ghost" onClick={() => void query.refetch()}>
            {t("board.retry")}
          </Button>
        </div>
      )}
      {history.length > 0 && (
        <details className="mt-5 border-t border-border pt-4">
          <summary className="cursor-pointer text-sm font-medium">{t("detail.history")}</summary>
          <ol className="mt-3 space-y-3">
            {history.map((entry) => (
              <li key={entry.id} className="flex flex-wrap items-baseline gap-x-3 text-sm">
                <span className="text-xs text-muted-foreground">
                  <BoardDate value={entry.time} />
                </span>
                <b>{entry.label ?? t(`history.${entry.kind}`)}</b>
                {entry.note && <span className="break-words">{entry.note}</span>}
                {(entry.from || entry.to) && (
                  <span className="break-words">
                    {t("history.change", { from: entry.from ?? "", to: entry.to ?? "" })}
                  </span>
                )}
              </li>
            ))}
          </ol>
        </details>
      )}
      <TaskSourceDetails display={current["omnigent.ai:sourceDisplay"]} />
    </section>
  );
}
