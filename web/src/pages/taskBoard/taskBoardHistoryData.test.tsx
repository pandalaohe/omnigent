import { act, cleanup, fireEvent, render, screen, within, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { historyCards, historySort, accountTotals, DAY } from "./taskBoardHistoryData";
import { TaskBoardLog } from "./TaskBoardLog";
import { TaskBoardHistory } from "./TaskBoardHistory";
import { TaskBoardToday } from "./TaskBoardToday";
import type { BoardTask } from "./taskBoardData";
import type { SourceCache } from "./taskBoardSnapshotData";

const fetcher = vi.hoisted(() => vi.fn());
vi.mock("@/lib/identity", () => ({ authenticatedFetch: fetcher }));

const now = Date.parse("2026-11-12T12:00:00Z");
const task = (uid: string, source = "engineering"): BoardTask => ({
  uid,
  title: uid,
  "omnigent.ai:source": source,
  progress: "completed",
  "omnigent.ai:sourceDisplay": {
    version: 1,
    identity: { key: uid, label: uid, groupId: "group", groupLabel: "A group" },
    historyState: {
      first: new Date(now - 2 * DAY).toISOString(),
      last: new Date(now).toISOString(),
      end: new Date(now).toISOString(),
      outcome: "completed",
      active: false,
      pinned: false,
      future: false,
    },
    intervals: [
      {
        id: "span",
        from: new Date(now - 2 * 3600000).toISOString(),
        to: new Date(now - 3600000).toISOString(),
        agentId: "codex",
        origin: "event",
        cost: { amount: 12, kind: "estimate" },
      },
    ],
  },
});
const cache = (tasks: BoardTask[] = []): SourceCache => ({
  pin: { sourceGeneration: "g", id: "snap", asOf: null, phase: "published" },
  display: { version: 1, asOf: new Date(now).toISOString(), timeZone: "UTC" },
  tasks,
  history: [],
  counts: [],
});
afterEach(cleanup);
it("keeps pinned/future cards out, includes stale unclosed cards, and prefers native session cost endpoints", () => {
  const a = task("closed"),
    b = task("pinned"),
    c = task("future"),
    d = task("stale");
  b["omnigent.ai:sourceDisplay"]!.historyState!.pinned = true;
  c["omnigent.ai:sourceDisplay"]!.historyState!.future = true;
  d.progress = "needs-action";
  delete d["omnigent.ai:sourceDisplay"]!.historyState!.end;
  d["omnigent.ai:sourceDisplay"]!.historyState!.last = new Date(now - 2 * DAY).toISOString();
  a["omnigent.ai:sourceDisplay"]!.sessions = [
    {
      id: "session",
      task: { source: "engineering", uid: "closed" },
      agentId: "pi",
      role: "root",
      label: "Pi",
      title: "Work",
      status: "running",
      createdAt: new Date(now - 7200000).toISOString(),
      lastActivityAt: new Date(now - 3600000).toISOString(),
      cost: { amount: null, kind: "estimate" },
    },
  ];
  const cards = historyCards(
    [a, b, c, d],
    [{ id: "session", status: "idle", updated_at: now / 1000 - 600, ended_at: now / 1000 - 1200 }],
    now,
  );
  expect(cards.map((card) => card.task.uid)).toEqual(["closed", "stale"]);
  expect(cards[0].nodes[0].to).toBe(now - 1200000);
  expect(cards[0].usd).toBeNull();
  expect(cards[1].usd).toBe(12);
  expect([...cards].sort(historySort("cost"))[0].id).toBe(cards[1].id);
});
it("deduplicates authoritative account days and preserves conflicts as unknown", () => {
  const a = cache();
  a.display.accounts = [
    {
      ref: { namespace: "team", id: "a" },
      label: "Shared",
      authoritySource: "engineering",
      timeZone: "UTC",
    },
  ];
  a.display.accountDays = ["one", "replay"].map((id) => ({
    id,
    account: { namespace: "team", id: "a" },
    date: "2026-11-12",
    amount: 5,
    kind: "estimate",
    sourceLabel: "Meter",
  }));
  expect(
    [...accountTotals({ engineering: a }, "2026-11-12", "2026-11-12").values()][0].amount,
  ).toBe(5);
  a.display.accountDays[1].amount = 7;
  expect(
    [...accountTotals({ engineering: a }, "2026-11-12", "2026-11-12").values()][0].amount,
  ).toBeNull();
});
it("freezes selected log rows, buffers arrivals and reconciles on resume", () => {
  const a = task("A"),
    b = task("B"),
    props = { tasks: [a, b], sessions: [], onSelect: vi.fn() },
    event = (uid: string) => ({
      source: "engineering",
      uid,
      id: uid,
      time: new Date(now).toISOString(),
      kind: "commit",
    });
  const view = render(<TaskBoardLog {...props} events={[event("A")]} />);
  fireEvent.click(screen.getByRole("button", { name: "Pause" }));
  view.rerender(<TaskBoardLog {...props} events={[event("B"), event("A")]} />);
  expect(screen.queryByRole("button", { name: "B" })).toBeNull();
  expect(screen.getByRole("button", { name: "Resume1" })).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Resume1" }));
  expect(screen.getByRole("button", { name: "B" })).toBeTruthy();
});
it("filters before rollups and expands source, group, task and nodes", () => {
  const a = task("Priced"),
    b = task("Unknown");
  b["omnigent.ai:sourceDisplay"]!.intervals![0].cost!.amount = null;
  render(
    <MemoryRouter>
      <TaskBoardHistory
        tasks={[a, b]}
        sessions={[]}
        caches={{ engineering: cache([a, b]) }}
        counts={[]}
        now={now}
        onSelect={vi.fn()}
      />
    </MemoryRouter>,
  );
  fireEvent.click(screen.getByRole("button", { name: "Priced" }));
  expect(screen.getByText("1 sources")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: /engineering.*1 groups/ }));
  fireEvent.click(screen.getByRole("button", { name: /A group/ }));
  fireEvent.click(screen.getByRole("button", { name: /Priced.*Completed/ }));
  expect(screen.queryByText(/token/)).toBeFalsy();
  expect(screen.getByText("Open task details")).toBeTruthy();
});
it("deduplicates commits by source and id and chooses only typed releases", () => {
  const a = task("A"),
    b = task("B"),
    commit = {
      id: "shared",
      sha: "abcdef",
      subject: "Release is a subject",
      at: new Date(now).toISOString(),
    };
  a["omnigent.ai:sourceDisplay"]!.commits = [commit];
  b["omnigent.ai:sourceDisplay"]!.commits = [commit];
  const c = cache([a, b]);
  c.display.releases = [
    { id: "1", source: "engineering", name: "v2", publishedAt: new Date(now).toISOString() },
  ];
  render(
    <TaskBoardToday
      tasks={[a, b]}
      sessions={[]}
      caches={{ engineering: c }}
      now={now}
      onSelect={vi.fn()}
    />,
  );
  expect(screen.getByText("v2")).toBeTruthy();
  expect(
    within(screen.getByText("Landed today").closest("section")!).getByText("1", { selector: "b" }),
  ).toBeTruthy();
});

