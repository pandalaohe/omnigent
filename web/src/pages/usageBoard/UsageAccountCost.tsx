import { useState } from "react";
import { Link, useSearchParams } from "@/lib/routing";
import { useQuery } from "@tanstack/react-query";
import { useUsageContextPreferences } from "@/hooks/useUsageContextPreferences";
import {
  useUsageAccountActivity,
  useUsageActivity,
  type UsageAccountActivity,
  type UsageTotals,
} from "./usageActivityData";
import type { AnalysisClasses, UsageAnalysisV2 } from "./usageAnalysisData";
import { USAGE_BOARD_POLL_MS } from "./usageBoardData";
import { usageRangeQuery } from "./UsageRangeControl";
import { usageJson } from "./usagePriceSourceData";
import { t } from "./usageText";
import type { UsageAccountView } from "./usageBoardData";
import { UsageCostChart } from "./UsageCostChart";
import { boardTotals, sumUsage, usageFigure } from "./usageAmountData";
import {
  formatFullDate,
  formatMoney,
  formatOverviewTokens,
  formatPercentReading,
} from "./usageBoardFormat";
import { UsageName } from "./UsageName";
import { FigureText } from "./UsageFigure";
import { UsageAccountMoney } from "./UsageAccountMoney";
import { usePricingBaseline, accountCostClass } from "./usagePricingData";
import { analysisLink, filterFromQuery, type PageFilter } from "./usageAnalysisFilters";
import { toDateInput } from "./usageBoardSelect";

export function LegacyUsageAccountCost({ account }: { account: UsageAccountView }) {
  const [selected, setSelected] = useState(1);
  const [search] = useSearchParams();
  const focus = search.get("focus");
  const today = useUsageAccountActivity(account.id, focus, true, { key: "today" });
  const week = useUsageAccountActivity(account.id, focus, true, { key: "d7" });
  const period = useUsageAccountActivity(account.id, focus, true, { key: "period" });
  const reads = [today, week, period];
  const amounts = reads.map((read) =>
    read.data?.accounts.find((item) => item.account === account.id),
  );
  const queryState = reads[selected];
  const own = amounts[selected];
  const error = queryState.error;
  const pricing = usePricingBaseline();
  if (pricing.data && accountCostClass(pricing.data, account.id) === "free") return null;
  return (
    <>
      <section aria-label="Cost" className="usage-card usage-detail-cost">
        <div className="usage-card-head">
          <h2 className="usage-card-title">Cost</h2>
          <span className="usage-caption">Reported cost</span>
        </div>
        {focus ? (
          <UsageName className="usage-caption">{`${focus} · Pool usage only`}</UsageName>
        ) : null}
        <div className="usage-detail-cells" role="group" aria-label="Cost time range">
          {["Today", "Last 7 days", "This period"].map((label, index) => {
            const read = reads[index];
            const amount = amounts[index];
            return (
              <button
                key={label}
                type="button"
                aria-label={
                  amount && !read.error
                    ? `${label} ${amount.mark ?? ""}${usageFigure(amount.totals, false)}`
                    : undefined
                }
                aria-pressed={selected === index}
                onClick={() => setSelected(index)}
              >
                <span>{label}</span>
                <b>
                  {read.error ? (
                    "Unavailable"
                  ) : amount ? (
                    <>
                      {amount.mark ? <span className="usage-approx">{amount.mark}</span> : null}
                      <FigureText text={usageFigure(amount.totals, false, true)} />
                    </>
                  ) : (
                    "—"
                  )}
                </b>
              </button>
            );
          })}
        </div>
        {error ? (
          <p role="alert" className="usage-caption text-destructive">
            Cost could not be read
          </p>
        ) : queryState.isPending ? (
          <p className="usage-caption">Reading cost…</p>
        ) : own ? (
          <>
            <p className="usage-caption">
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
          <p className="usage-caption">
            {focus ? "Not reported · No proven usage for this pool." : "No usage in this range."}
          </p>
        )}
        {!focus ? (
          <UsageAccountMoney
            accountId={account.id}
            range={{ key: selected === 0 ? "today" : selected === 1 ? "d7" : "period" }}
            compact
          />
        ) : null}
      </section>
      <details className="usage-card usage-fold">
        <summary>Cost breakdown</summary>
        {own?.models?.length ? (
          own.models.map((model) => (
            <div key={model.model} className="usage-row">
              <UsageName className="flex-1">{model.model}</UsageName>
              <FigureText className="usage-row-figure" text={usageFigure(model, false)} />
            </div>
          ))
        ) : (
          <p className="usage-caption">No model usage in this range.</p>
        )}
      </details>
    </>
  );
}

