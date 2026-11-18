import { useMemo, useState, type CSSProperties } from "react";
import { AnalysisPanel, AnalysisSection, AnalysisUnknown } from "./UsageAnalysisPanel";
import { UsageTip } from "./UsageTip";
import { UsageName } from "./UsageName";
import { UsageClassComposition } from "./UsageAccountCost";
import {
  formatAge,
  formatOverviewTokens,
  formatRingPercent,
  formatUsageValue,
} from "./usageBoardFormat";
import {
  Line,
  Bar,
  ComposedChart,
  CartesianGrid,
  ResponsiveContainer,
  XAxis,
  YAxis,
  Tooltip,
} from "recharts";
import { analysisSections } from "./UsageAnalysisRail";
import type { PageFilter } from "./usageAnalysisFilters";
import type { UsageHostView } from "./usageBoardData";
import {
  analysisTotals,
  analysisValueText,
  useAnalysisVisible,
  useUsageAnalysis,
  useUsageAnalysisV2,
  type AnalysisCard,
  type AnalysisPresentation,
  type AnalysisRow,
  type AnalysisValue,
  type UsageAnalysisV2,
} from "./usageAnalysisData";

export function AnalysisTrend({ rows }: { rows: AnalysisRow[] }) {
  const points = new Map<number, number>();
  for (const row of rows)
    if (row.contributes_usage && row.tokens !== null)
      points.set(row.hour, (points.get(row.hour) ?? 0) + row.tokens);
  const ordered = [...points].sort(([a], [b]) => a - b);
  const max = Math.max(1, ...ordered.map(([, value]) => value));
  if (!ordered.length || analysisTotals(rows).tokens === null)
    return <span data-unknown="">Unknown</span>;
  return (
    <svg className="ua-spark" viewBox="0 0 88 22" role="img" aria-label="Token trend">
      <path
        d={ordered
          .map(
            ([, value], index) =>
              `${index ? "L" : "M"}${ordered.length > 1 ? 1 + (86 * index) / (ordered.length - 1) : 44} ${20 - (18 * value) / max}`,
          )
          .join(" ")}
      />
    </svg>
  );
}
export function compareAnalysisDecimals(a: string, b: string) {
  const digits = Math.max(a.split(".")[1]?.length ?? 0, b.split(".")[1]?.length ?? 0);
  const integer = (value: string) => {
    const [whole, fraction = ""] = value.split(".");
    return BigInt(`${whole}${fraction.padEnd(digits, "0")}`);
  };
  const left = integer(a),
    right = integer(b);
  return left < right ? -1 : left > right ? 1 : 0;
}
export function AnalysisComparisons({
  data,
  ninety,
}: {
  data?: AnalysisPresentation | null;
  ninety: boolean;
}) {
  const reason = ninety
    ? "90 days has no previous period"
    : data?.comparison.reason
      ? "Missing compatible previous usage or payment"
      : "No compatible previous usage or payment";
  return (
    <AnalysisPanel title="Compared with previous" caption="Aggregate unit cost">
      <div className="ua-comparisons">
        {[
          ["Same usage", data?.comparison.same_usage_difference, "money"],
          ["Same spend", data?.comparison.same_spend_percent, "percent"],
        ].map(([label, value, unit]) => (
          <div key={String(label)}>
            <span>{String(label)}</span>
            {value && !ninety ? (
              <div className="ua-unknown">
                <strong
                  data-unknown={
                    analysisValueText(value as AnalysisValue, unit as "money" | "percent") ===
                      "Unknown" || undefined
                  }
                >
                  {analysisValueText(value as AnalysisValue, unit as "money" | "percent") ===
                  "Unknown"
                    ? "Unknown"
                    : `${label === "Same usage" ? ((value as AnalysisValue).low?.startsWith("-") ? "More spent" : "Less spent") : (value as AnalysisValue).low?.startsWith("-") ? "Less used" : "More used"} ${analysisValueText(value as AnalysisValue, unit as "money" | "percent").replace(/^-/, "")}`}
                </strong>
                <small>
                  {label === "Same usage"
                    ? `At previous unit cost: ${analysisValueText(data?.comparison.same_usage)}`
                    : `Token difference: ${analysisValueText(data?.comparison.same_spend_difference, "number")}`}
                </small>
              </div>
            ) : (
              <AnalysisUnknown reason={reason} />
            )}
          </div>
        ))}
      </div>
    </AnalysisPanel>
  );
}
export function AnalysisCards({
  data,
  sort,
  onSort,
}: {
  data?: AnalysisPresentation | null;
  sort: { key: string; direction: number };
  onSort: (key: "paid" | "quota" | "per") => void;
}) {
  const cards = [...(data?.cards ?? [])];
  const field = (card: AnalysisCard) =>
    sort.key === "quota" ? card.allowance : sort.key === "per" ? card.paid_per_quota : card.paid;
  cards.sort((a, b) => {
    const av = field(a)?.low,
      bv = field(b)?.low;
    return av == null
      ? bv == null
        ? 0
        : 1
      : bv == null
        ? -1
        : compareAnalysisDecimals(av, bv) * sort.direction;
  });
  return (
    <>
      <div className="ua-kpis">
        {[
          ["Paid", data?.projection.paid, "money"],
          ["Allowance", data?.allowance, "quota"],
          ["Paid per USD 1.00 allowance", data?.paid_per_quota, "money"],
          ["At API price", null, "money"],
        ].map(([label, value, unit]) => (
          <div className="ua-kpi" key={String(label)}>
            <span>{String(label)}</span>
            <strong
              data-unknown={
                analysisValueText(value as AnalysisValue, unit as "money" | "quota") ===
                  "Unknown" || undefined
              }
            >
              {analysisValueText(value as AnalysisValue, unit as "money" | "quota")}
            </strong>
            <small>
              {label === "At API price"
                ? "Per-card usage is not attributable"
                : "Purchased reset intervals · configured FX"}
            </small>
          </div>
        ))}
      </div>
      <AnalysisPanel title="Each item" caption="Cards · payments and allowance only">
        <div className="ua-card-table" role="table" aria-label="Purchased cards">
          <div role="row" className="ua-card-head">
            <span role="columnheader">Card</span>
            {(
              [
                ["paid", "Paid"],
                ["quota", "Allowance"],
                ["per", "Paid per USD 1.00"],
              ] as const
            ).map(([key, label]) => (
              <span
                key={key}
                role="columnheader"
                aria-sort={
                  sort.key === key ? (sort.direction < 0 ? "descending" : "ascending") : "none"
                }
              >
                <button type="button" onClick={() => onSort(key)}>
                  {label}
                  {sort.key === key ? (sort.direction < 0 ? " ↓" : " ↑") : ""}
                </button>
              </span>
            ))}
          </div>
          {cards.map((card) => (
            <div className="ua-card-row" role="row" key={card.id}>
              <span role="cell">
                <UsageName className="font-bold">{card.name}</UsageName>
                <small>
                  {card.original_paid ? `Original ${analysisValueText(card.original_paid)} · ` : ""}
                  {card.reset_count} resets · {new Date(card.starts_at * 1000).toLocaleDateString()}
                  –{new Date(card.ends_at * 1000).toLocaleDateString()}
                </small>
              </span>
              {[
                ["Paid", card.paid, "money"],
                ["Allowance", card.allowance, "quota"],
                ["Paid per USD 1.00", card.paid_per_quota, "money"],
              ].map(([label, value, unit]) => (
                <span
                  role="cell"
                  data-label={String(label)}
                  key={String(label)}
                  data-unknown={
                    analysisValueText(value as AnalysisValue, unit as "money" | "quota") ===
                      "Unknown" || undefined
                  }
                >
                  {analysisValueText(value as AnalysisValue, unit as "money" | "quota")}
                </span>
              ))}
            </div>
          ))}
          {!cards.length ? <AnalysisUnknown reason="No selected purchases in this range" /> : null}
        </div>
      </AnalysisPanel>
    </>
  );
}
export function AnalysisPayback({
  items,
  embedded = false,
}: {
  items: { key: string; name: string; projection?: { payback: AnalysisValue } }[];
  embedded?: boolean;
}) {
  const content = (
    <>
      <div className="ua-bars">
        {items.map((item) => {
          const value = item.projection?.payback;
          const number = value?.low == null ? null : Number(value.low);
          const position =
            number && number > 0
              ? Math.max(0, Math.min(100, ((Math.log10(number) + 2) / 4) * 100))
              : null;
          return (
            <UsageTip
              key={item.key}
              model={{
                title: item.name,
                rows: [
                  { label: "Payback", value: analysisValueText(value, "ratio") },
                  {
                    label: "Paid cohort",
                    value: value?.coverage
                      ? `${value.coverage.known_paid}/${value.coverage.total_items}`
                      : "Unknown",
                  },
                ],
              }}
              tapOpens
            >
              <button
                type="button"
                className="ua-bar ua-payback"
                data-tone={number === null ? undefined : number >= 1 ? "ok" : "warn"}
              >
                <UsageName>{item.name}</UsageName>
                <span className="ua-track">
                  <s style={{ left: "50%" }} />
                  {position === null ? null : <i style={{ left: `${position}%` }} />}
                </span>
                <b data-unknown={analysisValueText(value, "ratio") === "Unknown" || undefined}>
                  {analysisValueText(value, "ratio")}
                </b>
              </button>
            </UsageTip>
          );
        })}
      </div>
      <small className="text-xs text-muted-foreground">
        0.01× — 1× — 100× · unknown stays unknown
      </small>
    </>
  );
  return embedded ? (
    content
  ) : (
    <AnalysisPanel title="Subscription payback" caption="Matching known-paid cohort">
      {content}
    </AnalysisPanel>
  );
}

