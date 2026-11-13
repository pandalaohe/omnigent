import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import { useBoardCadence } from "./taskBoardPreferences";
import {
  readBoardJson,
  taskSourceGeneration,
  type BoardTask,
  type TaskHistory,
  type TaskSource,
} from "./taskBoardData";
import type { BoardSession } from "./taskBoardNative";
import type { SourceCache } from "./taskBoardSnapshotData";

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
  after?: string;
  revision: string;
}
const eventKey = (event: BoardEvent) => JSON.stringify([event.source, event.uid, event.id]);
function mergeEvents(a: BoardEvent[], b: BoardEvent[]) {
  return [...new Map([...a, ...b].map((event) => [eventKey(event), event])).values()].sort(
    (left, right) =>
      Date.parse(right.time) - Date.parse(left.time) ||
      eventKey(right).localeCompare(eventKey(left)),
  );
}
export function useBoardHistory(
  enabled = true,
  sourceCaches?: Record<string, SourceCache>,
  sources?: TaskSource[],
) {
  const client = useQueryClient(),
    cadence = useBoardCadence();
  const identities = sourceCaches
    ? Object.entries(sourceCaches).map(([id, source]) => ({
        id,
        generation: source.pin.sourceGeneration,
        publication: source.pin.id,
        revision: source.display.asOf,
      }))
    : (sources ?? []).map((source) => ({
        id: source.id,
        generation: taskSourceGeneration(source),
        publication: source.snapshot?.id ?? null,
        revision: source.last_write?.time ?? source.created ?? "",
      }));
  identities.sort((a, b) => a.id.localeCompare(b.id));
  const generation = JSON.stringify(identities.map((source) => [source.id, source.generation]));
  const revision = JSON.stringify(
    identities.map((source) => [source.id, source.publication, source.revision]),
  );
  const key = ["taskBoard", "windowedHistory", generation];
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
        // Source revisions also catch commits delayed beyond the timestamp overlap.
        const reconcile = (!sourceCaches && !sources) || previous.revision !== revision;
        const priorRevisions = new Map<string, [string | null, string]>(
          JSON.parse(previous.revision).map(
            ([id, publication, stamp]: [string, string | null, string]) => [
              id,
              [publication, stamp],
            ],
          ),
        );
        const changedSources = identities.filter(
          (source) =>
            JSON.stringify(priorRevisions.get(source.id)) !==
            JSON.stringify([source.publication, source.revision]),
        );
        const replacedSources = new Set<string>();
        const params = new URLSearchParams();
        if (!reconcile)
          params.set(
            "changed_after",
            new Date(Date.parse(previous.through) - 60_000).toISOString(),
          );
        if (previous.after) params.set("after", previous.after);
        const watermark = (value: string) => {
          through = value;
        };
        const updates = changedSources.length
          ? (
              await Promise.all(
                changedSources.map((source) => {
                  const scoped = new URLSearchParams(params);
                  scoped.set("source", source.id);
                  const prior = priorRevisions.get(source.id);
                  if (
                    source.publication === null &&
                    prior?.[0] === null &&
                    Number.isFinite(Date.parse(prior[1]))
                  ) {
                    scoped.set(
                      "changed_after",
                      new Date(Date.parse(prior[1]) - 60_000).toISOString(),
                    );
                  } else replacedSources.add(source.id);
                  return readBoardHistory(scoped, watermark);
                }),
              )
            ).flat()
          : await readBoardHistory(params, watermark);
        const latest = client.getQueryData<HistoryCache>(key) ?? previous;
        const retained = reconcile
          ? latest.entries.filter(
              (entry) =>
                (changedSources.length > 0 && !replacedSources.has(entry.source)) ||
                (previous.after && Date.parse(entry.time) < Date.parse(previous.after)),
            )
          : latest.entries;
        return { ...latest, entries: mergeEvents(retained, updates), through, revision };
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
        after,
        revision,
      };
    },
  });
  const cachedRevision = query.data?.revision;
  const refetch = query.refetch;
  useEffect(() => {
    if (enabled && cachedRevision !== undefined && cachedRevision !== revision) void refetch();
  }, [enabled, revision, cachedRevision, refetch]);
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
      const after = params.has("after")
        ? previous.after
        : page.cursor
          ? (page.entries.at(-1)?.time ?? previous.after)
          : undefined;
      if (page.cursor) params.set("cursor", page.cursor);
      else if (params.has("after")) {
        params.set("before", params.get("after")!);
        params.delete("after");
        params.delete("cursor");
      } else params.set("done", "1");
      client.setQueryData<HistoryCache>(key, (latest = previous) => ({
        ...latest,
        entries: mergeEvents(latest.entries, page.entries),
        earlier: params.toString(),
        after,
      }));
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
