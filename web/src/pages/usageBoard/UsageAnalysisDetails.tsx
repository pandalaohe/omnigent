import { useMemo } from "react";
import { AnalysisPanel, AnalysisUnknown } from "./UsageAnalysisPanel";
import { UsageTip } from "./UsageTip";
import {
  analysisTotals,
  analysisValueText,
  type AnalysisCard,
  type AnalysisPresentation,
  type AnalysisRow,
  type AnalysisValue,
} from "./usageAnalysisData";

export function AnalysisTrend({ rows }: { rows: AnalysisRow[] }) {
  const points = new Map<number, number>();
  for (const row of rows)
    if (row.contributes_usage && row.tokens !== null)
      points.set(row.hour, (points.get(row.hour) ?? 0) + row.tokens);
  const ordered = [...points].sort(([a], [b]) => a - b);
  const max = Math.max(1, ...ordered.map(([, value]) => value));
  if (!ordered.length || analysisTotals(rows).tokens === null) return <span>Unknown</span>;
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
                <strong>
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
            <strong>{analysisValueText(value as AnalysisValue, unit as "money" | "quota")}</strong>
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
                <b>{card.name}</b>
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
                <span role="cell" data-label={String(label)} key={String(label)}>
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
}: {
  items: { key: string; name: string; projection?: { payback: AnalysisValue } }[];
}) {
  return (
    <AnalysisPanel title="Subscription payback" caption="Matching known-paid cohort">
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
              <button type="button" className="ua-bar ua-payback">
                <span>{item.name}</span>
                <span className="ua-track">
                  <s style={{ left: "50%" }} />
                  {position === null ? null : <i style={{ left: `${position}%` }} />}
                </span>
                <b>{analysisValueText(value, "ratio")}</b>
              </button>
            </UsageTip>
          );
        })}
      </div>
      <small className="text-xs text-muted-foreground">
        0.01× — 1× — 100× · unknown stays unknown
      </small>
    </AnalysisPanel>
  );
}
export function AnalysisClosings({
  data,
  names,
  model,
}: {
  data?: AnalysisPresentation | null;
  names: Map<string, string>;
  model: boolean;
}) {
  const readings = new Map<string, NonNullable<AnalysisPresentation["quota_closings"]>[number]>();
  for (const closing of data?.quota_closings ?? []) {
    const key = JSON.stringify([closing.account_id, closing.window_id]);
    const old = readings.get(key);
    if (!old || closing.resets_at > old.resets_at) readings.set(key, closing);
  }
  return (
    <AnalysisPanel title="Unused last period" caption="Measured closing observation">
      {readings.size ? (
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
                <span>{names.get(reading.account_id) ?? reading.account_id}</span>
                <b>
                  {reading.remaining.state === "unknown"
                    ? "Unknown"
                    : reading.remaining.formatted?.high
                      ? `≤${reading.remaining.formatted.high.replace(/\.00$/, "")}%`
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
      )}
    </AnalysisPanel>
  );
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
                          background: `color-mix(in srgb, var(--info) ${Math.max(6, (tokens / max) * 100)}%, var(--muted))`,
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
