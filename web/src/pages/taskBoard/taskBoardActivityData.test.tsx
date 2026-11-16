import { boardDefaults } from "./taskBoardPreferences";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";
import { taskBoardFixture, dailyRepositoryFixture } from "./taskBoardFixtures";
import { TaskBoardActivity } from "./TaskBoardActivity";
import { TaskBoardSources } from "./TaskBoardSources";
import type { SourceCache } from "./taskBoardSnapshotData";
import { TaskBoardLog } from "./TaskBoardLog";
import { TaskSourceDetails } from "./TaskSourceDetails";
import { activityStamp } from "./TaskBoardActivityParts";
import type { BoardTask } from "./taskBoardData";
import { activityTaskName } from "./taskBoardPresentation";
import { TaskBoardToday } from "./TaskBoardToday";
import {
  eventMother,
  localDay,
  readBoardHistory,
  readLocalClosedCounts,
  useBoardHistory,
  repositoryCommitTotals,
} from "./taskBoardActivityData";

const fetcher = vi.hoisted(() => vi.fn());

it("shows the frozen Taipei 225 repository commits beside 172 linked without assigning links to repositories", () => {
  const { cache, now } = dailyRepositoryFixture(Intl.DateTimeFormat().resolvedOptions().timeZone);
  const { container } = render(
    <TaskBoardToday
      tasks={cache.tasks}
      sessions={[]}
      caches={{ example: cache }}
      now={now}
      onSelect={vi.fn()}
    />,
  );
  expect(screen.getByText("225").parentElement?.textContent).toContain("Repository commits");
  expect(screen.getByText("172").parentElement?.textContent).toContain("Task-linked commits");
  expect(
    [...container.querySelectorAll(".td-roots em")]
      .map((node) => Number(node.textContent))
      .reduce((a, b) => a + b, 0),
  ).toBe(225);
  expect(container.querySelectorAll(".td-roots span")).toHaveLength(6);
  expect(screen.queryByText(/Partial/)).toBeNull();
});

it("marks a missing repository/day unavailable and keeps a partial sum, including known zero", () => {
  const { cache, now } = dailyRepositoryFixture(Intl.DateTimeFormat().resolvedOptions().timeZone);
  const days = cache.display.repositoryDailyCommits!;
  days[0].date = "2026-11-02";
  days[1].totalCommits = 0;
  const totals = repositoryCommitTotals({ example: cache }, now);
  expect(totals).toMatchObject({ total: 181, partial: true });
  expect(totals.rows).toContainEqual({ repository: "Repository 1", count: null });
  expect(totals.rows).toContainEqual({ repository: "Repository 2", count: 0 });
  render(
    <TaskBoardToday
      tasks={cache.tasks}
      sessions={[]}
      caches={{ example: cache }}
      now={now}
      onSelect={vi.fn()}
    />,
  );
  expect(screen.getByText("Repository 1").parentElement?.textContent).toContain("Unavailable");
  expect(screen.getByText("181").parentElement?.textContent).toContain("Partial");
  expect(screen.getByText("172")).toBeTruthy();
});

