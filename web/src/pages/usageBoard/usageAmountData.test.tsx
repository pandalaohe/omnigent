import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CapabilitiesContext } from "@/lib/CapabilitiesContext";
import type { ServerInfo } from "@/lib/capabilities";
import {
  patchUsageBoardPreferences,
  readUsageContextPreferences,
} from "@/lib/usageContextPreferences";
import { UsageBoardRoute } from "./UsageBoardRoute";
import { usageBoardData } from "./usageBoardFixtures";
import { sampleTotals, usageActivityFixture, usageIssuesFixture } from "./usageActivityFixtures";
import { boardAmount, sumUsage } from "./usageAmountData";
import { usageChartPoints } from "./UsageCostChart";
import { UsageCostCard, UsageErrorMark } from "./UsageAmounts";

const { fetchMock } = vi.hoisted(() => ({ fetchMock: vi.fn() }));
const INFO = { usage_board_enabled: true } as ServerInfo;
vi.mock("@/lib/identity", () => ({ authenticatedFetch: fetchMock }));

beforeEach(() => {
  localStorage.clear();
  fetchMock.mockReset();
  patchUsageBoardPreferences({ enabled: true });
  fetchMock.mockImplementation(async (path: string) => {
    const url = new URL(path, "https://app.example.test");
    const activity = usageActivityFixture(url.searchParams);
    const overview = usageBoardData();
    overview.accounts.find((item) => item.id === "litellm")!.mark = {
      level: "serious",
      requests: 60,
      failed: 15,
      rate: 25,
    };
    return new Response(
      JSON.stringify(
        url.pathname.endsWith("/activity")
          ? activity
          : url.pathname.endsWith("/issues")
            ? usageIssuesFixture(activity)
            : url.pathname.endsWith("/models")
              ? {
                  account: "litellm",
                  display_name: "LiteLLM",
                  range: "d7",
                  span: null,
                  models: [
                    {
                      id: "model-a",
                      sources: [],
                      pricing: null,
                      usage: sampleTotals(1, 1000),
                    },
                  ],
                }
              : overview,
      ),
    );
  });
});
afterEach(cleanup);

function mount(path = "/usage/board/cost") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <CapabilitiesContext.Provider value={INFO}>
        <MemoryRouter initialEntries={[path]}>
          <Routes>
            <Route path="/usage/board" element={<UsageBoardRoute />} />
            <Route path="/usage/board/cost" element={<UsageBoardRoute view="cost" />} />
            <Route path="/usage/board/issues" element={<UsageBoardRoute view="issues" />} />
            <Route
              path="/usage/board/:accountId/issues"
              element={<UsageBoardRoute view="issues" />}
            />
            <Route
              path="/usage/board/:accountId/models"
              element={<UsageBoardRoute view="models" />}
            />
            <Route path="/usage/board/:accountId" element={<UsageBoardRoute view="account" />} />
          </Routes>
        </MemoryRouter>
      </CapabilitiesContext.Provider>
    </QueryClientProvider>,
  );
}

