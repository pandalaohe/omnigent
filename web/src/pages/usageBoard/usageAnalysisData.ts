import { useQuery } from "@tanstack/react-query";
import { usageJson } from "./usagePriceSourceData";
import { usageRangeQuery, type UsageRange } from "./UsageRangeControl";
import { USAGE_BOARD_POLL_MS } from "./usageBoardData";

export type AnalysisDimension = "account" | "pool" | "model";
export interface AnalysisRow {
  account_id: string;
  parent_account_id: string;
  pool_id: string | null;
  hour: number;
  model: string;
  basis: string;
  tokens: number | null;
  requests: number | null;
  failed: number | null;
  cost_usd: number | null;
  contributes_usage: boolean;
  contributes_money: boolean;
  attributed: boolean;
  coverage: string | null;
  reason: string | null;
}
export interface AnalysisValue {
  state: string;
  low: string | null;
  high: string | null;
  currency: string | null;
  reason: string | null;
}
export interface UsageAnalysis {
  generated_at: number;
  span: { requested_from: number; counted_from: number; until: number };
  pool_catalog: {
    parent_account_id: string;
    pool_id: string;
    display_name: string;
    usage_available: boolean;
  }[];
  rows: AnalysisRow[];
  partial: boolean;
  truncated: boolean;
  pricing?: { paid: AnalysisValue; tokens: AnalysisValue; payback: AnalysisValue } | null;
}
export function useUsageAnalysis(range: UsageRange) {
  const params = usageRangeQuery(range);
  return useQuery({
    queryKey: ["usageBoard", "analysis", params.toString()],
    queryFn: () => usageJson<UsageAnalysis>(`analysis?${params}`),
    retry: false,
    refetchInterval: () => (document.visibilityState === "visible" ? USAGE_BOARD_POLL_MS : false),
  });
}
export const poolKey = (parent: string, pool: string) => JSON.stringify([parent, pool]);
export function analysisKey(row: AnalysisRow, dimension: AnalysisDimension) {
  return dimension === "account"
    ? row.parent_account_id
    : dimension === "model"
      ? row.model
      : row.attributed && row.pool_id !== null
        ? poolKey(row.parent_account_id, row.pool_id)
        : null;
}
/** Empty and full picks both mean the whole currently available cohort. */
export function toggleAnalysisPick(current: string[] | null, key: string, all: string[]) {
  const live = current?.filter((item) => all.includes(item)) ?? null;
  const next = !live?.length
    ? [key]
    : live.includes(key)
      ? live.filter((item) => item !== key)
      : [...live, key];
  return !next.length || next.length === all.length ? null : next;
}
export function analysisTotals(rows: AnalysisRow[], incomplete = false) {
  const usage = rows.filter((row) => row.contributes_usage);
  const complete = (key: "tokens" | "requests" | "failed") =>
    !incomplete &&
    usage.length > 0 &&
    usage.every((row) => row[key] !== null && row.coverage !== "partial" && !row.reason);
  const sum = (key: "tokens" | "requests" | "failed") =>
    usage.reduce((n, row) => n + (row[key] ?? 0), 0);
  const requests = complete("requests") ? sum("requests") : null;
  const failed = complete("failed") ? sum("failed") : null;
  return {
    tokens: complete("tokens") ? sum("tokens") : null,
    requests,
    failed,
    failure: requests && failed !== null ? (failed / requests) * 100 : null,
  };
}
