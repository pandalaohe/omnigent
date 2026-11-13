import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { useState } from "react";
import { UsageAnalysisTable } from "./UsageAnalysisAmounts";
import { moneyText } from "./UsageAccountMoney";
import { afterEach, expect, it } from "vitest";
import {
  AnalysisCards,
  AnalysisClosings,
  AnalysisComparisons,
  AnalysisHeatmap,
} from "./UsageAnalysisDetails";
import {
  analysisValueText,
  type AnalysisPresentation,
  type AnalysisValue,
  type AnalysisRow,
} from "./usageAnalysisData";

afterEach(cleanup);
it.each([
  ["USD", "$10.00"],
  ["CNY", "¥10.00"],
  ["HKD", "HK$10.00"],
])("retains the accepted %s symbol in class-specific Analysis amounts", (currency, expected) => {
  const paid = value("10", "10.00", currency);
  render(
    <UsageAnalysisTable
      items={[{ key: "a", name: "Alpha", rows: [], projection: { ...fixture().projection, paid } }]}
      incomplete={false}
      sort={{ key: "paid", direction: 1 }}
      onSort={() => {}}
    />,
  );
  const table = screen.getByRole("table", { name: "Each item" });
  expect(within(table).getByText(expected, { exact: true })).toHaveAttribute(
    "data-label",
    "Paid / spend",
  );
});
it("uses the accepted formatter for raw money, ranges, lower bounds and payback", () => {
  const raw = { ...value("10", "10.00"), formatted: undefined };
  expect(moneyText(raw)).toBe("$10.00");
  expect(moneyText({ ...raw, state: "partial", high: null })).toBe("≥$10.00");
  expect(moneyText({ ...raw, state: "range", high: "12" })).toBe("$10.00–$12.00");
  expect(moneyText(value("9007199254740993.01", "9007199254740993.01"))).toBe(
    "$9,007,199,254,740,993.01",
  );
  expect(moneyText(value("2", "2.00", null), true)).toBe("2×");
  expect(moneyText({ ...raw, state: "unknown", low: null, high: null })).toBe("Unknown");
});
it.each([
  ["subscription", true, true, 7],
  ["prepaid", true, true, 7],
  ["metered", true, false, 6],
  ["free", false, false, 4],
] as const)(
  "keeps %s table tracks aligned with visible cells and lets CSS own responsive columns",
  (_kind, money, payback, count) => {
    const projection = { ...fixture().projection, show_money: money, show_payback: payback };
    render(
      <UsageAnalysisTable
        items={[{ key: "a", name: "Alpha", rows: [], projection }]}
        incomplete={false}
        sort={{ key: "paid", direction: 1 }}
        onSort={() => {}}
      />,
    );
    const table = screen.getByRole("table", { name: "Each item" });
    const rows = within(table).getAllByRole("row");
    expect(within(rows[0]).getAllByRole("columnheader")).toHaveLength(count);
    expect(within(rows[1]).getAllByRole("cell")).toHaveLength(count);
    expect(rows[1].style.gridTemplateColumns).toBe("");
    expect(table.style.getPropertyValue("--ua-metric-columns")).toBe(String(count - 2));
  },
);
function value(raw: string, formatted: string, currency: string | null = "USD"): AnalysisValue {
  return {
    state: "exact",
    low: raw,
    high: raw,
    currency,
    reason: null,
    formatted: { low: formatted, high: formatted },
  };
}
function fixture(): AnalysisPresentation {
  const cards = [
    {
      id: "a",
      name: "Card A",
      paid: value("100", "100.00"),
      original_paid: value("800", "800.00", "HKD"),
      allowance: value("200", "200.00"),
      paid_per_quota: value("0.5", "0.50"),
      reset_count: 1,
      starts_at: 1793577600,
      ends_at: 1794182400,
    },
    {
      id: "b",
      name: "Card B",
      paid: value("50", "50.00"),
      allowance: value("100", "100.00"),
      paid_per_quota: value("0.5", "0.50"),
      reset_count: 1,
      starts_at: 1793577600,
      ends_at: 1794182400,
    },
  ];
  return {
    rows: [],
    projection: {
      paid: value("150", "150.00"),
      tokens: value("1000000", "1000000.00", null),
      payback: value("2", "2.00", null),
    },
    items: [],
    previous: null,
    comparison: {
      same_usage_difference: value("-1.005", "-1.00"),
      same_usage: value("149", "149.00"),
      same_spend_percent: value("20", "20.00", null),
      same_spend_difference: value("200", "200.00", null),
    },
    cards,
    allowance: value("300", "300.00"),
    paid_per_quota: value("0.5", "0.50"),
    paid_series: [],
    quota_closings: [
      {
        account_id: "a",
        window_id: "week",
        observed_at: 1794182340,
        resets_at: 1794182400,
        remaining: {
          ...value("0", "0.00", null),
          state: "range",
          high: "30",
          formatted: { low: "0.00", high: "30.00" },
        },
      },
    ],
  };
}
it("uses server half-even display endpoints without converting the monetary decimal to float", () => {
  expect(analysisValueText(value("1.005", "1.00"))).toBe("$1.00");
  expect(analysisValueText(value("-1.005", "-1.00"))).toBe("-$1.00");
  expect(analysisValueText(value("9007199254740993.01", "9007199254740993.01"))).toBe(
    "$9,007,199,254,740,993.01",
  );
});
it("keeps bounded display uncertainty even when the formatted endpoints match", () => {
  const range = { ...value("49", "49.00"), state: "range" };
  expect(analysisValueText(range)).toBe("$49.00–$49.00");
});
it("renders purchased card payment, original currency, allowance and paid per quota", () => {
  render(
    <AnalysisCards data={fixture()} sort={{ key: "paid", direction: -1 }} onSort={() => {}} />,
  );
  const table = screen.getByRole("table", { name: "Purchased cards" });
  expect(table).toHaveTextContent("$100.00");
  expect(table).toHaveTextContent("HK$800.00");
  expect(table).toHaveTextContent("$200.00");
  expect(table).toHaveTextContent("$0.50");
  expect(screen.queryByRole("region", { name: "Failure rate" })).toBeNull();
});
it("keeps card sorting usable in both directions", () => {
  function Harness() {
    const [direction, setDirection] = useState(-1);
    return (
      <AnalysisCards
        data={fixture()}
        sort={{ key: "paid", direction }}
        onSort={() => setDirection(-direction)}
      />
    );
  }
  render(<Harness />);
  const table = screen.getByRole("table");
  expect(within(table).getAllByRole("row")[1]).toHaveTextContent("Card A");
  fireEvent.click(screen.getByRole("button", { name: "Paid ↓" }));
  expect(within(table).getAllByRole("row")[1]).toHaveTextContent("Card B");
});
it("shows directional same-usage and same-spend comparisons and suppresses 90 day comparisons", () => {
  const result = render(<AnalysisComparisons data={fixture()} ninety={false} />);
  expect(screen.getByText("More spent $1.00")).toBeVisible();
  expect(screen.getByText("More used 20%")).toBeVisible();
  expect(screen.getByText("Token difference: 200")).toBeVisible();
  const lower = fixture();
  lower.comparison.same_spend_percent = value("-20", "-20.00", null);
  lower.comparison.same_spend_difference = value("-200", "-200.00", null);
  result.rerender(<AnalysisComparisons data={lower} ninety={false} />);
  expect(screen.getByText("Less used 20%")).toBeVisible();
  expect(screen.getByText("Token difference: -200")).toBeVisible();
  result.rerender(<AnalysisComparisons data={fixture()} ninety />);
  expect(screen.getAllByText("90 days has no previous period")).toHaveLength(2);
  expect(screen.queryByText("More spent $1.00")).toBeNull();
});
it("keeps quota-closing evidence independent of the payment display", () => {
  const names = new Map([["a", "Alpha"]]);
  const data = fixture();
  data.quota_closings[0].remaining.high = "30.004";
  data.quota_closings[0].remaining.formatted!.high = "30.01";
  const result = render(<AnalysisClosings data={data} names={names} model={false} />);
  expect(screen.getByText("≤30.01%")).toBeVisible();
  data.projection.paid = value("1", "1.00");
  result.rerender(<AnalysisClosings data={data} names={names} model={false} />);
  expect(screen.getByText("≤30.01%")).toBeVisible();
  expect(screen.getByText("Alpha")).toBeVisible();
});
it("uses selected UTC hours for heatmap values and keeps partial data unknown", () => {
  const row = {
    account_id: "a",
    parent_account_id: "a",
    pool_id: null,
    hour: Date.parse("2026-11-09T23:00:00Z") / 1000,
    model: "Claude",
    basis: "metered",
    tokens: 100,
    requests: 10,
    failed: 0,
    cost_usd: null,
    contributes_usage: true,
    contributes_money: false,
    attributed: false,
    coverage: "complete",
    reason: null,
  } satisfies AnalysisRow;
  const result = render(<AnalysisHeatmap rows={[row]} partial={false} timezone="UTC" />);
  expect(screen.getByRole("button", { name: "Mon 23:00 100 tokens" })).toBeVisible();
  expect(screen.getByRole("button", { name: "Mon 22:00 0 tokens" })).toBeVisible();
  result.rerender(<AnalysisHeatmap rows={[row]} partial timezone="UTC" />);
  expect(screen.queryByRole("button", { name: "Mon 23:00 100 tokens" })).toBeNull();
  expect(screen.getByText("Incomplete selected token population")).toBeVisible();
});
