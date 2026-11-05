import type { UsageAccountActivity, UsageAmount, UsageTotals } from "./usageActivityData";
import { formatMoney } from "./usageBoardFormat";
import type { UsageTipModel } from "./UsageTip";

export function sumUsage(values: UsageTotals[]): UsageTotals {
  const sum = (
    key:
      | "cost_usd"
      | "counted_cost_usd"
      | "tokens"
      | "unpriced"
      | "covered_cost_usd"
      | "covered_tokens"
      | "requests"
      | "failed",
  ) => values.reduce((total, value) => total + (value[key] ?? 0), 0);
  const cost = values.some((value) => value.cost_usd !== null) ? sum("cost_usd") : null;
  const requests = values.some((value) => value.requests !== null) ? sum("requests") : null;
  const coveredTokens = sum("covered_tokens");
  const coveredCost = sum("covered_cost_usd");
  return {
    cost_usd: cost,
    counted_cost_usd: sum("counted_cost_usd"),
    tokens: values.some((value) => value.tokens !== null) ? sum("tokens") : null,
    unpriced: sum("unpriced"),
    covered_cost_usd: coveredCost,
    covered_tokens: coveredTokens,
    per_million: coveredTokens > 0 ? (coveredCost / coveredTokens) * 1e6 : null,
    cost_coverage_pct: cost && cost > 0 ? (coveredCost / cost) * 100 : null,
    tokens_complete: values.length > 0 && values.every((value) => value.tokens_complete),
    requests,
    failed: requests === null ? null : sum("failed"),
    failure_pct: requests ? (sum("failed") / requests) * 100 : null,
    reasons: Object.fromEntries(
      [...new Set(values.flatMap((value) => Object.keys(value.reasons)))].map((reason) => [
        reason,
        values.reduce((total, value) => total + (value.reasons[reason] ?? 0), 0),
      ]),
    ),
  };
}

/** The summary counts only visible accounts and their metered rows. */
export function boardAmount(
  accounts: UsageAccountActivity[],
  previous = false,
  tokens = false,
): UsageAmount | null {
  const amounts = accounts.flatMap((account) => {
    const amount = previous ? account.previous : account;
    return amount ? [amount] : [];
  });
  if (!amounts.length) return null;
  const points = new Map<number, UsageTotals[]>();
  for (const amount of amounts)
    for (const point of amount.series) {
      const group = points.get(point.at) ?? [];
      group.push(tokens ? point : (point.metered ?? point));
      points.set(point.at, group);
    }
  const totals = sumUsage(
    amounts.map((amount) => (tokens ? amount.totals : (amount.totals.metered ?? amount.totals))),
  );
  if (
    !tokens &&
    amounts.some((amount) => amount.totals.cost_usd !== null) &&
    totals.cost_usd === null
  )
    totals.cost_usd = 0;
  const approx = amounts.some((amount) => amount.span.approx);
  return {
    span: {
      requested_from: Math.min(...amounts.map((amount) => amount.span.requested_from)),
      counted_from: Math.min(...amounts.map((amount) => amount.span.counted_from)),
      until: Math.max(...amounts.map((amount) => amount.span.until)),
      approx,
    },
    mark: amounts.some((amount) => amount.mark === "~")
      ? "~"
      : totals.unpriced
        ? "≥"
        : amounts.some((amount) => amount.mark === "≈")
          ? "≈"
          : null,
    totals,
    series: [...points]
      .sort(([a], [b]) => a - b)
      .map(([at, group]) => ({ at, ...sumUsage(group) })),
  };
}

export function usageFigure(totals: UsageTotals, tokens: boolean): string {
  if (!tokens) return formatMoney(totals.cost_usd ?? (totals.unpriced > 0 ? 0 : null));
  if (totals.tokens === null) return "—";
  return new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 }).format(
    totals.tokens,
  );
}
export function amountTip(amount: UsageAmount, total = false): UsageTipModel {
  const rows: UsageTipModel["rows"] = [];
  if (amount.mark === "~")
    rows.push({ label: "Estimates", value: total ? "Left out of total" : "List-price estimate" });
  if (amount.totals.unpriced)
    rows.push({ label: "Unpriced rows", value: String(amount.totals.unpriced) });
  if (amount.span.approx)
    rows.push({
      label: "Counted from",
      value: new Date(amount.span.counted_from * 1000).toLocaleString("en-US", {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      }),
    });
  if (amount.totals.cost_coverage_pct !== null && amount.totals.cost_coverage_pct < 100)
    rows.push({
      label: "Covers",
      value: `${Math.round(amount.totals.cost_coverage_pct)}% of cost`,
    });
  if (!rows.length) rows.push({ label: "Basis", value: "Metered usage" });
  return { title: "Usage amount", rows };
}
