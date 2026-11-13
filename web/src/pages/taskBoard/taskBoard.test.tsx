import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SESSION_NAVIGATION_STORAGE_KEY } from "@/lib/sessionNavigationPreferences";
import type * as IdentityModule from "@/lib/identity";
import { TaskBoardRoute } from "./TaskBoardRoute";
import { TaskBoardSettings } from "./TaskBoardSettings";
import { createTaskBoardText } from "./taskBoardText";
import { taskBoardFixture } from "./taskBoardFixtures";
import { BOARD_PREFERENCES_KEY } from "./taskBoardPreferences";
import { taskProgress, type BoardTask, type TaskSource } from "./taskBoardData";
import type { BoardSession } from "./taskBoardNative";
import { usageBoardData } from "@/pages/usageBoard/usageBoardFixtures";
import type { UsageOverview } from "@/pages/usageBoard/usageBoardData";

const mocks = vi.hoisted(() => ({
  info: { task_board_enabled: true } as { task_board_enabled: boolean } | "loading",
  fetch: vi.fn(),
  tasks: vi.fn(),
  sessions: [] as BoardSession[],
  quota: null as UsageOverview | null,
}));
vi.mock("@/lib/CapabilitiesContext", () => ({ useServerInfo: () => mocks.info }));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  authenticatedFetch: (...args: unknown[]) => mocks.fetch(...args),
}));
const clients: QueryClient[] = [];
const sockets: { send: ReturnType<typeof vi.fn>; onmessage?: (event: { data: string }) => void }[] =
  [];
function renderBoard() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.push(client);
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <TaskBoardRoute />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}
function enableBoard(view = "sessions") {
  localStorage.setItem(SESSION_NAVIGATION_STORAGE_KEY, JSON.stringify({ showTaskBoard: true }));
  localStorage.setItem(BOARD_PREFERENCES_KEY, JSON.stringify({ defaultView: view }));
}
function report(
  tasks: BoardTask[],
  sources: TaskSource[] = [
    { id: "tracker", name: "Example tracker", source: "https://tracker.example.com" },
  ],
) {
  mocks.tasks.mockImplementation(async (path: string) => {
    const url = new URL(path, "https://example.com");
    if (url.pathname.endsWith("/sources")) return Response.json({ sources });
    if (url.pathname.endsWith("/item"))
      return Response.json(tasks.find((task) => task.uid === url.searchParams.get("uid")));
    const state = url.searchParams.get("state");
    return Response.json({
      entries: tasks.filter((task) =>
        state === "closed"
          ? ["completed", "failed", "cancelled"].includes(taskProgress(task))
          : !["completed", "failed", "cancelled"].includes(taskProgress(task)),
      ),
      updated: new Date().toISOString(),
    });
  });
}
beforeEach(() => {
  mocks.info = { task_board_enabled: true };
  mocks.fetch.mockReset();
  mocks.tasks.mockReset();
  mocks.sessions = [];
  mocks.quota = null;
  sockets.length = 0;
  localStorage.clear();
  vi.stubGlobal(
    "WebSocket",
    class {
      send = vi.fn();
      onopen?: () => void;
      onclose?: () => void;
      onmessage?: (event: { data: string }) => void;
      constructor() {
        sockets.push(this);
        queueMicrotask(() => this.onopen?.());
      }
      close() {}
    },
  );
  mocks.fetch.mockImplementation(async (path: string) => {
    if (path.startsWith("/v1/me/tasks")) return mocks.tasks(path);
    const url = new URL(path, "https://example.com");
    if (url.pathname === "/v1/sessions")
      return Response.json({
        data: mocks.sessions.filter((session) =>
          url.searchParams.has("archived_only") ? session.archived : !session.archived,
        ),
        has_more: false,
        last_id: null,
      });
    if (url.pathname.endsWith("/items"))
      return Response.json({
        data: [{ type: url.pathname.includes("tool-running") ? "function_call" : "message" }],
      });
    if (url.pathname.startsWith("/v1/sessions/"))
      return Response.json(
        mocks.sessions.find((session) => session.id === url.pathname.split("/").at(-1)),
      );
    if (url.pathname === "/v1/system/status")
      return Response.json({ hosts: [], findings: [], level: "ok" });
    if (url.pathname === "/v1/me/usage") return Response.json(mocks.quota ?? { accounts: [] });
    if (url.pathname === "/v1/me/usage/activity")
      return Response.json({ accounts: [{ account: "account-0", totals: { cost_usd: 123.456 } }] });
    if (url.pathname === "/v1/scheduled-tasks") return Response.json({ scheduled_tasks: [] });
    throw new Error(`Unexpected board read: ${path}`);
  });
  report([]);
});
afterEach(() => {
  cleanup();
  clients.forEach((client) => client.clear());
  clients.length = 0;
  vi.unstubAllGlobals();
});

