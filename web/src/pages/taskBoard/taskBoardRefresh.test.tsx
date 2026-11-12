import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";
import { TaskBoardRoutes } from "./TaskBoardRoutes";
import type { BoardTask } from "./taskBoardData";
import type { SourceCache } from "./taskBoardSnapshotData";
import { DAY } from "./taskBoardRouteData";

const clients: QueryClient[] = [];
afterEach(() => {
  cleanup();
  clients.forEach((client) => client.clear());
  clients.length = 0;
  vi.restoreAllMocks();
});
const now = Date.parse("2026-11-06T13:00:00Z");
const rows = (count: number): BoardTask[] =>
  Array.from({ length: count }, (_, index) => ({
    uid: `task-${index}`,
    title: `Task ${index + 1}`,
    progress: "completed",
    "omnigent.ai:source": "one",
    created: new Date(Date.parse("2026-11-06T00:00:00Z") + index * 1000).toISOString(),
    progressUpdated: "2026-11-06T12:00:00Z",
  }));
function board(
  tasks: BoardTask[],
  mode: "order" | "time" = "order",
  overrides: Partial<Parameters<typeof TaskBoardRoutes>[0]> = {},
) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.push(client);
  const cache: SourceCache = {
    pin: {
      sourceGeneration: "one",
      id: "snapshot",
      asOf: new Date(now).toISOString(),
      phase: "published",
    },
    display: { version: 1, asOf: new Date(now).toISOString(), timeZone: "UTC" },
    tasks,
    history: [],
    counts: [],
  };
  return (
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <TaskBoardRoutes
          tasks={tasks}
          sources={[{ id: "one", name: "Source", source: null }]}
          aliases={{}}
          mode={mode}
          onMode={() => {}}
          focus={[]}
          onFocus={() => {}}
          onSelect={() => {}}
          scheduled={[]}
          sourceCaches={{ one: cache }}
          {...overrides}
        />
      </MemoryRouter>
    </QueryClientProvider>
  );
}
it("refreshes and clamps an open capsule from current identities after updates and deletions", () => {
  vi.spyOn(Date, "now").mockReturnValue(now);
  const tasks = rows(16),
    view = render(board(tasks));
  fireEvent.click(screen.getByRole("button", { name: "Open 16 stops" }));
  const panel = screen.getByRole("region", { name: "Stops in this group" });
  fireEvent.click(within(panel).getByRole("button", { name: "Next page" }));
  expect(within(panel).getByText("Task 16")).toBeTruthy();
  view.rerender(
    board(
      tasks
        .slice(0, 11)
        .map((task, index) => (index === 0 ? { ...task, title: "Updated task" } : task)),
    ),
  );
  expect(within(panel).getByText("Updated task")).toBeTruthy();
  expect(within(panel).queryByText("Task 16")).toBeNull();
  view.rerender(board([]));
  expect(screen.queryByRole("region", { name: "Stops in this group" })).toBeNull();
});
it("uses eight phone stations per page and clamps after tasks disappear", () => {
  vi.spyOn(Date, "now").mockReturnValue(now);
  vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(390);
  const tasks = rows(13),
    view = render(board(tasks));
  fireEvent.click(screen.getByRole("button", { name: /Source/ }));
  const phone = within(view.container.querySelector(".rt-phone-order")! as HTMLElement);
  expect(phone.getByText("Task 13")).toBeTruthy();
  expect(phone.getByText("13 tasks")).toBeTruthy();
  expect(phone.getByText("13 total")).toBeTruthy();
  fireEvent.click(phone.getByRole("button", { name: "Next page" }));
  expect(phone.getByText("Task 1")).toBeTruthy();
  view.rerender(board(tasks.slice(0, 7)));
  expect(phone.getByText("Task 7")).toBeTruthy();
  expect(phone.queryByText("Task 13")).toBeNull();
  expect(phone.queryByRole("button", { name: "Next page" })).toBeNull();
});
it("selects an entire source inline and supports filtered keyboard selection", () => {
  vi.spyOn(Date, "now").mockReturnValue(now);
  const tasks = rows(2).map((task, index) => ({
    ...task,
    "omnigent.ai:group": { id: index ? "beta" : "alpha", name: index ? "Beta" : "Alpha" },
  }));
  const onFocus = vi.fn();
  const view = render(board(tasks, "order", { onFocus }));
  fireEvent.click(screen.getByRole("button", { name: "Focus" }));
  const panel = screen.getByRole("region", { name: "Show only these" });
  expect(panel.parentElement).toBe(view.container.querySelector(".rt-routes"));
  fireEvent.click(within(panel).getByRole("checkbox", { name: /Source/ }));
  expect(onFocus).toHaveBeenLastCalledWith(
    expect.arrayContaining([JSON.stringify(["one", "alpha"]), JSON.stringify(["one", "beta"])]),
  );
  const input = within(panel).getByPlaceholderText(
    "Search sources or groups; Enter toggles the first",
  );
  fireEvent.change(input, { target: { value: "Beta" } });
  fireEvent.keyDown(input, { key: "Enter" });
  expect(onFocus).toHaveBeenLastCalledWith([JSON.stringify(["one", "beta"])]);
  fireEvent.keyDown(input, { key: "Escape" });
  expect((input as HTMLInputElement).value).toBe("");
  fireEvent.keyDown(input, { key: "Escape" });
  expect(screen.queryByRole("region", { name: "Show only these" })).toBeNull();
});
it("counts only eligible queued groups as quiet in the current time window", () => {
  vi.spyOn(Date, "now").mockReturnValue(now);
  const tasks = rows(3).map((task, index) => ({
    ...task,
    "omnigent.ai:group": { id: `group-${index}`, name: `Group ${index}` },
    progress: index === 1 ? ("needs-action" as const) : task.progress,
    progressUpdated: new Date(index === 2 ? now - 100 * DAY : now - 3600000).toISOString(),
  }));
  render(board(tasks, "time"));
  fireEvent.click(screen.getByRole("button", { name: "Expand all" }));
  expect(screen.getByRole("button", { name: /1 groups had no activity/ })).toBeTruthy();
  expect(screen.queryByRole("button", { name: /2 groups had no activity/ })).toBeNull();
  expect(screen.queryByText("Group 2")).toBeNull();
});
it("continues a group's rail from the window edge when its earlier stop is outside the window", () => {
  vi.spyOn(Date, "now").mockReturnValue(now);
  const tasks = rows(2);
  tasks[0].progressUpdated = new Date(now - 100 * DAY).toISOString();
  const view = render(board(tasks, "time"));
  fireEvent.click(screen.getByRole("button", { name: "Expand all" }));
  expect(view.container.querySelector(".rt-rail")!.getAttribute("x1")).toBe("208");
  expect(screen.queryByText("Task 1")).toBeNull();
});
it("refreshes directional phone relation counts when tasks change", () => {
  vi.spyOn(Date, "now").mockReturnValue(now);
  vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(390);
  const tasks = rows(3);
  tasks[0].relatedTo = {
    "task-1": { relation: { next: true } },
    "task-2": { relation: { next: true } },
  };
  const view = render(board(tasks));
  fireEvent.click(screen.getByRole("button", { name: /Source/ }));
  expect(screen.getByRole("button", { name: "Task 1" }).textContent).toContain(
    "Connects to 2 tasks",
  );
  expect(screen.getByRole("button", { name: "Task 2" }).textContent).toContain(
    "Connected from 1 tasks",
  );
  view.rerender(board(tasks.slice(0, 2)));
  expect(screen.getByRole("button", { name: "Task 1" }).textContent).toContain(
    "Connects to 1 tasks",
  );
  expect(screen.queryByRole("button", { name: "Task 3" })).toBeNull();
});
it("keeps an unresolved failure visible after the recent board window", () => {
  vi.spyOn(Date, "now").mockReturnValue(now + 6 * DAY);
  vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(390);
  const view = render(board(rows(1).map((task) => ({ ...task, progress: "failed" }))));
  expect(
    within(view.container.querySelector(".rt-phone-order")! as HTMLElement).getByText("Task 1"),
  ).toBeTruthy();
});
it("zooms observed-close capsules around their observed time", () => {
  vi.spyOn(Date, "now").mockReturnValue(now);
  const tasks = rows(2).map((task) => ({
    ...task,
    created: "2020-01-01T00:00:00Z",
    progressUpdated: undefined,
    "omnigent.ai:closedSeen": "2026-11-06T12:00:00Z",
  }));
  const view = render(board(tasks, "time"));
  fireEvent.click(screen.getByRole("button", { name: "Open 2 stops" }));
  const map = view.container.querySelector(".rt-map")!;
  expect(Number(map.getAttribute("data-window-from"))).toBeLessThan(
    Date.parse("2026-11-06T12:00:00Z"),
  );
  expect(
    Number(map.getAttribute("data-window-to")) - Number(map.getAttribute("data-window-from")),
  ).toBe(12 * 3600000);
  expect(screen.getByRole("button", { name: "Open 2 stops" })).toBeTruthy();
});
it("explains a connection on focus and keyboard open, then opens its current task", () => {
  vi.spyOn(Date, "now").mockReturnValue(now);
  const tasks = rows(2).map((task, index) => ({
    ...task,
    "omnigent.ai:group": { id: index ? "beta" : "alpha", name: index ? "Beta" : "Alpha" },
    relatedTo: index === 0 ? { "task-1": { relation: { next: true } } } : undefined,
  }));
  render(board(tasks));
  fireEvent.click(screen.getByRole("button", { name: "Expand all" }));
  const edge = screen.getByRole("button", { name: "Task 1 → Task 2: Predecessor / successor" });
  fireEvent.focus(edge);
  expect(screen.getByRole("tooltip").textContent).toContain("No relationship date supplied");
  fireEvent.keyDown(edge, { key: "Enter" });
  const panel = screen.getByRole("region", { name: "Related tasks" });
  fireEvent.click(within(panel).getByRole("button", { name: "task-1" }));
  expect(screen.getByRole("region", { name: "Details: Task 2" })).toBeTruthy();
});
it("preserves plain vertical scrolling, pans the overview and restores the original now window", () => {
  vi.spyOn(Date, "now").mockReturnValue(now);
  const tasks = rows(2);
  tasks[0].progressUpdated = new Date(now - 100 * DAY).toISOString();
  const view = render(board(tasks, "time"));
  const map = view.container.querySelector(".rt-map")!,
    original = [map.getAttribute("data-window-from"), map.getAttribute("data-window-to")];
  const plain = new WheelEvent("wheel", { deltaY: 120, cancelable: true, bubbles: true });
  act(() => {
    map.dispatchEvent(plain);
  });
  expect(plain.defaultPrevented).toBe(false);
  fireEvent.click(screen.getByRole("button", { name: "Zoom out" }));
  expect(Number(map.getAttribute("data-window-to"))).toBe(Number(original[1]));
  expect(
    Number(map.getAttribute("data-window-to")) - Number(map.getAttribute("data-window-from")),
  ).toBe(4 * DAY);
  const overview = screen.getByRole("region", { name: /All-history overview/ }),
    pan = new WheelEvent("wheel", { deltaY: -90, cancelable: true, bubbles: true });
  act(() => {
    overview.dispatchEvent(pan);
  });
  expect(pan.defaultPrevented).toBe(true);
  expect(map.getAttribute("data-window-from")).not.toBe(original[0]);
  fireEvent.keyDown(map, { key: "0" });
  expect([map.getAttribute("data-window-from"), map.getAttribute("data-window-to")]).toEqual(
    original,
  );
});
it("shows scheduled native and source items with no held task and hides them at coarse detail", () => {
  vi.spyOn(Date, "now").mockReturnValue(now);
  const at = new Date(now + 3600000).toISOString();
  const source: SourceCache = {
    pin: {
      sourceGeneration: "one",
      id: "snapshot",
      asOf: new Date(now).toISOString(),
      phase: "published",
    },
    display: {
      version: 1,
      asOf: new Date(now).toISOString(),
      timeZone: "UTC",
      future: [
        { id: "shared", label: "Source schedule", at, sourceLabel: "Example feed" },
        { id: "external", label: "External schedule", at, sourceLabel: "Other calendar" },
      ],
      closedDays: [{ date: "2026-01-01", count: 12 }],
    },
    tasks: [],
    history: [],
    counts: [],
  };
  const view = render(
    board([], "time", {
      sourceCaches: { one: source },
      scheduled: [{ id: "shared", name: "Native schedule", state: "active", next_run_at: at }],
    }),
  );
  fireEvent.click(screen.getByLabelText("2 scheduled items"));
  expect(screen.getByRole("link", { name: "Source schedule" }).getAttribute("href")).toBe(
    "/tasks/shared",
  );
  expect(screen.getByText("External schedule")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "30D" }));
  expect(view.container.querySelector(".rt-future")).toBeNull();
  expect(view.container.querySelector(".rt-density-hit")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "All" }));
  expect(view.container.querySelector(".rt-density-hit")).toBeTruthy();
});