export function V2Payback({
  data,
  filter,
  names,
}: {
  data: UsageAnalysisV2;
  filter: PageFilter;
  names: Map<string, string>;
}) {
  const useMain = filter.group === "account";
  const query = useUsageAnalysisV2(
    { ...filter, group: "account" },
    { enabled: !useMain, previous: false, poll: false },
  );
  const result = useMain ? data : query.data;
  if (!useMain && query.error)
    return (
      <p className="usage-caption" role="alert">
        {query.error.message}
      </p>
    );
  if (!result) return <p className="usage-caption">Reading subscription payback…</p>;
  const items = result.groups.flatMap(({ key, payback }) =>
    typeof key === "string" && payback && (!filter.account.length || filter.account.includes(key))
      ? [{ key, name: names.get(key) ?? key, projection: { payback } }]
      : [],
  );
  return result.complete && items.length ? (
    <AnalysisPayback items={items} embedded />
  ) : (
    <AnalysisUnknown reason="No complete payback reading for the selected accounts" />
  );
}
export function AnalysisClosings({
  data,
  names,
  model,
  embedded = false,
}: {
  data?: AnalysisPresentation | null;
  names: Map<string, string>;
  model: boolean;
  embedded?: boolean;
}) {
  const readings = new Map<string, NonNullable<AnalysisPresentation["quota_closings"]>[number]>();
  for (const closing of data?.quota_closings ?? []) {
    const key = JSON.stringify([closing.account_id, closing.window_id]);
    const old = readings.get(key);
    if (!old || closing.resets_at > old.resets_at) readings.set(key, closing);
  }
  const content = readings.size ? (
    <div className="ua-bars">
      {[...readings].map(([key, reading]) => (
        <UsageTip
          key={key}
          model={{
            title: names.get(reading.account_id) ?? reading.account_id,
            rows: [
              {
                label: "Observed",
                value: new Date(reading.observed_at * 1000).toLocaleString(),
              },
              { label: "Reset", value: new Date(reading.resets_at * 1000).toLocaleString() },
              { label: "Window", value: reading.window_id },
            ],
          }}
          tapOpens
        >
          <button type="button" className="ua-closing">
            <UsageName>{names.get(reading.account_id) ?? reading.account_id}</UsageName>
            <b>
              {reading.remaining.state === "unknown"
                ? "Unknown"
                : reading.remaining.formatted?.high
                  ? `≤${formatRingPercent(reading.remaining.formatted.high)}%`
                  : "Unknown"}
            </b>
            <small>
              {new Date(reading.observed_at * 1000).toLocaleString(undefined, {
                month: "2-digit",
                day: "2-digit",
                hour: "2-digit",
                minute: "2-digit",
              })}
            </small>
          </button>
        </UsageTip>
      ))}
    </div>
  ) : (
    <AnalysisUnknown
      reason={
        model
          ? "Quota cannot be attributed by model"
          : "No eligible closing observation in this range"
      }
    />
  );
  return embedded ? (
    content
  ) : (
    <AnalysisPanel title="Unused last period" caption="Measured closing observation">
      {content}
    </AnalysisPanel>
  );
}