const overviewRanges = {
  today: "overviewToday",
  d7: "overviewWeek",
  d30: "overviewMonth",
  period: "overviewPeriod",
} as const;
type OverviewRange = keyof typeof overviewRanges;

export function useOverviewCostReads(account: string | undefined, enabled: boolean) {
  const today = useUsageActivity(account, enabled, { key: "today" });
  const d7 = useUsageActivity(account, enabled, { key: "d7" });
  const d30 = useUsageActivity(account, enabled, { key: "d30" });
  const period = useUsageActivity(account, enabled, { key: "period" });
  return { today, d7, d30, period };
}

export function overviewRange(range: string | null): OverviewRange {
  return range === "today" || range === "d30" || range === "period" ? range : "d7";
}

function overviewAnalysisRange(
  range: OverviewRange,
  amounts: UsageAccountActivity[],
): Pick<PageFilter, "range" | "from" | "to"> {
  if (range !== "period") return { range, from: null, to: null };
  const span = amounts[0]?.span;
  if (
    span &&
    amounts.every(
      (amount) =>
        amount.span.requested_from === span.requested_from && amount.span.until === span.until,
    )
  ) {
    const start = new Date(span.requested_from * 1000);
    const until = new Date(span.until * 1000);
    const from = toDateInput(start.getTime());
    const endDay = toDateInput(until.getTime());
    const endAtMidnight = until.getTime() === new Date(`${endDay}T00:00:00`).getTime();
    const to = endAtMidnight ? toDateInput(until.getTime() - 1) : endDay;
    if (
      (endAtMidnight || endDay === toDateInput(Date.now())) &&
      start.getTime() === new Date(`${from}T00:00:00`).getTime() &&
      span.requested_from === span.counted_from &&
      span.requested_from % 3600 === 0
    ) {
      const candidate = filterFromQuery(new URLSearchParams({ range: "custom", from, to }));
      if (candidate.range === "custom")
        return { range: candidate.range, from: candidate.from, to: candidate.to };
    }
  }
  return { range: "d30", from: null, to: null };
}

