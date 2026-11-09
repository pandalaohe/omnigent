import { useQuery } from "@tanstack/react-query";
import { useUsageContextPreferences } from "@/hooks/useUsageContextPreferences";
import { usageJson } from "./usagePriceSourceData";
import { usageRangeQuery } from "./UsageRangeControl";
import { USAGE_BOARD_POLL_MS } from "./usageBoardData";

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

export function useUsageActivity(account?: string, enabled = true) {
  return useUsageRead<UsageActivity>("activity", account, enabled);
}
export function useUsageIssues(account?: string, enabled = true) {
  return useUsageRead<UsageIssues>("issues", account, enabled);
}
function useUsageRead<T>(path: string, account: string | undefined, enabled: boolean) {
  const board = useUsageContextPreferences().board;
  const params = usageRangeQuery(board.range ?? { key: "d7" });
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
