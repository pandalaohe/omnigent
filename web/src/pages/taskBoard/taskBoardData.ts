import { useQuery, useQueryClient } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";

export type TaskProgress = "needs-action" | "in-process" | "completed" | "failed" | "cancelled";
export interface BoardTask {
  uid: string;
  title?: string;
  progress?: TaskProgress;
  created?: string;
  updated?: string;
  progressUpdated?: string;
  start?: string;
  due?: string;
  timeZone?: string;
  description?: string;
  percentComplete?: number;
  participants?: Record<
    string,
    { name?: string; roles: Record<string, boolean>; progress?: TaskProgress }
  >;
  keywords?: Record<string, boolean>;
  recurrenceRules?: unknown[];
  "omnigent.ai:source": string;
  "omnigent.ai:key"?: string;
  "omnigent.ai:group"?: { id: string; name?: string };
  "omnigent.ai:next"?: string;
  "omnigent.ai:sourceState"?: string;
  "omnigent.ai:resolution"?: string;
  "omnigent.ai:closedSeen"?: string;
  "omnigent.ai:gone"?: string | boolean | { at: string };
  "omnigent.ai:waiting"?: { on: string; note?: string; since?: string };
  "omnigent.ai:workedBy"?: {
    session: string;
    from: string;
    to?: string;
    agent?: string;
    host?: string;
  }[];
  "omnigent.ai:sourceFields"?: Record<string, { label: string; value: string }>;
  "omnigent.ai:history"?: TaskHistory[];
  relatedTo?: Record<string, { relation: Record<string, boolean> }>;
  links?: Record<string, { href: string; rel?: string; title?: string }>;
}
export interface TaskHistory {
  id: string;
  time: string;
  kind: string;
  label?: string;
  note?: string;
  actor?: string;
  field?: string;
  from?: string;
  to?: string;
}
export interface TaskSource {
  id: string;
  name: string | null;
  source: string | null;
  created?: string;
  last_write?: {
    time: string;
    applied?: number;
    unchanged?: number;
    held?: number;
    gone?: number;
    history_added?: number;
  } | null;
  counts?: { open: number; closed: number; gone: number };
  issues?: {
    code: string;
    path?: string;
    params?: Record<string, string | number>;
    reason?: string;
  }[];
}

export async function readBoardJson<T>(path: string): Promise<T> {
  const response = await authenticatedFetch(path);
  if (!response.ok) throw new Error(`Task board read failed (${response.status})`);
  return response.json() as Promise<T>;
}

interface TaskPage {
  entries: BoardTask[];
  updated?: string;
  "omnigent.ai:cursor"?: string;
  "omnigent.ai:changedThrough"?: string;
}
export interface BoardData {
  tasks: BoardTask[];
  sources: TaskSource[];
  changedThrough?: string;
}
export const taskIdentity = (task: BoardTask) =>
  JSON.stringify([task["omnigent.ai:source"], task.uid]);
export const groupIdentity = (task: BoardTask) =>
  JSON.stringify([task["omnigent.ai:source"], task["omnigent.ai:group"]?.id ?? ""]);
export const taskProgress = (task: BoardTask): TaskProgress => task.progress ?? "needs-action";
export const isOpenTask = (task: BoardTask) =>
  ["needs-action", "in-process"].includes(taskProgress(task));

async function readTasks(
  params: URLSearchParams,
): Promise<{ tasks: BoardTask[]; changedThrough?: string }> {
  const tasks: BoardTask[] = [];
  let cursor: string | undefined;
  let changedThrough: string | undefined;
  const seen = new Set<string>();
  do {
    const query = new URLSearchParams(params);
    if (cursor) query.set("cursor", cursor);
    // eslint-disable-next-line no-await-in-loop
    const page = await readBoardJson<TaskPage>(`/v1/me/tasks?${query}`);
    if (!cursor) changedThrough = page["omnigent.ai:changedThrough"] ?? page.updated;
    tasks.push(...page.entries);
    cursor = page["omnigent.ai:cursor"];
    if (cursor && seen.has(cursor)) throw new Error("Repeated task cursor");
    if (cursor) seen.add(cursor);
  } while (cursor);
  return { tasks, changedThrough };
}

/** Publish one atomic cache update only after every page and recreated source was read. */
export async function readTaskBoard(previous?: BoardData, now = Date.now()): Promise<BoardData> {
  const { sources } = await readBoardJson<{ sources: TaskSource[] }>("/v1/me/tasks/sources");
  const cutoff = new Date(now - 365 * 86_400_000).toISOString();
  const sourceIds = new Set(sources.map((source) => source.id));
  const oldSources = new Map(previous?.sources.map((source) => [source.id, source]));
  const recreated = new Set(
    sources
      .filter(
        (source) =>
          !oldSources.has(source.id) || source.created !== oldSources.get(source.id)?.created,
      )
      .map((source) => source.id),
  );
  const held = new Map(
    previous?.tasks
      .filter(
        (task) =>
          sourceIds.has(task["omnigent.ai:source"]) && !recreated.has(task["omnigent.ai:source"]),
      )
      .map((task) => [taskIdentity(task), task]),
  );
  const merge = (tasks: BoardTask[]) => {
    for (const task of tasks) {
      if (task["omnigent.ai:gone"]) held.delete(taskIdentity(task));
      else held.set(taskIdentity(task), task);
    }
  };
  let changedThrough: string | undefined;
  if (previous?.changedThrough) {
    const result = await readTasks(
      new URLSearchParams({
        changed_after: new Date(Date.parse(previous.changedThrough) - 60_000).toISOString(),
      }),
    );
    merge(result.tasks);
    changedThrough = result.changedThrough;
  }
  const fullSources = previous?.changedThrough ? [...recreated] : [undefined];
  for (const source of fullSources) {
    const params = new URLSearchParams({ state: "open" });
    if (source) params.set("source", source);
    // eslint-disable-next-line no-await-in-loop
    const open = await readTasks(params);
    params.set("state", "closed");
    params.set("closed_after", cutoff);
    // eslint-disable-next-line no-await-in-loop
    const closed = await readTasks(params);
    merge(open.tasks);
    merge(closed.tasks);
    if (!previous?.changedThrough) changedThrough = open.changedThrough;
  }
  const rows = new Map(sources.map((source) => [source.id, source]));
  const tasks = [...held.values()].filter(
    (task) =>
      isOpenTask(task) ||
      (task.progressUpdated ?? task["omnigent.ai:closedSeen"] ?? task.updated ?? cutoff) >= cutoff,
  );
  for (const task of tasks) {
    const id = task["omnigent.ai:source"];
    if (!rows.has(id)) rows.set(id, { id, name: null, source: null });
  }
  return { tasks, sources: [...rows.values()], changedThrough };
}

export function useTaskBoardData() {
  const client = useQueryClient();
  return useQuery({
    queryKey: ["taskBoard", "list"],
    queryFn: () => readTaskBoard(client.getQueryData<BoardData>(["taskBoard", "list"])),
    refetchInterval: () => (document.visibilityState === "visible" ? 60_000 : false),
    refetchOnWindowFocus: true,
    retry: false,
  });
}

export function safeSourceLink(href: string | undefined | null): string | undefined {
  if (!href) return undefined;
  try {
    const url = new URL(href);
    return ["https:", "http:"].includes(url.protocol) ? url.href : undefined;
  } catch {
    return undefined;
  }
}