export function UsageAccountCost({
  account,
  accounts,
  reads,
}: {
  account?: UsageAccountView;
  accounts: UsageAccountView[];
  reads: ReturnType<typeof useOverviewCostReads>;
}) {
  const [search, setSearch] = useSearchParams();
  const board = useUsageContextPreferences().board;
  const unit = search.get("unit");
  const tokens = unit === "tokens" || (unit !== "money" && board.unit === "tokens");
  const selected = overviewRange(search.get("range"));
  const [expanded, setExpanded] = useState(false);
  const visibleIds = new Set(accounts.map(({ id }) => id));
  const visibleAmounts = (read: typeof reads.d7) =>
    read.data?.accounts.filter((item) => visibleIds.has(item.account)) ?? [];
  const displayedTotals = (data: UsageAccountActivity[]) => {
    const totals = data.map((amount) => amount.totals);
    return account ? sumUsage(totals) : boardTotals(totals, tokens);
  };
  const query = reads[selected];
  const amounts = visibleAmounts(query);
  const selectedTotals = displayedTotals(amounts);
  // Classes use calendar spans. Keep their model comparison in the same span.
  const compositionRange = selected === "period" ? "d30" : selected;
  const own = account && amounts.find((amount) => amount.account === account.id);
  const monthlyFee = account ? board.fees?.[account.id] : undefined;
  const fee =
    monthlyFee !== undefined && own
      ? (monthlyFee * (own.span.until - own.span.requested_from)) / (30 * 86400)
      : undefined;
  const drill = overviewAnalysisRange(selected, amounts);

  function pick(key: string, value: string) {
    const next = new URLSearchParams(search);
    next.set(key, value);
    setSearch(next);
  }

  return (
    <>
      <section className="usage-card usage-overview-cost" aria-label={t("overviewCost")}>
        <div className="usage-card-head">
          <h2 className="usage-card-title">{t("overviewCost")}</h2>
          <div
            className="usage-overview-units flex text-xs"
            role="group"
            aria-label={t("overviewUnit")}
          >
            <button type="button" aria-pressed={!tokens} onClick={() => pick("unit", "money")}>
              {t("overviewMoney")}
            </button>
            <button type="button" aria-pressed={tokens} onClick={() => pick("unit", "tokens")}>
              {t("overviewTokens")}
            </button>
          </div>
        </div>
        <div
          className="usage-detail-cells usage-overview-cells"
          role="group"
          aria-label={t("overviewRange")}
        >
          {(Object.keys(overviewRanges) as OverviewRange[]).map((key) => {
            const read = reads[key];
            const data = visibleAmounts(read);
            const totals = displayedTotals(data);
            return (
              <button
                key={key}
                type="button"
                className="usage-detail-cell"
                aria-pressed={selected === key}
                onClick={() => pick("range", key)}
              >
                <span>{t(overviewRanges[key])}</span>
                <b className="usage-overview-cell-value">
                  <FigureText
                    text={
                      tokens && totals.tokens !== null
                        ? formatOverviewTokens(totals.tokens)
                        : usageFigure(totals, tokens)
                    }
                  />
                </b>
                {!tokens &&
                data.some((amount) => {
                  const value = account ? amount.totals : (amount.totals.metered ?? amount.totals);
                  return amount.mark && (value.cost_usd !== null || value.unpriced > 0);
                }) ? (
                  <small>{t("overviewEstimate")}</small>
                ) : null}
              </button>
            );
          })}
        </div>
        {query.error ? (
          <p role="alert" className="usage-caption text-destructive">
            {t("overviewReadError")}
          </p>
        ) : query.isPending ? (
          <p className="usage-caption">{t("overviewReading")}</p>
        ) : (
          <UsageCostChart
            accounts={amounts}
            tokens={tokens}
            total={!account}
            period={selected === "period"}
            now={query.data?.generated_at ?? Date.now() / 1000}
            fee={fee}
            bucketUnit={selected === "today" ? "hour" : "day"}
            showCurrent={
              tokens
                ? selectedTotals.tokens !== null
                : selectedTotals.cost_usd !== null || selectedTotals.unpriced > 0
            }
          />
        )}
        <Link
          className="justify-self-end text-xs underline underline-offset-2"
          to={analysisLink(search, { ...drill, account: account ? [account.id] : [] })}
        >
          {selected === "period" && drill.range !== "custom"
            ? t("overviewAnalysisRange", { range: t(overviewRanges[compositionRange]) })
            : t("overviewAnalysis")}{" "}
          <span aria-hidden="true">›</span>
        </Link>
      </section>
      <details
        className="usage-card usage-fold"
        onToggle={(event) => setExpanded(event.currentTarget.open)}
      >
        <summary>{t("overviewComposition")}</summary>
        {expanded ? (
          <UsageCostComposition
            accountIds={account ? [account.id] : accounts.map(({ id }) => id)}
            range={compositionRange}
            amounts={visibleAmounts(reads[compositionRange])}
          />
        ) : null}
      </details>
    </>
  );
}

