import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useBoardCadence } from "./taskBoardPreferences";
import { readBoardJson, type BoardTask, type TaskHistory } from "./taskBoardData";
import type { BoardSession } from "./taskBoardNative";

export interface BoardEvent extends TaskHistory {
  source: string;
  uid: string;
}
export interface ClosedDay {
  day: string;
  source: string;
  closed: number;
}
export const localDay = (value: string | number | Date) => {
  const date = new Date(value);
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
};
export function eventMother(
  event: BoardEvent,
  task: BoardTask | undefined,
  sessions: BoardSession[],
) {
  const coveringSpan = task?.["omnigent.ai:workedBy"]?.find(
    (span) =>
      Date.parse(span.from) <= Date.parse(event.time) &&
      (!span.to || Date.parse(span.to) >= Date.parse(event.time)),
  );
  const byId = new Map(sessions.map((session) => [session.id, session]));
  let id = coveringSpan?.session;
  const seen = new Set<string>();
  while (id && !seen.has(id)) {
    seen.add(id);
    const parent = byId.get(id)?.parent_session_id;
    if (!parent) return { id, label: byId.get(id)?.title || id };
    id = parent;
  }
  return undefined;
}
export async function readBoardHistory(
  params = new URLSearchParams(),
  watermark?: (value: string) => void,
): Promise<BoardEvent[]> {
  const entries: BoardEvent[] = [],
    seen = new Set<string>();
  for (;;) {
    // eslint-disable-next-line no-await-in-loop
    const page = await readBoardJson<{
      entries: BoardEvent[];
      cursor?: string;
      changedThrough?: string;
    }>(`/v1/me/tasks/history?${params}`);
    entries.push(...page.entries);
    if (!seen.size && page.changedThrough) watermark?.(page.changedThrough);
    if (!page.cursor) return entries;
    if (seen.has(page.cursor)) throw new Error("Repeated history cursor");
    seen.add(page.cursor);
    params.set("cursor", page.cursor);
  }
}

export { readLocalClosedCounts } from "./taskBoardClosedCounts";

interface HistoryCache {
  entries: BoardEvent[];
  through: string;
  earlier: string;
}
const eventKey = (event: BoardEvent) => JSON.stringify([event.source, event.uid, event.id]);
function mergeEvents(a: BoardEvent[], b: BoardEvent[]) {
  return [...new Map([...a, ...b].map((event) => [eventKey(event), event])).values()].sort(
    (left, right) =>
      Date.parse(right.time) - Date.parse(left.time) ||
      eventKey(right).localeCompare(eventKey(left)),
  );
}
export function useBoardHistory(enabled = true) {
  const client = useQueryClient(),
    cadence = useBoardCadence();
  const key = ["taskBoard", "windowedHistory"];
  const query = useQuery({
    queryKey: key,
    enabled,
    retry: false,
    staleTime: 60_000,
    refetchInterval: cadence,
    queryFn: async (): Promise<HistoryCache> => {
      const previous = client.getQueryData<HistoryCache>(key);
      let through = new Date().toISOString();
      if (previous) {
        const updates = await readBoardHistory(
          new URLSearchParams({
            changed_after: new Date(Date.parse(previous.through) - 60_000).toISOString(),
            after:
              previous.entries.at(-1)?.time ?? new Date(Date.now() - 3 * 86_400_000).toISOString(),
          }),
          (value) => {
            through = value;
          },
        );
        const latest = client.getQueryData<HistoryCache>(key) ?? previous;
        return { ...latest, entries: mergeEvents(latest.entries, updates), through };
      }
      const after = new Date(Date.now() - 3 * 86_400_000).toISOString();
      const params = new URLSearchParams({ after, limit: "500" });
      const page = await readBoardJson<{
        entries: BoardEvent[];
        cursor?: string;
        changedThrough?: string;
      }>(`/v1/me/tasks/history?${params}`);
      if (page.cursor) params.set("cursor", page.cursor);
      else {
        params.delete("after");
        params.set("before", after);
      }
      return {
        entries: page.entries,
        through: page.changedThrough ?? through,
        earlier: params.toString(),
      };
    },
  });
  const earlier = useQuery({
    queryKey: [...key, "earlier", query.data?.earlier],
    enabled: false,
    retry: false,
    queryFn: async () => {
      const previous = client.getQueryData<HistoryCache>(key)!;
      const params = new URLSearchParams(previous.earlier);
      const page = await readBoardJson<{ entries: BoardEvent[]; cursor?: string }>(
        `/v1/me/tasks/history?${params}`,
      );
      if (page.cursor) params.set("cursor", page.cursor);
      else if (params.has("after")) {
        params.set("before", params.get("after")!);
        params.delete("after");
        params.delete("cursor");
      } else params.set("done", "1");
      client.setQueryData<HistoryCache>(key, {
        ...previous,
        entries: mergeEvents(previous.entries, page.entries),
        earlier: params.toString(),
      });
      return true;
    },
  });
  return {
    ...query,
    events: query.data?.entries ?? [],
    hasEarlier: !!query.data && !new URLSearchParams(query.data.earlier).has("done"),
    loadEarlier: earlier.refetch,
    loadingEarlier: earlier.isFetching,
    earlierError: earlier.isError,
  };
}
