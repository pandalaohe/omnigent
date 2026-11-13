import type { BoardTask } from "./taskBoardData";
import { t } from "./taskBoardText";

const looksInternal = (value: string) =>
  /^kit[-_]\S+$/i.test(value) ||
  /^\S+@[0-9a-f]{8,}$/i.test(value) ||
  /^[0-9a-f-]{16,}$/i.test(value);

export const activityName = (...values: (string | null | undefined)[]) =>
  values.find(
    (value): value is string =>
      typeof value === "string" && Boolean(value.trim()) && !looksInternal(value.trim()),
  );

export const activityTaskName = (task: BoardTask) =>
  activityName(
    task["omnigent.ai:sourceDisplay"]?.identity?.label,
    task.title,
    task["omnigent.ai:key"],
  ) ?? t("g6.untitledTask");

export const activityTaskKey = (task: BoardTask) => {
  return activityName(task["omnigent.ai:sourceDisplay"]?.identity?.key, task["omnigent.ai:key"]);
};

export const activityAgentName = (id?: string | null, label?: string | null) => {
  const name = activityName(label);
  if (name) return name;
  if (!id) return t("g6.unassigned");
  const known: Record<string, string> = {
    pi: t("g6.agent.pi"),
    codex: t("g6.agent.codex"),
    claude: t("g6.agent.claude"),
    antigravity: t("g6.agent.antigravity"),
  };
  return known[id] ?? known[id.split("@")[0]] ?? t("g6.agent.unknown");
};

export const activitySourceName = (
  source: string,
  _task?: BoardTask,
  sourceLabels?: Record<string, string>,
) => {
  return activityName(sourceLabels?.[source]) ?? t("g6.taskSource");
};

export const activityFieldName = (field: string, task?: BoardTask) =>
  task?.["omnigent.ai:sourceFields"]?.[field]?.label ??
  (
    {
      assignee: "Assignee",
      field: "Fields",
      other: "Other",
      progress: "Status",
      status: "Status",
      description: "Description",
      artifact: "Artifact",
      parent: "Parent",
      title: "Title",
    } as Record<string, string>
  )[field] ??
  t("g6.event.field");

export const activityEventLabel = (kind: string, label?: string, note?: string) => {
  if (label || note) return label ?? note;
  const key = `g6.event.${kind}`;
  const translated = t(key);
  return translated === key ? t("g6.event.other") : translated;
};

export const isBlankDetailValue = (value: string | null | undefined) =>
  !value?.trim() || ["—", "-", "not run", "none", "n/a"].includes(value.trim().toLowerCase());

export const activityHistoryValue = (value: string) =>
  value
    .replace(/\bkit-[\w-]+/gi, t("g6.taskSource"))
    .replace(/@[0-9a-f]{8,}/gi, "")
    .replace(
      /\b[0-9a-f]{16,}\b|\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b/gi,
      t("g6.recordedReference"),
    );
