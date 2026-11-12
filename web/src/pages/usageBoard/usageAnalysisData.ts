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
  formatted?: { low: string | null; high: string | null };
  coverage?: { known_paid: number; total_items: number };
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
  pricing?: AnalysisProjection | null;
  presentation?: AnalysisPresentation | null;
  tz?: string;
}
export function useUsageAnalysis(
  range: UsageRange,
  selection?: {
    dimension: AnalysisDimension | "card";
    keys: string[];
    hidden: string[];
    enabled: boolean;
  },
) {
  const params = usageRangeQuery(range);
  if (selection) {
    params.set("dimension", selection.dimension);
    params.set("selection", JSON.stringify(selection.keys));
    for (const id of selection.hidden) params.append("exclude", id);
  }
  return useQuery({
    queryKey: ["usageBoard", "analysis", params.toString()],
    queryFn: () => usageJson<UsageAnalysis>(`analysis?${params}`),
    enabled: selection?.enabled ?? true,
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

export interface AnalysisProjection {
  paid: AnalysisValue;
  payback: AnalysisValue;
  tokens: AnalysisValue;
  coverage?: { known_paid: number; total_items: number };
  cards?: AnalysisCard[];
}
export interface AnalysisCard {
  id: string;
  name: string;
  parent_account_id?: string;
  pool_id?: string | null;
  paid: AnalysisValue;
  original_paid?: AnalysisValue;
  allowance: AnalysisValue;
  paid_per_quota: AnalysisValue;
  reset_count?: number;
  starts_at: number;
  ends_at: number;
}
export interface AnalysisPresentation {
  partial?: boolean;
  rows: AnalysisRow[];
  projection: AnalysisProjection;
  items: { key: string; projection: AnalysisProjection }[];
  previous: { span: UsageAnalysis["span"]; rows: AnalysisRow[]; partial: boolean } | null;
  comparison: {
    reason?: string;
    same_usage?: AnalysisValue;
    same_usage_difference?: AnalysisValue;
    same_spend?: AnalysisValue;
    same_spend_difference?: AnalysisValue;
    same_spend_percent?: AnalysisValue;
  };
  cards: AnalysisCard[];
  allowance: AnalysisValue;
  paid_per_quota: AnalysisValue;
  paid_series: { at: number; value: AnalysisValue }[];
  quota_closings: {
    account_id: string;
    window_id: string;
    observed_at: number;
    resets_at: number;
    remaining: AnalysisValue;
  }[];
}
export function analysisValueText(
  value: AnalysisValue | null | undefined,
  unit: "money" | "ratio" | "percent" | "quota" | "number" = "money",
): string {
  if (!value?.formatted?.low || value.state === "unknown") return "Unknown";
  const symbol =
    unit === "quota"
      ? "$"
      : unit === "money"
        ? ({ USD: "$", CNY: "¥", HKD: "HK$" }[value.currency ?? "USD"] ?? "")
        : "";
  const text = (raw: string) => {
    const [whole, decimal] = raw.split(".");
    const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    return unit === "money" || unit === "quota"
      ? `${grouped.startsWith("-") ? "-" : ""}${symbol}${grouped.replace(/^-/, "")}.${decimal ?? "00"}`
      : `${grouped}${decimal && Number(decimal) ? `.${decimal.replace(/0+$/, "")}` : ""}${unit === "ratio" ? "×" : unit === "percent" ? "%" : ""}`;
  };
  const low = text(value.formatted.low),
    high = value.formatted.high === null ? null : text(value.formatted.high);
  return high === null ? `≥${low}` : value.state === "exact" ? low : `${low}–${high}`;
}