it("opens explicit hub membership grouped by source and refreshes its task counts", () => {
  vi.spyOn(Date, "now").mockReturnValue(now);
  const tasks = rows(4).map((task, index) => ({
    ...task,
    "omnigent.ai:source": index > 1 ? "second" : "one",
    progress: index === 0 ? ("in-process" as const) : ("needs-action" as const),
    "omnigent.ai:group": { id: index === 0 ? "root" : "children" },
    "omnigent.ai:sourceDisplay": {
      version: 1 as const,
      state: index === 0 ? ("running" as const) : ("held" as const),
    },
  }));
  tasks[0]["omnigent.ai:sourceDisplay"] = {
    ...tasks[0]["omnigent.ai:sourceDisplay"],
    hub: {
      scope: "explicit",
      members: tasks
        .slice(1)
        .map((task) => ({ source: task["omnigent.ai:source"], uid: task.uid })),
    },
  } as (typeof tasks)[0]["omnigent.ai:sourceDisplay"];
  const view = render(
    board(tasks, "order", {
      sources: [
        { id: "one", name: "Source one", source: null },
        { id: "second", name: "Source two", source: null },
      ],
    }),
  );
  fireEvent.click(screen.getByRole("button", { name: "3 child tasks" }));
  const panel = screen.getByRole("region", { name: "Child tasks" }),
    groups = panel.querySelectorAll(".rt-hub-groups>section");
  expect(groups).toHaveLength(2);
  expect(groups[0].textContent).toContain("Source two");
  expect(groups[0].textContent).toContain("Task 3");
  expect(groups[1].textContent).toContain("Task 2");
  view.rerender(
    board(tasks.slice(0, 2), "order", {
      sources: [{ id: "one", name: "Source one", source: null }],
    }),
  );
  expect(panel.textContent).toContain("1 child tasks");
  expect(panel.textContent).not.toContain("Task 3");
});

