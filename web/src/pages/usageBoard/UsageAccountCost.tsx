import { useState } from "react";
import { useQueries } from "@tanstack/react-query";
import { useSearchParams } from "@/lib/routing";
import {
  useUsageActivity,
  type UsageAccountActivity,
  type UsageTotals,
  type UsageSpan,
} from "./usageActivityData";
import { USAGE_BOARD_POLL_MS, type UsageAccountView } from "./usageBoardData";
import { usageJson } from "./usagePriceSourceData";
import { usageRangeQuery } from "./UsageRangeControl";
import { UsageCostChart } from "./UsageCostChart";
import { usageFigure, sumUsage } from "./usageAmountData";
import { formatFullDate } from "./usageBoardFormat";

export function UsageAccountCost({ account }: { account: UsageAccountView }) {
  const [selected, setSelected] = useState(1);
  const [search] = useSearchParams();
  const focus = search.get("focus");
  const today = useUsageActivity(account.id, !focus, { key: "today" });
  const week = useUsageActivity(account.id, !focus, { key: "d7" });
  const period = useUsageActivity(account.id, !focus, { key: "period" });
  const reads = [today, week, period];
  const query = reads[selected];
  // A quota group label alone proves no usage attribution. Resolve the stored
  // relation first; ambiguous labels never fall back to the whole account.
  const pools = useQueries({
    queries: (["today", "d7", "period"] as const).map((key) => ({
      queryKey: ["usageBoard", "accountPool", account.id, key],
      enabled: !!focus,
      queryFn: async () => {
        const params = usageRangeQuery({ key });
        params.set("account", account.id);
        return usageJson<DetailAnalysis>(`analysis?${params}`);
      },
      retry: false,
      refetchInterval: () => (document.visibilityState === "visible" ? USAGE_BOARD_POLL_MS : false),
      refetchOnWindowFocus: true,
    })),
  });
  const amounts = focus
    ? pools.map((read) => poolAmount(read.data, account.id, focus))
    : reads.map((read) => read.data?.accounts.find((item) => item.account === account.id));
  const queryState = focus ? pools[selected] : query;
  const own = amounts[selected];
  const error = queryState.error;
  return (
    <>
      <section
        aria-label="Cost"
        className="usage-detail-cost rounded-[11px] border border-border bg-card px-3.5 py-3"
      >
        <div className="flex items-center justify-between gap-2">
          <h2 className="text-sm font-semibold">Cost</h2>
          <span className="text-xs text-muted-foreground">Reported cost</span>
        </div>
        {focus ? <p className="text-xs text-muted-foreground">{focus} · Pool usage only</p> : null}
        <div className="usage-detail-cells" role="group" aria-label="Cost time range">
          {["Today", "Last 7 days", "This period"].map((label, index) => {
            const read = focus ? pools[index] : reads[index];
            const amount = amounts[index];
            return (
              <button
                key={label}
                type="button"
                aria-pressed={selected === index}
                onClick={() => setSelected(index)}
              >
                <span>{label}</span>
                <b>
                  {read.error
                    ? "Unavailable"
                    : amount
                      ? `${amount.mark ?? ""}${usageFigure(amount.totals, false)}`
                      : "—"}
                </b>
              </button>
            );
          })}
        </div>
        <p className="text-xs text-muted-foreground">API price · Not available yet</p>
        {error ? (
          <p role="alert" className="text-xs text-destructive">
            Cost could not be read
          </p>
        ) : queryState.isPending ? (
          <p className="text-xs text-muted-foreground">Reading cost…</p>
        ) : own ? (
          <>
            <p className="text-xs text-muted-foreground">
              {formatFullDate(own.span.counted_from)} – {formatFullDate(own.span.until)}
            </p>
            <UsageCostChart
              accounts={[own]}
              tokens={false}
              now={queryState.data?.generated_at ?? Date.now() / 1000}
              period={selected === 2}
            />
          </>
        ) : (
          <p className="text-xs text-muted-foreground">
            {focus ? "Not reported · No proven usage for this pool." : "No usage in this range."}
          </p>
        )}
      </section>
      <details className="rounded-[11px] border border-border bg-card px-3.5 text-sm">
        <summary className="cursor-pointer py-3 font-semibold">Cost breakdown</summary>
        {own?.models?.length ? (
          own.models.map((model) => (
            <div
              key={model.model}
              className="flex min-w-0 items-center gap-3 border-t border-border py-2 text-xs"
            >
              <span className="min-w-0 flex-1 break-all">{model.model}</span>
              <span className="shrink-0 tabular-nums">{usageFigure(model, false)}</span>
            </div>
          ))
        ) : (
          <p className="pb-3 text-xs text-muted-foreground">No model usage in this range.</p>
        )}
      </details>
    </>
  );
}

interface DetailAnalysis {
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
        pool.parent_account_id === account && pool.pool_id === poolId && pool.channel === "usage",
    )
    .map((pool) => pool.totals);
  if (!totals.length) return;
  const rows = data.rows.filter(
    (row) => row.parent_account_id === account && row.pool_id === poolId && row.channel === "usage",
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
    previous: null,
    mark: base.unpriced || data.partial || data.truncated ? "≥" : null,
    series: [...byHour].sort(([a], [b]) => a - b).map(([at, items]) => ({ at, ...project(items) })),
    models: [...byModel].map(([model, items]) => ({ model, ...project(items) })),
  };
}