export function V2QuotaClosings({
  filter,
  names,
}: {
  filter: PageFilter;
  names: Map<string, string>;
}) {
  const visible = useAnalysisVisible();
  const keys = filter.account.length ? filter.account : [...names.keys()].sort();
  const range = filter.day
    ? { key: "custom" as const, from: filter.day, to: filter.day }
    : filter.range === "custom"
      ? { key: filter.range, from: filter.from ?? undefined, to: filter.to ?? undefined }
      : { key: filter.range };
  const query = useUsageAnalysis(
    range,
    {
      dimension: "account",
      keys,
      hidden: [],
      enabled: visible && keys.length > 0 && keys.length <= 256,
    },
    false,
  );
  if (query.error)
    return (
      <p className="usage-caption" role="alert">
        {query.error.message}
      </p>
    );
  if (!keys.length || keys.length > 256)
    return <AnalysisUnknown reason="No bounded account selection for closing observations" />;
  if (query.isPending) return <p className="usage-caption">Reading closing observations…</p>;
  return <AnalysisClosings data={query.data?.presentation} names={names} model={false} embedded />;
}
export function AnalysisHeatmap({
  rows,
  partial,
  timezone,
}: {
  rows: AnalysisRow[];
  partial: boolean;
  timezone: string;
}) {
  const cells = useMemo(() => {
    const buckets = Array<number>(168).fill(0);
    const format = new Intl.DateTimeFormat("en-US", {
      weekday: "short",
      hour: "numeric",
      hourCycle: "h23",
      timeZone: timezone,
    });
    for (const row of rows) {
      if (!row.contributes_usage || row.tokens === null) continue;
      const parts = format.formatToParts(new Date(row.hour * 1000));
      const day = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].indexOf(
        parts.find((part) => part.type === "weekday")?.value ?? "",
      );
      const hour = Number(parts.find((part) => part.type === "hour")?.value ?? 0);
      if (day >= 0) buckets[day * 24 + hour] += row.tokens;
    }
    return buckets;
  }, [rows, timezone]);
  const days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
    max = Math.max(1, ...cells);
  return (
    <AnalysisPanel title="When usage is highest" caption={`${timezone} · tokens`}>
      {partial || analysisTotals(rows).tokens === null ? (
        <AnalysisUnknown reason="Incomplete selected token population" />
      ) : (
        <div className="ua-heat" role="group" aria-label="Selected weekday and hour tokens">
          {days.map((day, d) => (
            <div key={day}>
              <span>{day}</span>
              <div>
                {Array.from({ length: 24 }, (_, hour) => hour).map((h) => {
                  const tokens = cells[d * 24 + h];
                  return (
                    <UsageTip
                      key={h}
                      className="ua-heat-cell"
                      model={{
                        title: `${day} ${h}:00 · ${timezone}`,
                        rows: [{ label: "Token", value: tokens.toLocaleString("en-US") }],
                      }}
                      tapOpens
                    >
                      <button
                        type="button"
                        aria-label={`${day} ${h}:00 ${tokens} tokens`}
                        style={{
                          background: `color-mix(in srgb, var(--usage-time) ${Math.max(6, (tokens / max) * 100)}%, var(--usage-track))`,
                        }}
                      />
                    </UsageTip>
                  );
                })}
              </div>
            </div>
          ))}
          <footer>
            <span>0</span>
            <span>6</span>
            <span>12</span>
            <span>18</span>
          </footer>
        </div>
      )}
    </AnalysisPanel>
  );
}

