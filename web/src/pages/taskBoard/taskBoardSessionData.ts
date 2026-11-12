import { taskIdentity, type BoardTask } from "./taskBoardData";
import { mergeDisplaySession, sessionObservation, type DisplaySession } from "./taskBoardDisplay";
import { sessionTree, type BoardSession } from "./taskBoardNative";
import type { SourceCache } from "./taskBoardSnapshotData";

const colors = [
  "#ee352e",
  "#0091c8",
  "#00933c",
  "#b933ad",
  "#ff6319",
  "#2f7fd4",
  "#d4972a",
  "#00a1a7",
  "#7c5cdb",
  "#df3c85",
  "#6cbe45",
  "#996633",
];
export interface SessionRow {
  session: DisplaySession;
  native?: BoardSession;
  task?: BoardTask;
  color: string;
  asOf?: string;
}

/** Source identity stays source-owned; native observations replace its runtime tuple. */
export function boardSessionRows(
  tasks: BoardTask[],
  native: BoardSession[],
  sourceCaches?: Record<string, SourceCache>,
): SessionRow[] {
  const byId = new Map(native.map((session) => [session.id, session]));
  const tasksById = new Map(tasks.map((task) => [taskIdentity(task), task]));
  const rows = new Map<string, SessionRow>();
  const groups = new Map<string, string>(),
    used = new Map<string, Set<string>>();
  const colorFor = (task?: BoardTask) => {
    if (!task) return "var(--muted-foreground)";
    const source = task["omnigent.ai:source"],
      display = task["omnigent.ai:sourceDisplay"];
    const group = display?.identity?.groupId ?? task["omnigent.ai:group"]?.id ?? "";
    const key = JSON.stringify([source, group]);
    if (groups.has(key)) return groups.get(key)!;
    let hash = 0;
    for (const letter of `${source}/${group}`) hash = (hash * 31 + letter.charCodeAt(0)) >>> 0;
    const taken = used.get(source) ?? new Set<string>();
    // Probe from the hashed slot, preserving distinct groups within a source.
    let result = colors[hash % colors.length];
    for (let offset = 0; offset < colors.length; offset++) {
      const candidate = colors[(hash + offset) % colors.length];
      if (!taken.has(candidate)) {
        result = candidate;
        break;
      }
    }
    taken.add(result);
    used.set(source, taken);
    groups.set(key, result);
    return result;
  };
  for (const task of tasks) colorFor(task);
  for (const task of tasks) {
    for (const source of task["omnigent.ai:sourceDisplay"]?.sessions ?? []) {
      if (rows.has(source.id)) continue;
      const linked = tasksById.get(JSON.stringify([source.task.source, source.task.uid]));
      rows.set(source.id, {
        session: mergeDisplaySession(source, byId.get(source.id)),
        native: byId.get(source.id),
        task: linked,
        color: colorFor(linked),
        asOf: byId.has(source.id)
          ? undefined
          : sourceCaches?.[task["omnigent.ai:source"]]?.display.asOf,
      });
    }
  }
  for (const session of native) {
    const supplied = rows.get(session.id);
    const steps = session.todos?.map((todo, index) => ({
      id: String(index),
      kind: "plan",
      label: todo.content,
      state:
        todo.status === "completed"
          ? ("done" as const)
          : todo.status === "in_progress"
            ? ("current" as const)
            : ("future" as const),
    }));
    if (supplied) {
      supplied.session = { ...supplied.session, steps: supplied.session.steps ?? steps };
      continue;
    }
    rows.set(session.id, {
      session: {
        ...mergeDisplaySession(
          {
            id: session.id,
            task: { source: "", uid: "" },
            role: session.parent_session_id ? "child" : "peer",
            parentId: session.parent_session_id ?? undefined,
            agentId: session.agent_id ?? "",
            label: session.title || session.id,
            title: session.title || session.id,
            host: session.host_id ?? undefined,
            model: session.reported_model ?? undefined,
            status: "idle",
            createdAt: "",
            lastActivityAt: "",
          },
          session,
        ),
        steps,
        now:
          session.todos?.find((todo) => todo.status === "in_progress")?.activeForm ??
          session.last_message_preview ??
          undefined,
        last: session.last_message_preview ?? undefined,
        cost:
          session.total_cost_usd == null
            ? undefined
            : { amount: session.total_cost_usd, kind: "actual" },
      },
      native: session,
      color: colorFor(),
    });
  }
  const nativeOrder = new Map(native.map((session, index) => [session.id, index]));
  return [...rows.values()]
    .map((row, index) => ({ row, index }))
    .sort(
      (a, b) =>
        (nativeOrder.get(a.row.session.id) ?? native.length + a.index) -
        (nativeOrder.get(b.row.session.id) ?? native.length + b.index),
    )
    .map(({ row }) => row);
}

