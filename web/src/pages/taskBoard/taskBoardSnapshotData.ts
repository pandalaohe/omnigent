import type { BoardData, BoardTask, TaskSource } from "./taskBoardData";
import { readBoardJson } from "./taskBoardRead";
import { readLocalClosedCounts } from "./taskBoardClosedCounts";
import type { BoardEvent, ClosedDay } from "./taskBoardActivityData";

export interface SourcePin {
  sourceGeneration: string;
  id: string | null;
  asOf: string | null;
  phase: "legacy" | "staging" | "published";
}
export interface SourceDisplay {
  version: 1;
  asOf: string;
  timeZone: string;
  identity?: { label: string; badge?: string; rank?: number };
  groups?: { id: string; label: string; badge?: string; rank?: number }[];
  checkHosts?: string[];
  accounts?: { ref: AccountRef; label: string; authoritySource: string; timeZone: string }[];
  future?: { id: string; label: string; at: string; sourceLabel: string }[];
  closedDays?: { date: string; count: number }[];
  accountDays?: {
    id: string;
    account: AccountRef;
    date: string;
    amount: number | null;
    kind: "estimate";
    sourceLabel: string;
  }[];
  releases?: { id: string; name: string; publishedAt: string; source: string }[];
}
export interface AccountRef {
  namespace: string;
  id: string;
}
export interface SourceCache {
  timeZone?: string;
  pin: SourcePin;
  display: SourceDisplay;
  tasks: BoardTask[];
  history: BoardEvent[];
  counts: ClosedDay[];
}

async function pages<T>(path: string, params: URLSearchParams, cursorKey = "cursor") {
  const entries: T[] = [],
    seen = new Set<string>();
  for (;;) {
    // eslint-disable-next-line no-await-in-loop
    const page = await readBoardJson<{
      entries: T[];
      cursor?: string;
      "omnigent.ai:cursor"?: string;
    }>(`${path}?${params}`);
    entries.push(...page.entries);
    const cursor = cursorKey === "cursor" ? page.cursor : page["omnigent.ai:cursor"];
    if (!cursor) return entries;
    if (seen.has(cursor)) throw new Error("Repeated source cursor");
    seen.add(cursor);
    params.set("cursor", cursor);
  }
}