describe("money projections", () => {
  it.each([
    ["account", "money"],
    ["account", "tokens"],
    ["overview", "money"],
    ["overview", "tokens"],
  ] as const)("uses metered Per 1M on the %s card in %s mode", (surface, unit) => {
    patchUsageBoardPreferences({ unit });
    const item = usageActivityFixture().accounts[2];
    const mixed = {
      ...item,
      totals: { ...sampleTotals(92, 10000), metered: sampleTotals(2, 1000) },
      previous: {
        ...item.previous!,
        totals: { ...sampleTotals(46, 10000), metered: sampleTotals(1, 1000) },
      },
    };
    render(
      <MemoryRouter>
        <UsageCostCard
          accounts={[mixed]}
          account={surface === "account" ? usageBoardData().accounts[2] : undefined}
          now={Date.now() / 1000}
        />
      </MemoryRouter>,
    );
    const efficiency = screen.getByRole("button", { name: /Per 1M tokens/ });
    expect(efficiency.textContent).toContain("$2,000");
    expect(efficiency.querySelector('[aria-label="Higher than previous"]')).toBeTruthy();
    fireEvent.click(efficiency);
    const tip = screen.getByTestId("usage-tip-card");
    expect(tip.textContent).toContain("$1,000");
  });
  it("weights only covered costs and tokens, including a zero-price row", () => {
    const result = sumUsage([
      sampleTotals(4, 1000000),
      sampleTotals(0, 1000000),
      sampleTotals(6, null),
      sampleTotals(null, 1000000, { unpriced: 1 }),
    ]);
    expect(result.per_million).toBe(2);
    expect(result.cost_coverage_pct).toBe(40);
    expect(result.tokens).toBe(3000000);
  });
  it("excludes subscription estimates from money but includes their tokens", () => {
    const accounts = usageActivityFixture().accounts;
    expect(boardAmount(accounts)!.totals.cost_usd).toBeCloseTo(
      accounts
        .filter((item) => !["codex", "claude"].includes(item.account))
        .reduce((sum, item) => sum + (item.totals.cost_usd ?? 0), 0),
    );
    expect(boardAmount(accounts)!.mark).toBe("~");
    expect(boardAmount(accounts, false, true)!.totals.tokens).toBe(
      accounts.reduce((sum, item) => sum + (item.totals.tokens ?? 0), 0),
    );
    expect(boardAmount(accounts.filter((item) => item.account === "litellm"))!.mark).toBe("≥");
  });
  it("fills silent buckets and aligns Previous separately for each reset span", () => {
    const now = new Date(2026, 10, 5, 12).getTime() / 1000;
    const accounts = usageActivityFixture(
      new URLSearchParams({ range: "period" }),
      now,
    ).accounts.filter((item) => item.account !== "grok");
    const points = usageChartPoints(accounts, false, true, true, now);
    expect(points.length).toBeGreaterThan(25);
    expect(points.at(-1)!.now).toBeCloseTo(boardAmount(accounts)!.totals.cost_usd!);
    expect(points.at(-1)!.previous).toBeCloseTo(boardAmount(accounts, true)!.totals.cost_usd!);
    const single = accounts.find((item) => item.account === "litellm")!;
    const sparse = { ...single, series: [single.series[0]], previous: null };
    const filled = usageChartPoints([sparse], false, false, false, now);
    expect(filled.filter((item) => item.bucket === 0).length).toBeGreaterThan(20);
    expect(filled.at(-1)!.previous).toBeUndefined();
  });
  it("keeps a partial first day when the server's Previous bucket starts before its cutoff", () => {
    const until = new Date(2026, 10, 5, 12).getTime() / 1000;
    const start = new Date(2026, 10, 2).getTime() / 1000;
    const before = start - (until - start);
    const previousDay = new Date(before * 1000);
    previousDay.setHours(0, 0, 0, 0);
    const account = usageActivityFixture().accounts[2];
    const points = usageChartPoints(
      [
        {
          ...account,
          span: { requested_from: start, counted_from: start, until, approx: false },
          series: [{ at: start, ...sampleTotals(1, 1000) }],
          previous: {
            span: { requested_from: before, counted_from: before, until: start, approx: false },
            mark: null,
            totals: sampleTotals(5, 1000),
            series: [{ at: previousDay.getTime() / 1000, ...sampleTotals(5, 1000) }],
          },
        },
      ],
      false,
      false,
      false,
      until,
    );
    expect(points.at(-1)!.previous).toBe(5);
  });
  it("keeps all thirteen Previous hours on the shorter New York spring-forward Today axis", () => {
    const start = Date.parse("2026-03-08T00:00:00-05:00") / 1000;
    const until = Date.parse("2026-03-08T12:40:00-04:00") / 1000;
    const before = Date.parse("2026-03-07T00:00:00-05:00") / 1000;
    const previousUntil = Date.parse("2026-03-07T12:40:00-05:00") / 1000;
    const item = usageActivityFixture().accounts[2];
    const points = usageChartPoints(
      [
        {
          ...item,
          span: { requested_from: start, counted_from: start, until, approx: false },
          totals: sampleTotals(12, 12000),
          series: Array.from({ length: 12 }, (_, index) => ({
            at: start + index * 3600,
            ...sampleTotals(1, 1000),
          })),
          previous: {
            span: {
              requested_from: before,
              counted_from: before,
              until: previousUntil,
              approx: false,
            },
            mark: null,
            totals: sampleTotals(13, 13000),
            series: Array.from({ length: 13 }, (_, index) => ({
              at: before + index * 3600,
              ...sampleTotals(1, 1000),
            })),
          },
        },
      ],
      false,
      false,
      false,
      until,
    );
    expect(points).toHaveLength(12);
    expect(points.at(-1)!.now).toBe(12);
    expect(points.at(-1)!.previous).toBe(13);
  });
  it("carries a 31-day reset window's opening spend onto the 30-day total axis", () => {
    const until = new Date(2026, 10, 5).getTime() / 1000;
    const start = until - 31 * 86400;
    const item = usageActivityFixture().accounts[2];
    const points = usageChartPoints(
      [
        {
          ...item,
          span: { requested_from: start, counted_from: start, until, approx: false },
          totals: sampleTotals(31, 31000),
          previous: null,
          series: Array.from({ length: 31 }, (_, index) => ({
            at: start + index * 86400,
            ...sampleTotals(1, 1000),
          })),
        },
      ],
      false,
      true,
      true,
      until,
    );
    expect(points).toHaveLength(30);
    expect(points.at(-1)!.now).toBe(31);
    expect(points[0].now).toBe(2);
    expect(points.reduce((sum, point) => sum + point.bucket, 0)).toBe(30);
  });
});

