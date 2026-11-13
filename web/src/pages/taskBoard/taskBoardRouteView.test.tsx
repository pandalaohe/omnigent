import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { TaskBoardRoutes } from "./TaskBoardRoutes";
import type { BoardTask } from "./taskBoardData";
import { DAY } from "./taskBoardRouteData";
import { ROUTE_VIEW_KEY } from "./taskBoardRouteView";

const now = Date.parse("2026-11-06T13:00:00Z");
const stored = () => JSON.parse(localStorage.getItem(ROUTE_VIEW_KEY) ?? "{}");
beforeEach(() => {
  localStorage.clear();
  vi.spyOn(Date, "now").mockReturnValue(now);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});
const card = (uid: string, group: string, extra: Partial<BoardTask> = {}): BoardTask => ({
  uid,
  title: `Card ${uid}`,
  progress: "needs-action",
  created: "2026-10-01T00:00:00Z",
  updated: "2026-10-01T00:00:00Z",
  "omnigent.ai:source": group.startsWith("quiet") ? "archive" : "work",
  "omnigent.ai:group": { id: group, name: `Group ${group}` },
  ...extra,
});
function board(tasks: BoardTask[]) {
  return (
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter>
        <TaskBoardRoutes
          tasks={tasks}
          sources={[
            { id: "work", name: "Work", source: null },
            { id: "archive", name: "Archive", source: null },
          ]}
          aliases={{}}
          mode="order"
          onMode={() => {}}
          focus={[]}
          onFocus={() => {}}
          onSelect={() => {}}
          scheduled={[]}
        />
      </MemoryRouter>
    </QueryClientProvider>
  );
}
const live = {
  progress: "in-process" as const,
  "omnigent.ai:workedBy": [{ session: "s", from: "2026-11-06" }],
};
const tasks = [
  card("1", "busy", live),
  card("2", "busy"),
  card("3", "recent", { updated: new Date(now - DAY).toISOString() }),
  card("4", "stale"),
  card("5", "quiet-a"),
];

it("shows rows needing attention by default and folds the rest behind show all", () => {
  render(board(tasks));
  expect(screen.getByRole("heading", { name: "Group busy" })).toBeTruthy();
  expect(screen.getByRole("heading", { name: "Group recent" })).toBeTruthy();
  expect(screen.queryByRole("heading", { name: "Group stale" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: /1 more project with nothing needing/ }));
  expect(stored().scope).toBe("all");
  expect(screen.getByRole("button", { name: "All rows", pressed: true })).toBeTruthy();
});

it("pins a group into the attention view and unpins it in place", () => {
  render(board(tasks));
  fireEvent.click(screen.getByRole("button", { name: "Keep in view" }));
  const panel = screen.getByRole("region", { name: "Keep in view" });
  const search = within(panel).getByPlaceholderText("Search a task or group; Enter pins the first");
  fireEvent.change(search, { target: { value: "Group stale" } });
  fireEvent.keyDown(search, { key: "Enter" });
  expect(stored().pins.groups).toEqual([JSON.stringify(["work", "stale"])]);
  expect(screen.getByRole("heading", { name: "Group stale" })).toBeTruthy();
  fireEvent.click(within(panel).getByRole("checkbox", { checked: true }));
  expect(stored().pins.groups).toEqual([]);
  expect(screen.queryByRole("heading", { name: "Group stale" })).toBeNull();
});

it("starts with compact rows and keeps the taller final rows once chosen", () => {
  const view = render(board(tasks));
  const lane = () =>
    view.container.querySelector<HTMLElement>(".rt-lane:not(.rt-source):not(.quiet)")!.style.height;
  expect(lane()).toBe("80px");
  fireEvent.click(screen.getByRole("button", { name: "Taller rows" }));
  expect(lane()).toBe("136px");
  expect(stored().rows).toBe("final");
});

it("animates flow only on live rows and caps it", () => {
  // Twenty live rows over two projects: more running segments than the cap.
  const busy = Array.from({ length: 20 }, (_, index) => {
    const source = { "omnigent.ai:source": index % 2 ? "archive" : "work" };
    return [
      card(`a${index}`, `g${index}`, { ...live, ...source }),
      card(`b${index}`, `g${index}`, { progress: "in-process", ...source }),
    ];
  }).flat();
  localStorage.setItem(ROUTE_VIEW_KEY, JSON.stringify({ scope: "all", rows: "compact" }));
  const view = render(board(busy));
  const flows = view.container.querySelectorAll(".rt-flow").length;
  expect(view.container.querySelectorAll(".rt-stop .rt-running")).toHaveLength(20);
  expect(flows).toBe(12);
  expect(view.container.querySelectorAll(".rt-symbol.rt-held").length).toBeGreaterThan(0);
});

it("dims the map for a connector only once the pointer rests on it", () => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  const linked = [
    card("x", "busy", { ...live, relatedTo: { y: { relation: { parent: true } } } }),
    card("y", "recent", { updated: new Date(now - DAY).toISOString() }),
  ];
  const view = render(board(linked));
  const edge = view.container.querySelector(".rt-edge-hit")!;
  fireEvent.mouseEnter(edge);
  fireEvent.mouseLeave(edge);
  act(() => vi.advanceTimersByTime(200));
  expect(view.container.querySelector(".rt-edge-hover")).toBeNull();
  fireEvent.mouseEnter(edge);
  act(() => vi.advanceTimersByTime(200));
  expect(view.container.querySelector(".rt-edge-hover")).toBeTruthy();
  vi.useRealTimers();
});

it("keeps recent and pinned cards that the board window would drop", () => {
  const old = new Date(now - 10 * DAY).toISOString();
  const cards = [
    card("1", "busy", live),
    card("late", "closed-late", {
      progress: "completed",
      progressUpdated: new Date(now - 2 * DAY).toISOString(),
      updated: new Date(now - DAY).toISOString(),
    }),
    card("archived", "archive-task", { progress: "completed", progressUpdated: old, updated: old }),
    card("gone", "archive-group", { progress: "completed", progressUpdated: old, updated: old }),
  ];
  localStorage.setItem(
    ROUTE_VIEW_KEY,
    JSON.stringify({
      pins: {
        tasks: [JSON.stringify(["work", "archived"])],
        groups: [JSON.stringify(["work", "archive-group"])],
      },
    }),
  );
  const view = render(board(cards));
  for (const name of ["Group closed-late", "Group archive-task", "Group archive-group"])
    expect(screen.getByRole("heading", { name })).toBeTruthy();
  for (const uid of ["late", "archived", "gone"])
    expect(view.container.querySelector(`.rt-stop[data-route-task="${uid}"]`)).toBeTruthy();
});
