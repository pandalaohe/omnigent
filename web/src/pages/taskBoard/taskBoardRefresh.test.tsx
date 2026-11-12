import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";
import { TaskBoardRoutes } from "./TaskBoardRoutes";
import type { BoardTask } from "./taskBoardData";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});
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
  onSelect: (task: BoardTask) => void = () => {},
) {
  return (
    <MemoryRouter>
      <TaskBoardRoutes
        tasks={tasks}
        sources={[{ id: "one", name: "Source", source: null }]}
        aliases={{}}
        mode={mode}
        onMode={() => {}}
        focus={[]}
        onFocus={() => {}}
        onSelect={onSelect}
        scheduled={[]}
      />
    </MemoryRouter>
  );
}
it("refreshes an open cluster from current task identities after updates and deletions", () => {
  vi.spyOn(Date, "now").mockReturnValue(Date.parse("2026-11-06T13:00:00Z"));
  const tasks = rows(16),
    view = render(board(tasks));
  fireEvent.click(screen.getByRole("button", { name: "Open 16 stops" }));
  const cluster = screen.getByRole("region", { name: "Stops in this group" });
  fireEvent.click(within(cluster).getByRole("button", { name: "Next page" }));
  expect(within(cluster).getByText("Task 16")).toBeTruthy();
  view.rerender(
    board(
      tasks
        .slice(0, 11)
        .map((task, index) => (index === 0 ? { ...task, title: "Updated task" } : task)),
    ),
  );
  expect(within(cluster).getByText("Updated task")).toBeTruthy();
  expect(within(cluster).queryByText("Task 16")).toBeNull();
  view.rerender(board([]));
  expect(screen.queryByRole("region", { name: "Stops in this group" })).toBeNull();
});
it("clamps the phone page after tasks disappear", () => {
  vi.spyOn(Date, "now").mockReturnValue(Date.parse("2026-11-06T13:00:00Z"));
  const tasks = rows(13),
    view = render(board(tasks));
  const phone = within(view.container.querySelector(".tb-route-phone")! as HTMLElement);
  fireEvent.click(phone.getByRole("button", { name: "Next page" }));
  expect(phone.getByText("Task 13")).toBeTruthy();
  view.rerender(board(tasks.slice(0, 11)));
  expect(phone.getByText("Task 1")).toBeTruthy();
  expect(phone.queryByText("Task 13")).toBeNull();
});
it("keeps an unresolved failure visible after the recent board window", () => {
  vi.spyOn(Date, "now").mockReturnValue(Date.parse("2026-11-12T13:00:00Z"));
  const view = render(board(rows(1).map((task) => ({ ...task, progress: "failed" }))));
  const phone = within(view.container.querySelector(".tb-route-phone")! as HTMLElement);
  expect(phone.getByText("Task 1")).toBeTruthy();
});
it("zooms observed-close clusters around their observed time", () => {
  const now = Date.parse("2026-11-06T13:00:00Z");
  vi.spyOn(Date, "now").mockReturnValue(now);
  const tasks = rows(2).map((task) => ({
    ...task,
    created: "2020-01-01T00:00:00Z",
    progressUpdated: undefined,
    "omnigent.ai:closedSeen": "2026-11-06T12:00:00Z",
  }));
  const view = render(board(tasks, "time"));
  fireEvent.click(screen.getByRole("button", { name: "Open 2 stops" }));
  const graph = view.container.querySelector("svg[aria-label='Task route map']")!;
  expect(graph.querySelectorAll("g.tb-station")).toHaveLength(1);
  expect(screen.getByRole("button", { name: "Open 2 stops" })).toBeTruthy();
});
it("lets a route relation open its endpoints", () => {
  vi.spyOn(Date, "now").mockReturnValue(Date.parse("2026-11-06T13:00:00Z"));
  const tasks = rows(17).map((task, index) => ({
    ...task,
    "omnigent.ai:group": { id: index < 16 ? "cluster" : "target" },
    relatedTo: index === 15 ? { "task-16": { relation: { next: true } } } : undefined,
  }));
  const onSelect = vi.fn();
  render(board(tasks, "order", onSelect));
  fireEvent.click(
    screen.getByRole("button", { name: "Task 16 → Task 17: Predecessor / successor" }),
  );
  const panel = screen.getByRole("region", { name: "Related tasks" });
  expect(within(panel).getAllByRole("button")).toHaveLength(3);
  expect(within(panel).queryByText("Task 1", { exact: true })).toBeNull();
  fireEvent.click(within(panel).getByRole("button", { name: /Task 16$/ }));
  expect(onSelect).toHaveBeenLastCalledWith(tasks[15]);
  fireEvent.click(within(panel).getByRole("button", { name: /Task 17$/ }));
  expect(onSelect).toHaveBeenLastCalledWith(tasks[16]);
});