it("deduplicates repeated repositories, withholds conflicting totals and incompatible zones, and retains exact integers", () => {
  const { cache, now } = dailyRepositoryFixture(Intl.DateTimeFormat().resolvedOptions().timeZone);
  expect(repositoryCommitTotals({ first: cache, second: cache }, now).total).toBe(225);
  const other = structuredClone(cache);
  other.display.repositoryDailyCommits![0].totalCommits = 27;
  expect(repositoryCommitTotals({ first: cache, second: other }, now)).toMatchObject({
    total: 199,
    partial: true,
  });
  cache.display.repositoryDailyCommits![0].timeZone = "America/New_York";
  cache.display.repositoryDailyCommits![1].totalCommits = 12345;
  const { container } = render(
    <TaskBoardToday
      tasks={[]}
      sessions={[]}
      caches={{ example: cache }}
      now={now}
      onSelect={vi.fn()}
    />,
  );
  expect(container.querySelector(".td-roots")?.textContent).toContain("12345");
  expect(screen.getByText("Repository 1").parentElement?.textContent).toContain("Unavailable");
  expect(repositoryCommitTotals({}, now)).toMatchObject({ total: null, partial: true });
});
vi.mock("@/lib/CapabilitiesContext", () => ({
  useServerInfo: () => ({ usage_board_enabled: false }),
}));
vi.mock("@/lib/identity", () => ({ authenticatedFetch: fetcher }));
afterEach(() => {
  cleanup();
  fetcher.mockReset();
});
it("retains rich work details inside a folded other event without treating its work start as an old field value", () => {
  const task: BoardTask = {
    uid: "review",
    title: "Review task",
    "omnigent.ai:source": "engineering",
    "omnigent.ai:sourceDisplay": {
      version: 1,
      events: [
        {
          id: "work-1",
          kind: "other",
          at: "2026-11-13T12:00:00Z",
          task: { source: "engineering", uid: "review" },
          label: "Review completed",
          from: "2026-11-13T11:00:00Z",
          workSeconds: 3600,
          agentId: "codex",
          host: "workstation",
          commitIds: ["commit-1"],
        },
      ],
      commits: [
        {
          id: "commit-1",
          sha: "abcdef1",
          subject: "Preserve review details",
          at: "2026-11-13T11:30:00Z",
        },
      ],
    },
  };
  const { container } = wrap(
    <TaskBoardLog tasks={[task]} events={[]} sessions={[]} onSelect={vi.fn()} />,
  );
  fireEvent.click(container.querySelector(".lr")!);
  const detail = container.querySelector(".lgd-changes")!;
  expect(detail.textContent).toContain("Codex");
  expect(detail.textContent).toContain("workstation");
  expect(detail.textContent).toContain("1h");
  expect(detail.textContent).toContain(activityStamp(Date.parse("2026-11-13T11:00:00Z")));
  expect(detail.textContent).toContain(activityStamp(Date.parse("2026-11-13T12:00:00Z")));
  expect(detail.textContent).toContain("abcdef1");
  expect(detail.textContent).toContain("Preserve review details");
  expect(within(detail as HTMLElement).queryByText("From")).toBeNull();
});
it("retains canonical change values and actor alongside matching rich work details", () => {
  const event = {
    source: "engineering",
    uid: "review",
    id: "change-1",
    time: "2026-11-13T12:00:00Z",
    kind: "field",
    field: "progress",
    actor: "claude",
    from: "in-process",
    to: "completed",
  };
  const task: BoardTask = {
    uid: "review",
    title: "Review task",
    "omnigent.ai:source": "engineering",
    "omnigent.ai:sourceDisplay": {
      version: 1,
      events: [
        {
          id: event.id,
          at: event.time,
          kind: "other",
          task: { source: event.source, uid: event.uid },
          label: "Status changed",
          agentId: "codex",
          host: "workstation",
          from: "2026-11-13T11:00:00Z",
          workSeconds: 3600,
        },
      ],
    },
  };
  const { container } = wrap(
    <TaskBoardLog tasks={[task]} events={[event]} sessions={[]} onSelect={vi.fn()} />,
  );
  expect(container.querySelectorAll(".lr")).toHaveLength(1);
  expect(container.textContent).toContain("1 field changes");
  fireEvent.click(container.querySelector(".lr")!);
  const detail = container.querySelector(".lgd-changes")!;
  const members = within(detail as HTMLElement).getAllByRole("listitem");
  expect(members).toHaveLength(1);
  const member = within(members[0]);
  expect(member.getByText("in-process")).toBeTruthy();
  expect(member.getByText("completed")).toBeTruthy();
  expect(member.getByText(/Claude/)).toBeTruthy();
  expect(member.getByText(/Status changed/)).toBeTruthy();
  expect(member.getByText(/Codex.*workstation.*1h/)).toBeTruthy();
  expect(member.getByText(activityStamp(Date.parse("2026-11-13T11:00:00Z")))).toBeTruthy();
});
it.each([false, true])(
  "retains canonical events with rich events present=%s and deduplicates only matching event identities",
  (rich) => {
    const event = {
      source: "engineering",
      uid: "review",
      id: "change-1",
      time: "2026-11-13T12:00:00Z",
      kind: "field",
      field: "progress",
      from: "in-process",
      to: "completed",
    };
    const task: BoardTask = {
      uid: "review",
      title: "Review task",
      "omnigent.ai:source": "engineering",
      "omnigent.ai:sourceDisplay": {
        version: 1,
        events: rich
          ? [
              {
                id: "match",
                kind: "open",
                at: event.time,
                task: { source: "engineering", uid: "review" },
                label: "Rich created",
              },
            ]
          : [],
      },
    };
    const { container } = wrap(
      <TaskBoardLog
        tasks={[task]}
        events={[
          event,
          ...(rich ? [{ ...event, id: "match", kind: "created", label: "Canonical created" }] : []),
        ]}
        sessions={[]}
        onSelect={vi.fn()}
      />,
    );
    expect(container.querySelectorAll(".lr")).toHaveLength(rich ? 2 : 1);
    expect(container.textContent).toContain("1 field changes");
    if (rich) {
      expect(container.textContent).toContain("Rich created");
      expect(container.textContent).not.toContain("Canonical created");
    }
    fireEvent.click(container.querySelector(".lr-other")!);
    expect(screen.getByText("in-process")).toBeTruthy();
    expect(screen.getByText("completed")).toBeTruthy();
  },
);
it.each(["By time", "By root session"])(
  "buffers another history page while expanded in %s and resumes without losing either event",
  (grouping) => {
    const task = taskBoardFixture().tasks[0];
    const event = {
      source: "engineering",
      uid: task.uid,
      id: "first",
      time: "2026-11-13T12:00:00Z",
      kind: "field",
      field: "progress",
      note: "Initial change",
    };
    const props = { tasks: [task], sessions: [], onSelect: vi.fn() };
    const { container, rerender } = render(<TaskBoardLog {...props} events={[event]} />);
    fireEvent.click(screen.getByRole("button", { name: grouping }));
    fireEvent.click(container.querySelector(".lr")!);
    rerender(
      <TaskBoardLog
        {...props}
        events={[
          event,
          { ...event, id: "second", time: "2026-11-13T11:00:00Z", note: "Earlier page change" },
        ]}
      />,
    );
    expect(container.querySelector(".lr")?.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByText(/Initial change/)).toBeTruthy();
    expect(screen.queryByText(/Earlier page change/)).toBeNull();
    const resume = screen.getByRole("button", { name: /Resume/ });
    expect(resume.hasAttribute("disabled")).toBe(false);
    expect(container.querySelector(".l-n")?.textContent).toBe("1");
    fireEvent.click(resume);
    expect(container.textContent).toContain("2 field changes");
    expect(screen.getByText(/Initial change/)).toBeTruthy();
    expect(screen.getByText(/Earlier page change/)).toBeTruthy();
  },
);
it("checks identity names and group badges without rewriting ordinary prose", () => {
  const internal = "abcdef0123456789abcdef0123456789";
  const task: BoardTask = {
    uid: internal,
    title: "Readable title",
    "omnigent.ai:source": "engineering",
    "omnigent.ai:sourceDisplay": {
      version: 1,
      identity: { label: `ENG-123@${internal}`, key: "ENG-123", groupId: internal },
    },
  };
  expect(activityTaskName(task)).toBe("Readable title");
  expect(
    activityTaskName({
      ...task,
      title: internal,
      "omnigent.ai:key": undefined,
      "omnigent.ai:sourceDisplay": { version: 1 },
    }),
  ).toBe("Untitled task");
  const prose = `Investigate ENG-123@${internal} when reported`;
  expect(
    activityTaskName({ ...task, title: prose, "omnigent.ai:sourceDisplay": { version: 1 } }),
  ).toBe(prose);
  const { container } = render(
    <TaskBoardLog
      tasks={[task]}
      events={[
        {
          source: "engineering",
          uid: internal,
          id: "created",
          time: "2026-11-13T12:00:00Z",
          kind: "created",
        },
      ]}
      sessions={[]}
      onSelect={vi.fn()}
    />,
  );
  expect(container.textContent).not.toContain(internal);
});
it("folds only loaded changes by source, task and day, preserving every event on expansion", () => {
  const task = {
    ...taskBoardFixture().tasks[0],
    uid: "same@abcdef0123456789",
    title: "Readable task",
  };
  const events = Array.from({ length: 30 }, (_, i) => ({
    source: "engineering",
    uid: task.uid,
    id: `change-${i}`,
    time: "2026-11-13T12:00:00Z",
    kind: i % 2 ? "field" : "assignee",
    field: i % 2 ? "progress" : undefined,
    note: `Change ${i}`,
  }));
  const { container } = wrap(
    <TaskBoardLog
      tasks={[task]}
      sessions={[]}
      events={[
        ...events,
        { ...events[0], id: "closed", kind: "closed" },
        { ...events[0], id: "created", kind: "created" },
        { ...events[0], id: "other-source", source: "docs", kind: "other" },
      ]}
      sourceLabels={{ engineering: "Engineering", docs: "Documentation" }}
      onSelect={vi.fn()}
    />,
  );
  expect(container.querySelectorAll(".lr")).toHaveLength(4);
  expect(container.textContent).toContain("30 field changes");
  expect(container.textContent).not.toContain(task.uid);
  fireEvent.click(screen.getByRole("button", { name: "Field changes" }));
  expect(container.querySelectorAll(".lr")).toHaveLength(2);
  const summary = container.querySelector(".lr")!;
  fireEvent.click(summary);
  expect(container.querySelectorAll(".lgd-changes li")).toHaveLength(25);
  fireEvent.click(screen.getByRole("button", { name: "Next page" }));
  expect(container.querySelectorAll(".lgd-changes li")).toHaveLength(5);
});
it("hides empty source rows and offers the entire long source value on demand", () => {
  const long = "Source text ".repeat(50);
  const { container } = wrap(
    <TaskSourceDetails
      display={{
        version: 1,
        rows: [
          { id: "empty", label: "Empty field", value: "None" },
          { id: "long", label: "Details", value: long },
          { id: "zero", label: "Count", value: "0" },
        ],
      }}
    />,
  );
  expect(screen.queryByText("Empty field")).toBeNull();
  expect(screen.getByText("0")).toBeTruthy();
  expect(container.querySelector(".tb-fold-value.collapsed")?.textContent).toBe(long);
  fireEvent.click(screen.getByRole("button", { name: "Show more" }));
  expect(container.querySelector(".tb-fold-value.collapsed")).toBeNull();
  expect(screen.getByRole("button", { name: "Show less" }).getAttribute("aria-expanded")).toBe(
    "true",
  );
});
function wrap(ui: React.ReactNode) {
  return render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })}
    >
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}
const cache = (generation = "first", revision = "2026-11-13T00:00:00Z"): SourceCache => ({
  pin: { sourceGeneration: generation, id: null, asOf: null, phase: "legacy" },
  display: { version: 1, asOf: revision, timeZone: "UTC" },
  tasks: [],
  history: [],
  counts: [],
});
const event = (id: string, time = new Date().toISOString()) => ({
  id,
  time,
  source: "engineering",
  uid: "upload",
  kind: "comment",
});
function HistoryReader({ caches }: { caches?: Record<string, SourceCache> }) {
  const history = useBoardHistory(true, caches);
  return (
    <>
      <output>
        {history.events
          .map((row) => row.id)
          .sort()
          .join(",")}
      </output>
      <span data-testid="watermark">{history.data?.through}</span>
      <button type="button" onClick={() => void history.refetch()}>
        Refresh
      </button>
      <button type="button" onClick={() => void history.loadEarlier()}>
        Earlier
      </button>
    </>
  );
}
it("refreshes the requested window including a late event older than its first result", async () => {
  const firstTime = new Date(Date.now() - 86400000).toISOString();
  const lateTime = new Date(Date.now() - 2 * 86400000).toISOString();
  fetcher.mockImplementation(async (path: string) => {
    const query = new URL(path, "https://example.test").searchParams;
    return Response.json({
      entries:
        fetcher.mock.calls.length === 1
          ? [event("first", firstTime)]
          : query.get("after")! <= lateTime
            ? [event("late", lateTime)]
            : [],
    });
  });
  wrap(<HistoryReader caches={{ engineering: cache() }} />);
  await screen.findByText("first", { selector: "output" });
  fireEvent.click(screen.getByText("Refresh"));
  await screen.findByText("first,late", { selector: "output" });
  const queries = fetcher.mock.calls.map(
    ([path]) => new URL(path, "https://example.test").searchParams,
  );
  expect(queries[1].get("after")).toBe(queries[0].get("after"));
});
it.each(["earlier", "refresh"])("preserves both responses when %s finishes last", async (last) => {
  let finishEarlier!: (response: Response) => void;
  let finishRefresh!: (response: Response) => void;
  fetcher.mockImplementation((path: string) => {
    const params = new URL(path, "https://example.test").searchParams;
    if (params.has("changed_after"))
      return new Promise<Response>((resolve) => {
        finishRefresh = resolve;
      });
    if (params.has("cursor"))
      return new Promise<Response>((resolve) => {
        finishEarlier = resolve;
      });
    return Promise.resolve(
      Response.json({
        entries: [event("first")],
        cursor: "next",
        changedThrough: "2026-11-13T01:00:00Z",
      }),
    );
  });
  wrap(<HistoryReader caches={{ engineering: cache() }} />);
  await screen.findByText("first", { selector: "output" });
  fireEvent.click(screen.getByText("Earlier"));
  fireEvent.click(screen.getByText("Refresh"));
  await waitFor(() => expect(finishRefresh).toBeTypeOf("function"));
  const earlier = () => finishEarlier(Response.json({ entries: [event("older")] }));
  const refresh = () =>
    finishRefresh(
      Response.json({ entries: [event("new")], changedThrough: "2026-11-13T02:00:00Z" }),
    );
  (last === "earlier" ? refresh : earlier)();
  await screen.findByText(last === "earlier" ? "first,new" : "first,older", { selector: "output" });
  (last === "earlier" ? earlier : refresh)();
  await screen.findByText("first,new,older", { selector: "output" });
  expect(screen.getByTestId("watermark").textContent).toBe("2026-11-13T02:00:00Z");
});
it("shows Log failures and Retry with source caches", async () => {
  fetcher
    .mockResolvedValueOnce(Response.json({}, { status: 500 }))
    .mockResolvedValue(Response.json({ entries: [event("recovered")] }));
  wrap(
    <TaskBoardActivity
      view="log"
      tasks={taskBoardFixture().tasks}
      sessions={[]}
      sourceCaches={{ engineering: cache() }}
      onSelect={vi.fn()}
    />,
  );
  const alert = await screen.findByRole("alert");
  fireEvent.click(within(alert).getByRole("button", { name: "Try again" }));
  await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
  expect(fetcher).toHaveBeenCalledTimes(2);
});
it("removes deleted source history and replaces a recreated generation without remount", async () => {
  let entries = [event("old")];
  fetcher.mockImplementation(async () => Response.json({ entries }));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const ui = (caches: Record<string, SourceCache>) => (
    <QueryClientProvider client={client}>
      <HistoryReader caches={caches} />
    </QueryClientProvider>
  );
  const view = render(ui({ engineering: cache() }));
  await screen.findByText("old", { selector: "output" });
  entries = [];
  view.rerender(ui({}));
  await waitFor(() => expect(screen.getByRole("status").textContent).toBe(""));
  entries = [event("new")];
  view.rerender(ui({ engineering: cache("second") }));
  await screen.findByText("new", { selector: "output" });
});
it("reconciles a source revision whose commit missed the timestamp overlap", async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  let committed = false;
  fetcher.mockImplementation(async (path: string) => {
    const params = new URL(path, "https://example.test").searchParams;
    const lower = params.get("changed_after");
    return Response.json({
      entries: [
        event("first"),
        ...(committed && (!lower || Date.parse(lower) <= Date.parse("2026-11-13T11:00:00Z"))
          ? [event("delayed")]
          : []),
      ],
      changedThrough: "2026-11-13T12:00:00Z",
    });
  });
  const ui = (revision: string) => (
    <QueryClientProvider client={client}>
      <HistoryReader caches={{ engineering: cache("first", revision) }} />
    </QueryClientProvider>
  );
  const view = render(ui("2026-11-13T10:00:00Z"));
  await screen.findByText("first", { selector: "output" });
  committed = true;
  view.rerender(ui("2026-11-13T11:00:00Z"));
  await screen.findByText("delayed,first", { selector: "output" });
  expect(
    new URL(fetcher.mock.calls.at(-1)![0], "https://example.test").searchParams.get(
      "changed_after",
    ),
  ).toBe("2026-11-13T09:59:00.000Z");
});
it("loads one history page, pages on demand, and recovers after a failed incremental refresh", async () => {
  let failed = true;
  fetcher.mockImplementation(async (path: string) => {
    const params = new URL(path, "https://example.test").searchParams;
    const row = (id: string) => ({
      id,
      source: "example",
      uid: "one",
      time: new Date().toISOString(),
      kind: "comment",
    });
    if (params.has("changed_after")) {
      if (failed) {
        failed = false;
        return Response.json({}, { status: 422 });
      }
      return Response.json({ entries: [row("new")] });
    }
    return Response.json(
      params.has("cursor")
        ? { entries: [row("older")] }
        : { entries: [row("first")], cursor: "next" },
    );
  });
  function Reader() {
    const history = useBoardHistory(true, { engineering: cache() });
    return (
      <>
        <output>{history.events.length}</output>
        <button type="button" onClick={() => void history.loadEarlier()}>
          Earlier
        </button>
        <button type="button" onClick={() => void history.refetch()}>
          Refresh
        </button>
        {history.isError && <span>Failed</span>}
      </>
    );
  }
  wrap(<Reader />);
  await screen.findByText("1", { selector: "output" });
  expect(fetcher).toHaveBeenCalledTimes(1);
  const first = new URL(fetcher.mock.calls[0][0], "https://example.test");
  expect(first.searchParams.has("after")).toBe(true);
  expect(first.searchParams.get("limit")).toBe("500");
  fireEvent.click(screen.getByText("Earlier"));
  await screen.findByText("2", { selector: "output" });
  fireEvent.click(screen.getByText("Refresh"));
  await screen.findByText("Failed");
  expect(screen.getByText("2", { selector: "output" })).toBeTruthy();
  fireEvent.click(screen.getByText("Refresh"));
  await screen.findByText("3", { selector: "output" });
  await waitFor(() => expect(screen.queryByText("Failed")).toBeNull());
  for (const [path] of fetcher.mock.calls.slice(2))
    expect(new URL(path, "https://example.test").searchParams.has("changed_after")).toBe(true);
});
it("drains history cursors and rejects repeated cursors", async () => {
  fetcher
    .mockResolvedValueOnce(Response.json({ entries: [{ id: "a" }], cursor: "next" }))
    .mockResolvedValueOnce(Response.json({ entries: [{ id: "b" }] }));
  expect(await readBoardHistory()).toHaveLength(2);
  expect(fetcher.mock.calls[1][0]).toContain("cursor=next");
  fetcher.mockImplementation(async () => Response.json({ entries: [], cursor: "loop" }));
  await expect(readBoardHistory()).rejects.toThrow("Repeated history cursor");
});
it("uses browser-local dates and joins the span's root session without cycling", () => {
  const f = taskBoardFixture();
  const sampleEvent = {
    ...f.tasks[0]["omnigent.ai:history"]![0],
    source: "engineering",
    uid: "upload",
  };
  expect(eventMother(sampleEvent, f.tasks[0], f.sessions)).toEqual({
    id: "parent",
    label: "Transfer reliability",
  });
  expect(localDay(new Date(2026, 10, 7, 0, 30))).toBe("2026-11-07");
});
it.each([
  ["mother", "Fix upload"],
  ["group", "Reliability"],
])("keeps same-named %s log groups separate by identity", async (group, label) => {
  const f = taskBoardFixture();
  const tasks = ["engineering", "docs", "engineering"].map((source, index) => ({
    ...f.tasks[0],
    uid: `upload-${index}`,
    title: `Upload task ${index}`,
    "omnigent.ai:source": source,
    "omnigent.ai:workedBy": [
      { session: index === 1 ? "other-root" : "child", from: "2026-11-06T00:00:00Z" },
    ],
  }));
  const sessions = [
    { id: "parent", title: "Fix upload", status: "running" },
    { id: "child", parent_session_id: "parent", status: "running" },
    { id: "other-root", title: "Fix upload", status: "running" },
  ];
  fetcher.mockImplementation(async (url: string) =>
    Response.json(
      url.includes("closed-counts")
        ? { days: [] }
        : {
            entries: tasks.map((task, index) => ({
              id: `event-${index}`,
              source: task["omnigent.ai:source"],
              uid: task.uid,
              time: "2026-11-07T00:00:00Z",
              kind: "commit",
            })),
          },
    ),
  );
  wrap(<TaskBoardActivity view="log" tasks={tasks} sessions={sessions} onSelect={vi.fn()} />);
  await screen.findByRole("button", { name: "Upload task 0" });
  fireEvent.click(
    screen.getByRole("button", { name: group === "mother" ? "By root session" : "By task group" }),
  );
  const headings = screen.getAllByRole("heading", { name: new RegExp(label) });
  expect(headings).toHaveLength(2);
  const first = within(headings[0].parentElement!);
  const second = within(headings[1].parentElement!);
  expect(first.getAllByRole("button", { name: /^Upload task \d$/ })).toHaveLength(2);
  expect(first.getByRole("button", { name: "Upload task 0" })).toBeTruthy();
  expect(first.getByRole("button", { name: "Upload task 2" })).toBeTruthy();
  expect(second.getAllByRole("button", { name: /^Upload task \d$/ })).toHaveLength(1);
  expect(second.getByRole("button", { name: "Upload task 1" })).toBeTruthy();
});
it("renders full-past counts without inventing old task bodies", async () => {
  fetcher.mockImplementation(async (url: string) =>
    Response.json(
      url.includes("closed-counts")
        ? { days: [{ day: "2020-01-01", source: "engineering", closed: 23 }] }
        : { entries: [] },
    ),
  );
  wrap(<TaskBoardActivity view="history" tasks={[]} sessions={[]} onSelect={vi.fn()} />);
  await waitFor(() =>
    expect(screen.getByRole("group", { name: /Closed tasks by date/ }).textContent).toContain("23"),
  );
  expect(screen.queryByText("2020-01-01", { selector: ".h-name b" })).toBeNull();
  expect(screen.getByText("No records match these filters.")).toBeTruthy();
});
it("translates the Today closed-count tile", async () => {
  fetcher.mockImplementation(async (url: string) =>
    Response.json(url.includes("closed-counts") ? { days: [] } : { entries: [] }),
  );
  const { container } = wrap(
    <TaskBoardActivity view="today" tasks={[]} sessions={[]} onSelect={vi.fn()} />,
  );
  expect(await screen.findByText("Closed today")).toBeTruthy();
  expect(container.textContent).not.toContain("toclosedDay.closed");
});
it("keeps aliases local and requires a named counted confirmation before deletion", async () => {
  const source = { ...taskBoardFixture().sources[0], counts: { open: 2, closed: 3, gone: 1 } };
  const update = vi.fn();
  fetcher.mockResolvedValue(new Response(null, { status: 204 }));
  wrap(
    <TaskBoardSources
      sources={[source]}
      preferences={{
        ...boardDefaults,
        view: "settings",
        mode: "order",
        focus: [],
        aliases: {},
        hiddenSources: [],
        pinnedQuota: [],
      }}
      update={update}
    />,
  );
  fireEvent.change(screen.getByLabelText("Display name for Engineering"), {
    target: { value: "Local name" },
  });
  expect(update).toHaveBeenCalledWith({ aliases: { engineering: "Local name" } });
  expect(fetcher).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Delete source" }));
  expect(screen.getByText(/Engineering and its 6 stored tasks/)).toBeTruthy();
  expect(fetcher).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(fetcher).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Delete source" }));
  fireEvent.click(screen.getByRole("button", { name: "Delete source" }));
  await waitFor(() => expect(screen.getByRole("status").textContent).toBe("Deleted Engineering."));
  expect(fetcher).toHaveBeenCalledWith("/v1/me/tasks/sources/engineering", { method: "DELETE" });
});