it("keeps an unchanged all-legacy Log incremental through the real task-board page", async () => {
  enableBoard("log");
  const stamp = new Date().toISOString();
  let revision = stamp;
  const row: BoardTask = {
    uid: "one",
    title: "Legacy task",
    progress: "in-process",
    "omnigent.ai:source": "tracker",
  };
  mocks.tasks.mockImplementation(async (path: string) => {
    const url = new URL(path, "https://example.test");
    if (url.pathname.endsWith("/sources"))
      return Response.json({
        sources: [
          {
            id: "tracker",
            name: "Tracker",
            source: null,
            created: stamp,
            last_write: { time: revision },
            snapshot: {
              sourceGeneration: "legacy-generation",
              phase: "legacy",
              id: null,
              asOf: null,
            },
          },
        ],
      });
    if (url.pathname.endsWith("/history"))
      return Response.json({
        entries: [{ id: "event", source: "tracker", uid: "one", time: stamp, kind: "comment" }],
        ...(url.searchParams.has("changed_after") ? {} : { cursor: "more" }),
        changedThrough: stamp,
      });
    return Response.json({
      entries: url.searchParams.get("state") === "closed" ? [] : [row],
      "omnigent.ai:changedThrough": stamp,
    });
  });
  renderBoard();
  await screen.findByRole("button", { name: "Legacy task" });
  const histories = () =>
    mocks.tasks.mock.calls
      .map(([path]) => new URL(path, "https://example.test"))
      .filter((url) => url.pathname.endsWith("/history"));
  expect(histories()).toHaveLength(1);
  await act(async () => {
    await clients[0].invalidateQueries({ queryKey: ["taskBoard", "list"] });
  });
  expect(histories()).toHaveLength(1);
  // Refresh the same active query used by the 60-second cadence, without advancing timers.
  await act(async () => {
    await clients[0].invalidateQueries({ queryKey: ["taskBoard", "windowedHistory"] });
  });
  expect(histories()).toHaveLength(2);
  expect(histories()[1].searchParams.has("changed_after")).toBe(true);
  expect(histories()[1].searchParams.has("cursor")).toBe(false);
  revision = new Date(Date.parse(stamp) + 60_000).toISOString();
  await act(async () => {
    await clients[0].invalidateQueries({ queryKey: ["taskBoard", "list"] });
  });
  await waitFor(() => expect(histories()).toHaveLength(3));
  expect(histories()[2].searchParams.get("source")).toBe("tracker");
  expect(histories()[2].searchParams.get("changed_after")).toBe(
    new Date(Date.parse(stamp) - 60_000).toISOString(),
  );
  expect(histories()[2].searchParams.has("cursor")).toBe(false);
});

