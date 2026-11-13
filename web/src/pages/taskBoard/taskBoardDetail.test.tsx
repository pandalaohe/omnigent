import userEvent from "@testing-library/user-event";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";
import { TaskBoardDetail } from "./TaskBoardDetail";
import type { BoardTask } from "./taskBoardData";
import type { DisplaySession } from "./taskBoardDisplay";
import { boardGroupColors, boardSessionRows } from "./taskBoardSessionData";
import type { BoardSession } from "./taskBoardNative";
import type { SourceCache } from "./taskBoardSnapshotData";

vi.mock("./taskBoardNative", () => ({ useBoardSessionDetails: () => [] }));
afterEach(cleanup);
it("uses readable task names and agent names in selected-session headings and timeline tooltips", () => {
  const internal = "abcdef0123456789abcdef0123456789";
  const current: BoardTask = {
    ...task,
    title: "Readable task",
    "omnigent.ai:sourceDisplay": {
      version: 1,
      identity: { key: "ENG-1", label: `ENG-1@${internal}`, groupId: internal },
      intervals: [
        {
          id: "span",
          from: "2026-11-12T01:00:00Z",
          to: "2026-11-12T02:00:00Z",
          agentId: internal,
          origin: "event",
        },
      ],
    },
  };
  const { container } = mount(current, "source-session");
  expect(container.querySelector(".tb-dc-header > b")?.textContent).toBe("Readable task");
  const title = container.querySelector(".tb-dc-segment")?.getAttribute("title");
  expect(title).toContain("Agent");
  expect(title).not.toContain(internal);
});
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
function mount(
  current = task,
  selectedSessionId?: string,
  native: boolean | BoardSession[] = false,
  allTasks = [current, other],
) {
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
          tasks={allTasks}
          sessions={
            Array.isArray(native)
              ? native
              : native
                ? [{ id: "session-1", title: "Session 1", status: "idle", archived: true }]
                : []
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
  expect(container.querySelector(".tb-dc-segment")?.getAttribute("title")).toContain("Pi");
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
  expect(screen.queryByText("Handlers")).toBeNull();
  expect(screen.queryByText("Tests")).toBeNull();
  expect(screen.queryByText("Commits")).toBeNull();
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
  expect(document.querySelector(".tb-dc-segment.live")).toBeNull();
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

it.each(["next", "first"])(
  "retains %s ordering and a dependency on both ends in frame mode",
  (relation) => {
    window.history.replaceState({}, "", "/frame/task-board");
    const left: BoardTask = {
      uid: "left",
      title: "Left",
      "omnigent.ai:source": "example",
      relatedTo: { right: { relation: { [relation]: true, "omnigent.ai:depends-on": true } } },
    };
    const right: BoardTask = { uid: "right", title: "Right", "omnigent.ai:source": "example" };
    const view = mount(left, undefined, false, [left, right]);
    const forward = relation === "next";
    expect(screen.getByText(forward ? "Successor" : "Predecessor")).toBeTruthy();
    expect(screen.getByText("Depends on")).toBeTruthy();
    expect(view.container.querySelectorAll(".tb-dc-relation")).toHaveLength(2);
    view.unmount();
    mount(right, undefined, false, [left, right]);
    expect(screen.getByText(forward ? "Predecessor" : "Successor")).toBeTruthy();
    expect(screen.getByText("Blocking")).toBeTruthy();
    window.history.replaceState({}, "", "/");
  },
);
it.each([0, 1.25])("always marks actual cost %s and explains pricing", (amount) => {
  const { container } = mount({
    ...task,
    "omnigent.ai:sourceDisplay": {
      version: 1,
      sessions: [
        { ...sessions[0], cost: { amount, kind: "actual", sourceLabel: "Source pricing" } },
      ],
    },
  });
  expect(container.querySelector(".tb-dc-money.actual i")?.textContent).toBe("Actual");
  expect(container.querySelector(".tb-dc-money")?.getAttribute("title")).toContain(
    "Source pricing",
  );
  expect(container.querySelector(".tb-dc-money")?.textContent).toContain(`$${amount.toFixed(2)}`);
});
it("marks mixed known and unknown estimated nodes and retains missing-price information", () => {
  const { container } = mount();
  expect(container.querySelector(".tb-dc-money.estimate i")?.textContent).toBe("Est.");
  expect(container.querySelector(".tb-dc-money")?.getAttribute("title")).toContain("4 unpriced");
  expect(container.querySelector(".tb-dc-money")?.getAttribute("title")).toContain("token");
});
it("shares full-board collision colours even without sessions, and separates logo treatments", () => {
  const make = (uid: string, group: string): BoardTask => ({
    ...task,
    uid,
    "omnigent.ai:sourceDisplay": {
      ...task["omnigent.ai:sourceDisplay"]!,
      identity: { key: uid, label: uid, groupId: group, groupBadge: "G" },
      sessions: sessions.map((item) => ({
        ...item,
        id: uid + item.id,
        task: { source: "example", uid },
      })),
    },
  });
  const first = make("first", "a"),
    second = make("second", "m");
  // These group hashes collide in the twelve-colour palette.
  const all = [first, second];
  const colors = boardGroupColors(all);
  expect(colors.get(JSON.stringify(["example", "first"]))).not.toBe(
    colors.get(JSON.stringify(["example", "second"])),
  );
  const { container, unmount } = mount(second, undefined, false, all);
  expect(container.querySelector<HTMLElement>(".tb-dc-group-badge")?.style.background).toBe(
    "rgb(0, 145, 200)",
  );
  expect(boardSessionRows(all, []).find((row) => row.task?.uid === "second")?.color).toBe(
    colors.get(JSON.stringify(["example", "second"])),
  );
  expect(
    container.querySelector(".tb-dc-who .tb-agent-logo:not(.compact) svg")?.getAttribute("width"),
  ).toBe("18");
  expect(
    container.querySelector(".tb-dc-axis .tb-agent-logo.compact svg")?.getAttribute("width"),
  ).toBe("11");
  expect(container.querySelector(".tb-dc-sessions .tb-identity-mark")).toBeTruthy();
  unmount();
  const empty = {
    ...second,
    "omnigent.ai:sourceDisplay": { ...second["omnigent.ai:sourceDisplay"]!, sessions: [] },
  };
  const view = mount(empty, undefined, false, [first, empty]);
  expect(view.container.querySelector<HTMLElement>(".tb-dc-group-badge")?.style.background).toBe(
    "rgb(0, 145, 200)",
  );
});
it.each([undefined, "session-0"])(
  "shows tool name, elapsed and cap at source asOf in context %s",
  (selected) => {
    const tool = {
      id: "tool",
      name: "shell",
      label: "Run checks",
      startedAt: "2026-11-12T03:50:00Z",
      limitSeconds: 1200,
    };
    const current = {
      ...task,
      "omnigent.ai:sourceDisplay": { version: 1 as const, sessions: [{ ...sessions[0], tool }] },
    };
    const { container, unmount } = mount(current, selected);
    expect(container.querySelector(".tb-dc-tool")?.textContent).toBe(
      "Run checks (shell) running 10 min, limit 20 min",
    );
    unmount();
    const noCap = {
      ...current,
      "omnigent.ai:sourceDisplay": {
        version: 1 as const,
        sessions: [{ ...sessions[0], tool: { ...tool, limitSeconds: undefined } }],
      },
    };
    const next = mount(noCap, selected);
    expect(next.container.querySelector(".tb-dc-tool")?.textContent).toBe(
      "Run checks (shell) running 10 min",
    );
    next.unmount();
    mount(
      {
        ...current,
        "omnigent.ai:sourceDisplay": {
          version: 1,
          sessions: [{ ...sessions[0], tool: { ...tool, limitSeconds: 300 } }],
        },
      },
      selected,
    );
    expect(document.querySelector(".tb-dc-tool")).toBeNull();
  },
);

it("uses the native tool tuple and live clock instead of the older source tool", () => {
  vi.spyOn(Date, "now").mockReturnValue(Date.parse("2026-11-12T05:00:00Z"));
  const tool = {
    id: "old",
    name: "old-tool",
    label: "Source work",
    startedAt: "2026-11-12T03:50:00Z",
  };
  const current = {
    ...task,
    "omnigent.ai:sourceDisplay": { version: 1 as const, sessions: [{ ...sessions[0], tool }] },
  };
  const { container } = mount(current, "session-0", [
    {
      id: "session-0",
      status: "running",
      updated_at: Date.now() / 1000,
      current_tool: {
        id: "native",
        name: "native-shell",
        label: "Native work",
        startedAt: "2026-11-12T04:50:00Z",
        limitSeconds: 1200,
      },
    },
  ]);
  expect(container.querySelector(".tb-dc-tool")?.textContent).toBe(
    "Native work (native-shell) running 10 min, limit 20 min",
  );
  expect(screen.queryByText(/Source work/)).toBeNull();
  vi.restoreAllMocks();
});

it("shows file short names, deduplicates paths, folds old versions and reports clipboard success or failure", async () => {
  const { TaskSourceDetails } = await import("./TaskSourceDetails");
  const path = "/opt/work/261024_T260101-EX-AG-001_task-placement-trust-separation_design.html";
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
  render(
    <TaskSourceDetails
      display={{
        version: 1,
        rows: [
          { id: "path-0", label: "Design", value: path },
          { id: "path-1", label: "Design", value: path },
          {
            id: "path-2",
            label: "Design",
            value:
              "/opt/work/generation-history/v1/261024_T260101-EX-AG-001_task-placement-trust-separation_design.md",
          },
          { id: "event_age", label: "Snapshot event age", value: "12345" },
        ],
      }}
    />,
  );
  const file = screen.getByTitle(path);
  expect(file.textContent).toContain("task placement trust separation");
  expect(screen.getAllByTitle(path)).toHaveLength(1);
  expect(screen.queryByText("Snapshot event age")).toBeNull();
  expect(screen.queryByTitle(/generation-history/)).toBeNull();
  fireEvent.click(file);
  await screen.findByText("Copied");
  expect(writeText).toHaveBeenCalledWith(path);
  writeText.mockRejectedValueOnce(Error("denied"));
  fireEvent.click(file);
  await screen.findByText("Copy failed — try again");
  fireEvent.click(screen.getByRole("button", { name: "Older versions (1)" }));
  expect(screen.getByTitle(/generation-history/)).toBeTruthy();
});

it("activates artifact copy by keyboard and hides placeholder path actions", async () => {
  const { TaskSourceDetails } = await import("./TaskSourceDetails");
  const user = userEvent.setup();
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
  const path = "/opt/work/omnigent/fork/example/design.html";
  const { container } = render(
    <TaskSourceDetails
      display={{
        version: 1,
        rows: [
          { id: "path-0", label: "Design", value: "—" },
          { id: "path-1", label: "Design", value: path },
        ],
      }}
    />,
  );
  expect(container.querySelectorAll(".tb-file")).toHaveLength(1);
  await user.tab();
  expect(document.activeElement).toBe(screen.getByTitle(path));
  await user.keyboard("{Enter}");
  await screen.findByText("Copied");
  expect(writeText).toHaveBeenCalledWith(path);
  await user.keyboard(" ");
  expect(writeText).toHaveBeenCalledTimes(2);
});
