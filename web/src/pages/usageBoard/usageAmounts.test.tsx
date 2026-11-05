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
import { boardAmount, sumUsage } from "./usageAmounts";
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
            <Route path="/usage/board/:accountId" element={<UsageBoardRoute view="account" />} />
          </Routes>
        </MemoryRouter>
      </CapabilitiesContext.Provider>
    </QueryClientProvider>,
  );
}

describe("money projections", () => {
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
});