it.each(["/usage/board/issues", "/usage/board/litellm/issues"])(
  "does not call an unsuccessful Issues read empty at %s",
  async (path) => {
    fetchMock.mockImplementation(async (url: string) =>
      url.includes("/issues")
        ? new Response(JSON.stringify({ detail: "Issue store unavailable" }), { status: 503 })
        : new Response(JSON.stringify(usageBoardData())),
    );
    mount(path);
    await screen.findByText("Issue store unavailable");
    expect(screen.queryByText("No reported failures in this range.")).toBeNull();
    expect(screen.queryByText("No unpriced usage in this range.")).toBeNull();
    expect(screen.queryByText("No source issues in this range.")).toBeNull();
  },
);

it("shows empty Issues only after a successful empty read", async () => {
  fetchMock.mockImplementation(
    async (url: string) =>
      new Response(
        JSON.stringify(
          url.includes("/issues")
            ? { failures: [], unpriced_models: [], source_reports: [] }
            : usageBoardData(),
        ),
      ),
  );
  mount("/usage/board/issues");
  expect(screen.queryByText("No reported failures in this range.")).toBeNull();
  await screen.findByText("No reported failures in this range.");
  expect(screen.getByText("No unpriced usage in this range.")).toBeTruthy();
  expect(screen.getByText("No source issues in this range.")).toBeTruthy();
});

