import { boardDefaults } from "./taskBoardPreferences";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";
import { taskBoardFixture } from "./taskBoardFixtures";
import { TaskBoardActivity } from "./TaskBoardActivity";
import { TaskBoardSources } from "./TaskBoardSources";
import { TaskBoardLog } from "./TaskBoardLog";
import { TaskSourceDetails } from "./TaskSourceDetails";
import {
  eventMother,
  localDay,
  readBoardHistory,
  readLocalClosedCounts,
} from "./taskBoardActivityData";

const fetcher = vi.hoisted(() => vi.fn());
vi.mock("@/lib/CapabilitiesContext", () => ({
  useServerInfo: () => ({ usage_board_enabled: false }),
}));
vi.mock("@/lib/identity", () => ({ authenticatedFetch: fetcher }));
afterEach(() => {
  cleanup();
  fetcher.mockReset();
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
  const event = { ...f.tasks[0]["omnigent.ai:history"]![0], source: "engineering", uid: "upload" };
  expect(eventMother(event, f.tasks[0], f.sessions)).toEqual({
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
