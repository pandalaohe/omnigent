import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { UsageCostPage } from "./UsageCostPage";
import { UsageAnalysisShares } from "./UsageAnalysisAmounts";
import { analysisTotals, analysisKey, poolKey, type AnalysisRow } from "./usageAnalysisData";
import { usageAnalysisV2Fixture } from "./usageActivityFixtures";
import { filterFromQuery, toQuery } from "./usageAnalysisFilters";
import { toDateInput } from "./usageBoardSelect";

const { fetchMock } = vi.hoisted(() => ({ fetchMock: vi.fn() }));
vi.mock("@/lib/identity", () => ({ authenticatedFetch: fetchMock }));

beforeEach(() => {
  fetchMock.mockReset();
  fetchMock.mockImplementation(async (path: string) => {
    const url = new URL(path, "https://app.example.test");
    if (url.pathname.endsWith("/analysis"))
      return Response.json(usageAnalysisV2Fixture(url.searchParams));
    return Response.json({
      accounts: [
        {
          id: "claude",
          display_name: "Alpha",
          hosts: [{ host_id: "east", name: "East", read_at: null }],
        },
        {
          id: "litellm",
          display_name: "Beta",
          hosts: [{ host_id: "west", name: "West", read_at: null }],
        },
      ],
    });
  });
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function CurrentUrl() {
  const { pathname, search } = useLocation();
  return (
    <output data-testid="location">
      {pathname}
      {search}
    </output>
  );
}

function mount(query = "", prefix = "") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[`${prefix}/usage/board/analysis${query}`]}>
        <UsageCostPage />
        <CurrentUrl />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

it.each(["", "/frame"])(
  "shows evidence bounds and the split share in the %s shell",
  async (prefix) => {
    const respond = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation((path: string, init?: RequestInit) => {
      const url = new URL(path, "https://app.example.test");
      if (!url.pathname.endsWith("/analysis")) return respond(path, init);
      const data = usageAnalysisV2Fixture(url.searchParams);
      const host = url.searchParams.get("host");
      data.cache_savings = {
        amount:
          host === "west"
            ? null
            : {
                state: "range",
                low: "1.001",
                high: "1.002",
                currency: "USD",
                reason: null,
                formatted: { low: "1.00", high: "1.01" },
              },
        unknown_tokens: host === "east" ? 70 : host === "west" ? 50 : 0,
        split_keys: host ? 1 : 0,
        split_tokens: host === "east" ? 70 : host === "west" ? 50 : 0,
        unbounded_keys: 0,
      };
      return Response.json(data);
    });
    mount("?group=host", prefix);
    const fold = (await screen.findByText("Cache savings", { selector: "summary" })).closest(
      "details",
    )!;
    fold.open = true;
    fireEvent(fold, new Event("toggle"));
    expect(await within(fold).findByText("$1.00–$1.01")).toBeVisible();
    expect(within(fold).queryByText(/cannot be split/)).toBeNull();

    const table = within(await screen.findByRole("table", { name: "Each item" }));
    const east = table.getByRole("button", { name: "East" });
    fireEvent.click(east);
    expect(
      await within(fold).findByText("70 cache-read tokens cannot be split by Host / Agent."),
    ).toBeVisible();
    expect(within(fold).getByText("$1.00–$1.01")).toBeVisible();

    fireEvent.click(east);
    fireEvent.click(table.getByRole("button", { name: "West" }));
    expect(await within(fold).findByText("Unknown")).toBeVisible();
    expect(
      within(fold).getByText("50 cache-read tokens cannot be split by Host / Agent."),
    ).toBeVisible();
  },
);

function row(overrides: Partial<AnalysisRow> = {}): AnalysisRow {
  return {
    account_id: "a",
    parent_account_id: "a",
    pool_id: null,
    hour: 3600,
    model: "Claude",
    basis: "metered",
    tokens: 100,
    requests: 10,
    failed: 0,
    cost_usd: 2,
    contributes_usage: true,
    contributes_money: true,
    attributed: false,
    coverage: "complete",
    reason: null,
    ...overrides,
  };
}
it("uses request-weighted failures and preserves valid zero tokens", () => {
  expect(analysisTotals([row({ tokens: 0 })]).tokens).toBe(0);
  expect(analysisTotals([row(), row({ requests: 30, failed: 3 })]).failure).toBe(7.5);
});
it("does not turn partial or absent counters into known zero", () => {
  expect(analysisTotals([row({ failed: null })]).failure).toBeNull();
  expect(analysisTotals([row({ tokens: null })]).tokens).toBeNull();
  expect(analysisTotals([row()], true).tokens).toBeNull();
  expect(analysisTotals([]).tokens).toBeNull();
});
it("ignores money-channel tokens and never assigns historical parent rows to a pool", () => {
  expect(analysisTotals([row(), row({ contributes_usage: false, tokens: 99999 })]).tokens).toBe(
    100,
  );
  expect(analysisKey(row({ pool_id: "p", attributed: false }), "pool")).toBeNull();
  expect(analysisKey(row({ pool_id: "p", attributed: true }), "pool")).toBe(poolKey("a", "p"));
});
it("combines versioned model identities into the target families and keeps other models together", () => {
  render(
    <UsageAnalysisShares
      incomplete={false}
      rows={[
        row({ model: "anthropic/claude-sonnet", tokens: 100 }),
        row({ model: "claude-opus", tokens: 200 }),
        row({ model: "unclassified", tokens: 100 }),
      ]}
    />,
  );
  expect(screen.getByRole("button", { name: "Claude token share 75.00%" })).toBeVisible();
  expect(screen.getByRole("button", { name: "Other token share 25.00%" })).toBeVisible();
});

it("round-trips repeated values and defaults over ranges, groupings, Unicode and blank tags", () => {
  const now = new Date(2026, 10, 19, 12);
  const shell = new URLSearchParams("theme=dark&pool=https%3A%2F%2Fapp.example.test%2Fp%3Fa%3D1");
  const read = (query: URLSearchParams) => filterFromQuery(query, now);
  for (const range of ["today", "d7", "d30", "d90", "custom"] as const) {
    for (const group of ["account", "model", "host", "agent"] as const) {
      for (const value of ["", "text with spaces", "comma,separated", "模型 + 🦊", "\uE000"]) {
        const filter = {
          range,
          group,
          account: ["a", "b"],
          model: [value],
          host: [value],
          agent: ["", "codex"],
          day: range === "custom" ? null : "2026-11-12",
          slot: "2-14",
          from: range === "custom" ? "2026-11-10" : null,
          to: range === "custom" ? "2026-11-12" : null,
        };
        const canonical = toQuery(filter, shell);
        expect(read(canonical)).toEqual(filter);
        expect(toQuery(read(canonical), canonical).toString()).toBe(canonical.toString());
      }
    }
  }
  const raw = new URLSearchParams(
    "host=z&host=a&host=z&range=d30&theme=dark&group=account&agent=&agent=BAD&slot=2-24&day=2026-02-30&tz=UTC&from=2026-01-01",
  );
  expect(toQuery(read(raw), raw).toString()).toBe("host=a&host=z&agent=&theme=dark");
  expect(
    toQuery(read(new URLSearchParams("model=%EE%80%80&model=%F0%9F%A6%8A"))).getAll("model"),
  ).toEqual(["\uE000", "🦊"]);
});

it.each([
  ["model", "Sonnet", "GPT"],
  ["host", "East", "West"],
  ["agent", "claude", "codex"],
])(
  "keeps the %s group's other rows and removes a filter on the second click",
  async (group, selected, other) => {
    mount(`?group=${group}&theme=dark&pool=example`);
    const table = async () => within(await screen.findByRole("table", { name: "Each item" }));
    fireEvent.click((await table()).getByRole("button", { name: selected }));
    await waitFor(async () =>
      expect((await table()).getByRole("button", { name: selected })).toHaveAttribute(
        "aria-pressed",
        "true",
      ),
    );
    expect((await table()).getByRole("button", { name: other })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
    expect(
      new URLSearchParams(screen.getByTestId("location").textContent!.split("?")[1]).has(group),
    ).toBe(true);
    fireEvent.click((await table()).getByRole("button", { name: selected }));
    await waitFor(async () =>
      expect((await table()).getByRole("button", { name: selected })).toHaveAttribute(
        "aria-pressed",
        "false",
      ),
    );
    expect(screen.getByTestId("location")).toHaveTextContent(
      `?group=${group}&theme=dark&pool=example`,
    );
  },
);

it("keeps a focused source row mounted while its filter loads and lets it undo the read", async () => {
  const respond = fetchMock.getMockImplementation()!;
  let pending: AbortSignal | null | undefined;
  fetchMock.mockImplementation((path: string, init?: RequestInit) => {
    if (
      path.includes("/analysis?") &&
      new URL(path, "https://app.example.test").searchParams.has("model")
    ) {
      pending = init?.signal;
      return new Promise(() => {});
    }
    return respond(path, init);
  });
  mount("?group=model");
  const results = await screen.findByTestId("analysis-results");
  const button = within(results).getByRole("button", { name: "Sonnet" });
  button.focus();
  fireEvent.click(button);

  await waitFor(() => expect(pending?.aborted).toBe(false));
  expect(screen.getByTestId("analysis-results")).toBe(results);
  expect(results).toHaveAttribute("aria-busy", "true");
  const totals = within(results).getByRole("region", { name: "Totals" });
  expect(within(totals).getByText("$8.00", { exact: true })).toBeVisible();
  expect(within(results).getByRole("button", { name: "Sonnet" })).toBe(button);
  expect(button).toHaveFocus();
  expect(button).toHaveAttribute("aria-pressed", "true");

  fireEvent.click(button);
  await waitFor(() => expect(pending?.aborted).toBe(true));
  expect(results).toHaveAttribute("aria-busy", "false");
  expect(button).toHaveFocus();
  expect(button).toHaveAttribute("aria-pressed", "false");
  expect(screen.getByTestId("location")).toHaveTextContent("/usage/board/analysis?group=model");
});

it("restores the originating custom range when a day chip is removed after reload", async () => {
  const localDate = (daysAgo: number) => {
    const date = new Date();
    date.setDate(date.getDate() - daysAgo);
    return toDateInput(date.getTime());
  };
  const from = localDate(12);
  const to = localDate(2);
  const day = localDate(5);
  const base = "/usage/board/analysis";
  const original = `?range=custom&from=${from}&to=${to}&theme=dark`;
  const drilled = `?range=custom&day=${day}&from=${from}&to=${to}&theme=dark`;
  const apiReads = () =>
    fetchMock.mock.calls.flatMap(([path]) =>
      path.includes("/analysis?") ? [new URL(path, "https://app.example.test").searchParams] : [],
    );
  const view = mount(original);
  await screen.findByTestId("analysis-results");
  fireEvent.change(screen.getByLabelText("Date"), { target: { value: day } });

  await waitFor(() => expect(apiReads().at(-1)?.get("day")).toBe(day));
  expect(apiReads().at(-1)?.get("range")).toBe("custom");
  expect(apiReads().at(-1)?.has("from")).toBe(false);
  expect(apiReads().at(-1)?.has("to")).toBe(false);
  expect(screen.getByTestId("location")).toHaveTextContent(`${base}${drilled}`);

  view.unmount();
  mount(drilled);
  await screen.findByTestId("analysis-results");
  fireEvent.click(screen.getByRole("button", { name: `Remove Date: ${day}` }));
  expect(screen.getByTestId("location")).toHaveTextContent(`${base}${original}`);
  await waitFor(() => expect(apiReads().at(-1)?.get("from")).toBe(from));
  expect(apiReads().at(-1)?.get("to")).toBe(to);
  expect(apiReads().at(-1)?.has("day")).toBe(false);
  expect(await screen.findByTestId("analysis-results")).toBeVisible();
});

it.each(["host=east", "agent=claude", "model=Sonnet", "slot=2-14"])(
  "keeps allocated payment unknown for %s",
  async (query) => {
    mount(`?${query}`);
    const totals = await screen.findByRole("region", { name: "Totals" });
    expect(within(totals).getAllByText("未知")).toHaveLength(2);
    expect(within(totals).getAllByText("实付不分 Host / Agent / 模型 / 时段")).toHaveLength(2);
  },
);

it.each([false, true])(
  "refuses numbers on a missing or changed filter echo (%s)",
  async (changed) => {
    fetchMock.mockImplementation(async (path: string) => {
      if (!path.includes("/analysis?")) return Response.json({ accounts: [] });
      const params = new URL(path, "https://app.example.test").searchParams;
      const data = usageAnalysisV2Fixture(params);
      return Response.json(
        params.has("host")
          ? {
              ...data,
              filter: changed ? { ...data.filter, host: ["west"] } : undefined,
            }
          : data,
      );
    });
    mount(changed ? "?group=host" : "?host=east");
    if (changed) {
      const table = await screen.findByRole("table", { name: "Each item" });
      fireEvent.click(within(table).getByRole("button", { name: "east" }));
    }
    expect(await screen.findByRole("alert")).toHaveTextContent("服务器版本不支持这个筛选");
    expect(screen.queryByRole("region", { name: "Totals" })).toBeNull();
    expect(screen.queryByRole("table", { name: "Each item" })).toBeNull();
  },
);

it("keeps a well-formed unknown chip and clears selection without changing the shell", async () => {
  mount("?host=retired&range=d30&group=none&theme=light");
  await screen.findByTestId("analysis-results");
  expect(screen.getByTestId("location")).toHaveTextContent("?host=retired&theme=light");
  expect(screen.getByRole("button", { name: "Remove Host: retired" })).toHaveTextContent("· 0");
  fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
  expect(screen.getByTestId("location")).toHaveTextContent("/usage/board/analysis?theme=light");
});

it("aborts the analysis read when the view leaves", async () => {
  let signal: AbortSignal | undefined;
  fetchMock.mockImplementation((path: string, init: RequestInit) => {
    if (path.includes("/analysis?")) {
      signal = init.signal ?? undefined;
      return new Promise(() => {});
    }
    return Promise.resolve(Response.json({ accounts: [] }));
  });
  const view = mount();
  await waitFor(() => expect(signal?.aborted).toBe(false));
  view.unmount();
  expect(signal?.aborted).toBe(true);
});
