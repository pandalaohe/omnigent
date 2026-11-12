import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";
import { TaskBoardSessions } from "./TaskBoardSessions";
import {
  archivedToday,
  boardSessionRows,
  recentSessionFacts,
  sessionGroups,
} from "./taskBoardSessionData";
import type { BoardTask } from "./taskBoardData";
import type { BoardSession } from "./taskBoardNative";
import type { DisplaySession } from "./taskBoardDisplay";

const now = Date.parse("2026-11-12T04:00:00Z"),
  asOf = new Date(now).toISOString();
const fetcher = vi.hoisted(() => vi.fn());
vi.mock("@/lib/identity", () => ({ authenticatedFetch: fetcher }));
const clients: QueryClient[] = [];
afterEach(() => {
  cleanup();
  clients.forEach((client) => client.clear());
  clients.length = 0;
  vi.restoreAllMocks();
});
const session = (id: string, change: Partial<DisplaySession> = {}): DisplaySession => ({
  id,
  task: { source: "source", uid: "task" },
  role: "root",
  agentId: "codex",
  label: id,
  title: `Title ${id}`,
  status: "running",
  createdAt: asOf,
  lastActivityAt: asOf,
  idleSeconds: 0,
  ...change,
});
const task = (sessions: DisplaySession[]): BoardTask => ({
  uid: "task",
  title: "Linked task",
  "omnigent.ai:source": "source",
  "omnigent.ai:sourceDisplay": {
    version: 1,
    identity: { key: "ENG-1", label: "Linked task", groupId: "work", groupBadge: "W" },
    sessions,
  },
});
function mount(tasks: BoardTask[], sessions: BoardSession[] = []) {
  vi.spyOn(Date, "now").mockReturnValue(now);
  fetcher.mockImplementation(async (path: string) =>
    Response.json(
      path.includes("/sessions/")
        ? sessions.find((row) => path.includes(`/sessions/${row.id}?`))
        : tasks[0],
    ),
  );
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  clients.push(client);
  const select = vi.fn(),
    view = vi.fn();
  const rendered = render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <div className="tb-page">
          <TaskBoardSessions tasks={tasks} sessions={sessions} onSelect={select} onView={view} />
        </div>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...rendered, select, view };
}

