import { useSyncExternalStore } from "react";
import { analysisDimensions, toQuery, type PageFilter } from "./usageAnalysisFilters";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";
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
export interface CacheSavings {
  amount: AnalysisValue | null;
  unknown_tokens: number;
  split_keys: number;
  split_tokens: number | null;
  unbounded_keys: number;
}
export interface UsageAnalysisV2 {
  generated_at: number;
  filter: AnalysisFilter;
  tz: string;
  span: UsageAnalysis["span"];
  complete: boolean;
  incomplete_reason:
    | "hour_over_limit"
    | "scan_over_limit"
    | "card_models_over_limit"
    | "aggregation_over_limit"
    | null;
  approx: boolean;
  requests_configured?: boolean;
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
  cache_savings: CacheSavings;
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
  live = true,
) {
  const params = usageRangeQuery(range);
  if (selection) {
    params.set("dimension", selection.dimension);
    params.set("selection", JSON.stringify(selection.keys));
    for (const id of selection.hidden) params.append("exclude", id);
  }
  return useQuery({
    queryKey: ["usageBoard", "analysis", params.toString()],
    queryFn: ({ signal }) => usageJson<UsageAnalysis>(`analysis?${params}`, { signal }),
    enabled: selection?.enabled ?? true,
    retry: false,
    refetchInterval: live
      ? () => (document.visibilityState === "visible" ? USAGE_BOARD_POLL_MS : false)
      : false,
    refetchOnWindowFocus: live,
    refetchOnReconnect: live,
    staleTime: live ? 0 : 300_000,
  });
}
export const ANALYSIS_POLL_MS = 300_000;

export class AnalysisFilterMismatch extends Error {
  constructor() {
    super("The server did not return the requested usage filter");
  }
}

function sameFilter(actual: unknown, expected: AnalysisFilter) {
  if (typeof actual !== "object" || actual === null) return false;
  const echo = actual as Partial<AnalysisFilter>;
  return (
    (["group", "range", "tz", "day", "slot", "from", "to"] as const).every(
      (key) => echo[key] === expected[key],
    ) &&
    analysisDimensions.every(
      (key) =>
        Array.isArray(echo[key]) &&
        echo[key].length === expected[key].length &&
        echo[key].every((value, index) => value === expected[key][index]),
    )
  );
}

const visible = () => document.visibilityState === "visible";
function subscribeVisibility(change: () => void) {
  document.addEventListener("visibilitychange", change);
  return () => document.removeEventListener("visibilitychange", change);
}

export function useAnalysisVisible() {
  return useSyncExternalStore(subscribeVisibility, visible, () => true);
}

export function useUsageAnalysisV2(
  filter: PageFilter,
  { previous = true, poll = true, enabled = true, keepResults = false } = {},
) {
  const isVisible = useAnalysisVisible();
  const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const params = toQuery(filter, undefined, true);
  params.set("tz", tz);
  if (previous) params.append("include", "previous");
  return useQuery({
    queryKey: ["usageBoard", "analysisV2", params.toString()],
    queryFn: async ({ signal }) => {
      const data = await usageJson<UsageAnalysisV2>(`analysis?${params}`, { signal });
      const expected = {
        ...filter,
        tz,
        from: params.get("from"),
        to: params.get("to"),
        ...Object.fromEntries(analysisDimensions.map((key) => [key, params.getAll(key)])),
      };
      if (!sameFilter(data?.filter, expected)) throw new AnalysisFilterMismatch();
      return data;
    },
    enabled: isVisible && enabled,
    placeholderData: keepResults ? keepPreviousData : undefined,
    staleTime: ANALYSIS_POLL_MS,
    gcTime: 2 * ANALYSIS_POLL_MS,
    retry: false,
    refetchInterval: poll ? ANALYSIS_POLL_MS : false,
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  });
}

export interface UsageAnalysisHoursResponse {
  filter: AnalysisFilter;
  span: UsageAnalysis["span"];
  complete: boolean;
  incomplete_reason: UsageAnalysisV2["incomplete_reason"];
  approx: boolean;
  requests_configured?: boolean;
  hours: AnalysisHour[];
}

export function useUsageAnalysisHours(filter: PageFilter, day: string | null, enabled = true) {
  const isVisible = useAnalysisVisible();
  const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const params = toQuery({ ...filter, day }, undefined, true);
  params.set("tz", tz);
  return useQuery({
    queryKey: ["usageBoard", "analysisHours", params.toString()],
    queryFn: ({ signal }) =>
      usageJson<UsageAnalysisHoursResponse>(`analysis/hours?${params}`, { signal }),
    enabled: isVisible && enabled && day !== null,
    staleTime: ANALYSIS_POLL_MS,
    gcTime: 2 * ANALYSIS_POLL_MS,
    retry: false,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  });
}

export interface UsageRequestTokens {
  input: number | null;
  cache_read: number | null;
  cache_write: number | null;
  output: number | null;
  reasoning: number | null;
}

export interface UsageRequestRow {
  id: string;
  source: string | null;
  at: string;
  host: string | null;
  agent: string | null;
  account: string | null;
  group: string | null;
  model: string | null;
  model_requested: string | null;
  tokens: UsageRequestTokens;
  cost_usd: string | null;
  cost_basis: string | null;
  ttft_ms: number | null;
  duration_ms: number | null;
  tps: number | null;
  status: string;
  http_status: number | null;
  error_class: string | null;
  fallback: boolean | null;
}

export interface UsageRequestSection {
  state: string;
  reason: string | null;
}

export interface UsageRequestsPage {
  hour: number;
  tz: string;
  state: "ok" | "expired" | "partial";
  rows: UsageRequestRow[];
  next_cursor: string | null;
  retained_from: number | null;
  sections: Record<string, UsageRequestSection> | null;
}

export class UsageRequestsHttpError extends Error {
  status: number;
  detail: string;
  constructor(status: number, detail: string) {
    super(detail);
    this.status = status;
    this.detail = detail;
  }
}

export async function fetchUsageRequests({
  hour,
  tz,
  filter,
  cursor,
  limit = 8,
  signal,
}: {
  hour: number;
  tz: string;
  filter: PageFilter;
  cursor: string | null;
  limit?: number;
  signal?: AbortSignal;
}): Promise<UsageRequestsPage> {
  const params = new URLSearchParams();
  params.set("hour", String(hour));
  params.set("tz", tz);
  params.set("limit", String(limit));
  for (const key of analysisDimensions) {
    for (const value of filter[key]) params.append(key, value);
  }
  if (cursor !== null) params.set("cursor", cursor);
  const response = await authenticatedFetch(`/v1/me/usage/requests?${params}`, { signal });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const detail =
      typeof body?.detail === "string"
        ? body.detail
        : `Usage requests read failed (${response.status})`;
    throw new UsageRequestsHttpError(response.status, detail);
  }
  return response.json() as Promise<UsageRequestsPage>;
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