it("pages the visible phone group set by ten and source bands by twelve", () => {
  vi.spyOn(Date, "now").mockReturnValue(now);
  vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(390);
  const groups = rows(11).map((task, index) => ({
    ...task,
    progress: "in-process" as const,
    "omnigent.ai:group": { id: `group-${index}`, name: `Group ${index + 1}` },
  }));
  const view = render(board(groups));
  expect(view.container.querySelectorAll(".rt-phone-lane")).toHaveLength(10);
  const firstPage = [...view.container.querySelectorAll(".rt-phone-lane h3")].map(
    (el) => el.textContent,
  );
  fireEvent.click(screen.getByRole("button", { name: "Next page" }));
  expect(view.container.querySelectorAll(".rt-phone-lane")).toHaveLength(1);
  const last = view.container.querySelector(".rt-phone-lane h3")!.textContent;
  expect(firstPage).not.toContain(last);
  expect(new Set([...firstPage, last]).size).toBe(11);
  const sources = Array.from({ length: 13 }, (_, index) => ({
    id: `source-${index}`,
    name: `Source ${index + 1}`,
    source: null,
  }));
  const tasks = sources.map((source, index) => ({
    ...groups[0],
    uid: `source-task-${index}`,
    "omnigent.ai:source": source.id,
  }));
  view.rerender(board(tasks, "order", { sources }));
  expect(view.container.querySelectorAll(".rt-phone-source")).toHaveLength(12);
  fireEvent.click(screen.getByRole("button", { name: "Next page" }));
  expect(view.container.querySelectorAll(".rt-phone-source")).toHaveLength(1);
  expect(screen.getByText("Source 13")).toBeTruthy();
});
