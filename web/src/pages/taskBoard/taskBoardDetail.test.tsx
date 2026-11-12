import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";
import { TaskBoardDetail } from "./TaskBoardDetail";
import type { BoardTask } from "./taskBoardData";
import type { DisplaySession } from "./taskBoardDisplay";
import type { SourceCache } from "./taskBoardSnapshotData";

vi.mock("./taskBoardNative", () => ({ useBoardSessionDetails: () => [] }));
afterEach(cleanup);
const asOf = "2026-11-12T04:00:00Z";
const ref = { source: "example", uid: "task" };
const sessions: DisplaySession[] = Array.from({ length: 5 }, (_, index) => ({
  id: `session-${index}`,
  task: ref,
  role: "root",
  agentId: index ? "unknown" : "codex",
  label: `Agent ${index}`,
  title: `Session ${index}`,
  status: index ? "archived" : "running",
  createdAt: "2026-11-12T02:00:00Z",
  lastActivityAt: "2026-11-12T03:00:00Z",
  archivedAt: index ? asOf : undefined,
  cost: { amount: index ? null : 0.06, kind: "estimate" },
  now: "Current work",
  next: "Next work",
  need: "Decision required",
}));
const task: BoardTask = {
  uid: "task",
  title: "Example task",
  "omnigent.ai:source": "example",
  "omnigent.ai:sourceDisplay": {
    version: 1,
    state: "running",
    identity: {
      key: "ENG-1",
      label: "Example task",
      groupId: "work",
      groupLabel: "Example group",
      groupBadge: "ENG",
    },
    current: "Current fallback",
    next: "Next fallback",
    sessions,
    intervals: [
      {
        id: "span",
        from: "2026-11-12T01:00:00Z",
        to: null,
        agentId: "pi",
        origin: "event",
        cost: { amount: 999, kind: "actual" },
      },
    ],
    quotes: [
      { id: "quote", text: "Literal <script>example</script> text", date: "2026-11-12", task: ref },
    ],
    commits: Array.from({ length: 5 }, (_, index) => ({
      id: String(index),
      sha: `abcdef${index}`,
      subject: `Subject ${index}`,
      at: `2026-11-12T0${index}:00:00Z`,
    })),
    checks: [{ id: "check", host: "workstation", verdict: "failed", count: 0, at: asOf }],
    relations: Array.from({ length: 5 }, (_, index) => ({
      id: String(index),
      from: { source: "example", uid: `other-${index}` },
      to: ref,
      kind: "dependency",
      origin: "manual",
      provenance: "Source note",
    })),
  },
};
const other: BoardTask = {
  uid: "other-0",
  title: "Upstream task",
  "omnigent.ai:source": "example",
};
function mount(current = task, selectedSessionId?: string, native = false) {
  const cache = {
    display: { version: 1, asOf, timeZone: "UTC", checkHosts: ["workstation", "other-host"] },
  } as SourceCache;
  const onSelect = vi.fn(),
    onClose = vi.fn();
  const view = render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter>
        <TaskBoardDetail
          task={current}
          tasks={[current, other]}
          sessions={
            native ? [{ id: "session-1", title: "Session 1", status: "idle", archived: true }] : []
          }
          sourceCache={cache}
          onSelect={onSelect}
          onClose={onClose}
          selectedSessionId={selectedSessionId}
        />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...view, onSelect, onClose };
}
it("renders distinct current, need and next, estimated sessions-first cost, checks, literal quote and recent three commits", () => {
  const { container } = mount();
  expect(screen.getByText("Current work")).toBeTruthy();
  expect(container.querySelector(".tb-dc-group-badge")?.textContent).toBe("ENG");
  expect(screen.getByText("Decision required")).toBeTruthy();
  expect(screen.getByText("Next work")).toBeTruthy();
  expect(screen.getByText("$0.06")).toBeTruthy();
  expect(screen.queryByText("$999.00")).toBeNull();
  expect(container.querySelector(".tb-dc-tile.failed")?.textContent).toContain("0 items");
  expect(container.querySelector(".tb-dc-tile.failed")?.textContent).toContain(
    "other-host No record",
  );
  expect(container.querySelector(".tb-dc-segment")?.getAttribute("title")).toContain("pi");
  expect(container.querySelectorAll(".tb-dc-commits li")).toHaveLength(3);
  expect(container.querySelector(".tb-dc-commits li")?.textContent).toContain("abcdef4");
  expect(container.querySelector("blockquote")?.textContent).toContain("<script>example</script>");
  expect(container.querySelector("script")).toBeNull();
});
it("excludes the expanded session from the three-session footer and avoids inventing source-only exits", () => {
  const { container } = mount(task, "session-0");
  expect(container.querySelector(".tb-dc-now")).toBeNull();
  const footer = container.querySelector(".tb-dc-sessions") as HTMLElement;
  expect(within(footer).queryByText("Session 0")).toBeNull();
  expect(footer.querySelectorAll("li")).toHaveLength(3);
  expect(footer.querySelector("a")).toBeNull();
});
it("selects scoped relations, pages four at a time and closes without executing actions", () => {
  const { container, onSelect, onClose } = mount();
  expect(container.querySelectorAll(".tb-dc-relation")).toHaveLength(4);
  fireEvent.click(screen.getByRole("button", { name: /Upstream task/ }));
  expect(onSelect).toHaveBeenCalledWith(other);
  fireEvent.click(screen.getByRole("button", { name: "Next page" }));
  expect(container.querySelectorAll(".tb-dc-relation")).toHaveLength(1);
  fireEvent.click(screen.getByRole("button", { name: "Close" }));
  expect(onClose).toHaveBeenCalledOnce();
});
it("keeps missing cost absent and known zero visible without empty footer scaffolding", () => {
  const { container, unmount } = mount({ ...task, "omnigent.ai:sourceDisplay": { version: 1 } });
  expect(container.querySelector(".tb-dc-footer")).toBeNull();
  expect(screen.queryByText("Cost")).toBeNull();
  unmount();
  mount({
    ...task,
    "omnigent.ai:sourceDisplay": {
      version: 1,
      intervals: [
        {
          id: "zero",
          from: asOf,
          to: asOf,
          agentId: "unknown",
          origin: "event",
          cost: { amount: 0, kind: "actual" },
        },
      ],
    },
  });
  expect(screen.getByText("$0.00")).toBeTruthy();
});

it("opens a real session outside the board frame in a new tab", () => {
  window.history.replaceState({}, "", "/frame/task-board");
  const { container } = mount(task, "session-0", true);
  const link = container.querySelector(".tb-dc-sessions a");
  expect(link?.getAttribute("href")).toBe("/c/session-1");
  expect(link?.getAttribute("target")).toBe("_blank");
  expect(link?.getAttribute("rel")).toContain("noopener");
  window.history.replaceState({}, "", "/");
});

it("does not charge a session whose explicit task reference belongs to another source", () => {
  const foreign = {
    ...sessions[0],
    task: { source: "other", uid: "task" },
    cost: { amount: 500, kind: "actual" as const },
  };
  mount({ ...task, "omnigent.ai:sourceDisplay": { version: 1, sessions: [foreign] } });
  expect(screen.queryByText("$500.00")).toBeNull();
  expect(screen.queryByText("Cost")).toBeNull();
});
