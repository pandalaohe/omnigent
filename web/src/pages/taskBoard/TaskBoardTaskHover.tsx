import { NetworkIcon } from "lucide-react";
import { taskIdentity, taskProgress, type BoardTask } from "./taskBoardData";
import { taskTitle } from "./TaskBoardDetail";
import { TaskProgressIcon } from "./TaskProgressIcon";
import { formatDate, t } from "./taskBoardText";

export function TaskBoardTaskHover({ task, tasks }: { task: BoardTask; tasks: BoardTask[] }) {
  const display = task["omnigent.ai:sourceDisplay"];
  const state = display?.state;
  const id = taskIdentity(task);
  const byId = new Map(tasks.map((item) => [taskIdentity(item), item]));
  const relations = new Map(
    tasks.flatMap((item) =>
      (item["omnigent.ai:sourceDisplay"]?.relations ?? [])
        .filter(
          (edge) =>
            JSON.stringify([edge.from.source, edge.from.uid]) === id ||
            JSON.stringify([edge.to.source, edge.to.uid]) === id,
        )
        .map((edge) => [JSON.stringify([item["omnigent.ai:source"], edge.id]), edge] as const),
    ),
  );
  const handlers = [
    ...new Set(
      display?.actors?.map((actor) => actor.label || actor.agentId) ??
        task["omnigent.ai:workedBy"]?.flatMap((span) => (span.agent ? [span.agent] : [])) ??
        [],
    ),
  ];
  const session = display?.sessions?.find((item) => item.status !== "archived");
  const current = session?.now ?? display?.current;
  const need = display?.need ?? task["omnigent.ai:waiting"]?.note;
  const last = display?.lastActivity ?? task.updated;
  return (
    <div className="tb-task-hover">
      <header>
        <span className={`tb-task-hover-state ${state ?? "queued"}`}>
          <TaskProgressIcon
            progress={
              state === "done"
                ? "completed"
                : state === "running"
                  ? "in-process"
                  : state === "failed"
                    ? "failed"
                    : taskProgress(task)
            }
            waiting={state === "user" || !!task["omnigent.ai:waiting"]}
          />
        </span>
        <div>
          <b>{state ? t(`sessions.taskState.${state}`) : t(`progress.${taskProgress(task)}`)}</b>
          <small>
            {[
              display?.identity?.key ?? task["omnigent.ai:key"] ?? task.uid,
              display?.identity?.groupLabel ?? task["omnigent.ai:group"]?.name,
            ]
              .filter(Boolean)
              .join(" · ")}
          </small>
        </div>
      </header>
      {(last || handlers.length > 0) && (
        <div className="tb-session-hover-meta">
          {last && (
            <span>
              {t("sessions.lastActivity", {
                time: formatDate(Date.parse(last), { dateStyle: "medium", timeStyle: "short" }),
              })}
            </span>
          )}
          {handlers.length > 0 && (
            <span>{t("sessions.handlers", { names: handlers.join(" · ") })}</span>
          )}
        </div>
      )}
      {need && <p>{need}</p>}
      {current && <p>{current}</p>}
      {task.description && task.description !== current && (
        <p className="tb-task-hover-description">{task.description.slice(0, 96)}</p>
      )}
      {[...relations.entries()].slice(0, 4).map(([key, edge]) => {
        const fromHere = JSON.stringify([edge.from.source, edge.from.uid]) === id;
        const ref = fromHere ? edge.to : edge.from;
        const other = byId.get(JSON.stringify([ref.source, ref.uid]));
        const kind =
          edge.kind === "parent"
            ? fromHere
              ? "childTask"
              : "parentTask"
            : edge.kind === "include"
              ? fromHere
                ? "includes"
                : "includedBy"
              : "dependency";
        return (
          <p key={key} className="tb-task-hover-relation">
            <NetworkIcon size={13} />
            {edge.label || t(`relation.${kind}`)}
            {" · "}
            {other ? taskTitle(other) : ref.uid}
          </p>
        );
      })}
      {!!display?.checks?.length && (
        <div className="tb-task-hover-checks">
          <span>{t("sessions.receipts")}</span>
          {display.checks.map((check) => (
            <span key={check.id} className={`tb-task-check ${check.verdict}`}>
              {check.host} {t(`sessions.check.${check.verdict}`)}
              {" · "}
              {check.count}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}
