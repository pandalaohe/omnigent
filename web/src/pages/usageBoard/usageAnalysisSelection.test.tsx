import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";
import { UsageCostPage } from "./UsageCostPage";
import { UsageAnalysisShares } from "./UsageAnalysisAmounts";
import {
  analysisTotals,
  analysisKey,
  poolKey,
  toggleAnalysisPick,
  type AnalysisRow,
} from "./usageAnalysisData";

const state = vi.hoisted(() => ({
  range: "d7",
  partial: false,
  rows: [] as unknown[],
  hidden: [] as string[],
  readKeys: [] as string[],
}));
vi.mock("./usageActivityData", () => ({ useUsageActivity: () => ({ data: { accounts: [] } }) }));
vi.mock("./usageBoardData", () => ({
  USAGE_BOARD_POLL_MS: 60000,
  useUsageOverview: () => ({
    data: {
      accounts: [
        { id: "a", display_name: "Alpha" },
        { id: "b", display_name: "Beta" },
      ],
    },
  }),
}));
vi.mock("./usageAnalysisData", async (original) => ({
  ...(await original<object>()),
  useUsageAnalysis: (_range: unknown, selection?: { keys: string[] }) => {
    if (selection) state.readKeys = selection.keys;
    return {
      data: {
        generated_at: 10000,
        span: { requested_from: 0, counted_from: 0, until: 10000 },
        rows: state.rows,
        partial: state.partial,
        pool_catalog: [
          { parent_account_id: "a", pool_id: "p", display_name: "Pool P", usage_available: true },
          {
            parent_account_id: "a",
            pool_id: "q",
            display_name: "Quota only",
            usage_available: false,
          },
        ],
      },
    };
  },
}));
vi.mock("@/hooks/useUsageContextPreferences", () => ({
  useUsageContextPreferences: () => ({
    board: { hidden: state.hidden, range: { key: state.range } },
  }),
}));
vi.mock("@/lib/usageContextPreferences", () => ({ patchUsageBoardPreferences: vi.fn() }));
vi.mock("./UsageCostChart", () => ({
  UsageAnalysisChart: ({
    rows,
    previousUnavailable,
  }: {
    rows: AnalysisRow[];
    previousUnavailable: boolean;
  }) => (
    <div data-testid="selected-chart">
      {rows.map((entry) => entry.model).join(",")}
      {previousUnavailable ? "previous unavailable" : ""}
    </div>
  ),
}));
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
function mount() {
  state.rows = [
    row(),
    row({
      account_id: "b",
      parent_account_id: "b",
      model: "GPT",
      tokens: 300,
      requests: 30,
      failed: 3,
    }),
  ];
  return render(
    <MemoryRouter initialEntries={["/usage/board/cost"]}>
      <UsageCostPage />
    </MemoryRouter>,
  );
}
afterEach(() => {
  cleanup();
  state.range = "d7";
  state.partial = false;
  state.hidden = [];
});
it("empty and full selections mean all; toggling an all view starts one item", () => {
  expect(toggleAnalysisPick(null, "a", ["a", "b"])).toEqual(["a"]);
  expect(toggleAnalysisPick(["a"], "a", ["a", "b"])).toBeNull();
  expect(toggleAnalysisPick(["a"], "b", ["a", "b"])).toBeNull();
});
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
it("selects the same account for KPI table shares failures and chart", () => {
  mount();
  fireEvent.click(
    within(screen.getByRole("group", { name: "Select items" })).getByRole("button", {
      name: "Alpha",
    }),
  );
  expect(screen.getByTestId("selected-chart").textContent).toBe("Claudeprevious unavailable");
  expect(screen.getByRole("table")).toHaveTextContent("Alpha");
  expect(screen.getByRole("table")).not.toHaveTextContent("Beta");
  expect(screen.getByRole("button", { name: "Claude token share 100.0%" })).toBeVisible();
  expect(screen.queryByRole("button", { name: /Beta failure rate/ })).toBeNull();
});
it("changes grouping without reusing a different dimension's picks", () => {
  mount();
  fireEvent.click(screen.getByRole("button", { name: "Alpha" }));
  fireEvent.click(screen.getByRole("button", { name: "Model" }));
  expect(screen.getByRole("button", { name: "All" })).toHaveAttribute("aria-pressed", "true");
  fireEvent.click(screen.getByRole("button", { name: "GPT" }));
  expect(screen.getByTestId("selected-chart")).toHaveTextContent("GPTprevious unavailable");
  expect(screen.queryByRole("button", { name: /Claude token share/ })).toBeNull();
});
it("keeps an unavailable model selection empty until the user actively chooses All", () => {
  const result = mount();
  fireEvent.click(screen.getByRole("button", { name: "Model" }));
  fireEvent.click(screen.getByRole("button", { name: "GPT" }));
  expect(state.readKeys).toEqual(["GPT"]);
  state.rows = [row()];
  state.range = "d30";
  result.rerender(
    <MemoryRouter initialEntries={["/usage/board/cost"]}>
      <UsageCostPage />
    </MemoryRouter>,
  );
  expect(state.readKeys).toEqual([]);
  expect(screen.getByRole("button", { name: "All" })).toHaveAttribute("aria-pressed", "false");
  expect(screen.getByRole("status")).toHaveTextContent("Selected items are unavailable");
  expect(screen.getByTestId("selected-chart")).not.toHaveTextContent("Claude");
  fireEvent.click(screen.getByRole("button", { name: "All" }));
  expect(state.readKeys).toEqual(["Claude"]);
  expect(screen.getByTestId("selected-chart")).toHaveTextContent("Claude");
});
it("keeps a quota-only pool unknown instead of falling back to parent data", () => {
  mount();
  fireEvent.click(screen.getByRole("button", { name: "Pool" }));
  fireEvent.click(screen.getByRole("button", { name: "Quota only" }));
  expect(screen.getByTestId("selected-chart").textContent).toBe("previous unavailable");
  expect(screen.getByRole("table")).toHaveTextContent("Unknown");
  expect(screen.queryByRole("button", { name: /token share/ })).toBeNull();
});
it("sorts token rows both ways and puts unknowns last", () => {
  mount();
  fireEvent.click(screen.getByRole("button", { name: "Token" }));
  let rows = within(screen.getByRole("table")).getAllByRole("row");
  expect(rows[1]).toHaveTextContent("Beta");
  fireEvent.click(screen.getByRole("button", { name: "Token ↓" }));
  rows = within(screen.getByRole("table")).getAllByRole("row");
  expect(rows[1]).toHaveTextContent("Alpha");
});
it("suppresses every prior-period comparison at 90 days", () => {
  state.range = "d90";
  mount();
  expect(screen.getAllByText("90 days has no previous period")).toHaveLength(2);
  expect(screen.getByTestId("selected-chart")).not.toHaveTextContent("previous");
});
it("does not include hidden accounts in any panel", () => {
  state.hidden = ["b"];
  mount();
  expect(screen.queryByRole("button", { name: "Beta" })).toBeNull();
  expect(screen.getByTestId("selected-chart")).not.toHaveTextContent("GPT");
});
it("labels incomplete data and never publishes a complete share population", () => {
  state.partial = true;
  mount();
  expect(screen.getByText("Partial source coverage")).toBeVisible();
  expect(screen.queryByRole("button", { name: /token share/ })).toBeNull();
});
it("keeps price evidence unavailable rather than presenting reported cost as API value", () => {
  mount();
  expect(screen.getAllByText("Not available yet · price evidence")[0]).toBeVisible();
  expect(screen.getByRole("table")).not.toHaveTextContent("$2.00");
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
  expect(screen.getByRole("button", { name: "Claude token share 75.0%" })).toBeVisible();
  expect(screen.getByRole("button", { name: "Other token share 25.0%" })).toBeVisible();
});
