import { act, cleanup, fireEvent, render, screen, within, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import userEvent from "@testing-library/user-event";
import { historyCards, historySort, accountTotals, DAY } from "./taskBoardHistoryData";
import { TaskBoardLog } from "./TaskBoardLog";
import { TaskBoardHistory } from "./TaskBoardHistory";
import { TaskBoardToday } from "./TaskBoardToday";
import { TaskBoardHistoryChart } from "./TaskBoardHistoryChart";
import { activityMoney } from "./TaskBoardActivityParts";
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
afterEach(() => {
  cleanup();
  fetcher.mockReset();
});

it.each(["source", "group", "node"])("clamps the %s page when refreshed data shrinks", (level) => {
  const tasks = Array.from(
    { length: level === "source" ? 11 : level === "group" ? 9 : 1 },
    (_, i) => {
      const value = task(`Task ${i}`, level === "source" ? `source-${i}` : "engineering");
      if (level === "group") value["omnigent.ai:sourceDisplay"]!.identity!.groupId = `group-${i}`;
      if (level === "node")
        value["omnigent.ai:sourceDisplay"]!.intervals = Array.from({ length: 9 }, (_entry, n) => ({
          id: `node-${n}`,
          from: new Date(now - 3600000).toISOString(),
          to: new Date(now).toISOString(),
          agentId: "codex",
          host: `host-${n}`,
          origin: "event" as const,
        }));
      return value;
    },
  );
  const props = { tasks, sessions: [], caches: {}, counts: [], now, onSelect: vi.fn() };
  const view = render(
    <MemoryRouter>
      <TaskBoardHistory {...props} />
    </MemoryRouter>,
  );
  if (level !== "source") fireEvent.click(view.container.querySelector(".h-p")!);
  if (level === "node") {
    fireEvent.click(view.container.querySelector(".h-srow")!);
    fireEvent.click(view.container.querySelector(".h-trow")!);
  }
  fireEvent.click(screen.getByRole("button", { name: "Next page" }));
  const reduced =
    level === "node"
      ? [
          {
            ...tasks[0],
            "omnigent.ai:sourceDisplay": {
              ...tasks[0]["omnigent.ai:sourceDisplay"]!,
              intervals: tasks[0]["omnigent.ai:sourceDisplay"]!.intervals!.slice(0, 8),
            },
          },
        ]
      : tasks.slice(0, -1);
  view.rerender(
    <MemoryRouter>
      <TaskBoardHistory {...props} tasks={reduced} />
    </MemoryRouter>,
  );
  expect(
    view.container.querySelectorAll(
      level === "source" ? ".h-p" : level === "group" ? ".h-srow" : ".h-nodes li",
    ),
  ).toHaveLength(level === "source" ? 10 : 8);
  expect(screen.queryByRole("button", { name: "Next page" })).toBeNull();
});

it.each(["button", "brush"])(
  "keeps a newer %s range when an old month finishes",
  async (control) => {
    fetcher.mockReset();
    let finish!: (response: Response) => void;
    fetcher.mockImplementation(
      () =>
        new Promise<Response>((resolve) => {
          finish = resolve;
        }),
    );
    const view = render(
      <MemoryRouter>
        <TaskBoardHistory
          tasks={[]}
          sessions={[]}
          caches={{}}
          counts={[{ day: "2020-01-20", source: "engineering", closed: 1 }]}
          now={now}
          onSelect={vi.fn()}
        />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Find by month" }));
    fireEvent.change(view.container.querySelector("input[type=month]")!, {
      target: { value: "2020-01" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Load month" }));
    if (control === "button") fireEvent.click(screen.getByRole("button", { name: "1 week" }));
    else
      fireEvent.keyDown(screen.getByRole("slider", { name: "Range start" }), { key: "ArrowRight" });
    const range = view.container.querySelector(".h-range")!.textContent;
    expect(screen.queryByRole("button", { name: "Cancel" })).toBeNull();
    const old = task("Old result");
    old["omnigent.ai:sourceDisplay"]!.historyState!.end = "2020-01-20T00:00:00Z";
    await act(async () => finish(Response.json({ entries: [old] })));
    expect(view.container.querySelector(".h-range")!.textContent).toBe(range);
    expect(view.container.querySelector(".h-stat>b")?.textContent).toBe("0");
  },
);

it("does not let an old generation failure clear a new month's loading", async () => {
  fetcher.mockReset();
  const pending: { resolve: (value: Response) => void; reject: (reason: Error) => void }[] = [];
  fetcher.mockImplementation(
    () =>
      new Promise<Response>((resolve, reject) => {
        pending.push({ resolve, reject });
      }),
  );
  const props = {
    tasks: [],
    sessions: [],
    counts: [{ day: "2020-01-20", source: "engineering", closed: 1 }],
    now,
    onSelect: vi.fn(),
  };
  const generation = (id: string) => ({ engineering: { ...cache(), pin: { ...cache().pin, id } } });
  const view = render(
    <MemoryRouter>
      <TaskBoardHistory {...props} caches={generation("initial")} />
    </MemoryRouter>,
  );
  view.rerender(
    <MemoryRouter>
      <TaskBoardHistory {...props} caches={generation("A")} />
    </MemoryRouter>,
  );
  fireEvent.click(screen.getByRole("button", { name: "Find by month" }));
  fireEvent.change(view.container.querySelector("input[type=month]")!, {
    target: { value: "2020-01" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Load month" }));
  view.rerender(
    <MemoryRouter>
      <TaskBoardHistory {...props} caches={generation("B")} />
    </MemoryRouter>,
  );
  fireEvent.click(screen.getByRole("button", { name: "Load month" }));
  await act(async () => pending[0].reject(new Error("old pin rejected")));
  expect(screen.getByRole("button", { name: "Cancel" })).toBeTruthy();
  expect(screen.queryByRole("alert")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  await act(async () => pending[1].resolve(Response.json({ entries: [] })));
});

it("freezes open intervals at their source observation time", () => {
  const value = task("Frozen");
  value["omnigent.ai:sourceDisplay"]!.intervals![0].to = null;
  const c = cache([value]);
  c.display.asOf = new Date(now - 3600000).toISOString();
  const first = historyCards([value], [], now, { engineering: c });
  const later = historyCards([value], [], now + 2 * 3600000, { engineering: c });
  expect(first[0].seconds).toBe(3600);
  expect(later[0].seconds).toBe(first[0].seconds);
});

it.each([365, 1825])("propagates an unknown account day inside a %s-day chart bucket", (days) => {
  const from = new Date(2026, 10, 12).getTime() - days * DAY;
  const view = render(
    <TaskBoardHistoryChart
      cards={[]}
      counts={[]}
      accounts={
        new Map([
          ["2026-11-10", 5],
          ["2026-11-11", null],
        ])
      }
      from={from}
      to={now}
      now={now}
      historyStart={from}
      capped={false}
      sourceOrder={[]}
      onRange={vi.fn()}
    />,
  );
  const title = [...view.container.querySelectorAll(".r-hit title")].find(
    (el) => el.textContent?.includes("2026-11-09") || el.textContent?.includes("2026-11-01"),
  );
  expect(title).toBeTruthy();
  expect(title!.textContent).not.toContain("$5");
  expect(view.container.querySelector(".r-cost")).toBeNull();
});

it("uses two money decimals around magnitude boundaries", () => {
  expect([0, 9.999, 10, 99.999, 100, 999.999, 1000, 1234.56].map(activityMoney)).toEqual([
    "$0.00",
    "$10.00",
    "$10.00",
    "$100.00",
    "$100.00",
    "$1,000.00",
    "$1,000.00",
    "$1,234.56",
  ]);
});

it("limits rendered history counts and token counts to three significant digits", () => {
  const values = Array.from({ length: 1234 }, (_, i) => task(`Task ${i}`));
  values[0]["omnigent.ai:sourceDisplay"]!.intervals![0].tokens = 1234567;
  const view = render(
    <MemoryRouter>
      <TaskBoardHistory
        tasks={values}
        sessions={[]}
        caches={{}}
        counts={[]}
        now={now}
        onSelect={vi.fn()}
      />
    </MemoryRouter>,
  );
  expect(view.container.querySelector(".h-stat>b")?.textContent).toBe("1.23K");
  fireEvent.click(view.container.querySelector(".h-p")!);
  fireEvent.click(view.container.querySelector(".h-srow")!);
  fireEvent.click(screen.getByRole("button", { name: /Task 0.*Completed/ }));
  expect(screen.getByText("1.23M token")).toBeTruthy();
  expect(screen.getByText("1.23K total")).toBeTruthy();
});
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
it("shows the note of legacy history events without a supplied label", () => {
  render(
    <TaskBoardLog
      tasks={[task("Older task")]}
      sessions={[]}
      onSelect={vi.fn()}
      events={[
        {
          id: "legacy-commit",
          source: "engineering",
          uid: "Older task",
          time: "2020-01-10T12:00:00Z",
          kind: "commit",
          note: "Keep confirmed chunks",
        },
        {
          id: "legacy-open",
          source: "engineering",
          uid: "Older task",
          time: "2020-01-10T11:00:00Z",
          kind: "open",
          note: "Opened for recovery",
        },
      ]}
    />,
  );
  expect(screen.getByText("Keep confirmed chunks")).toBeTruthy();
  expect(screen.getByText(/Opened for recovery/)).toBeTruthy();
});
it("explains when the log has no events", () => {
  render(<TaskBoardLog tasks={[]} sessions={[]} onSelect={vi.fn()} events={[]} />);
  expect(screen.getByText("No activity to show.")).toBeTruthy();
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
  expect(document.querySelector(".r-head text")).toBeNull();
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
  expect(screen.getByText("Task-linked commits")).toBeTruthy();
  expect(
    screen.getByTitle(
      "Only commits attached to supplied tasks; excludes repository commits without a task link.",
    ),
  ).toBeTruthy();
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
  expect(container.querySelector(".h-range")?.textContent).toContain("January 1");
  expect(fetcher).toHaveBeenCalledTimes(4);
  expect(fetcher.mock.calls[2][0]).toContain("cursor=later");
  fireEvent.click(screen.getByRole("button", { name: /engineering.*1 groups/ }));
  fireEvent.click(screen.getByRole("button", { name: /A group/ }));
  expect(screen.getByRole("button", { name: /Old A.*Completed/ })).toBeTruthy();
  expect(screen.getByRole("button", { name: /Old B.*Completed/ })).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Back to recent" }));
  await waitFor(() => expect(container.querySelector(".h-stat>b")?.textContent).toBe("0"));
  expect(screen.queryByText("Old A")).toBeNull();
  expect(screen.queryByText("Old B")).toBeNull();
});

it("loads old-month cursor pages for a cacheless source without recent tasks", async () => {
  fetcher.mockReset();
  const older = [task("Old A"), task("Old B")];
  older.forEach((old) => {
    const history = old["omnigent.ai:sourceDisplay"]!.historyState!;
    history.first = "2020-01-01T00:00:00Z";
    history.last = "2020-01-20T00:00:00Z";
    history.end = history.last;
  });
  fetcher
    .mockResolvedValueOnce(Response.json({ entries: [older[0]], "omnigent.ai:cursor": "later" }))
    .mockResolvedValueOnce(Response.json({ entries: [older[1]] }));
  const { container } = render(
    <MemoryRouter>
      <TaskBoardHistory
        tasks={[]}
        sessions={[]}
        caches={{}}
        counts={[{ day: "2020-01-20", source: "engineering", closed: 2 }]}
        now={now}
        onSelect={vi.fn()}
      />
    </MemoryRouter>,
  );
  fireEvent.click(screen.getByRole("button", { name: "Find by month" }));
  fireEvent.change(container.querySelector("input[type=month]")!, { target: { value: "2020-01" } });
  fireEvent.click(screen.getByRole("button", { name: "Load month" }));
  await waitFor(() => expect(container.querySelector(".h-stat>b")?.textContent).toBe("2"));
  expect(fetcher).toHaveBeenCalledTimes(2);
  const queries = fetcher.mock.calls.map(
    ([url]) => new URL(url, "https://example.test").searchParams,
  );
  expect(queries.map((query) => query.get("source"))).toEqual(["engineering", "engineering"]);
  expect(queries[1].get("cursor")).toBe("later");
  for (const query of queries) {
    expect(query.get("include_history")).toBe("1");
    expect(query.get("state")).toBe("closed");
    expect(query.has("snapshot")).toBe(false);
    expect(query.has("source_generation")).toBe(false);
  }
  fireEvent.click(screen.getByRole("button", { name: /engineering.*1 groups/ }));
  fireEvent.click(screen.getByRole("button", { name: /A group/ }));
  expect(screen.getByRole("button", { name: /Old A.*Completed/ })).toBeTruthy();
  expect(screen.getByRole("button", { name: /Old B.*Completed/ })).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Back to recent" }));
  await waitFor(() => expect(container.querySelector(".h-stat>b")?.textContent).toBe("0"));
});

it("keeps native sessions without a supplied task in the Today worker totals", () => {
  render(
    <TaskBoardToday
      tasks={[]}
      sessions={[
        {
          id: "independent",
          agent_id: "codex",
          status: "running",
          updated_at: now / 1000,
          total_cost_usd: 3,
        },
      ]}
      caches={{}}
      now={now}
      onSelect={vi.fn()}
    />,
  );
  expect(screen.getByText("Codex", { selector: "b" })).toBeTruthy();
  expect(screen.getByText(/1 sessions, 1 open/)).toBeTruthy();
  expect(screen.getByText(/\$3\.00/)).toBeTruthy();
});
it("uses supplied activity instead of duplicating task-body update history", () => {
  const a = task("A");
  a["omnigent.ai:sourceDisplay"]!.events = [
    {
      id: "source-event",
      at: new Date(now).toISOString(),
      kind: "commit",
      task: { source: "engineering", uid: "A" },
      label: "Source commit",
    },
  ];
  const { container } = render(
    <TaskBoardLog
      tasks={[a]}
      sessions={[]}
      onSelect={vi.fn()}
      events={[
        {
          id: "native-delta",
          source: "engineering",
          uid: "A",
          time: new Date(now).toISOString(),
          kind: "update",
        },
      ]}
    />,
  );
  expect(container.querySelectorAll(".lr")).toHaveLength(1);
  expect(screen.getByText("Source commit")).toBeTruthy();
});

it("opens the task button from the keyboard without toggling its enclosing event", async () => {
  const a = task("Keyboard task"),
    select = vi.fn();
  const { container } = render(
    <TaskBoardLog
      tasks={[a]}
      sessions={[]}
      onSelect={select}
      events={[
        {
          id: "event",
          source: "engineering",
          uid: a.uid,
          time: new Date(now).toISOString(),
          kind: "commit",
        },
      ]}
    />,
  );
  const user = userEvent.setup();
  screen.getByRole("button", { name: "Keyboard task" }).focus();
  await user.keyboard("{Enter}");
  expect(select).toHaveBeenCalledWith(a);
  expect(screen.getByRole("button", { name: "Resume" })).toBeTruthy();
  expect(container.querySelector(".lr")?.getAttribute("aria-expanded")).toBe("false");
});