it.each(["none", "recent", "archive"])(
  "opens one history detail through the real page with %s relation navigation",
  async (relation) => {
    enableBoard("history");
    const old: BoardTask = {
      uid: "archive",
      title: "Archive recovery",
      progress: "completed",
      "omnigent.ai:source": "tracker",
      "omnigent.ai:group": { id: "archive", name: "Archive group" },
      created: "2020-01-01T00:00:00Z",
      progressUpdated: "2020-01-20T00:00:00Z",
    };
    const recent: BoardTask = {
      ...old,
      uid: "recent",
      title: "Recent selection",
      created: new Date(Date.now() - 3600000).toISOString(),
      progressUpdated: new Date().toISOString(),
    };
    const related: BoardTask = {
      ...(relation === "recent" ? recent : old),
      uid: "related",
      title: "Related task",
    };
    const current =
      relation === "none" ? [] : [recent, ...(relation === "recent" ? [related] : [])];
    const archived = [old, ...(relation === "archive" ? [related] : [])];
    if (relation !== "none") old.relatedTo = { related: { relation: { parent: true } } };
    mocks.tasks.mockImplementation(async (path: string) => {
      const url = new URL(path, "https://example.test");
      if (url.pathname.endsWith("/sources"))
        return Response.json({ sources: [{ id: "tracker", name: "Tracker", source: null }] });
      if (url.pathname.endsWith("/closed-counts"))
        return Response.json({
          days: [{ source: "tracker", day: "2020-01-20", closed: archived.length }],
        });
      if (url.pathname.endsWith("/history")) return Response.json({ entries: [] });
      if (url.pathname.endsWith("/item"))
        return Response.json(
          [...current, ...archived].find((task) => task.uid === url.searchParams.get("uid")),
        );
      return Response.json({
        entries: url.searchParams.has("closed_before")
          ? archived
          : url.searchParams.get("state") === "closed"
            ? current
            : [],
        updated: new Date().toISOString(),
      });
    });
    const view = renderBoard();
    if (relation !== "none") {
      await waitFor(() => expect(view.container.querySelector(".h-p")).toBeTruthy());
      fireEvent.click(view.container.querySelector(".h-p")!);
      fireEvent.click(view.container.querySelector(".h-srow")!);
      fireEvent.click(screen.getByRole("button", { name: /Recent selection.*Completed/ }));
      const recentTrigger = screen.getByRole("button", { name: "Open task details" });
      recentTrigger.focus();
      fireEvent.click(recentTrigger);
      expect(await screen.findByRole("region", { name: "Details: Recent selection" })).toBeTruthy();
    }
    fireEvent.click(await screen.findByRole("button", { name: "Find by month" }));
    fireEvent.change(view.container.querySelector("input[type=month]")!, {
      target: { value: "2020-01" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Load month" }));
    await waitFor(() =>
      expect(view.container.querySelector(".h-stat>b")?.textContent).toBe(String(archived.length)),
    );
    fireEvent.click(view.container.querySelector(".h-p")!);
    fireEvent.click(view.container.querySelector(".h-srow")!);
    fireEvent.click(screen.getByRole("button", { name: /Archive recovery.*Completed/ }));
    const trigger = screen.getByRole("button", { name: "Open task details" });
    trigger.focus();
    fireEvent.click(trigger);
    const detail = await screen.findByRole("region", { name: "Details: Archive recovery" });
    expect(view.container.querySelectorAll(".tb-dc")).toHaveLength(1);
    fireEvent.click(within(detail).getByRole("button", { name: "Close" }));
    expect(view.container.querySelectorAll(".tb-dc")).toHaveLength(0);
    expect(document.activeElement).toBe(trigger);
    if (relation === "none") {
      fireEvent.click(trigger);
      fireEvent.keyDown(screen.getByRole("slider", { name: "Range start" }), { key: "ArrowRight" });
      expect(view.container.querySelectorAll(".tb-dc")).toHaveLength(0);
      fireEvent.click(view.container.querySelector(".h-p")!);
      fireEvent.click(view.container.querySelector(".h-srow")!);
      fireEvent.click(screen.getByRole("button", { name: /Archive recovery.*Completed/ }));
      const sameTask = screen.getByRole("button", { name: "Open task details" });
      sameTask.focus();
      fireEvent.click(sameTask);
      const reopened = await screen.findByRole("region", { name: "Details: Archive recovery" });
      expect(view.container.querySelectorAll(".tb-dc")).toHaveLength(1);
      fireEvent.click(within(reopened).getByRole("button", { name: "Close" }));
      expect(view.container.querySelectorAll(".tb-dc")).toHaveLength(0);
      expect(document.activeElement).toBe(sameTask);
    }
    if (relation !== "none") {
      fireEvent.click(trigger);
      const reopened = await screen.findByRole("region", { name: "Details: Archive recovery" });
      fireEvent.click(within(reopened).getByRole("button", { name: /related.*Related task/ }));
      const target = await screen.findByRole("region", { name: "Details: Related task" });
      expect(view.container.querySelectorAll(".tb-dc")).toHaveLength(1);
      expect(screen.queryByRole("region", { name: "Details: Archive recovery" })).toBeNull();
      expect(screen.queryByRole("region", { name: "Details: Recent selection" })).toBeNull();
      fireEvent.click(within(target).getByRole("button", { name: "Close" }));
      expect(view.container.querySelectorAll(".tb-dc")).toHaveLength(0);
      expect(document.activeElement).toBe(trigger);
    }
  },
);

it("keeps deployment and user gates ahead of every read", () => {
  mocks.info = { task_board_enabled: false };
  const view = renderBoard();
  expect(screen.getByRole("heading", { name: "Task board is off on this server" })).toBeTruthy();
  expect(mocks.fetch).not.toHaveBeenCalled();
  view.unmount();
  mocks.info = { task_board_enabled: true };
  localStorage.setItem(SESSION_NAVIGATION_STORAGE_KEY, JSON.stringify({ showTaskBoard: false }));
  renderBoard();
  expect(screen.getByRole("link", { name: "Turn it on in Settings" }).getAttribute("href")).toBe(
    "/settings/general#task-board-settings",
  );
  expect(mocks.fetch).not.toHaveBeenCalled();
});
it("opens an empty board with source management in Settings", async () => {
  localStorage.setItem(BOARD_PREFERENCES_KEY, JSON.stringify({ defaultView: "routes" }));
  renderBoard();
  expect(await screen.findByRole("heading", { name: "No tasks yet" })).toBeTruthy();
  expect(
    screen.getByRole("link", { name: "Manage task sources in Settings" }).getAttribute("href"),
  ).toBe("/settings/general#task-board-settings");
  expect(screen.queryByText("/v1/me/tasks/sources/{source_id}")).toBeNull();
});
it("defaults to native parent/child sessions and watches without device activity", async () => {
  enableBoard();
  const fixture = taskBoardFixture();
  mocks.sessions = fixture.sessions.map((session) =>
    session.archived ? { ...session, archived_at: Date.now() / 1000 } : session,
  );
  renderBoard();
  const region = await screen.findByRole("region", { name: "Sessions" });
  expect(await within(region).findByText("Verifying resumed chunks")).toBeTruthy();
  fireEvent.click(
    within(region).getByRole("button", { name: "Collapse child sessions: Transfer reliability" }),
  );
  expect(within(region).queryByRole("link", { name: /Check retry behaviour/ })).toBeNull();
  fireEvent.click(
    within(region).getByRole("button", { name: "Expand child sessions: Transfer reliability" }),
  );
  expect(
    within(region)
      .getByRole("link", { name: /Check retry behaviour/ })
      .getAttribute("href"),
  ).toBe("/c/child");
  fireEvent.click(within(region).getByRole("button", { name: "1 archived today" }));
  expect(
    within(region)
      .getByRole("link", { name: /Document the recovery path/ })
      .getAttribute("href"),
  ).toBe("/c/archived");
  await waitFor(() => expect(sockets[0]?.send).toHaveBeenCalled());
  expect(
    sockets.flatMap((socket) => socket.send.mock.calls.map(([body]) => JSON.parse(body))),
  ).toEqual([{ type: "watch", session_ids: ["child", "parent"] }]);
  const reads = mocks.fetch.mock.calls.map(([path]) => new URL(path, "https://example.com"));
  expect(
    reads
      .filter((url) => url.pathname === "/v1/sessions")
      .every((url) => url.searchParams.get("kind") === "any"),
  ).toBe(true);
  expect(
    reads.some(
      (url) => url.searchParams.has("archived_only") && url.searchParams.has("updated_after"),
    ),
  ).toBe(true);
  expect(mocks.fetch.mock.calls.every((call) => call.length === 1)).toBe(true);
});
it("checks a quiet running session before reporting an exception and exempts a live tool", async () => {
  enableBoard();
  mocks.sessions = [
    {
      id: "quiet",
      title: "Quiet session",
      status: "running",
      updated_at: Date.now() / 1000 - 1300,
    },
    {
      id: "tool-running",
      title: "Tool session",
      status: "running",
      updated_at: Date.now() / 1000 - 1300,
    },
  ];
  renderBoard();
  const exceptions = await screen.findByRole("button", { name: "1 Exceptions" });
  fireEvent.click(exceptions);
  const panel = screen.getByRole("region", { name: "Exceptions" });
  expect(within(panel).getByRole("link", { name: /Quiet session/ })).toBeTruthy();
  expect(within(panel).queryByRole("link", { name: /Tool session/ })).toBeNull();
  expect(screen.getByRole("button", { name: "1 Running" })).toBeTruthy();
});
it("prioritizes quota attention, exposes every account and saves pins locally", async () => {
  enableBoard();
  const overview = usageBoardData(),
    base = overview.accounts[0];
  mocks.quota = {
    ...overview,
    accounts: Array.from({ length: 5 }, (_, index) => ({
      ...base,
      id: `account-${index}`,
      display_name: `Account ${index + 1}`,
      windows: base.windows.map((reading) => ({
        ...reading,
        percent_used: index === 4 ? 93 : 10 + index,
      })),
    })),
  };
  renderBoard();
  const quota = await screen.findByRole("region", { name: "Quota" });
  expect(await within(quota).findByRole("button", { name: /Account 5\s*93%/ })).toBeTruthy();
  fireEvent.click(within(quota).getByRole("button", { name: "Show all 5 accounts" }));
  expect(await screen.findByText(/Today’s estimate \$123.46/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Pin Account 1" }));
  expect(JSON.parse(localStorage.getItem(BOARD_PREFERENCES_KEY)!).pinnedQuota).toEqual([
    "account-0",
  ]);
  expect(within(quota).getAllByRole("button")[1].textContent).toContain("Account 1");
});
it("maps source tasks to routes and keeps unsafe source URLs as text", async () => {
  enableBoard("routes");
  const tasks: BoardTask[] = [
    {
      uid: "one",
      title: "Retry uploads",
      progress: "in-process",
      "omnigent.ai:source": "tracker",
      "omnigent.ai:key": "ENG-1",
      "omnigent.ai:group": { id: "v2", name: "Version two" },
      links: { page: { rel: "alternate", href: "https://tracker.example.com/one" } },
    },
    {
      uid: "two",
      title: "Remove old endpoint",
      progress: "cancelled",
      "omnigent.ai:resolution": "not_planned",
      "omnigent.ai:source": "tracker",
      links: { page: { rel: "alternate", href: "javascript:alert(1)" } },
    },
  ];
  report(tasks);
  renderBoard();
  expect((await screen.findAllByText("2 tasks")).length).toBeGreaterThan(0);
  expect(screen.getAllByRole("heading", { name: "Version two" }).length).toBeGreaterThan(0);
  fireEvent.click(screen.getAllByRole("button", { name: "Retry uploads" })[0]);
  const detail = await screen.findByRole("region", { name: "Details: Retry uploads" });
  expect(await within(detail).findByRole("link", { name: "Open source" })).toBeTruthy();
  fireEvent.click(screen.getAllByRole("button", { name: "Remove old endpoint" })[0]);
  const noPage = await screen.findByRole("region", { name: "Details: Remove old endpoint" });
  expect(within(noPage).queryByRole("link", { name: "Open source" })).toBeNull();
  expect(await within(noPage).findByText("This task is closed.")).toBeTruthy();
  expect(document.querySelector('a[href^="javascript:"]')).toBeNull();
  expect(mocks.fetch.mock.calls.every((call) => call.length === 1)).toBe(true);
});
it("joins task waits with native prompts and opens real session links", async () => {
  enableBoard();
  const fixture = taskBoardFixture();
  mocks.sessions = fixture.sessions;
  report(fixture.tasks, fixture.sources);
  renderBoard();
  await screen.findByRole("button", { name: /2 Waiting on me/ });
  fireEvent.click(screen.getByRole("button", { name: /2 Waiting on me/ }));
  const waiting = screen.getByRole("region", { name: "Waiting on me" });
  expect(within(waiting).getByRole("button", { name: /Choose the retry budget/ })).toBeTruthy();
  expect(
    within(waiting)
      .getByRole("link", { name: /Check retry behaviour/ })
      .getAttribute("href"),
  ).toBe("/c/child");
  fireEvent.click(within(waiting).getByRole("button", { name: /Choose the retry budget/ }));
  const detail = await screen.findByRole("region", { name: "Details: Choose the retry budget" });
  expect(within(detail).getByText("Approve a limit of three retries.")).toBeTruthy();
  expect(within(detail).getByText("Depends on")).toBeTruthy();
  fireEvent.click(within(detail).getByRole("button", { name: /ENG-123/ }));
  const changed = await screen.findByRole("region", {
    name: "Details: Recover interrupted uploads",
  });
  expect(within(changed).queryByText("65%")).toBeNull();
  expect(await within(changed).findByText("$1.90")).toBeTruthy();
});
it("keeps native sessions usable with no source and retries a failed source read", async () => {
  enableBoard();
  mocks.tasks.mockResolvedValue(Response.json({}, { status: 503 }));
  renderBoard();
  expect(await screen.findByRole("region", { name: "Sessions" })).toBeTruthy();
  const error = await screen.findByRole("alert");
  report([]);
  fireEvent.click(within(error).getByRole("button", { name: "Try again" }));
  await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
  fireEvent.click(screen.getByRole("button", { name: "Routes" }));
  expect(await screen.findByRole("heading", { name: "No tasks yet" })).toBeTruthy();
  expect(screen.getByRole("link", { name: "Manage task sources in Settings" })).toBeTruthy();
});
it("stores the display switch in the existing navigation namespace", async () => {
  render(<TaskBoardSettings />);
  const toggle = screen.getByRole("switch", { name: "Show Task board" });
  expect(toggle.getAttribute("aria-checked")).toBe("true");
  fireEvent.click(toggle);
  await waitFor(() =>
    expect(JSON.parse(localStorage.getItem(SESSION_NAVIGATION_STORAGE_KEY)!)).toMatchObject({
      showTaskBoard: false,
    }),
  );
  fireEvent.click(toggle);
  await waitFor(() => expect(localStorage.getItem(SESSION_NAVIGATION_STORAGE_KEY)).toBeNull());
});

describe("translation catalogue", () => {
  it("uses English plural rules and interpolates exact counts", () => {
    const { t } = createTaskBoardText();
    expect(t("board.tasks", { count: 1 })).toBe("1 task");
    expect(t("board.tasks", { count: 2 })).toBe("2 tasks");
    expect(t("issue.missing_property", { path: "uid" })).toBe(
      "A required property is missing: uid",
    );
  });
  it("takes a partial pack per key and formats through its locale", () => {
    const text = createTaskBoardText({
      _meta: { locale: "zh-CN" },
      "board.title": "任务看板",
      "board.tasks_other": "{{count}} 项任务",
    });
    expect(text.t("board.title")).toBe("任务看板");
    expect(text.t("board.tasks", { count: 1 })).toBe("1 项任务");
    expect(text.t("board.empty")).toBe("No tasks yet");
    expect(
      text.formatDate(new Date("2026-11-06T00:00:00Z"), {
        timeZone: "UTC",
        year: "numeric",
        month: "long",
        day: "numeric",
      }),
    ).toBe("2026年11月6日");
    expect(text.formatNumber(1234)).toBe("1,234");
    expect(() => createTaskBoardText({ "removed.key": "stale" })).toThrow(
      "Unknown task board translation key",
    );
  });
});

it("reveals a related task at the expanded session anchor and restores its row on close", async () => {
  enableBoard();
  const fixture = taskBoardFixture();
  const origin = fixture.tasks[1];
  origin["omnigent.ai:sourceDisplay"] = {
    version: 1,
    sessions: [
      {
        id: "source-session",
        task: { source: origin["omnigent.ai:source"], uid: origin.uid },
        role: "root",
        agentId: "pi",
        label: "Example worker",
        title: "Example worker",
        status: "running",
        createdAt: new Date().toISOString(),
        lastActivityAt: new Date().toISOString(),
      },
    ],
  };
  report(fixture.tasks, fixture.sources);
  renderBoard();
  const row = await screen.findByRole("button", { name: "Session details: Example worker" });
  fireEvent.click(row);
  const original = await screen.findByRole("region", { name: "Details: Choose the retry budget" });
  fireEvent.click(within(original).getByRole("button", { name: /ENG-123/ }));
  const target = await screen.findByRole("region", {
    name: "Details: Recover interrupted uploads",
  });
  expect(target.closest(".tb-session-group")).toBeTruthy();
  expect(screen.queryByRole("region", { name: "Details: Choose the retry budget" })).toBeNull();
  fireEvent.click(within(target).getByRole("button", { name: "Close" }));
  expect(screen.queryByRole("region", { name: "Details: Recover interrupted uploads" })).toBeNull();
  expect(document.activeElement).toBe(row);
});
