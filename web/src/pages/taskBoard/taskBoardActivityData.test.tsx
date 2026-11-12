import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";
import { taskBoardFixture } from "./taskBoardFixtures";
import { TaskBoardActivity } from "./TaskBoardActivity";
import { TaskBoardSources } from "./TaskBoardSources";
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
  fireEvent.change(screen.getByLabelText("Group by"), { target: { value: group } });
  const headings = screen.getAllByRole("heading", { name: label });
  expect(headings).toHaveLength(2);
  const first = within(headings[0].parentElement!);
  const second = within(headings[1].parentElement!);
  expect(first.getAllByRole("listitem")).toHaveLength(2);
  expect(first.getByRole("button", { name: "Upload task 0" })).toBeTruthy();
  expect(first.getByRole("button", { name: "Upload task 2" })).toBeTruthy();
  expect(second.getAllByRole("listitem")).toHaveLength(1);
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
  await screen.findByTitle("Week of 2019-12-30: 23 closed");
  fireEvent.click(screen.getByTitle("Week of 2019-12-30: 23 closed"));
  expect((screen.getByLabelText("Find month") as HTMLSelectElement).value).toBe("2020-01");
  expect(screen.getByText(/outside the retained task window/)).toBeTruthy();
});
it("keeps aliases local and requires a named counted confirmation before deletion", async () => {
  const source = { ...taskBoardFixture().sources[0], counts: { open: 2, closed: 3, gone: 1 } };
  const update = vi.fn();
  fetcher.mockResolvedValue(new Response(null, { status: 204 }));
  wrap(
    <TaskBoardSources
      sources={[source]}
      preferences={{
        view: "sources",
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
    await screen.findByTitle("Week of 2026-03-09: 2 closed");
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
        view: "sources",
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
        view: "sources",
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