export function UsageOverviewHosts({
  accounts,
  account,
  range,
  query,
}: {
  accounts: UsageAccountView[];
  account?: string;
  range: OverviewRange;
  query: ReturnType<typeof useUsageActivity>;
}) {
  const [search] = useSearchParams();
  const hostNames = new Map(
    accounts.flatMap((item) => item.hosts.map((host) => [host.host_id, host.name])),
  );
  const visibleIds = new Set(accounts.map(({ id }) => id));
  const amounts = query.data?.accounts.filter((item) => visibleIds.has(item.account)) ?? [];
  const drill = overviewAnalysisRange(range, amounts);
  const hosts = new Map<string, number>();
  for (const amount of amounts) {
    for (const host of amount.hosts ?? []) {
      if (host.tokens !== null)
        hosts.set(host.host_id, (hosts.get(host.host_id) ?? 0) + host.tokens);
    }
  }
  const totalTokens = sumUsage(amounts.map((item) => item.totals)).tokens ?? 0;
  return (
    <section className="usage-card" aria-label={t("overviewHosts")}>
      <h2 className="usage-card-title">{t("overviewHosts")}</h2>
      {query.isPending ? (
        <p className="usage-caption">{t("overviewReading")}</p>
      ) : query.error ? (
        <p className="usage-caption">{t("overviewReadError")}</p>
      ) : hosts.size ? (
        <>
          {range === "period" && drill.range !== "custom" ? (
            <p className="usage-caption">
              {t("overviewAnalysisRange", { range: t(overviewRanges.d30) })}
            </p>
          ) : null}
          <ul>
            {[...hosts]
              .sort((a, b) => b[1] - a[1])
              .map(([id, count]) => {
                const percent = totalTokens ? (count / totalTokens) * 100 : 0;
                return (
                  <li key={id}>
                    <Link
                      className="usage-row"
                      to={analysisLink(search, {
                        ...drill,
                        account: account === undefined ? [] : [account],
                        host: [id],
                      })}
                    >
                      <UsageName className="flex-1">
                        {hostNames.get(id) ?? (id || t("overviewUnknown"))}
                      </UsageName>
                      <span className="w-20 shrink-0" aria-hidden="true">
                        <span
                          className="usage-overview-share"
                          style={{ width: `${Math.min(100, percent)}%` }}
                        />
                      </span>
                      <span className="tabular-nums">{formatPercentReading(percent)}%</span>
                    </Link>
                  </li>
                );
              })}
          </ul>
        </>
      ) : (
        <p className="usage-caption">{t("overviewNoHosts")}</p>
      )}
    </section>
  );
}

const classLabels = {
  input: "overviewClassInput",
  cache_read: "overviewClassCacheRead",
  cache_write: "overviewClassCacheWrite",
  output: "overviewClassOutput",
} as const;
const classColors = {
  input: "var(--usage-series)",
  cache_read: "var(--success)",
  cache_write: "var(--warning)",
  output: "var(--chart-4)",
};

function UsageCostComposition({
  accountIds,
  range,
  amounts,
}: {
  accountIds: string[];
  range: Exclude<OverviewRange, "period">;
  amounts: UsageAccountActivity[];
}) {
  const [tab, setTab] = useState<"class" | "model">("class");
  const params = usageRangeQuery({ key: range });
  params.set("group", "account");
  for (const id of [...accountIds].sort()) params.append("account", id);
  const query = useQuery({
    queryKey: ["usageBoard", "overviewClasses", params.toString()],
    queryFn: () => usageJson<UsageAnalysisV2>(`analysis?${params}`),
    enabled: tab === "class",
    staleTime: USAGE_BOARD_POLL_MS,
    retry: false,
  });
  return (
    <div className="space-y-2 pb-2">
      <div
        className="usage-overview-units flex text-xs"
        role="group"
        aria-label={t("overviewComposition")}
      >
        <button type="button" aria-pressed={tab === "class"} onClick={() => setTab("class")}>
          {t("overviewByClass")}
        </button>
        <button type="button" aria-pressed={tab === "model"} onClick={() => setTab("model")}>
          {t("overviewByModel")}
        </button>
      </div>
      <p className="usage-caption">
        {t("overviewCompositionRange", { range: t(overviewRanges[range]) })}
      </p>
      {tab === "model" ? (
        <OverviewModels amounts={amounts} />
      ) : query.error ? (
        <p role="alert" className="usage-caption text-destructive">
          {t("overviewReadError")}
        </p>
      ) : query.isPending ? (
        <p className="usage-caption">{t("overviewReading")}</p>
      ) : query.data.classes ? (
        <UsageClassComposition classes={query.data.classes} complete={query.data.complete} />
      ) : (
        <p className="usage-caption">{t("overviewUnknown")}</p>
      )}
    </div>
  );
}

