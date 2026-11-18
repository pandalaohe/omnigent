import type {
  UsageAccountActivity,
  UsageActivity,
  UsageIssues,
  UsageSpan,
  UsageTotals,
} from "./usageActivityData";
import { sumUsage } from "./usageAmountData";
import type { AnalysisFilter, AnalysisValue, UsageAnalysisV2 } from "./usageAnalysisData";

export function usageAnalysisV2Fixture(
  params: URLSearchParams,
  now = Math.floor(Date.now() / 1000),
): UsageAnalysisV2 {
  const dimensions = ["account", "model", "host", "agent"] as const;
  const filter: AnalysisFilter = {
    range: params.get("range") as AnalysisFilter["range"],
    group: params.get("group") as AnalysisFilter["group"],
    account: params.getAll("account").sort(),
    model: params.getAll("model").sort(),
    host: params.getAll("host").sort(),
    agent: params.getAll("agent").sort(),
    tz: params.get("tz") ?? "UTC",
    day: params.get("day"),
    slot: params.get("slot"),
    from: params.get("from"),
    to: params.get("to"),
  };
  const current = [
    {
      account: "claude",
      model: "Sonnet",
      host: "east",
      agent: "claude",
      tokens: 100,
      requests: 10,
      failed: 0,
    },
    {
      account: "litellm",
      model: "GPT",
      host: "west",
      agent: "codex",
      tokens: 300,
      requests: 30,
      failed: 3,
    },
  ];
  const restricted = !!(
    filter.host.length ||
    filter.agent.length ||
    filter.model.length ||
    filter.slot
  );
  const cost = (n: number): AnalysisValue => ({
    state: "exact",
    low: String(n),
    high: String(n),
    currency: "USD",
    reason: null,
    formatted: { low: n.toFixed(2), high: n.toFixed(2) },
  });
  const valueUnknown: AnalysisValue = {
    state: "unknown",
    low: null,
    high: null,
    currency: null,
    reason: "shared_payment_cannot_be_split_by_filter",
  };
  const aggregate = (rows: typeof current, paid = !restricted) => {
    const tokens = rows.reduce((sum, row) => sum + row.tokens, 0);
    return {
      tokens,
      requests: rows.reduce((sum, row) => sum + row.requests, 0),
      failed: rows.reduce((sum, row) => sum + row.failed, 0),
      last_at: rows.length ? now : null,
      coverage: "complete" as const,
      api_value: cost(tokens / 50),
      paid: paid ? cost(tokens / 100) : valueUnknown,
      payback: paid ? { ...cost(2), currency: null } : valueUnknown,
    };
  };
  const apply = (ignore?: (typeof dimensions)[number]) =>
    current.filter((row) =>
      dimensions.every(
        (key) => key === ignore || !filter[key].length || filter[key].includes(row[key]),
      ),
    );
  const facet = (dimension: (typeof dimensions)[number]) =>
    apply(dimension).map((row) => ({
      key: row[dimension],
      selected: filter[dimension].includes(row[dimension]),
      ...aggregate([row], !restricted && dimension === "account"),
    }));
  const span = { requested_from: now - 3600, counted_from: now - 3600, until: now };
  const totals = aggregate(apply());
  const series = [{ at: now - 3600, ...totals }];
  return {
    generated_at: now,
    span,
    totals,
    series,
    complete: true,
    incomplete_reason: null,
    approx: false,
    filter,
    tz: filter.tz,
    facets: {
      account: facet("account"),
      model: facet("model"),
      host: facet("host"),
      agent: facet("agent"),
    },
    groups: facet(dimensions.find((key) => key === filter.group) ?? "account"),
    heat: [{ slot: "2-14", ...aggregate(apply()) }],
    days: [],
    hours: [],
    rows: [],
    rows_truncated: false,
    classes: {
      basis: "price_table_estimate",
      items: [],
      unpriced_tokens: {},
      unsplit_tokens: 0,
      unsplit_cost: { low: "0", high: "0" },
    },
    cache_savings: { state: "unknown" },
    previous: {
      span: { requested_from: now - 7200, counted_from: now - 7200, until: now - 3600 },
      totals,
      series: [{ at: now - 7200, ...totals }],
    },
  };
}

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

const HOUR = 3600;
const DAY = 24 * HOUR;
const fixtureAccounts = ["codex", "claude", "litellm", "grok", "pi"];
const empty = sampleTotals(null, null);

