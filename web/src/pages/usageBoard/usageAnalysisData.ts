import { useQuery } from "@tanstack/react-query";
import { usageJson } from "./usagePriceSourceData";
import { usageRangeQuery, type UsageRange } from "./UsageRangeControl";
import { USAGE_BOARD_POLL_MS } from "./usageBoardData";
import { formatRingPercent } from "./usageBoardFormat";

export type AnalysisDimension = "account" | "pool" | "model";
export type AnalysisGroup = AnalysisDimension | "card" | "host" | "agent";
export interface AnalysisFilter {
  group: AnalysisGroup;
  range: "today" | "d7" | "d30" | "d90" | "custom";
  tz: string;
  account: string[];
  model: string[];
  host: string[];
  agent: string[];
  day: string | null;
  slot: string | null;
  from: string | null;
  to: string | null;
}

export interface AnalysisCounts {
  tokens: number | null;
  requests: number | null;
  failed: number | null;
  last_at: number | null;
  coverage: "complete" | "partial";
}
export interface AnalysisAmounts extends AnalysisCounts {
  api_value: AnalysisValue;
  paid: AnalysisValue;
  payback: AnalysisValue;
}
export interface AnalysisClasses {
  basis: "price_table_estimate";
  items: {
    kind: "input" | "cache_read" | "cache_write" | "output";
    tokens: number;
    cost_low_usd: string;
    cost_high_usd: string;
    unpriced_tokens: number;
  }[];
  unpriced_tokens: Record<string, number>;
  unsplit_tokens: number;
  unsplit_cost: { low: string; high: string | null };
}
export interface AnalysisHour extends AnalysisCounts {
  start: number;
  label: string;
  totals: AnalysisAmounts;
  top_host: string | null;
  top_agent: string | null;
  top_model: string | null;
}
export interface UsageAnalysisV2 {
  generated_at: number;
  filter: AnalysisFilter;
  tz: string;
  span: UsageAnalysis["span"];
  complete: boolean;
  incomplete_reason: "hour_over_limit" | "scan_over_limit" | "card_models_over_limit" | null;
  approx: boolean;
  totals: AnalysisAmounts;
  series: (AnalysisAmounts & { at: number | string })[];
  groups: (Partial<AnalysisAmounts> & {
    key: string | [string, string] | null;
    selected?: boolean;
    remainder?: boolean;
    trend?: (AnalysisCounts & { at: number | string })[];
  })[];
  facets: Record<"account" | "model" | "host" | "agent", UsageAnalysisV2["groups"]>;
  heat: (AnalysisCounts & { slot: string })[];
  days: (AnalysisAmounts & { day: string; classes: AnalysisClasses; hours: AnalysisHour[] })[];
  classes: AnalysisClasses;
  cache_savings: { state: string; reason?: string };
  previous?: Pick<UsageAnalysisV2, "span" | "totals" | "series">;
  rows: (AnalysisRow & { host_id: string; agent: string })[];
  rows_truncated: boolean;
  hours: AnalysisHour[];
}
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
  api_value?: AnalysisValue;
  spend?: AnalysisValue;
  spend_estimated?: boolean;
  show_money?: boolean;
  show_payback?: boolean;
  account_money?: Record<string, AccountMoney>;
  paid: AnalysisValue;
  payback: AnalysisValue;
  tokens: AnalysisValue;
  coverage?: { known_paid: number; total_items: number };
  cards?: AnalysisCard[];
}
export interface AccountMoney {
  cost_class: "subscription" | "prepaid" | "metered" | "free" | null;
  show_money: boolean;
  show_payback: boolean;
  estimated: boolean;
  spend: AnalysisValue;
  api_value: AnalysisValue;
  paid: AnalysisValue;
  payback: AnalysisValue;
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
    const [whole, decimal] = (unit === "percent" ? formatRingPercent(raw) : raw).split(".");
    const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    if (unit === "percent") return `${grouped}.${decimal}%`;
    return unit === "money" || unit === "quota"
      ? `${grouped.startsWith("-") ? "-" : ""}${symbol}${grouped.replace(/^-/, "")}.${decimal ?? "00"}`
      : `${grouped}${decimal && Number(decimal) ? `.${decimal.replace(/0+$/, "")}` : ""}${unit === "ratio" ? "×" : ""}`;
  };
  const low = text(value.formatted.low),
    high = value.formatted.high === null ? null : text(value.formatted.high);
  return high === null ? `≥${low}` : value.state === "exact" ? low : `${low}–${high}`;
}
