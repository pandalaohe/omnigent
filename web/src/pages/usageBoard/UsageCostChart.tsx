import { useMemo } from "react";
import {
  analysisTotals,
  analysisValueText,
  type AnalysisPresentation,
  type AnalysisRow,
  type AnalysisValue,
  type UsageAnalysis,
} from "./usageAnalysisData";
import {
  Bar,
  CartesianGrid,
  ComposedChart,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { UsageAccountActivity } from "./usageActivityData";
import { formatCount, formatMoney, formatTileAmount, formatUsageValue } from "./usageBoardFormat";
import { UsageTipCard } from "./UsageTip";

export interface UsageChartPoint {
  at: number;
  now: number;
  previous?: number;
  bucket: number;
}

const EMPTY_PAID_SERIES: AnalysisPresentation["paid_series"] = [];
function AnalysisChartTooltip({
  active,
  payload,
  tokensUnknown,
}: {
  active?: boolean;
  payload?: readonly {
    payload?: { at: number; tokens?: number; previous?: number; payment?: AnalysisValue };
  }[];
  tokensUnknown: boolean;
}) {
  const point = payload?.[0]?.payload;
  return active && point ? (
    <div className="rounded-lg border border-border bg-popover p-2.5 shadow-md">
      <UsageTipCard
        model={{
          title: new Date(point.at * 1000).toLocaleString(),
          rows: [
            ...(!tokensUnknown ? [{ label: "Token", value: String(point.tokens) }] : []),
            ...(point.previous === undefined
              ? []
              : [{ label: "Previous", value: String(point.previous) }]),
            ...(point.payment ? [{ label: "Paid", value: analysisValueText(point.payment) }] : []),
          ],
        }}
      />
    </div>
  ) : null;
}

export function UsageAnalysisChart({
  rows,
  span,
  unknown,
  previousUnavailable,
  previousAccounts,
  previous,
  paidSeries = EMPTY_PAID_SERIES,
}: {
  rows: AnalysisRow[];
  span?: UsageAnalysis["span"];
  unknown: boolean;
  previousUnavailable: boolean;
  previousAccounts?: UsageAccountActivity[];
  previous?: AnalysisPresentation["previous"];
  paidSeries?: AnalysisPresentation["paid_series"];
}) {
  const prior = useMemo(() => {
    if (previous !== undefined) {
      if (!previous || analysisTotals(previous.rows, previous.partial).tokens === null || !span)
        return [];
      const offset = span.counted_from - previous.span.counted_from,
        buckets = new Map<number, number>();
      for (const row of previous.rows)
        if (row.contributes_usage && row.tokens !== null) {
          const at = Math.min(span.until, Math.max(span.counted_from, row.hour + offset));
          buckets.set(at, (buckets.get(at) ?? 0) + row.tokens);
        }
      let sum = 0;
      return [...buckets]
        .sort(([a], [b]) => a - b)
        .map(([at, tokens]) => ({ at, previous: (sum += tokens) }));
    }
    const current = new Map<string, number>();
    for (const row of rows) {
      if (!row.contributes_usage) continue;
      if (row.account_id !== row.parent_account_id || row.tokens === null) return [];
      current.set(row.parent_account_id, (current.get(row.parent_account_id) ?? 0) + row.tokens);
    }
    return previousAccounts?.length &&
      previousAccounts.length === current.size &&
      previousAccounts.every(
        (account) =>
          current.get(account.account) === account.totals.tokens &&
          account.totals.tokens_complete &&
          account.previous?.totals.tokens_complete &&
          account.previous.series.every((point) => point.tokens_complete),
      )
      ? usageChartPoints(previousAccounts, true, false, false, span?.until ?? 0)
      : [];
  }, [previous, previousAccounts, rows, span]);
  const points = useMemo(() => {
    const buckets = new Map<number, number>();
    for (const row of rows)
      if (row.contributes_usage && row.tokens !== null)
        buckets.set(row.hour, (buckets.get(row.hour) ?? 0) + row.tokens);
    const times = [
      ...new Set([
        ...buckets.keys(),
        ...prior.map((point) => point.at),
        ...paidSeries.map((point) => point.at),
        ...(span ? [span.counted_from, span.until] : []),
      ]),
    ].sort((a, b) => a - b);
    let total = 0,
      index = 0,
      paidIndex = 0;
    return times.map((at) => {
      total += buckets.get(at) ?? 0;
      while (index + 1 < prior.length && prior[index + 1].at <= at) index++;
      while (paidIndex + 1 < paidSeries.length && paidSeries[paidIndex + 1].at <= at) paidIndex++;
      const payment = paidSeries.length ? paidSeries[paidIndex]?.value : undefined;
      return {
        at,
        tokens: unknown ? undefined : total,
        previous: prior.length && prior[index].at <= at ? prior[index].previous : undefined,
        paid: payment?.low == null ? undefined : Number(payment.low),
        payment,
      };
    });
  }, [rows, span, prior, paidSeries, unknown]);
  const hasPaid = paidSeries.some((point) => point.value.low !== null);
  if (!span || (unknown && !hasPaid) || (!rows.length && !hasPaid))
    return (
      <div className="ua-unknown">
        <strong>Unknown</strong>
        <small>No complete selected token series</small>
      </div>
    );
  const currency = paidSeries.find((point) => point.value.currency)?.value.currency ?? "USD";
  return (
    <figure aria-label="Cumulative selected tokens and paid">
      <div style={{ height: 190, width: "100%", minWidth: 0 }}>
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={points} margin={{ top: 8, right: 10, left: 0, bottom: 0 }}>
            <XAxis
              dataKey="at"
              type="number"
              domain={[span.counted_from, span.until]}
              tickFormatter={(at) =>
                new Date(at * 1000).toLocaleDateString("en-US", { month: "short", day: "numeric" })
              }
              tick={{ fontSize: 11 }}
              tickLine={false}
              axisLine={false}
            />
            <YAxis
              yAxisId="tokens"
              width={40}
              tickFormatter={(value) =>
                new Intl.NumberFormat("en-US", {
                  notation: "compact",
                  maximumFractionDigits: 2,
                }).format(value)
              }
              tick={{ fontSize: 11 }}
              tickLine={false}
              axisLine={false}
            />
            {hasPaid ? (
              <YAxis
                yAxisId="paid"
                orientation="right"
                width={60}
                tickFormatter={(value) => formatUsageValue(value, currency)}
                tick={{ fontSize: 10 }}
                tickLine={false}
                axisLine={false}
              />
            ) : null}
            <Tooltip content={<AnalysisChartTooltip tokensUnknown={unknown} />} />
            {!unknown ? (
              <Line
                yAxisId="tokens"
                dataKey="tokens"
                name="Token"
                type="linear"
                dot={false}
                stroke="var(--info)"
                strokeWidth={2}
                isAnimationActive={false}
              />
            ) : null}
            {prior.length ? (
              <Line
                yAxisId="tokens"
                dataKey="previous"
                name="Previous"
                type="linear"
                dot={false}
                stroke="var(--muted-foreground)"
                strokeDasharray="4 4"
                strokeWidth={1.25}
                isAnimationActive={false}
              />
            ) : null}
            {hasPaid ? (
              <Line
                yAxisId="paid"
                dataKey="paid"
                name="Paid"
                type="linear"
                dot={false}
                stroke="var(--success)"
                strokeWidth={2}
                isAnimationActive={false}
              />
            ) : null}
          </LineChart>
        </ResponsiveContainer>
      </div>
      <figcaption className="text-xs text-muted-foreground">
        Token · {hasPaid ? "Paid (right axis) · " : ""}At API price: Not available yet
      </figcaption>
      {previousUnavailable && !prior.length ? (
        <figcaption className="text-xs text-muted-foreground">
          Previous selected hourly series not available yet
        </figcaption>
      ) : null}
    </figure>
  );
}

/** Rebucket after aligning each account's Previous to its own current start. */
export function usageChartPoints(
  accounts: UsageAccountActivity[],
  tokens: boolean,
  total: boolean,
  period: boolean,
  now: number,
): UsageChartPoint[] {
  if (
    !accounts.length ||
    accounts.every((account) => !account.series.length && !account.previous?.series.length)
  )
    return [];
  const from =
    period && total
      ? now - 30 * 86400
      : Math.min(...accounts.map((account) => account.span.counted_from));
  const until = Math.max(...accounts.map((account) => account.span.until));
  const length = until - from;
  const hourly = length <= 48 * 3600;
  const weekly = length > 45 * 86400;
  function bucketAt(at: number) {
    if (hourly) return Math.floor(at / 3600) * 3600;
    const date = new Date(at * 1000);
    date.setHours(0, 0, 0, 0);
    if (weekly) date.setDate(date.getDate() - ((date.getDay() + 6) % 7));
    return date.getTime() / 1000;
  }
  const buckets = new Map<number, { now: number; previous: number }>();
  for (let at = bucketAt(from); at < until;) {
    buckets.set(at, { now: 0, previous: 0 });
    if (hourly) at += 3600;
    else {
      const date = new Date(at * 1000);
      date.setDate(date.getDate() + (weekly ? 7 : 1));
      at = date.getTime() / 1000;
    }
  }
  const firstBucket = buckets.keys().next().value;
  const lastBucket = [...buckets.keys()].at(-1);
  if (firstBucket === undefined || lastBucket === undefined) return [];
  const opening = { now: 0, previous: 0 };
  for (const account of accounts) {
    for (const kind of ["now", "previous"] as const) {
      const amount = kind === "now" ? account : account.previous;
      if (!amount) continue;
      const offset = kind === "previous" ? account.span.counted_from - amount.span.counted_from : 0;
      for (const point of amount.series) {
        // A partial day's server bucket names midnight, before its accounting
        // cutoff. Align the counted part so Previous cannot lose that first day.
        const at = Math.max(point.at, amount.span.counted_from) + offset;
        const value = total && !tokens ? (point.metered ?? point) : point;
        const figure = (tokens ? value.tokens : value.cost_usd) ?? 0;
        const key = bucketAt(at);
        if (key < firstBucket) {
          // The axis is bounded; earlier in-window spend still opens the line.
          opening[kind] += figure;
        } else {
          // A calendar Previous can be longer across DST. Keep its tail in
          // the last comparison bucket instead of discarding counted usage.
          const bucket = buckets.get(Math.min(key, lastBucket));
          if (bucket) bucket[kind] += figure;
        }
      }
    }
  }
  let current = opening.now,
    previous = opening.previous;
  const hasPrevious = accounts.some((account) => account.previous !== null);
  return [...buckets].map(([at, value]) => {
    current += value.now;
    previous += value.previous;
    return { at, now: current, previous: hasPrevious ? previous : undefined, bucket: value.now };
  });
}

function ChartTip({
  active,
  payload,
  tokens,
}: {
  active?: boolean;
  payload?: readonly { payload?: UsageChartPoint }[];
  tokens: boolean;
}) {
  const point = payload?.[0]?.payload;
  if (!active || !point) return null;
  const figure = (value: number) => (tokens ? formatCount(value) : formatMoney(value));
  return (
    <div className="rounded-lg border border-border bg-popover p-2.5 shadow-md">
      <UsageTipCard
        model={{
          title: new Date(point.at * 1000).toLocaleString("en-US", {
            month: "short",
            day: "numeric",
            hour: "2-digit",
            minute: "2-digit",
          }),
          rows: [
            { label: "Now", value: figure(point.now) },
            ...(point.previous === undefined
              ? []
              : [{ label: "Previous", value: figure(point.previous) }]),
            { label: "This bucket", value: figure(point.bucket) },
          ],
          dividerBefore: 2,
        }}
      />
    </div>
  );
}

/** Round axis steps (1, 2, 2.5 or 5 × 10ⁿ) from zero past the series' top. */
export function niceTicks(top: number, count = 4, minStep = 0): number[] {
  if (!(top > 0)) return [0, Math.max(1, minStep)];
  const raw = Math.max(top / count, minStep);
  const power = 10 ** Math.floor(Math.log10(raw));
  const step = ([1, 2, 2.5, 5, 10].find((factor) => factor * power >= raw) ?? 10) * power;
  const ticks: number[] = [];
  for (let value = 0; value < top + step * 0.001; value += step) ticks.push(value);
  if (ticks[ticks.length - 1] < top) ticks.push(ticks[ticks.length - 1] + step);
  return ticks.map((value) => Number(value.toPrecision(12)));
}

function LegendSwatch({ kind }: { kind: "now" | "previous" | "bucket" | "fee" }) {
  return <span className="usage-legend-swatch" data-kind={kind} aria-hidden="true" />;
}

export function UsageCostChart({
  accounts,
  tokens,
  total = false,
  period = false,
  now,
  fee,
}: {
  accounts: UsageAccountActivity[];
  tokens: boolean;
  total?: boolean;
  period?: boolean;
  now: number;
  fee?: number;
}) {
  const points = usageChartPoints(accounts, tokens, total, period, now);
  const last = points.at(-1);
  const formatter = (value: number) => (tokens ? formatCount(value) : formatMoney(value));
  // Axis ticks are scale labels: two-decimal steps never repeat, large ones shorten.
  const tickText = (value: number) =>
    tokens ? formatCount(value) : formatTileAmount(value, "USD");
  const hourly = points.length > 1 && points[1].at - points[0].at <= 3600;
  const dateLabel = (at: number) =>
    new Date(at * 1000).toLocaleString(
      "en-US",
      hourly ? { hour: "numeric" } : { month: "short", day: "numeric" },
    );
  if (!points.length) return <p className="usage-chart-empty">No usage recorded in this range.</p>;
  const showFee = !tokens && fee !== undefined && fee <= 4 * (last?.now ?? 0);
  const top = Math.max(
    ...points.map((point) => Math.max(point.now, point.previous ?? 0)),
    showFee ? (fee ?? 0) : 0,
  );
  // Bars and lines share this one axis, so a bar's height is its real amount.
  const ticks = niceTicks(top, 4, tokens ? 1 : 0.01);
  const axisWidth = Math.max(44, Math.max(...ticks.map((tick) => tickText(tick).length)) * 7 + 18);
  return (
    <figure aria-label="Cumulative usage and usage per bucket" className="usage-chart">
      <figcaption className="usage-legend">
        <span>
          <LegendSwatch kind="now" />
          Now <b>{formatter(last?.now ?? 0)}</b>
        </span>
        {last?.previous !== undefined ? (
          <span>
            <LegendSwatch kind="previous" />
            Previous <b>{formatter(last.previous)}</b>
          </span>
        ) : null}
        <span>
          <LegendSwatch kind="bucket" />
          {hourly ? "Per hour" : "Per day"}
        </span>
        {!tokens && fee !== undefined ? (
          <span>
            <LegendSwatch kind="fee" />
            Monthly fee <b>{formatMoney(fee)}</b>
          </span>
        ) : null}
      </figcaption>
      <div className="usage-chart-plot">
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart data={points} margin={{ top: 6, right: 4, left: 0, bottom: 0 }}>
            <CartesianGrid
              yAxisId="total"
              vertical={false}
              stroke="var(--border)"
              strokeDasharray="2 4"
            />
            <XAxis
              dataKey="at"
              tickFormatter={dateLabel}
              tick={{ fontSize: 12, fill: "var(--muted-foreground)" }}
              tickLine={false}
              axisLine={false}
              interval="preserveStartEnd"
              minTickGap={28}
              tickMargin={6}
            />
            <YAxis
              yAxisId="total"
              width={axisWidth}
              ticks={ticks}
              domain={[0, ticks[ticks.length - 1]]}
              tickFormatter={tickText}
              tick={{ fontSize: 12, fill: "var(--muted-foreground)" }}
              tickLine={false}
              axisLine={false}
            />
            <Tooltip content={<ChartTip tokens={tokens} />} cursor={{ fill: "var(--accent)" }} />
            <Bar
              yAxisId="total"
              dataKey="bucket"
              fill="var(--muted-foreground)"
              fillOpacity={0.8}
              radius={[2, 2, 0, 0]}
              maxBarSize={28}
              isAnimationActive={false}
            />
            <Line
              yAxisId="total"
              type="stepAfter"
              dataKey="previous"
              stroke="var(--muted-foreground)"
              strokeDasharray="4 4"
              strokeWidth={1.25}
              dot={false}
              isAnimationActive={false}
            />
            <Line
              yAxisId="total"
              type="stepAfter"
              dataKey="now"
              stroke="var(--usage-series)"
              strokeWidth={2}
              dot={false}
              isAnimationActive={false}
            />
            {showFee ? (
              <ReferenceLine
                yAxisId="total"
                y={fee}
                stroke="var(--warning)"
                strokeDasharray="3 3"
              />
            ) : null}
          </ComposedChart>
        </ResponsiveContainer>
      </div>
    </figure>
  );
}
