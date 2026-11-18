import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { patchUsageBoardPreferences } from "@/lib/usageContextPreferences";
import { UsageBoardPage } from "./UsageBoardPage";
import { usageBoardData } from "./usageBoardFixtures";
import { usageActivityFixture } from "./usageActivityFixtures";
import { sumUsage } from "./usageAmountData";
import { formatMoney, formatOverviewTokens } from "./usageBoardFormat";

const { fetchMock } = vi.hoisted(() => ({ fetchMock: vi.fn() }));
vi.mock("@/lib/identity", () => ({ authenticatedFetch: fetchMock }));
const now = new Date(2026, 10, 17, 18, 20).getTime() / 1000;
const classes = {
  basis: "price_table_estimate",
  items: [
    {
      kind: "input",
      tokens: 2000,
      cost_low_usd: "0.25",
      cost_high_usd: "0.50",
      unpriced_tokens: 500,
    },
  ],
  unpriced_tokens: { input: 500 },
  unsplit_tokens: 1000,
  unsplit_cost: { low: "0.10", high: "0.10" },
};

beforeEach(() => {
  localStorage.clear();
  patchUsageBoardPreferences({ fees: { codex: 20 } });
  fetchMock.mockReset();
  fetchMock.mockImplementation(async (path: string) => {
    const url = new URL(path, "https://example.test");
    const response = {
      "/v1/me/usage": () => usageBoardData(),
      "/v1/me/usage/activity": () => usageActivityFixture(url.searchParams, now),
      "/v1/me/usage/analysis": () => ({ complete: true, classes }),
      "/v1/me/usage/pricing-baseline": () => ({ defaults: {}, cards: [] }),
      "/v1/me/usage/events": () => ({ events: [] }),
      "/v1/me/usage/sources": () => ({ sources: [] }),
    }[url.pathname];
    return Response.json(response ? response() : {}, { status: response ? 200 : 404 });
  });
});

afterEach(cleanup);

function mountOverview(query = "") {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter initialEntries={[`/usage/board${query}`]}>
        <UsageBoardPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

it.each(["", "?account=codex"])(
  "shows the overview, with matching cells and chart, at %s",
  async (query) => {
    const { container } = mountOverview(query);
    await screen.findByTestId("usage-overview");
    for (const label of ["Quota", "Cost", "Card pricing", "Used in", "Hosts", "事件记录"])
      expect(screen.getByRole("heading", { name: label })).toBeTruthy();
    expect(container.querySelector(".usage-tiles, .usage-strip")).toBeNull();
    const cells = screen.getByRole("group", { name: "Cost time range" });
    expect(within(cells).getAllByRole("button")).toHaveLength(4);
    const week = within(cells).getByRole("button", { name: /Last 7 days/ });
    const current = await screen.findByTestId("usage-chart-now");
    expect(week).toHaveAttribute("aria-pressed", "true");
    expect(week.querySelector("b")?.textContent).toEqual(current.querySelector("b")?.textContent);
    fireEvent.click(screen.getByRole("button", { name: "Token" }));
    await waitFor(() =>
      expect(week.querySelector("b")?.textContent).toEqual(current.querySelector("b")?.textContent),
    );
    expect(current.querySelector("b")?.textContent).not.toContain("$");
    expect(fetchMock.mock.calls.some(([path]) => path.includes("/analysis?"))).toBe(false);
    const fold = screen.getByText("Cost composition").closest("details")!;
    fold.open = true;
    fireEvent(fold, new Event("toggle"));
    const unknown = await screen.findByText("Unpriced / Unsplit");
    expect(unknown.closest("li")).toHaveTextContent("1.50K Token");
    expect(container.querySelector('[data-usage-class="Unpriced / Unsplit"]')).toBeTruthy();
  },
);

it("counts only metered money but includes subscription tokens in All", async () => {
  patchUsageBoardPreferences({ hidden: ["claude", "grok", "pi"] });
  const fixture = usageActivityFixture(new URLSearchParams({ range: "d7" }), now);
  const subscription = fixture.accounts.find(({ account }) => account === "codex")!;
  const payPerUse = fixture.accounts.find(({ account }) => account === "litellm")!;
  expect(subscription.totals.cost_usd).toBeGreaterThan(0);
  expect(subscription.totals.metered?.cost_usd).toBeNull();
  const meteredAmount = formatMoney(payPerUse.totals.cost_usd);
  const allTokens = formatOverviewTokens(subscription.totals.tokens! + payPerUse.totals.tokens!);

  mountOverview();
  const current = await screen.findByTestId("usage-chart-now");
  const week = screen.getByRole("button", { name: /Last 7 days/ });
  expect(week.querySelector("b")?.textContent).toBe(meteredAmount);
  expect(current.querySelector("b")?.textContent).toBe(meteredAmount);
  fireEvent.click(screen.getByRole("button", { name: "Token" }));
  expect(week.querySelector("b")?.textContent).toBe(allTokens);
  expect(current.querySelector("b")?.textContent).toBe(allTokens);
});

it("labels the 30-day Analysis link before leaving This period", async () => {
  mountOverview("?account=codex&range=period");
  const link = await screen.findByRole("link", { name: "View in Analysis · Last 30 days" });
  expect(link).toHaveAttribute("href", "/usage/board/cost?range=d30&account=codex");
  fireEvent.click(screen.getByRole("button", { name: /Last 7 days/ }));
  expect(screen.getByRole("link", { name: "View in Analysis" })).toHaveAttribute(
    "href",
    "/usage/board/cost?range=d7&account=codex",
  );
});

it.each(["today", "h24", "d7", "d30", "d90", "period", "custom"])(
  "rolls the same hours into bucket totals for %s",
  (range) => {
    const params = new URLSearchParams({ range, from: "2026-10-25", to: "2026-11-16" });
    const daily = usageActivityFixture(params, now, { bucketUnit: "day" });
    const hourly = usageActivityFixture(params, now, { bucketUnit: "hour" });
    daily.accounts.forEach((account, i) => {
      for (const [rolled, raw] of [
        [account, hourly.accounts[i]],
        [account.previous, hourly.accounts[i].previous],
      ]) {
        expect(sumUsage(rolled?.series ?? [])).toEqual(sumUsage(raw?.series ?? []));
      }
    });
  },
);

it("counts the same Today's activity in the same week's totals", () => {
  const today = usageActivityFixture(new URLSearchParams({ range: "today" }), now);
  const week = usageActivityFixture(new URLSearchParams({ range: "d7" }), now, {
    bucketUnit: "hour",
  });
  for (const [i, account] of today.accounts.entries()) {
    expect(account.totals.cost_usd ?? 0).toBeLessThanOrEqual(week.accounts[i].totals.cost_usd ?? 0);
    expect(account.series).toEqual(
      week.accounts[i].series.filter((row) => row.at >= account.span.counted_from),
    );
  }
});
