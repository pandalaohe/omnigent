import type {
  UsageAccountActivity,
  UsageActivity,
  UsageIssues,
  UsageTotals,
} from "./usageActivityData";
import { sumUsage } from "./usageAmounts";

/** Neutral, explicit billing inputs shared by the tests and operable demo. */
export function sampleTotals(
  cost: number | null,
  tokens: number | null,
  extra: Partial<UsageTotals> = {},
): UsageTotals {
  return {
    cost_usd: cost,
    counted_cost_usd: cost ?? 0,
    tokens,
    unpriced: 0,
    per_million: cost !== null && tokens ? (cost / tokens) * 1e6 : null,
    covered_cost_usd: tokens ? (cost ?? 0) : 0,
    covered_tokens: cost !== null ? (tokens ?? 0) : 0,
    cost_coverage_pct: cost ? (tokens ? 100 : 0) : null,
    tokens_complete: tokens !== null,
    requests: null,
    failed: null,
    failure_pct: null,
    reasons: {},
    ...extra,
  };
}

export function usageActivityFixture(
  params = new URLSearchParams(),
  now = Date.now() / 1000,
): UsageActivity {
  const range = params.get("range") ?? "d7";
  const midnight = (days: number) => {
    const day = new Date(now * 1000);
    day.setHours(0, 0, 0, 0);
    day.setDate(day.getDate() - days);
    return day.getTime() / 1000;
  };
  const start =
    range === "today"
      ? midnight(0)
      : range === "h24"
        ? now - 86400
        : range === "d30"
          ? midnight(29)
          : range === "d90"
            ? midnight(89)
            : range === "period"
              ? now - 30 * 86400
              : range === "custom"
                ? new Date(`${params.get("from")}T00:00:00`).getTime() / 1000
                : midnight(6);
  const until =
    range === "custom" && params.get("to")
      ? Math.min(now, new Date(`${params.get("to")}T00:00:00`).getTime() / 1000 + 86400)
      : now;
  const empty = sampleTotals(null, null);
  const accounts: UsageAccountActivity[] = ["codex", "claude", "litellm", "grok", "pi"].map(
    (account, index) => {
      const from = range === "period" && account === "codex" ? now - 3 * 86400 : start;
      const counted = Math.floor(from / 3600) * 3600;
      const length = until - counted;
      const hourly = length <= 48 * 3600;
      const n = hourly
        ? Math.max(1, Math.ceil(length / 3600))
        : Math.max(1, Math.ceil(length / 86400));
      const series =
        account === "grok"
          ? []
          : Array.from({ length: n }, (_, bucket) => {
              const cost = (index + 1) * (0.3 + (bucket % 4) * 0.15);
              const value = sampleTotals(
                cost,
                300000 + bucket * 10000,
                account === "litellm"
                  ? {
                      requests: 60,
                      failed: 15,
                      failure_pct: 25,
                      reasons: { timeout: 10, gateway: 5 },
                      unpriced: bucket === 0 ? 1 : 0,
                    }
                  : {},
              );
              return {
                at: counted + bucket * (hourly ? 3600 : 86400),
                ...value,
                metered: index < 2 ? empty : value,
              };
            });
      const totals = sumUsage(series);
      totals.metered = sumUsage(series.map((point) => point.metered));
      const previousSeries = series.map((point) => {
        const value = sampleTotals((point.cost_usd ?? 0) * 0.8, point.tokens);
        return { at: point.at - length, ...value, metered: index < 2 ? empty : value };
      });
      const previousTotals = sumUsage(previousSeries);
      previousTotals.metered = sumUsage(previousSeries.map((point) => point.metered));
      return {
        account,
        span: { requested_from: from, counted_from: counted, until, approx: from !== counted },
        mark: index < 2 ? "~" : account === "litellm" ? "≥" : from !== counted ? "≈" : null,
        totals,
        series,
        previous: previousSeries.length
          ? {
              span: {
                requested_from: counted - length,
                counted_from: counted - length,
                until: counted,
                approx: false,
              },
              mark: index < 2 ? "~" : null,
              totals: previousTotals,
              series: previousSeries,
            }
          : null,
        models: series.length ? [{ model: index < 2 ? "model-a" : "model-b", ...totals }] : [],
        hosts: series.length ? [{ host_id: "h1", ...totals }] : [],
      };
    },
  );
  return {
    generated_at: now,
    range,
    tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
    accounts: accounts.filter(
      (item) => !params.get("account") || item.account === params.get("account"),
    ),
  };
}

export function usageIssuesFixture(activity: UsageActivity): UsageIssues {
  return {
    failures: activity.accounts
      .filter((item) => item.totals.failed)
      .map((item) => ({ account: item.account, ...item.totals })),
    unpriced_models: activity.accounts.flatMap((item) =>
      (item.models ?? [])
        .filter((model) => model.unpriced)
        .map((model) => ({ account: item.account, ...model })),
    ),
    source_reports: activity.accounts
      .filter((item) => item.account === "claude")
      .map((item) => ({ account: item.account, kind: "silent", at: activity.generated_at - 7200 })),
  };
}