export function V2Cumulative({ data }: { data: UsageAnalysisV2 }) {
  const currency = data.totals.api_value.currency;
  const money = (value: number) => formatUsageValue(value, currency);
  const points = useMemo(() => {
    const dayLabel = new Intl.DateTimeFormat("sv-SE", {
      timeZone: data.tz,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    });
    const ordinal = (at: string | number, span: UsageAnalysisV2["span"]) => {
      if (typeof at === "number") return Math.round((at - span.counted_from) / 3600);
      const start = dayLabel.format(new Date(span.requested_from * 1000));
      return Math.round((Date.parse(at) - Date.parse(start)) / 86_400_000);
    };
    const rows = new Map<
      number,
      { index: number; current?: number; previous?: number; bucket?: number; label?: string }
    >();
    for (const [series, span, key] of [
      [data.series, data.span, "current"],
      [data.previous?.series ?? [], data.previous?.span ?? data.span, "previous"],
    ] as const) {
      if (
        !series.every(
          (point) => point.api_value.currency === currency && exactAmount(point.api_value) !== null,
        )
      )
        continue;
      let total = 0;
      for (const point of series) {
        const index = ordinal(point.at, span);
        const row = rows.get(index) ?? { index };
        const amount = exactAmount(point.api_value) ?? 0;
        total += amount;
        row[key] = total;
        if (key === "current") row.bucket = amount;
        if (key === "current" || row.label === undefined)
          row.label =
            typeof point.at === "string"
              ? point.at
              : new Date(point.at * 1000).toLocaleTimeString([], {
                  hour: "2-digit",
                  minute: "2-digit",
                  timeZone: data.tz,
                });
        rows.set(index, row);
      }
    }
    return [...rows.values()].sort((left, right) => left.index - right.index);
  }, [currency, data]);
  return (
    <AnalysisSection id="cumulative" title={analysisSections.cumulative}>
      {data.complete && points.length ? (
        <>
          <p className="usage-caption">At API price · Current period / Previous period</p>
          <div
            className="ua-v2-chart"
            role="img"
            aria-label="Current and previous cumulative API-priced usage"
          >
            <ResponsiveContainer width="100%" height="100%" minWidth={0}>
              <ComposedChart data={points} margin={{ top: 8, right: 14, bottom: 4, left: 0 }}>
                <CartesianGrid vertical={false} stroke="var(--usage-line)" />
                <XAxis
                  dataKey="index"
                  type="number"
                  tickFormatter={(index: number) =>
                    points.find((point) => point.index === index)?.label ?? ""
                  }
                />
                <YAxis width={67} tickFormatter={money} />
                <Tooltip
                  labelFormatter={(index) =>
                    points.find((point) => point.index === index)?.label ?? ""
                  }
                  formatter={(value) => (typeof value === "number" ? money(value) : "—")}
                />
                <Bar
                  dataKey="bucket"
                  name="Usage in bucket"
                  fill="var(--usage-accent-soft)"
                  isAnimationActive={false}
                />
                <Line
                  dataKey="current"
                  name="Current period"
                  type="stepAfter"
                  stroke="var(--usage-time)"
                  dot={false}
                  connectNulls
                  isAnimationActive={false}
                />
                <Line
                  dataKey="previous"
                  name="Previous period"
                  type="stepAfter"
                  stroke="var(--usage-ink-3)"
                  strokeDasharray="4 4"
                  dot={false}
                  connectNulls
                  isAnimationActive={false}
                />
              </ComposedChart>
            </ResponsiveContainer>
          </div>
        </>
      ) : (
        <AnalysisUnknown reason="No complete API price series" />
      )}
    </AnalysisSection>
  );
}

