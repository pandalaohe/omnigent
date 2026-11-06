import { useQuery } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";

export type TaskProgress = "needs-action" | "in-process" | "completed" | "failed" | "cancelled";
export interface BoardTask {
  uid: string;
  title?: string;
  progress: TaskProgress;
  "omnigent.ai:source": string;
  "omnigent.ai:key"?: string;
  "omnigent.ai:group"?: { id: string; name?: string };
  links?: Record<string, { href: string; rel?: string }>;
}
export interface TaskSource {
  id: string;
  name: string | null;
  source: string | null;
}

async function readJson<T>(path: string): Promise<T> {
  const response = await authenticatedFetch(path);
  if (!response.ok) throw new Error(`Task board read failed (${response.status})`);
  return response.json() as Promise<T>;
}

async function readTasks(): Promise<BoardTask[]> {
  const tasks: BoardTask[] = [];
  let cursor: string | undefined;
  do {
    // Each page supplies the cursor needed to request the next one.
    // eslint-disable-next-line no-await-in-loop
    const page = await readJson<{ entries: BoardTask[]; "omnigent.ai:cursor"?: string }>(
      `/v1/me/tasks${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ""}`,
    );
    tasks.push(...page.entries);
    cursor = page["omnigent.ai:cursor"];
  } while (cursor);
  return tasks;
}

export function useTaskBoardData() {
  return useQuery({
    queryKey: ["taskBoard", "list"],
    queryFn: async () => {
      const [tasks, { sources }] = await Promise.all([
        readTasks(),
        readJson<{ sources: TaskSource[] }>("/v1/me/tasks/sources"),
      ]);
      const sourceRows = new Map(sources.map((source) => [source.id, source]));
      for (const task of tasks) {
        const id = task["omnigent.ai:source"];
        if (!sourceRows.has(id)) sourceRows.set(id, { id, name: null, source: null });
      }
      return { tasks, sources: [...sourceRows.values()] };
    },
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
