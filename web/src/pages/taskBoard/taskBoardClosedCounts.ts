import { readBoardJson } from "./taskBoardRead";
import type { ClosedDay } from "./taskBoardActivityData";

/** The count endpoint takes a fixed offset; split at actual browser-zone transitions. */
export async function readLocalClosedCounts(
  filters = new URLSearchParams(),
): Promise<{ days: ClosedDay[] }> {
  const offsetAt = (at: number) => -new Date(at).getTimezoneOffset();
  const initialQuery = new URLSearchParams(filters);
  initialQuery.set("offset", String(offsetAt(Date.now())));
  const initial = await readBoardJson<{ days: ClosedDay[] }>(
    `/v1/me/tasks/closed-counts?${initialQuery}`,
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
      ...Object.fromEntries(filters),
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