it("keeps hidden accounts out of totals, the account list and By model", async () => {
  patchUsageBoardPreferences({ hidden: ["litellm", "pi"] });
  mount();
  const list = await screen.findByRole("region", { name: "Accounts" });
  expect(within(list).queryByText("LiteLLM")).toBeNull();
  const cost = screen.getByRole("region", { name: "Cost" });
  expect(within(cost).getByRole("button", { name: "~$0" })).toBeTruthy();
  fireEvent.click(screen.getByText("By model"));
  expect(screen.queryByText("model-b")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Tokens" }));
  await waitFor(() => expect(readUsageContextPreferences().board.unit).toBe("tokens"));
  expect(screen.getByRole("button", { name: "Money" }).getAttribute("aria-pressed")).toBe("false");
});

it("takes the summary tap to Cost, then its failure row to ranged Issues blocks", async () => {
  mount("/usage/board");
  const summary = await screen.findByRole("region", { name: "Usage summary" });
  await waitFor(() => expect(summary.textContent).toContain("$"));
  fireEvent.click(within(summary).getByRole("link"));
  await screen.findByRole("heading", { name: "Cost", level: 1 });
  fireEvent.click(screen.getByRole("link", { name: /requests.*failed.*unpriced.*Issues/ }));
  await screen.findByRole("heading", { name: "Issues", level: 1 });
  expect((await screen.findByRole("region", { name: "Failures" })).textContent).toContain(
    "timeout",
  );
  expect(screen.getByRole("region", { name: "Unpriced models" }).textContent).toContain("model-b");
  fireEvent.click(screen.getByRole("button", { name: "Usage range" }));
  fireEvent.click(screen.getByRole("button", { name: /^24 hours/ }));
  await waitFor(() =>
    expect(fetchMock.mock.calls.some(([path]) => path.includes("/issues?range=h24"))).toBe(true),
  );
});

it("opens an error card without navigating and suppresses a stale reading's triangle", async () => {
  const account = usageBoardData().accounts[2];
  const mark = { level: "down", requests: 100, failed: 60, rate: 60 };
  const result = render(<UsageErrorMark account={{ ...account, mark }} />);
  fireEvent.click(screen.getByRole("button", { name: /errors in the last 24 hours/ }));
  expect(await screen.findByText("60.0%")).toBeTruthy();
  expect(
    screen.getByRole("button").querySelector("svg")?.classList.contains("fill-destructive"),
  ).toBe(true);
  result.rerender(<UsageErrorMark account={{ ...account, mark, freshness: "old" }} />);
  expect(screen.queryByRole("button")).toBeNull();
});

it("keeps missing usage, request counts and Previous absent instead of inventing zeroes", () => {
  const noUsage = usageActivityFixture().accounts.find((item) => item.account === "grok")!;
  render(
    <MemoryRouter>
      <UsageCostCard
        accounts={[noUsage]}
        account={usageBoardData().accounts[3]}
        now={Date.now() / 1000}
      />
    </MemoryRouter>,
  );
  expect(screen.queryByText(/^Previous/)).toBeNull();
  expect(screen.queryByText(/requests/)).toBeNull();
  expect(screen.queryByText(/Per 1M/)).toBeNull();
  expect(screen.getByRole("button", { name: "—" })).toBeTruthy();
});

it("reports a failed activity read and does not fetch while the user switch is off", async () => {
  fetchMock.mockImplementation(async (path: string) =>
    path.includes("/activity")
      ? new Response(JSON.stringify({ detail: "Hours unavailable" }), { status: 503 })
      : new Response(JSON.stringify(usageBoardData())),
  );
  mount();
  await screen.findByText("Hours unavailable");
  cleanup();
  fetchMock.mockClear();
  patchUsageBoardPreferences({ enabled: false });
  mount();
  expect(screen.getByText("Usage board is off")).toBeTruthy();
  expect(fetchMock).not.toHaveBeenCalled();
  cleanup();
  mount("/usage/board");
  expect(screen.getByText("Usage board is off")).toBeTruthy();
  expect(fetchMock).not.toHaveBeenCalled();
});

it("loads Issues, account activity and account models while the board switch is off", async () => {
  patchUsageBoardPreferences({ enabled: false });
  mount("/usage/board/issues");
  await screen.findByRole("heading", { name: "Issues", level: 1 });
  expect((await screen.findByRole("region", { name: "Failures" })).textContent).toContain(
    "timeout",
  );
  cleanup();
  mount("/usage/board/litellm/issues");
  await screen.findByRole("heading", { name: "LiteLLM · Issues", level: 1 });
  expect((await screen.findByRole("region", { name: "Failures" })).textContent).toContain(
    "timeout",
  );
  cleanup();
  mount("/usage/board/litellm");
  await screen.findByRole("heading", { name: "LiteLLM", level: 1 });
  await waitFor(() =>
    expect(fetchMock.mock.calls.some(([path]) => String(path).includes("/activity"))).toBe(true),
  );
  cleanup();
  mount("/usage/board/litellm/models");
  await screen.findByText("model-a");
});
