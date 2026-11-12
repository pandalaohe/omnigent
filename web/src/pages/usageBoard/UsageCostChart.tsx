import { useId, useMemo } from "react";
import type { AnalysisRow, UsageAnalysis } from "./usageAnalysisData";
import {
  Bar,
  BarChart,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { UsageAccountActivity } from "./usageActivityData";
import { formatMoney } from "./usageBoardFormat";
import { UsageTipCard } from "./UsageTip";

export interface UsageChartPoint {
  at: number;
  now: number;
  previous?: number;
  bucket: number;
}

export function UsageAnalysisChart({
  rows,
  span,
  unknown,
  previousUnavailable,
  previousAccounts,
}: {
  rows: AnalysisRow[];
  span?: UsageAnalysis["span"];
  unknown: boolean;
  previousUnavailable: boolean;
  previousAccounts?: UsageAccountActivity[];
}) {
  const prior = useMemo(
    () =>
      previousAccounts?.length &&
      previousAccounts.every(
        (account) =>
          account.previous?.totals.tokens_complete &&
          account.previous.series.every((point) => point.tokens_complete),
      )
        ? usageChartPoints(previousAccounts, true, false, false, span?.until ?? 0)
        : [],
    [previousAccounts, span?.until],
  );
  const points = useMemo(() => {
    const buckets = new Map<number, number>();
    for (const row of rows)
      if (row.contributes_usage && row.tokens !== null)
        buckets.set(row.hour, (buckets.get(row.hour) ?? 0) + row.tokens);
    let sum = 0;
    const current = [...buckets]
      .sort(([a], [b]) => a - b)
      .map(([at, tokens]) => ({ at, tokens: (sum += tokens) }));
    let index = 0;
    return current.map((point) => {
      while (index + 1 < prior.length && prior[index + 1].at <= point.at) index++;
      return { ...point, previous: prior.length ? prior[index]?.previous : undefined };
    });
  }, [rows, prior]);
  if (unknown || !span || !points.length)
    return (
      <div className="ua-unknown">
        <strong>Unknown</strong>
        <small>No complete selected token series</small>
      </div>
    );
  return (
    <figure aria-label="Cumulative selected tokens">
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
            <Tooltip
              labelFormatter={(at) => new Date(Number(at) * 1000).toLocaleString()}
              formatter={(value) =>
                new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 }).format(Number(value))
              }
            />
            <Line
              dataKey="tokens"
              name="Token"
              type="linear"
              dot={false}
              stroke="var(--info)"
              strokeWidth={2}
              isAnimationActive={false}
            />
            {prior.length ? (
              <Line
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
          </LineChart>
        </ResponsiveContainer>
      </div>
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
  const figure = (value: number) =>
    tokens
      ? new Intl.NumberFormat("en-US", { notation: "compact" }).format(value)
      : formatMoney(value);
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
  const syncId = useId();
  const points = usageChartPoints(accounts, tokens, total, period, now);
  const last = points.at(-1);
  const formatter = (value: number) =>
    tokens
      ? new Intl.NumberFormat("en-US", { notation: "compact" }).format(value)
      : formatMoney(value);
  const dateLabel = (at: number) =>
    new Date(at * 1000).toLocaleString(
      "en-US",
      points.length > 1 && points[1].at - points[0].at <= 3600
        ? { hour: "numeric" }
        : { month: "short", day: "numeric" },
    );
  if (!points.length)
    return (
      <p className="py-10 text-center text-sm text-muted-foreground">
        No usage recorded in this range.
      </p>
    );
  return (
    <figure aria-label="Cumulative usage and usage per bucket" className="min-w-0">
      <figcaption className="mb-1 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
        <span className="text-info">Now {formatter(last?.now ?? 0)}</span>
        {last?.previous !== undefined ? <span>Previous {formatter(last.previous)}</span> : null}
        {!tokens && fee !== undefined ? <span>Monthly fee {formatMoney(fee)}</span> : null}
      </figcaption>
      <div className="h-40 w-full min-w-0">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart
            data={points}
            syncId={syncId}
            margin={{ top: 8, right: 10, left: 0, bottom: 0 }}
          >
            <XAxis dataKey="at" hide />
            <YAxis
              width={52}
              tickFormatter={formatter}
              tick={{ fontSize: 11 }}
              tickLine={false}
              axisLine={false}
            />
            <Tooltip content={<ChartTip tokens={tokens} />} />
            <Line
              type="stepAfter"
              dataKey="now"
              stroke="var(--info)"
              dot={false}
              strokeWidth={2}
              isAnimationActive={false}
            />
            <Line
              type="stepAfter"
              dataKey="previous"
              stroke="var(--muted-foreground)"
              strokeDasharray="4 4"
              dot={false}
              isAnimationActive={false}
            />
            {!tokens && fee !== undefined && fee <= 4 * (last?.now ?? 0) ? (
              <ReferenceLine
                y={fee}
                stroke="var(--warning)"
                strokeDasharray="3 3"
                ifOverflow="extendDomain"
              />
            ) : null}
          </LineChart>
        </ResponsiveContainer>
      </div>
      <div className="h-20 w-full min-w-0">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart
            data={points}
            syncId={syncId}
            margin={{ top: 4, right: 10, left: 0, bottom: 0 }}
          >
            <XAxis
              dataKey="at"
              tickFormatter={dateLabel}
              tick={{ fontSize: 10 }}
              tickLine={false}
              axisLine={false}
              interval="preserveStartEnd"
              minTickGap={24}
            />
            <YAxis
              width={52}
              tickFormatter={formatter}
              tick={{ fontSize: 10 }}
              tickLine={false}
              axisLine={false}
              tickCount={2}
            />
            <Tooltip content={<ChartTip tokens={tokens} />} cursor={{ fill: "var(--accent)" }} />
            <Bar
              dataKey="bucket"
              fill="var(--info)"
              opacity={0.35}
              radius={[2, 2, 0, 0]}
              isAnimationActive={false}
            />
          </BarChart>
        </ResponsiveContainer>
      </div>
    </figure>
  );
}