const weekdays = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
export function V2Heatmap({
  data,
  selected,
  onSlot,
}: {
  data: UsageAnalysisV2;
  selected: string | null;
  onSlot: (slot: string) => void;
}) {
  const cells = new Map(data.heat.map((item) => [item.slot, item.tokens]));
  const max = Math.max(1, ...data.heat.map((cell) => cell.tokens ?? 0));
  return (
    <AnalysisSection id="heat" title={analysisSections.heat} folded>
      <div className="ua-v2-heat-scroll">
        <div className="ua-v2-heat">
          {weekdays.map((name, day) => (
            <div key={name} className="ua-v2-heat-row">
              <span>{name}</span>
              {Array.from({ length: 24 }, (_, hour) => {
                const time = `${String(hour).padStart(2, "0")}:00`;
                const slot = `${day + 1}-${time.slice(0, 2)}`;
                const value = cells.get(slot);
                const text = `${name} ${time} · ${value == null ? "Unknown" : formatOverviewTokens(value)} tokens`;
                return (
                  <button
                    type="button"
                    key={slot}
                    title={text}
                    aria-label={text}
                    aria-pressed={slot === selected}
                    onClick={() => onSlot(slot)}
                    style={
                      {
                        "--ua-heat-strength": `${value ? 15 + (75 * value) / max : 0}%`,
                      } as CSSProperties
                    }
                  />
                );
              })}
            </div>
          ))}
        </div>
      </div>
      <p className="usage-caption">{data.tz}</p>
    </AnalysisSection>
  );
}

