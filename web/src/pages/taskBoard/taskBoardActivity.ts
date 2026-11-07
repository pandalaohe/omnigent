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

/** The count endpoint takes a fixed offset; split at actual browser-zone transitions. */
export async function readLocalClosedCounts(): Promise<{ days: ClosedDay[] }> {
  const offsetAt = (at: number) => -new Date(at).getTimezoneOffset();
  const initial = await readBoardJson<{ days: ClosedDay[] }>(
    `/v1/me/tasks/closed-counts?offset=${offsetAt(Date.now())}`,
  );
  if (!initial.days.length) return initial;
  const dates = initial.days.map((row) => Date.parse(`${row.day}T00:00:00Z`));
  const start = Math.min(...dates) - 2 * 86_400_000,
    end = Math.max(...dates) + 3 * 86_400_000;
  const segments: { from: number; to: number; offset: number }[] = [];
  let from = start,
    offset = offsetAt(start);
  for (let probe = start + 86_400_000; probe <= end; probe += 86_400_000) {
    if (offsetAt(probe) === offset) continue;
    let low = probe - 86_400_000,
      high = probe;
    while (high - low > 1) {
      const middle = Math.floor((low + high) / 2);
      if (offsetAt(middle) === offset) low = middle;
      else high = middle;
    }
    segments.push({ from, to: high, offset });
    from = high;
    offset = offsetAt(high);
  }
  segments.push({ from, to: end, offset });
  if (segments.length === 1 && segments[0].offset === offsetAt(Date.now())) return initial;
  const totals = new Map<string, ClosedDay>();
  for (const segment of segments) {
    const params = new URLSearchParams({
      from: new Date(segment.from).toISOString().replace(/\.000Z$/, "Z"),
      to: new Date(segment.to).toISOString().replace(/\.000Z$/, "Z"),
      offset: String(segment.offset),
    });
    // eslint-disable-next-line no-await-in-loop
    const { days } = await readBoardJson<{ days: ClosedDay[] }>(
      `/v1/me/tasks/closed-counts?${params}`,
    );
    for (const day of days) {
      const key = JSON.stringify([day.source, day.day]);
      totals.set(key, { ...day, closed: (totals.get(key)?.closed ?? 0) + day.closed });
    }
  }
  return { days: [...totals.values()] };
}