/** The incumbent cycle-safe tree keeps orphaned and cyclic components reachable. */
export function sessionGroups(rows: SessionRow[], asOf: string) {
  const live = rows.filter((row) => row.session.status !== "archived");
  const rank = (row: SessionRow) =>
    ({ waiting: 0, stale: 1, failed: 1, running: 2, idle: 3, archived: 4 })[
      sessionObservation(row.session, row.asOf ?? asOf).state
    ] ?? 3;
  const byId = new Map(live.map((row) => [row.session.id, row]));
  const ordered = live
    .map((row, index) => ({ row, index }))
    .sort((a, b) => rank(a.row) - rank(b.row) || a.index - b.index);
  const tree = sessionTree(
    ordered.map(({ row }) => ({
      id: row.session.id,
      status: row.session.status,
      parent_session_id: row.session.parentId,
    })),
  );
  const groups: { root: SessionRow; children: { row: SessionRow; depth: number }[] }[] = [];
  for (const entry of tree) {
    const row = byId.get(entry.session.id)!;
    if (!entry.depth) groups.push({ root: row, children: [] });
    else groups.at(-1)!.children.push({ row, depth: entry.depth });
  }
  const order = new Map(live.map((row, index) => [row.session.id, index]));
  groups.sort(
    (a, b) =>
      Number(a.root.session.role !== "root") - Number(b.root.session.role !== "root") ||
      order.get(a.root.session.id)! - order.get(b.root.session.id)!,
  );
  return groups;
}

export function archivedToday(rows: SessionRow[], now: number) {
  const day = new Date(now).toDateString();
  return rows.filter(
    ({ session }) =>
      session.status === "archived" &&
      session.archivedAt &&
      new Date(session.archivedAt).toDateString() === day,
  );
}

export function recentSessionFacts(tasks: BoardTask[]) {
  const byId = new Map(tasks.map((task) => [taskIdentity(task), task]));
  const events = tasks.flatMap((task) =>
    (task["omnigent.ai:sourceDisplay"]?.events ?? [])
      .filter((event) => event.kind !== "commit")
      .flatMap((event) => {
        const linked = byId.get(JSON.stringify([event.task.source, event.task.uid]));
        return linked ? [{ task: linked, event }] : [];
      }),
  );
  const quotes = tasks.flatMap((task) =>
    (task["omnigent.ai:sourceDisplay"]?.quotes ?? [])
      .filter((quote) => quote.text.length > 8 && !/^(推荐|按表格)/.test(quote.text))
      .flatMap((quote) => {
        const linked = byId.get(JSON.stringify([quote.task.source, quote.task.uid]));
        return linked ? [{ task: linked, quote }] : [];
      }),
  );
  events.sort(
    (a, b) =>
      b.event.at.replace(/Z$/, "").localeCompare(a.event.at.replace(/Z$/, "")) ||
      (a.event.rank ?? 0) - (b.event.rank ?? 0),
  );
  quotes.sort(
    (a, b) =>
      (b.quote.date ?? b.quote.at ?? "").localeCompare(a.quote.date ?? a.quote.at ?? "") ||
      (a.quote.rank ?? 0) - (b.quote.rank ?? 0),
  );
  return { events: events.slice(0, 7), quotes: quotes.slice(0, 3) };
}