function hourlyRows(account: string, index: number, from: number, until: number) {
  if (account === "grok") return [];
  const length = Math.max(0, Math.ceil((until - from) / HOUR));
  return Array.from({ length }, (_, offset) => {
    const at = from + offset * HOUR;
    const hour = Math.floor(at / HOUR);
    const profile = (((hour * 19 + index * 3) % 17) + 17) % 17;
    const value = sampleTotals(
      ((index + 1) * (profile + 4)) / 128,
      11000 + profile * 4000,
      account === "litellm"
        ? {
            requests: 5,
            failed: profile === 0 ? 1 : 0,
            failure_pct: profile === 0 ? 20 : 0,
            reasons: profile === 0 ? { timeout: 1 } : {},
            unpriced: profile === 0 ? 1 : 0,
          }
        : {},
    );
    return { at, ...value, last_at: at, metered: index < 2 ? empty : value };
  });
}

function fixtureTotals(rows: ReturnType<typeof hourlyRows>): UsageTotals {
  return {
    ...sumUsage(rows),
    last_at: rows.at(-1)?.at ?? null,
    metered: sumUsage(rows.map((row) => row.metered)),
  };
}

function fixtureSeries(
  rows: ReturnType<typeof hourlyRows>,
  span: UsageSpan,
  bucketUnit?: "hour" | "day",
) {
  if (
    bucketUnit === "hour" ||
    (bucketUnit === undefined && span.until - span.requested_from <= 48 * HOUR)
  )
    return rows;
  const buckets = new Map<number, typeof rows>();
  for (const row of rows) {
    const date = new Date(row.at * 1000);
    date.setHours(0, 0, 0, 0);
    if (bucketUnit === undefined && span.until - span.requested_from > 45 * DAY)
      date.setDate(date.getDate() - ((date.getDay() + 6) % 7));
    const at = date.getTime() / 1000;
    const bucket = buckets.get(at) ?? [];
    bucket.push(row);
    buckets.set(at, bucket);
  }
  return [...buckets].map(([at, bucket]) => ({ at, ...fixtureTotals(bucket) }));
}

function fixtureSpan(requested_from: number, until: number): UsageSpan {
  const counted_from = Math.floor(requested_from / HOUR) * HOUR;
  return { requested_from, counted_from, until, approx: counted_from !== requested_from };
}

export function usageActivityFixture(
  params = new URLSearchParams(),
  now = Date.now() / 1000,
  { bucketUnit }: { bucketUnit?: "hour" | "day" } = {},
): UsageActivity {
  const range = params.get("range") ?? "d7";
  const midnight = (days: number) => {
    const date = new Date(now * 1000);
    date.setHours(0, 0, 0, 0);
    date.setDate(date.getDate() - days);
    return date.getTime() / 1000;
  };
  const localDate = (input: string | null, next = false) => {
    const date = new Date(`${input}T00:00:00`);
    if (next) date.setDate(date.getDate() + 1);
    return date.getTime() / 1000;
  };
  const start =
    {
      today: midnight(0),
      h24: now - DAY,
      d7: midnight(6),
      d30: midnight(29),
      d90: midnight(89),
      period: now - 30 * DAY,
      custom: localDate(params.get("from")),
    }[range] ?? midnight(6);
  const until = range === "custom" ? Math.min(now, localDate(params.get("to"), true)) : now;
  const accounts: UsageAccountActivity[] = fixtureAccounts.flatMap((account, index) => {
    if (params.get("account") && params.get("account") !== account) return [];
    const current = fixtureSpan(
      range === "period" && account === "codex" ? now - 3 * DAY : start,
      until,
    );
    const elapsed = Math.ceil((current.until - current.counted_from) / HOUR) * HOUR;
    const yesterday = new Date(now * 1000);
    yesterday.setDate(yesterday.getDate() - 1);
    const prior =
      range === "today"
        ? fixtureSpan(midnight(1), yesterday.getTime() / 1000)
        : fixtureSpan(current.counted_from - elapsed, current.counted_from);
    const rows = hourlyRows(account, index, current.counted_from, current.until);
    const previousRows = hourlyRows(account, index, prior.counted_from, prior.until);
    const mark = index < 2 ? "~" : account === "litellm" ? "≥" : current.approx ? "≈" : null;
    const totals = fixtureTotals(rows);
    return [
      {
        account,
        span: current,
        mark,
        totals,
        series: fixtureSeries(rows, current, bucketUnit),
        previous: previousRows.length
          ? {
              span: prior,
              mark,
              totals: fixtureTotals(previousRows),
              series: fixtureSeries(previousRows, prior, bucketUnit),
            }
          : null,
        models: rows.length ? [{ model: index < 2 ? "model-a" : "model-b", ...totals }] : [],
        hosts: rows.length ? [{ host_id: "h1", ...totals }] : [],
      },
    ];
  });
  return {
    generated_at: now,
    range,
    tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
    accounts,
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