/** Replace a source only after every required read completes against its generation. */
export async function readPinnedSources(
  sources: TaskSource[],
  previous?: BoardData,
  now = Date.now(),
): Promise<BoardData> {
  const caches: Record<string, SourceCache> = {},
    states: Record<string, string> = {};
  const tasks: BoardTask[] = [];
  const timeZone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const cutoff = new Date(now - 365 * 86400000).toISOString();
  for (const source of sources) {
    const pin = source.snapshot;
    const prior = previous?.sourceCaches?.[source.id];
    const compatible =
      prior && prior.pin.sourceGeneration === pin?.sourceGeneration && prior.timeZone === timeZone;
    if (!pin || pin.phase === "legacy") {
      const held: BoardTask[] = [];
      for (const state of ["open", "closed"]) {
        const query = new URLSearchParams({ source: source.id, state, include_history: "1" });
        if (state === "closed") query.set("closed_after", cutoff);
        // eslint-disable-next-line no-await-in-loop
        held.push(...(await pages<BoardTask>("/v1/me/tasks", query, "omnigent.ai:cursor")));
      }
      const query = new URLSearchParams({ source: source.id });
      // eslint-disable-next-line no-await-in-loop
      const history = await pages<BoardEvent>("/v1/me/tasks/history", new URLSearchParams(query));
      // eslint-disable-next-line no-await-in-loop
      const counts = await readLocalClosedCounts(new URLSearchParams(query));
      caches[source.id] = {
        timeZone,
        pin: pin ?? {
          sourceGeneration: source.created ?? source.id,
          id: null,
          asOf: null,
          phase: "legacy",
        },
        display: { version: 1, asOf: new Date(now).toISOString(), timeZone: "UTC" },
        tasks: held,
        history,
        counts: counts.days,
      };
      tasks.push(...held);
      continue;
    }
    if (compatible && (pin.phase === "staging" || pin.id === prior.pin.id)) {
      caches[source.id] = prior;
      tasks.push(...prior.tasks);
      if (pin.phase === "staging") states[source.id] = "updating";
      continue;
    }
    if (pin.phase === "staging" || !pin.id) {
      states[source.id] = "updating";
      continue;
    }
    const query = new URLSearchParams({
      source: source.id,
      snapshot: pin.id,
      source_generation: pin.sourceGeneration,
    });
    const path = `/v1/me/tasks/sources/${encodeURIComponent(source.id)}/display`;
    try {
      // eslint-disable-next-line no-await-in-loop
      const identity = await readBoardJson<{ display: SourceDisplay; snapshot: SourcePin }>(
        `${path}?${query}`,
      );
      const held: BoardTask[] = [];
      for (const state of ["open", "closed"]) {
        const filters = new URLSearchParams(query);
        filters.set("state", state);
        filters.set("include_history", "1");
        if (state === "closed") filters.set("closed_after", cutoff);
        // eslint-disable-next-line no-await-in-loop
        held.push(...(await pages<BoardTask>("/v1/me/tasks", filters, "omnigent.ai:cursor")));
      }
      const display = identity.display;
      for (const part of ["future", "closedDays", "accountDays", "releases"] as const) {
        const filters = new URLSearchParams(query);
        filters.set("part", part);
        // eslint-disable-next-line no-await-in-loop
        Object.assign(display, { [part]: await pages<unknown>(path, filters) });
      }
      // eslint-disable-next-line no-await-in-loop
      const history = await pages<BoardEvent>("/v1/me/tasks/history", new URLSearchParams(query));
      // eslint-disable-next-line no-await-in-loop
      const counts = await readLocalClosedCounts(new URLSearchParams(query));
      // A generation change between the final two reads invalidates the whole assembly.
      // eslint-disable-next-line no-await-in-loop
      await readBoardJson(`${path}?${query}`);
      caches[source.id] = { pin, timeZone, display, tasks: held, history, counts: counts.days };
      tasks.push(...held);
    } catch (error) {
      if (compatible) {
        caches[source.id] = prior;
        tasks.push(...prior.tasks);
      }
      states[source.id] = error instanceof Error ? error.message : "unavailable";
    }
  }
  return { tasks, sources, sourceCaches: caches, sourceStates: states };
}

/** Account/date amounts are owned by explicit authorities, never display labels. */
export function accountDayAmounts(caches: Record<string, SourceCache>) {
  const declarations = new Map<string, { authority: string; zone: string; conflict: boolean }>();
  for (const cache of Object.values(caches))
    for (const account of cache.display.accounts ?? []) {
      const key = JSON.stringify([account.ref.namespace, account.ref.id]);
      const old = declarations.get(key);
      declarations.set(key, {
        authority: account.authoritySource,
        zone: account.timeZone,
        conflict:
          !!old &&
          (old.conflict ||
            old.authority !== account.authoritySource ||
            old.zone !== account.timeZone),
      });
    }
  const result = new Map<string, number | null>();
  for (const [source, cache] of Object.entries(caches))
    for (const day of cache.display.accountDays ?? []) {
      const ref = JSON.stringify([day.account.namespace, day.account.id]);
      const declaration = declarations.get(ref);
      if (!declaration || (!declaration.conflict && declaration.authority !== source)) continue;
      const key = JSON.stringify([day.account.namespace, day.account.id, day.date]);
      const value = declaration.conflict ? null : day.amount;
      result.set(key, result.has(key) && result.get(key) !== value ? null : value);
    }
  return result;
}

export function latestRelease(caches: Record<string, SourceCache>) {
  return Object.values(caches)
    .flatMap((cache) => cache.display.releases ?? [])
    .sort(
      (a, b) =>
        compareText(utcSortKey(b.publishedAt), utcSortKey(a.publishedAt)) ||
        compareText(a.source, b.source) ||
        compareText(a.id, b.id),
    )[0];
}

const utcSortKey = (value: string) => (value.endsWith("Z") ? value.slice(0, -1) : value);
const compareText = (a: string, b: string) => (a < b ? -1 : a > b ? 1 : 0);
export function sourceHistory(caches: Record<string, SourceCache>): BoardEvent[] {
  return Object.values(caches)
    .flatMap((cache) => cache.history)
    .sort(
      (a, b) =>
        compareText(utcSortKey(b.time), utcSortKey(a.time)) ||
        compareText(b.source, a.source) ||
        compareText(b.uid, a.uid) ||
        compareText(b.id, a.id),
    );
}
