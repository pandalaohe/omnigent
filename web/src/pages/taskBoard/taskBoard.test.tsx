import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SESSION_NAVIGATION_STORAGE_KEY } from "@/lib/sessionNavigationPreferences";
import type * as IdentityModule from "@/lib/identity";
import { TaskBoardRoute } from "./TaskBoardRoute";
import { TaskBoardSettings } from "./TaskBoardSettings";
import { createTaskBoardText } from "./taskBoardText";

const mocks = vi.hoisted(() => ({
  info: { task_board_enabled: true } as { task_board_enabled: boolean } | "loading",
  fetch: vi.fn(),
}));
vi.mock("@/lib/CapabilitiesContext", () => ({ useServerInfo: () => mocks.info }));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  authenticatedFetch: (...args: unknown[]) => mocks.fetch(...args),
}));

function renderBoard() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <TaskBoardRoute />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mocks.info = { task_board_enabled: true };
  mocks.fetch.mockReset();
  localStorage.clear();
});
afterEach(cleanup);

it("keeps deployment and user gates ahead of every read", () => {
  mocks.info = { task_board_enabled: false };
  const view = renderBoard();
  expect(screen.getByRole("heading", { name: "Task board is off on this server" })).toBeTruthy();
  expect(mocks.fetch).not.toHaveBeenCalled();
  view.unmount();
  mocks.info = { task_board_enabled: true };
  renderBoard();
  expect(screen.getByRole("link", { name: "Turn it on in Settings" }).getAttribute("href")).toBe(
    "/settings/general#task-board-settings",
  );
  expect(mocks.fetch).not.toHaveBeenCalled();
});

it("reads every page and groups source tasks with safe links and progress icons", async () => {
  localStorage.setItem(SESSION_NAVIGATION_STORAGE_KEY, JSON.stringify({ showTaskBoard: true }));
  mocks.fetch.mockImplementation(async (path: string) => {
    if (path === "/v1/me/tasks/sources")
      return Response.json({
        sources: [
          { id: "tracker", name: "Example tracker", source: "https://tracker.example.com" },
        ],
      });
    if (path === "/v1/me/tasks?cursor=next-page")
      return Response.json({
        entries: [
          {
            uid: "two",
            title: "Remove old endpoint",
            progress: "cancelled",
            "omnigent.ai:source": "tracker",
            links: { page: { rel: "alternate", href: "javascript:alert(1)" } },
          },
        ],
      });
    return Response.json({
      entries: [
        {
          uid: "one",
          title: "Retry uploads",
          progress: "in-process",
          "omnigent.ai:source": "tracker",
          "omnigent.ai:key": "ENG-1",
          "omnigent.ai:group": { id: "v2", name: "Version two" },
          links: { page: { rel: "alternate", href: "https://tracker.example.com/one" } },
        },
      ],
      "omnigent.ai:cursor": "next-page",
    });
  });
  renderBoard();
  const section = await screen.findByRole("region", { name: "Example tracker" });
  expect(within(section).getByRole("heading", { name: "Version two" })).toBeTruthy();
  expect(within(section).getByText("Retry uploads")).toBeTruthy();
  expect(within(section).getByText("Remove old endpoint")).toBeTruthy();
  expect(within(section).getByRole("img", { name: "In progress" })).toBeTruthy();
  expect(within(section).getByRole("img", { name: "Cancelled" })).toBeTruthy();
  expect(within(section).getByText("The source gives no page")).toBeTruthy();
  expect(screen.getByText("2 tasks")).toBeTruthy();
  const links = within(section)
    .getAllByRole("link")
    .map((link) => link.getAttribute("href"));
  expect(links).toContain("https://tracker.example.com/one");
  expect(links.some((link) => link?.startsWith("javascript:"))).toBe(false);
  expect(mocks.fetch.mock.calls).toHaveLength(3);
  expect(mocks.fetch.mock.calls.every((call) => call.length === 1)).toBe(true);
});

it("shows an empty state and recovers from a failed read", async () => {
  localStorage.setItem(SESSION_NAVIGATION_STORAGE_KEY, JSON.stringify({ showTaskBoard: true }));
  mocks.fetch.mockResolvedValue(Response.json({}, { status: 503 }));
  renderBoard();
  expect(await screen.findByRole("alert")).toBeTruthy();
  mocks.fetch.mockImplementation(async (path: string) =>
    Response.json(path.endsWith("/sources") ? { sources: [] } : { entries: [] }),
  );
  fireEvent.click(screen.getByRole("button", { name: "Try again" }));
  expect(await screen.findByRole("heading", { name: "No tasks yet" })).toBeTruthy();
});

it("keeps tasks visible when their source is created between the two reads", async () => {
  localStorage.setItem(SESSION_NAVIGATION_STORAGE_KEY, JSON.stringify({ showTaskBoard: true }));
  mocks.fetch.mockImplementation(async (path: string) =>
    Response.json(
      path.endsWith("/sources")
        ? { sources: [] }
        : {
            entries: [
              {
                uid: "new",
                title: "Newly reported task",
                progress: "needs-action",
                "omnigent.ai:source": "new-source",
              },
            ],
          },
    ),
  );
  renderBoard();
  const section = await screen.findByRole("region", { name: "new-source" });
  expect(within(section).getByText("Newly reported task")).toBeTruthy();
  expect(screen.getByText("1 task")).toBeTruthy();
});

it("stores the display switch in the existing navigation namespace", async () => {
  render(<TaskBoardSettings />);
  const toggle = screen.getByRole("switch", { name: "Show Task board" });
  expect(toggle.getAttribute("aria-checked")).toBe("false");
  fireEvent.click(toggle);
  await waitFor(() =>
    expect(JSON.parse(localStorage.getItem(SESSION_NAVIGATION_STORAGE_KEY)!)).toMatchObject({
      showTaskBoard: true,
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