export function UsageClassComposition({
  classes,
  complete,
}: {
  classes: AnalysisClasses;
  complete: boolean;
}) {
  const rows = classes.items.map((item) => ({
    label: t(classLabels[item.kind]),
    color: classColors[item.kind],
    tokens: Math.max(0, item.tokens - item.unpriced_tokens),
    low: Number(item.cost_low_usd),
    high: Number(item.cost_high_usd) as number | null,
  }));
  const unpriced = Object.values(classes.unpriced_tokens).reduce((sum, value) => sum + value, 0);
  const unknown = {
    label: t("overviewClassUnknown"),
    color: "var(--usage-ink-3)",
    tokens: unpriced + classes.unsplit_tokens,
    low: Number(classes.unsplit_cost.low),
    high: unpriced || classes.unsplit_cost.high === null ? null : Number(classes.unsplit_cost.high),
  };
  if (unknown.tokens || unknown.low || unknown.high) rows.push(unknown);
  const weights = rows.map((row) => row.high ?? row.low);
  const total = weights.reduce((sum, value) => sum + value, 0);
  return (
    <>
      <p className="usage-caption">{t("overviewClassBasis")}</p>
      {!complete ? <p className="usage-caption">{t("overviewIncomplete")}</p> : null}
      {total > 0 || rows.some((row) => row.tokens > 0) ? (
        <div className="usage-overview-stack" aria-hidden="true">
          {rows.map((row, index) => (
            <span
              key={row.label}
              data-usage-class={row.label}
              style={{
                background: row.color,
                flexGrow: total > 0 ? weights[index] : row.tokens,
                flexBasis: 0,
                minWidth: row.tokens > 0 || weights[index] > 0 ? 3 : 0,
              }}
            />
          ))}
        </div>
      ) : null}
      <ul>
        {rows.map((row) => (
          <li className="usage-row" key={row.label}>
            <span
              className="size-2 shrink-0 rounded-sm"
              style={{ background: row.color }}
              aria-hidden="true"
            />
            <span className="flex-1">{row.label}</span>
            <span>{formatOverviewTokens(row.tokens)} Token</span>
            <span className="tabular-nums">
              {row.high === null
                ? row.low
                  ? `≥${formatMoney(row.low)}`
                  : "—"
                : row.low === row.high
                  ? formatMoney(row.low)
                  : `${formatMoney(row.low)}–${formatMoney(row.high)}`}
            </span>
          </li>
        ))}
      </ul>
    </>
  );
}

function OverviewModels({ amounts }: { amounts: UsageAccountActivity[] }) {
  const models = new Map<string, UsageTotals[]>();
  for (const account of amounts) {
    for (const model of account.models ?? []) {
      const values = models.get(model.model) ?? [];
      values.push(model);
      models.set(model.model, values);
    }
  }
  const rows = [...models].map(([name, values]) => ({ name, totals: sumUsage(values) }));
  const costTotal = rows.reduce((sum, { totals }) => sum + (totals.cost_usd ?? 0), 0);
  if (!rows.length) return <p className="usage-caption">{t("overviewNoUsage")}</p>;
  return (
    <ul>
      {rows
        .sort((a, b) => (b.totals.cost_usd ?? 0) - (a.totals.cost_usd ?? 0))
        .map(({ name, totals }) => {
          const percent = costTotal ? ((totals.cost_usd ?? 0) / costTotal) * 100 : 0;
          return (
            <li className="usage-row" key={name}>
              <UsageName className="flex-1">{name}</UsageName>
              <span className="w-14 shrink-0" aria-label={`${formatPercentReading(percent)}%`}>
                <span className="usage-overview-share" style={{ width: `${percent}%` }} />
              </span>
              <span>
                {totals.per_million === null ? "—" : `${formatMoney(totals.per_million)}/M`}
              </span>
              <span>{usageFigure(totals, false)}</span>
            </li>
          );
        })}
    </ul>
  );
}
