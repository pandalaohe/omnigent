import { readBoardJson, type BoardTask, type TaskHistory } from "./taskBoardData";
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

/** Repository labels identify totals independently of task/source membership. */
export function repositoryCommitTotals(caches: Record<string, SourceCache>, now: number) {
  const today = localDay(now);
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const repositories = new Map<string, number | null>();
  const observed = new Map<string, number | null>();
  let partial = Object.keys(caches).length === 0;
  for (const cache of Object.values(caches)) {
    const days = cache.display.repositoryDailyCommits ?? [];
    if (!days.length) partial = true;
    for (const day of days) {
      repositories.set(day.repository, null);
      if (day.date !== today) continue;
      const compatible =
        new Intl.DateTimeFormat("en", { timeZone: day.timeZone }).resolvedOptions().timeZone ===
          zone && Date.parse(day.collectedAt) <= now;
      const value = compatible ? day.totalCommits : null;
      observed.set(
        day.repository,
        observed.has(day.repository) && observed.get(day.repository) !== value ? null : value,
      );
    }
  }
  for (const [repository, count] of observed) repositories.set(repository, count);
  const rows = [...repositories].map(([repository, count]) => ({ repository, count }));
  rows.sort(
    (a, b) => (b.count ?? -1) - (a.count ?? -1) || a.repository.localeCompare(b.repository),
  );
  const known = rows.filter((row) => row.count !== null);
  const total = known.length ? known.reduce((sum, row) => sum + row.count!, 0) : null;
  return { rows, total, partial: partial || known.length !== rows.length };
}
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
export async function readBoardHistory(params = new URLSearchParams()): Promise<BoardEvent[]> {
  const entries: BoardEvent[] = [],
    seen = new Set<string>();
  for (;;) {
    // eslint-disable-next-line no-await-in-loop
    const page = await readBoardJson<{ entries: BoardEvent[]; cursor?: string }>(
      `/v1/me/tasks/history?${params}`,
    );
    entries.push(...page.entries);
    if (!page.cursor) return entries;
    if (seen.has(page.cursor)) throw new Error("Repeated history cursor");
    seen.add(page.cursor);
    params.set("cursor", page.cursor);
  }
}

export { readLocalClosedCounts } from "./taskBoardClosedCounts";