function exactAmount(value: AnalysisValue | undefined) {
  return value?.state === "exact" && value.low !== null ? Number(value.low) : null;
}
function share(value: number | null | undefined, all: (number | null | undefined)[]) {
  if (value == null || all.some((item) => item == null)) return "Unknown";
  const total = all.reduce<number>((sum, item) => sum + (item ?? 0), 0);
  return total > 0 ? `${formatRingPercent((value / total) * 100)}%` : "—";
}

export function V2Hosts({
  data,
  filter,
  hosts,
  onHost,
}: {
  data: UsageAnalysisV2;
  filter: PageFilter;
  hosts: Map<string, UsageHostView>;
  onHost: (key: string) => void;
}) {
  const [expanded, setExpanded] = useState<string | null>(null);
  const rows = data.facets.host;
  const tokens = rows.map((row) => row.tokens);
  const costs = rows.map((row) =>
    row.api_value?.currency === data.totals.api_value.currency ? exactAmount(row.api_value) : null,
  );
  return (
    <AnalysisSection id="hosts" title={analysisSections.hosts}>
      {rows.length ? (
        <div className="ua-v2-hosts">
          {rows.map((row, index) => {
            const key = typeof row.key === "string" ? row.key : null;
            const host = key === null ? undefined : hosts.get(key);
            const title = key === null ? "Other hosts" : (host?.name ?? (key || "No host"));
            const active = key !== null && filter.host.includes(key);
            return (
              <div
                className="ua-v2-host"
                key={JSON.stringify(row.key)}
                data-selected={active || undefined}
                data-dimmed={(!!filter.host.length && !active) || undefined}
              >
                <div className="ua-v2-host-head">
                  {key === null ? (
                    <strong>{title}</strong>
                  ) : (
                    <button
                      type="button"
                      aria-pressed={active}
                      disabled={filter.host.length >= 64 && !active}
                      onClick={() => onHost(key)}
                    >
                      <UsageName>{title}</UsageName>
                    </button>
                  )}
                  <small>
                    {host?.read_at == null
                      ? "No host reading"
                      : `Read ${formatAge(Math.max(0, data.generated_at - host.read_at))} ago`}
                  </small>
                </div>
                <div className="usage-caption">
                  {analysisValueText(row.api_value)} ·{" "}
                  {row.tokens == null ? "Unknown" : formatOverviewTokens(row.tokens)} tokens ·{" "}
                  {row.requests?.toLocaleString() ?? "Unknown"} requests
                </div>
                <div className="usage-caption">
                  Token share {data.complete ? share(row.tokens, tokens) : "Unknown"} · Cost share{" "}
                  {data.complete ? share(costs[index], costs) : "Unknown"}
                </div>
                {key !== null ? (
                  <>
                    <button
                      type="button"
                      className="ua-host-agents"
                      aria-expanded={expanded === key}
                      onClick={() => setExpanded(expanded === key ? null : key)}
                    >
                      Top Agents
                    </button>
                    {expanded === key ? <HostAgents filter={filter} host={key} /> : null}
                  </>
                ) : null}
              </div>
            );
          })}
        </div>
      ) : (
        <p className="usage-caption">No host usage in this range.</p>
      )}
    </AnalysisSection>
  );
}

function HostAgents({ filter, host }: { filter: PageFilter; host: string }) {
  const query = useUsageAnalysisV2(
    { ...filter, group: "agent", host: [host] },
    { previous: false, poll: false },
  );
  if (query.error)
    return (
      <p className="usage-caption" role="alert">
        {query.error.message}
      </p>
    );
  if (!query.data) return <p className="usage-caption">Reading Agents…</p>;
  const selected = filter.agent;
  const agents = query.data.groups
    .filter(
      (row) =>
        typeof row.key === "string" &&
        row.tokens != null &&
        (!selected.length || selected.includes(row.key)),
    )
    .sort((left, right) => (right.tokens ?? 0) - (left.tokens ?? 0))
    .slice(0, 3);
  return query.data.complete && agents.length ? (
    <ul className="usage-caption">
      {agents.map((row) => (
        <li key={String(row.key)}>
          {row.key || "Untagged"} · {formatOverviewTokens(row.tokens!)}
        </li>
      ))}
    </ul>
  ) : (
    <p className="usage-caption">No complete Agent breakdown.</p>
  );
}

export function V2Composition({ data }: { data: UsageAnalysisV2 }) {
  return (
    <AnalysisSection id="composition" title={analysisSections.composition} folded>
      <UsageClassComposition classes={data.classes} complete={data.complete} />
    </AnalysisSection>
  );
}
