import type { BoardTask } from "./taskBoardData";
import { taskIdentity, taskProgress } from "./taskBoardData";
import { mergeDisplaySession, sessionObservation, type DisplaySession } from "./taskBoardDisplay";
import type { BoardSession } from "./taskBoardNative";
import { t } from "./taskBoardText";

export interface AttentionRow {
  id: string;
  text: string;
  caption: string;
  session?: DisplaySession;
  task?: BoardTask;
}
/** Source identity is explicit; native observations replace the runtime tuple as a unit. */
export function boardAttention(
  tasks: BoardTask[],
  native: BoardSession[],
  silent: Set<string>,
  now = Date.now(),
) {
  const nativeById = new Map(native.map((session) => [session.id, session]));
  const supplied = new Map<string, DisplaySession>();
  for (const task of tasks)
    for (const session of task["omnigent.ai:sourceDisplay"]?.sessions ?? [])
      if (!supplied.has(session.id))
        supplied.set(session.id, mergeDisplaySession(session, nativeById.get(session.id)));
  for (const session of native)
    if (!supplied.has(session.id))
      supplied.set(
        session.id,
        mergeDisplaySession(
          {
            id: session.id,
            task: { source: "", uid: "" },
            role: "peer",
            agentId: "",
            label: session.title || session.id,
            title: session.title || session.id,
            status: "idle",
            createdAt: "",
            lastActivityAt: "",
          },
          session,
        ),
      );
  const waiting: AttentionRow[] = [],
    anomalies: AttentionRow[] = [],
    running: AttentionRow[] = [];
  const asOf = new Date(now).toISOString();
  for (const session of supplied.values()) {
    const caption = [session.title, session.host].filter(Boolean).join(" · ");
    for (const question of session.questions ?? [])
      waiting.push({ id: `${session.id}:q:${question.id}`, text: question.text, caption, session });
    if (session.need)
      waiting.push({ id: `${session.id}:need`, text: session.need, caption, session });
    const observation = sessionObservation(session, asOf);
    if (session.status !== "archived") {
      const stale = nativeById.has(session.id)
        ? silent.has(session.id)
        : observation.state === "stale";
      if (stale)
        anomalies.push({
          id: `${session.id}:silent`,
          text: t("sessions.silent"),
          caption,
          session,
        });
      if (
        session.context != null &&
        session.compactAt != null &&
        session.compactAt > 0 &&
        session.context / session.compactAt >= 0.9
      )
        anomalies.push({
          id: `${session.id}:context`,
          text: t("band.context", {
            percent: Math.round((session.context / session.compactAt) * 100),
          }),
          caption,
          session,
        });
      if (session.nativeStatus === "failed")
        anomalies.push({ id: `${session.id}:failed`, text: t("session.failed"), caption, session });
      if (session.status === "running" && !stale)
        running.push({
          id: session.id,
          text: session.tool?.label || session.now || session.title,
          caption,
          session,
        });
    }
  }
  for (const task of tasks) {
    const display = task["omnigent.ai:sourceDisplay"];
    const caption = display?.identity?.label || task.title || task.uid;
    if (
      task["omnigent.ai:waiting"]?.on === "user" &&
      !waiting.some(
        (row) =>
          row.session?.task.source === task["omnigent.ai:source"] &&
          row.session?.task.uid === task.uid,
      )
    )
      waiting.push({
        id: `${taskIdentity(task)}:wait`,
        text: task["omnigent.ai:waiting"]?.note || display?.need || caption,
        caption,
        task,
      });
    if (taskProgress(task) === "failed" || display?.state === "failed")
      anomalies.push({
        id: `${taskIdentity(task)}:failed`,
        text: display?.need || task.description || caption,
        caption,
        task,
      });
  }
  return {
    waiting,
    anomalies,
    running,
    idle: [...supplied.values()].filter((session) => session.status === "idle").length,
  };
}