it("queries historical counts with the date's offset, splitting a daylight transition", async () => {
  const boundary = Date.parse("2026-03-08T10:00:00Z");
  const offset = vi.spyOn(Date.prototype, "getTimezoneOffset").mockImplementation(function (
    this: Date,
  ) {
    return this.getTime() < boundary ? 480 : 420;
  });
  fetcher.mockImplementation(async (url: string) => {
    const query = new URL(url, "https://example.com").searchParams;
    if (!query.has("from"))
      return Response.json({
        days: [
          { day: "2026-03-07", source: "a", closed: 1 },
          { day: "2026-03-09", source: "a", closed: 2 },
        ],
      });
    return Response.json({
      days: [
        {
          day: query.get("offset") === "-480" ? "2026-03-07" : "2026-03-09",
          source: "a",
          closed: 1,
        },
      ],
    });
  });
  try {
    expect((await readLocalClosedCounts()).days).toHaveLength(2);
    const queries = fetcher.mock.calls
      .slice(1)
      .map((call) => new URL(call[0], "https://example.com").searchParams);
    expect(queries.map((q) => q.get("offset"))).toEqual(["-480", "-420"]);
    expect(queries[0].get("to")).toBe("2026-03-08T10:00:00Z");
    expect(queries[1].get("from")).toBe(queries[0].get("to"));
  } finally {
    offset.mockRestore();
  }
});
it("loads History counts through the query boundary across a daylight transition", async () => {
  const boundary = Date.parse("2026-03-08T10:00:00Z");
  const offset = vi.spyOn(Date.prototype, "getTimezoneOffset").mockImplementation(function (
    this: Date,
  ) {
    return this.getTime() < boundary ? 480 : 420;
  });
  const segments: URLSearchParams[] = [];
  fetcher.mockImplementation(async (path: string) => {
    const url = new URL(path, "https://example.test");
    if (!url.pathname.endsWith("closed-counts")) return Response.json({ entries: [] });
    if (!url.searchParams.has("from"))
      return Response.json({ days: [{ day: "2026-03-09", source: "a", closed: 2 }] });
    segments.push(url.searchParams);
    return Response.json({ days: [{ day: "2026-03-09", source: "a", closed: 1 }] });
  });
  try {
    wrap(<TaskBoardActivity view="history" tasks={[]} sessions={[]} onSelect={vi.fn()} />);
    await waitFor(() => expect(segments).toHaveLength(2));
    expect(segments.map((query) => query.get("offset"))).toEqual(["-480", "-420"]);
    expect(segments.every((query) => !query.has("client"))).toBe(true);
  } finally {
    offset.mockRestore();
  }
});
it("reports browser storage failures and interpolates issue paths", () => {
  const source = {
    ...taskBoardFixture().sources[0],
    issues: [{ code: "missing_property", path: "entries[0].uid" }],
  };
  wrap(
    <TaskBoardSources
      sources={[source]}
      preferences={{
        ...boardDefaults,
        view: "settings",
        mode: "order",
        focus: [],
        aliases: {},
        hiddenSources: [],
        pinnedQuota: [],
      }}
      update={() => false}
    />,
  );
  expect(screen.getByText("A required property is missing: entries[0].uid")).toBeTruthy();
  fireEvent.change(screen.getByLabelText("Display name for Engineering"), {
    target: { value: "Local" },
  });
  expect(screen.getByRole("status").textContent).toContain("Browser storage could not save it");
});

it("offers an available standards reference before a source is connected", () => {
  wrap(
    <TaskBoardSources
      sources={[]}
      preferences={{
        ...boardDefaults,
        view: "settings",
        mode: "order",
        focus: [],
        aliases: {},
        hiddenSources: [],
        pinnedQuota: [],
      }}
      update={vi.fn()}
    />,
  );
  expect(screen.getByRole("link", { name: "JSCalendar reference" }).getAttribute("href")).toBe(
    "https://www.rfc-editor.org/rfc/rfc8984.html",
  );
});
