import { cleanup, fireEvent, render, renderHook, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import {
  boardDefaults,
  BOARD_PREFERENCES_KEY,
  BoardRefreshContext,
  useBoardCadence,
  useBoardPreferences,
} from "./taskBoardPreferences";
import { TaskBoardDisplaySettings } from "./TaskBoardDisplaySettings";
import { quotaAccounts, TaskBoardQuotaBand, TaskBoardQuotaPanel } from "./TaskBoardQuota";
import { hostReading, rankedHosts, TaskBoardHostsPanel } from "./TaskBoardHosts";
import { boardAttention } from "./boardAttentionData";
import { usageBoardData } from "@/pages/usageBoard/usageBoardFixtures";
import type { SystemStatusHost } from "@/hooks/useSystemStatus";
import { detailLevel } from "./taskBoardRouteData";
import type { BoardTask } from "./taskBoardData";

beforeEach(() => localStorage.clear());
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});
it("keeps the opening default separate from the current tab and resets display only", () => {
  function Settings() {
    const [preferences, update, storage] = useBoardPreferences();
    return (
      <TaskBoardDisplaySettings
        preferences={preferences}
        update={update}
        storage={storage}
        sources={[]}
        accounts={[]}
      />
    );
  }
  const first = render(<Settings />);
  fireEvent.click(screen.getByRole("button", { name: "History" }));
  expect(JSON.parse(localStorage.getItem(BOARD_PREFERENCES_KEY)!).defaultView).toBe("history");
  first.unmount();
  const hook = renderHook(useBoardPreferences);
  expect(hook.result.current[0].view).toBe("history");
  hook.unmount();
  render(<Settings />);
  fireEvent.click(screen.getByRole("button", { name: "Restore defaults" }));
  expect(JSON.parse(localStorage.getItem(BOARD_PREFERENCES_KEY)!)).toMatchObject({
    defaultView: "sessions",
    refresh: 60,
    hiddenSources: [],
  });
});
it("explains malformed storage and retains a failed save for this visit", () => {
  localStorage.setItem(BOARD_PREFERENCES_KEY, "{");
  function Settings() {
    const [preferences, update, storage] = useBoardPreferences();
    return (
      <TaskBoardDisplaySettings
        preferences={preferences}
        update={update}
        storage={storage}
        sources={[]}
        accounts={[]}
      />
    );
  }
  render(<Settings />);
  expect(screen.getByRole("status").textContent).toContain("could not be read");
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw new Error("Storage denied");
  });
  fireEvent.click(screen.getByRole("button", { name: "Off" }));
  expect(screen.getByRole("button", { name: "Off" }).getAttribute("aria-pressed")).toBe("true");
  expect(screen.getByRole("status").textContent).toContain("this visit only");
});
it("honours refresh off and all three intervals, and hides polling with the page", () => {
  const hook = renderHook(useBoardCadence, {
    wrapper: ({ children }) => (
      <BoardRefreshContext.Provider value={300}>{children}</BoardRefreshContext.Provider>
    ),
  });
  expect(hook.result.current()).toBe(300000);
  hook.unmount();
  for (const seconds of [0, 30, 60]) {
    const next = renderHook(useBoardCadence, {
      wrapper: ({ children }) => (
        <BoardRefreshContext.Provider value={seconds}>{children}</BoardRefreshContext.Provider>
      ),
    });
    expect(next.result.current()).toBe(seconds ? seconds * 1000 : false);
    vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    expect(next.result.current()).toBe(false);
    vi.restoreAllMocks();
    next.unmount();
  }
  expect(detailLevel(3 * 86400000, 3, 7)).toBe("station");
  expect(detailLevel(8 * 86400000, 3, 7)).toBe("segment");
});
it("falls back from unsupported saved values with a visible storage explanation", () => {
  localStorage.setItem(
    BOARD_PREFERENCES_KEY,
    JSON.stringify({ defaultView: "unknown", refresh: 7 }),
  );
  const hook = renderHook(useBoardPreferences);
  expect(hook.result.current[0]).toMatchObject({ view: "sessions", refresh: 60 });
  expect(hook.result.current[2]).toBe("invalid");
});
it("uses pin order then fullest window and excludes expired readings", () => {
  const base = usageBoardData().accounts[0];
  const accounts = Array.from({ length: 5 }, (_, index) => ({
    ...base,
    id: `a${index}`,
    display_name: `Account ${index}`,
    windows: base.windows.map((window, wi) => ({
      ...window,
      percent_used: wi === 0 ? 10 : 50 + index,
      past_reset: false,
      state: "ok",
    })),
  }));
  expect(quotaAccounts(accounts, ["a2", "a0"]).map((account) => account.id)).toEqual([
    "a2",
    "a0",
    "a4",
    "a3",
    "a1",
  ]);
  const open = vi.fn();
  render(
    <TaskBoardQuotaBand
      accounts={accounts}
      preferences={{ ...boardDefaults, pinnedQuota: ["a2", "a0"] }}
      onOpen={open}
      loading={false}
      failed={false}
      retry={vi.fn()}
    />,
  );
  const slots = screen.getByRole("region", { name: "Quota" });
  expect(within(slots).getAllByRole("button")[1].textContent).toContain("Account 2");
  fireEvent.click(within(slots).getAllByRole("button")[1]);
  expect(open).toHaveBeenCalledOnce();
  cleanup();
  const update = vi.fn();
  render(
    <TaskBoardQuotaPanel
      accounts={accounts}
      preferences={{ ...boardDefaults, pinnedQuota: ["a0", "a1", "a2", "a3"] }}
      update={update}
    />,
  );
  expect(
    (screen.getByRole("button", { name: "Pin Account 4" }) as HTMLButtonElement).disabled,
  ).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "Unpin Account 0" }));
  expect(update).toHaveBeenCalledWith({ pinnedQuota: ["a1", "a2", "a3"] });
  expect(
    quotaAccounts(
      [
        { ...base, state: "paused" },
        { ...base, freshness: "old" },
      ],
      [],
    ),
  ).toEqual([]);
});
it("ranks host load against its own threshold and reaches every paged host", () => {
  const hosts: SystemStatusHost[] = Array.from({ length: 10 }, (_, i) => ({
    host_id: `h${i}`,
    name: `Host ${i}`,
    owner: null,
    state: "online",
    since: null,
    last_snapshot: null,
    last_runner_count: 0,
    load_per_core: i,
    lines: { heavy: i === 9 ? 100 : 2, new_session: 101 },
    mem_pct: 40,
    cpu_count: 8,
  }));
  expect(rankedHosts(hosts)[0].host_id).toBe("h8");
  expect(hostReading({ ...hosts[8], state: "offline" }).load).toBeNull();
  render(<TaskBoardHostsPanel hosts={hosts} />);
  fireEvent.click(screen.getByRole("button", { name: "Next page" }));
  expect(screen.getByText("Host 9")).toBeTruthy();
});
it("renders individual archived questions and near-context facts without inventing observations", () => {
  const task: BoardTask = {
    uid: "task",
    "omnigent.ai:source": "source",
    "omnigent.ai:sourceDisplay": {
      version: 1,
      sessions: [
        {
          id: "session",
          task: { source: "source", uid: "task" },
          role: "root",
          agentId: "agent",
          label: "Session",
          title: "Session",
          createdAt: "2026-11-01T00:00:00Z",
          lastActivityAt: "2026-11-01T00:00:00Z",
          status: "archived",
          questions: [
            { id: "a", text: "Choose A" },
            { id: "b", text: "Choose B" },
          ],
          need: "Approve the plan",
        },
      ],
    },
  };
  expect(boardAttention([task], [], new Set()).waiting.map((row) => row.text)).toEqual([
    "Choose A",
    "Choose B",
    "Approve the plan",
  ]);
  const source = task["omnigent.ai:sourceDisplay"]!.sessions![0];
  task["omnigent.ai:sourceDisplay"]!.sessions = [
    { ...source, status: "running", context: 0.95, compactAt: 1 },
  ];
  const native = [{ id: "session", status: "running", updated_at: Date.now() / 1000 }];
  expect(boardAttention([task], native, new Set()).anomalies.map((row) => row.text)).toEqual([
    "Context at 95% of the compaction limit",
  ]);
  expect(boardAttention([task], native, new Set()).waiting).toEqual([]);
  const now = Date.parse("2026-11-12T04:00:00Z");
  task["omnigent.ai:sourceDisplay"]!.sessions = [
    {
      ...source,
      status: "running",
      idleSeconds: 0,
      now: "Checking results",
      tool: {
        id: "tool",
        name: "check",
        label: "Tool is running",
        startedAt: "2026-11-12T03:59:00Z",
        limitSeconds: 30,
      },
    },
  ];
  expect(boardAttention([task], [], new Set(), now).running[0]).toMatchObject({
    text: "Checking results",
    toolRunning: false,
  });
});