it("loads pinned old-month cursor pages and discards a cancelled response", async () => {
  const pending: ((response: Response) => void)[] = [];
  fetcher.mockImplementation(
    () =>
      new Promise<Response>((resolve) => {
        pending.push(resolve);
      }),
  );
  const c = cache(),
    props = {
      tasks: [],
      sessions: [],
      caches: { engineering: c },
      counts: [{ day: "2020-01-01", source: "engineering", closed: 2 }],
      now,
      onSelect: vi.fn(),
    };
  const { container } = render(
    <MemoryRouter>
      <TaskBoardHistory {...props} />
    </MemoryRouter>,
  );
  fireEvent.click(screen.getByRole("button", { name: "Find by month" }));
  fireEvent.change(container.querySelector("input[type=month]")!, { target: { value: "2020-01" } });
  fireEvent.click(screen.getByRole("button", { name: "Load month" }));
  expect(fetcher.mock.calls[0][0]).toContain("source_generation=g");
  expect(fetcher.mock.calls[0][0]).toContain("snapshot=snap");
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  await act(async () =>
    pending[0](Response.json({ entries: [task("Old")], "omnigent.ai:cursor": "later" })),
  );
  expect(fetcher).toHaveBeenCalledTimes(1);
  expect(screen.queryByText("Old")).toBeNull();
  const older = [task("Old A"), task("Old B")];
  older.forEach((old) => {
    const h = old["omnigent.ai:sourceDisplay"]!.historyState!;
    h.first = "2020-01-01T00:00:00Z";
    h.last = "2020-01-20T00:00:00Z";
    h.end = h.last;
  });
  fetcher
    .mockResolvedValueOnce(Response.json({ entries: [older[0]], "omnigent.ai:cursor": "later" }))
    .mockResolvedValueOnce(Response.json({ entries: [older[1]] }))
    .mockResolvedValueOnce(Response.json({ display: c.display, snapshot: c.pin }));
  fireEvent.click(screen.getByRole("button", { name: "Load month" }));
  await waitFor(() => expect(container.querySelector(".h-stat>b")?.textContent).toBe("2"));
  expect(fetcher).toHaveBeenCalledTimes(4);
  expect(fetcher.mock.calls[2][0]).toContain("cursor=later");
});
