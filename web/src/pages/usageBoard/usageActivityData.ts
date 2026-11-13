import { useQuery } from "@tanstack/react-query";
import { useUsageContextPreferences } from "@/hooks/useUsageContextPreferences";
import { usageJson } from "./usagePriceSourceData";
import { usageRangeQuery } from "./UsageRangeControl";
import { USAGE_BOARD_POLL_MS } from "./usageBoardData";
import { sumUsage } from "./usageAmountData";

export interface UsageTotals {
  partial_rows?: number;
  cost_usd: number | null;
  counted_cost_usd: number;
  tokens: number | null;
  unpriced: number;
  per_million: number | null;
  covered_cost_usd: number;
  covered_tokens: number;
  cost_coverage_pct: number | null;
  tokens_complete: boolean;
  requests: number | null;
  failed: number | null;
  failure_pct: number | null;
  reasons: Record<string, number>;
  /** Same figures restricted to billable rows, for the board total. */
  metered?: UsageTotals;
}
export interface UsageSpan {
  requested_from: number;
  counted_from: number;
  until: number;
  approx: boolean;
}
export interface UsageAmount {
  span: UsageSpan;
  mark: "~" | "≥" | "≈" | null;
  totals: UsageTotals;
  series: ({ at: number } & UsageTotals)[];
}
export interface UsageAccountActivity extends UsageAmount {
  account: string;
  previous: UsageAmount | null;
  models?: ({ model: string } & UsageTotals)[];
  hosts?: ({ host_id: string } & UsageTotals)[];
}
export interface UsageActivity {
  generated_at: number;
  range: string;
  tz: string;
  accounts: UsageAccountActivity[];
}
export interface UsageIssues {
  failures: ({ account: string } & UsageTotals)[];
  unpriced_models: ({ account: string; model: string } & UsageTotals)[];
  source_reports: { account: string; kind: string; at: number }[];
  price_source?: { at: number; error: string }[];
}

export function useUsageActivity(
  account?: string,
  enabled = true,
  range?: Parameters<typeof usageRangeQuery>[0],
) {
  return useUsageRead<UsageActivity>("activity", account, enabled, range);
}

/** Every detail consumer uses the same proven pool cohort, including its prior. */
export function useUsageAccountActivity(
  account: string,
  focus: string | null,
  enabled = true,
  range?: Parameters<typeof usageRangeQuery>[0],
) {
  const board = useUsageContextPreferences().board;
  const parent = useUsageActivity(account, enabled && !focus, range);
  const params = usageRangeQuery(range ?? board.range ?? { key: "d7" });
  params.set("account", account);
  params.set("include_previous", "true");
  const pool = useQuery({
    queryKey: ["usageBoard", "accountPoolActivity", params.toString(), focus],
    enabled: enabled && !!focus,
    queryFn: async (): Promise<UsageActivity> => {
      const data = await usageJson<DetailAnalysis>(`analysis?${params}`);
      const amount = poolAmount(data, account, focus!);
      return {
        generated_at: data.generated_at,
        range: params.get("range")!,
        tz: params.get("tz")!,
        accounts: amount ? [amount] : [],
      };
    },
    retry: false,
    refetchInterval: () => (document.visibilityState === "visible" ? USAGE_BOARD_POLL_MS : false),
    refetchOnWindowFocus: true,
  });
  return focus ? pool : parent;
}
export function useUsageIssues(account?: string, enabled = true) {
  return useUsageRead<UsageIssues>("issues", account, enabled);
}
function useUsageRead<T>(
  path: string,
  account: string | undefined,
  enabled: boolean,
  range?: Parameters<typeof usageRangeQuery>[0],
) {
  const board = useUsageContextPreferences().board;
  const params = usageRangeQuery(range ?? board.range ?? { key: "d7" });
  if (account) params.set("account", account);
  params.set("include", "models");
  return useQuery({
    queryKey: ["usageBoard", path, params.toString()],
    queryFn: () => usageJson<T>(`${path}?${params}`),
    enabled,
    refetchInterval: () => (document.visibilityState === "visible" ? USAGE_BOARD_POLL_MS : false),
    refetchOnWindowFocus: true,
    retry: false,
  });
}

interface DetailAnalysis {
  previous?: DetailAnalysis | null;
  partial?: boolean;
  truncated?: boolean;
  generated_at: number;
  span: UsageSpan;
  account_spans: Record<string, UsageSpan>;
  pool_catalog: {
    parent_account_id: string;
    pool_id: string;
    display_name: string;
    usage_available: boolean;
  }[];
  pools: { parent_account_id: string; pool_id: string; channel: string; totals: UsageTotals }[];
  rows: {
    parent_account_id: string;
    pool_id: string | null;
    channel: string | null;
    hour: number;
    model: string;
    tokens: number | null;
    cost_usd: number | null;
    contributes_money: boolean;
    contributes_usage: boolean;
  }[];
}

function poolAmount(
  data: DetailAnalysis | undefined,
  account: string,
  focus: string,
): UsageAccountActivity | undefined {
  if (!data) return;
  const matches = data.pool_catalog.filter(
    (pool) =>
      pool.parent_account_id === account && (pool.pool_id === focus || pool.display_name === focus),
  );
  if (matches.length !== 1 || !matches[0].usage_available) return;
  const poolId = matches[0].pool_id;
  const totals = data.pools
    .filter(
      (pool) =>
        pool.parent_account_id === account &&
        pool.pool_id === poolId &&
        ["usage", "metered_cost"].includes(pool.channel),
    )
    .map((pool) => pool.totals);
  if (!totals.length) return;
  const rows = data.rows.filter(
    (row) =>
      row.parent_account_id === account &&
      row.pool_id === poolId &&
      (row.contributes_money || row.contributes_usage),
  );
  const base = sumUsage(totals);
  const project = (items: DetailAnalysis["rows"]): UsageTotals => ({
    ...base,
    cost_usd: items.some((row) => row.contributes_money && row.cost_usd !== null)
      ? items.reduce((sum, row) => sum + (row.contributes_money ? (row.cost_usd ?? 0) : 0), 0)
      : null,
    tokens: items.some((row) => row.contributes_usage && row.tokens !== null)
      ? items.reduce((sum, row) => sum + (row.contributes_usage ? (row.tokens ?? 0) : 0), 0)
      : null,
  });
  const byHour = new Map<number, DetailAnalysis["rows"]>();
  const byModel = new Map<string, DetailAnalysis["rows"]>();
  for (const row of rows) {
    const hour = byHour.get(row.hour) ?? [];
    hour.push(row);
    byHour.set(row.hour, hour);
    const model = byModel.get(row.model) ?? [];
    model.push(row);
    byModel.set(row.model, model);
  }
  return {
    account,
    span: data.account_spans[account] ?? data.span,
    totals: base,
    previous: data.previous ? (poolAmount(data.previous, account, focus) ?? null) : null,
    mark: base.unpriced || data.partial || data.truncated ? "≥" : null,
    series: [...byHour].sort(([a], [b]) => a - b).map(([at, items]) => ({ at, ...project(items) })),
    models: [...byModel].map(([model, items]) => ({ model, ...project(items) })),
  };
}