it("joins only explicit source/task ids and preserves supplied identity, steps and estimated cost", () => {
  const supplied = session("native", {
    now: "Source work",
    need: "Old need",
    steps: [],
    cost: { amount: 1.234, kind: "estimate", sourceLabel: "Token transcript" },
  });
  const other = { ...task([]), "omnigent.ai:source": "other", title: "Wrong task" };
  const rows = boardSessionRows(
    [task([supplied]), other],
    [
      {
        id: "native",
        status: "idle",
        title: "Native title",
        updated_at: now / 1000,
        total_cost_usd: 99,
        todos: [{ content: "Native step", status: "completed" }],
      },
    ],
  );
  expect(rows).toHaveLength(1);
  expect(rows[0].task?.title).toBe("Linked task");
  expect(rows[0].session).toMatchObject({
    label: "native",
    now: "Source work",
    status: "idle",
    steps: [],
    cost: { amount: 1.234, kind: "estimate" },
  });
  expect(rows[0].session.need).toBeUndefined();
  const missing = boardSessionRows(
    [task([session("external", { task: { source: "missing", uid: "task" } })])],
    [],
  );
  expect(missing[0].task).toBeUndefined();
  const waiting = boardSessionRows(
    [task([supplied])],
    [
      {
        id: "native",
        status: "waiting",
        pending_elicitations_count: 1,
        questions: [{ id: "approval", text: "Approve this change?" }],
      },
    ],
  )[0].session;
  expect(waiting.questions).toHaveLength(1);
  expect(waiting.need).toBeUndefined();
});
it("preserves mother/peer order and ranks children while retaining orphans and cycles", () => {
  const sessions = [
    session("root"),
    session("idle", { role: "child", parentId: "root", status: "idle" }),
    session("running", { role: "child", parentId: "root" }),
    session("waiting", { role: "child", parentId: "root", need: "Approval" }),
    session("orphan", { role: "child", parentId: "absent" }),
    session("cycle-a", { role: "child", parentId: "cycle-b" }),
    session("cycle-b", { role: "child", parentId: "cycle-a" }),
  ];
  const groups = sessionGroups(boardSessionRows([task(sessions)], []), asOf);
  expect(groups[0].root.session.id).toBe("root");
  expect(groups[0].children.map(({ row }) => row.session.id)).toEqual([
    "waiting",
    "running",
    "idle",
  ]);
  expect(
    groups
      .flatMap((group) => [group.root, ...group.children.map(({ row }) => row)])
      .map((row) => row.session.id)
      .sort(),
  ).toEqual(sessions.map((row) => row.id).sort());
});
it("shows only local-today archives, with no guessed time for unknown archives", () => {
  const rows = boardSessionRows(
    [
      task([
        session("today", { status: "archived", archivedAt: asOf }),
        session("old", { status: "archived", archivedAt: "2020-01-01T00:00:00Z" }),
        session("unknown", { status: "archived" }),
      ]),
    ],
    [],
  );
  expect(archivedToday(rows, now).map((row) => row.session.id)).toEqual(["today"]);
});
it("selects seven non-commit events and three eligible quotes by their supplied task refs", () => {
  const source = task([]),
    display = source["omnigent.ai:sourceDisplay"]!;
  display.events = Array.from({ length: 10 }, (_, index) => ({
    id: `event-${index}`,
    task: { source: "source", uid: "task" },
    kind: index === 9 ? "commit" : "start",
    at: new Date(now + index * 1000).toISOString(),
  }));
  display.quotes = Array.from({ length: 6 }, (_, index) => ({
    id: `quote-${index}`,
    task: { source: "source", uid: "task" },
    text: index === 5 ? "推荐 a decision to ignore" : `A decision long enough ${index}`,
    date: `2026-11-0${index + 1}`,
  }));
  display.quotes.push({
    id: "unknown",
    task: { source: "absent", uid: "task" },
    date: "2026-12-01",
    text: "Never attach to the containing task",
  });
  const facts = recentSessionFacts([source]);
  expect(facts.events.map(({ event }) => event.id)).toEqual([
    "event-8",
    "event-7",
    "event-6",
    "event-5",
    "event-4",
    "event-3",
    "event-2",
  ]);
  expect(facts.quotes.map(({ quote }) => quote.id)).toEqual(["quote-4", "quote-3", "quote-2"]);
});
it("opens source-only rows inline, keeps native exits separate, and returns focus on close", async () => {
  const source = task([
    session("external", {
      next: "Verify the recovery path",
      context: 0.42,
      cost: { amount: 1.234, kind: "estimate", sourceLabel: "Transcript" },
    }),
  ]);
  const native = [{ id: "native", title: "Native row", status: "running", updated_at: now / 1000 }];
  const { container } = mount([source], native);
  const row = screen.getByRole("button", { name: "Session details: external" });
  expect(within(row).queryByRole("link")).toBeNull();
  expect(within(row).getByText("$1.23").getAttribute("title")).toContain("Transcript");
  fireEvent.mouseEnter(row, { clientX: 100, clientY: 100 });
  expect(screen.getByRole("tooltip").textContent).toContain("Verify the recovery path");
  expect(screen.getByRole("tooltip").textContent).toContain("42%");
  fireEvent.click(row);
  const details = await screen.findByRole("region", { name: "Details: Linked task" });
  expect(row.parentElement?.nextElementSibling).toBe(details);
  fireEvent.click(within(details).getByRole("button", { name: "Close" }));
  expect(document.activeElement).toBe(row);
  const link = screen.getByRole("link", { name: "Open session: Native row" });
  expect(link.getAttribute("href")).toBe("/c/native");
  expect(container.querySelectorAll(".tb-session-line.is-open")).toHaveLength(0);
});
it("compacts more than eight steps, omits absent rails and uses source/native step precedence", () => {
  const steps = Array.from({ length: 10 }, (_, index) => ({
    id: `s${index}`,
    label: `Step ${index}`,
    kind: "code",
    state: index === 4 ? ("current" as const) : ("future" as const),
  }));
  mount([task([session("many", { steps }), session("none")])]);
  const many = screen.getByRole("button", { name: "Session details: many" }),
    none = screen.getByRole("button", { name: "Session details: none" });
  expect(many.querySelectorAll(".tb-session-station")).toHaveLength(1);
  expect(many.textContent).toContain("Step 4 · 5/10");
  expect(none.querySelector(".tb-session-track")).toBeNull();
  const step = within(many).getByRole("button", { name: /Step 4 · Step 5 of 10/ });
  fireEvent.focus(step);
  expect(screen.getByRole("tooltip").textContent).toContain("Step 5 of 10");
});
it("pages six roots, eight children and ten archives without stranding the last page", () => {
  const supplied = [
    ...Array.from({ length: 7 }, (_, index) => session(`root-${index}`)),
    ...Array.from({ length: 9 }, (_, index) =>
      session(`child-${index}`, { role: "child", parentId: "root-0" }),
    ),
    ...Array.from({ length: 11 }, (_, index) =>
      session(`archive-${index}`, { status: "archived", archivedAt: asOf }),
    ),
  ];
  mount([task(supplied)]);
  const region = screen.getByRole("region", { name: "Sessions" });
  expect(region.querySelectorAll(".tb-session-line.root")).toHaveLength(6);
  const group = within(region.querySelector(".tb-session-group")! as HTMLElement);
  expect(group.getAllByRole("button", { name: /Session details: child-/ })).toHaveLength(8);
  fireEvent.click(group.getByRole("button", { name: "Next page" }));
  expect(group.getByRole("button", { name: "Session details: child-8" })).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "11 archived today" }));
  const archive = within(region.querySelector(".tb-session-archived")! as HTMLElement);
  expect(archive.getAllByRole("button", { name: /Session details: archive-/ })).toHaveLength(10);
  fireEvent.click(archive.getByRole("button", { name: "Next page" }));
  expect(archive.getByRole("button", { name: "Session details: archive-10" })).toBeTruthy();
});
it("routes recent facts to the right task and the Log/Today tabs", () => {
  const source = task([]);
  source["omnigent.ai:sourceDisplay"]!.events = [
    {
      id: "ev",
      task: { source: "source", uid: "task" },
      kind: "start",
      at: asOf,
      label: "Started work",
    },
  ];
  source["omnigent.ai:sourceDisplay"]!.quotes = [
    {
      id: "quote",
      task: { source: "source", uid: "task" },
      text: "Keep the recovery path available",
      date: "2026-11-12",
    },
  ];
  const { select, view } = mount([source]);
  fireEvent.click(screen.getByRole("button", { name: /Started work/ }));
  expect(select).toHaveBeenCalledWith(source);
  fireEvent.click(screen.getByRole("button", { name: /Keep the recovery path available/ }));
  expect(select).toHaveBeenCalledTimes(2);
  fireEvent.click(screen.getByRole("button", { name: "See all log" }));
  fireEvent.click(screen.getByRole("button", { name: "See today" }));
  expect(view.mock.calls).toEqual([["log"], ["today"]]);
});
