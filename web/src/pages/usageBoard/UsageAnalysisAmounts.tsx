import {
  type AnalysisRow,
  type AnalysisProjection,
  analysisTotals,
  analysisValueText,
} from "./usageAnalysisData";
import { AnalysisPanel, AnalysisUnknown } from "./UsageAnalysisPanel";
import { UsageTip } from "./UsageTip";
import { AnalysisTrend, compareAnalysisDecimals } from "./UsageAnalysisDetails";
import { UsageName } from "./UsageName";
import { moneyText } from "./UsageAccountMoney";
import type { CSSProperties } from "react";

interface Item {
  key: string;
  name: string;
  rows: AnalysisRow[];
  projection?: AnalysisProjection;
}
type SortKey = "api" | "paid" | "tokens" | "payback" | "failure";
const number = (value: number | null) =>
  value === null
    ? "Unknown"
    : new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 2 }).format(
        value,
      );
const percent = (value: number | null) => (value === null ? "Unknown" : `${value.toFixed(1)}%`);
const colors = [
  "var(--usage-series-1)",
  "var(--usage-series-2)",
  "var(--usage-series-3)",
  "var(--usage-series-4)",
  "var(--usage-series-5)",
];
export function UsageAnalysisTable({
  items,
  incomplete,
  sort,
  onSort,
}: {
  items: Item[];
  incomplete: boolean;
  sort: { key: SortKey; direction: number };
  onSort: (key: SortKey) => void;
}) {
  const data = items.map((item) => ({ ...item, ...analysisTotals(item.rows, incomplete) }));
  const showMoney = items.some((item) => item.projection?.show_money !== false);
  const showPayback = items.some((item) => item.projection?.show_payback !== false);
  const paymentLabel = showPayback ? "Paid / spend" : "Spend";
  const columns = {
    "--ua-metric-columns": 2 + (showMoney ? 2 : 0) + (showPayback ? 1 : 0),
  } as CSSProperties;
  data.sort((a, b) => {
    if (sort.key === "paid" || sort.key === "payback") {
      const av = a.projection?.[sort.key]?.low,
        bv = b.projection?.[sort.key]?.low;
      return av == null
        ? bv == null
          ? 0
          : 1
        : bv == null
          ? -1
          : compareAnalysisDecimals(av, bv) * sort.direction;
    }
    const av = sort.key === "tokens" ? a.tokens : sort.key === "failure" ? a.failure : null;
    const bv = sort.key === "tokens" ? b.tokens : sort.key === "failure" ? b.failure : null;
    return av === null ? (bv === null ? 0 : 1) : bv === null ? -1 : (av - bv) * sort.direction;
  });
  return (
    <AnalysisPanel title="Each item" caption={`${items.length} items`}>
      <div className="ua-table" role="table" aria-label="Each item" style={columns}>
        <div className="ua-table-head" role="row">
          <span role="columnheader">Item</span>
          <span role="columnheader">Trend</span>
          {(
            [
              ["api", "At API price"],
              ["paid", paymentLabel],
              ["tokens", "Token"],
              ["payback", "Payback"],
              ["failure", "Failure rate"],
            ] as const
          )
            .filter(
              ([key]) =>
                (showMoney || !["api", "paid"].includes(key)) && (showPayback || key !== "payback"),
            )
            .map(([key, label]) => (
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
        {data.map((item) => (
          <div role="row" className="ua-table-row" key={item.key} data-item={item.key}>
            <span role="cell" className="ua-name">
              <UsageName>{item.name}</UsageName>
            </span>
            <span role="cell">
              <AnalysisTrend rows={item.rows} />
            </span>
            {[
              [
                "At API price",
                item.projection?.show_money === false ? "—" : moneyText(item.projection?.api_value),
              ],
              [
                paymentLabel,
                item.projection?.show_money === false
                  ? "—"
                  : `${item.projection?.spend_estimated ? "Estimated " : ""}${moneyText(
                      item.projection?.show_payback === false
                        ? item.projection?.spend
                        : item.projection?.paid,
                    )}`,
              ],
              ["Token", number(item.tokens)],
              [
                "Payback",
                item.projection?.show_payback === false
                  ? "—"
                  : analysisValueText(item.projection?.payback, "ratio"),
              ],
              ["Failure rate", percent(item.failure)],
            ]
              .filter(
                ([label]) =>
                  (showMoney || !["At API price", paymentLabel].includes(label)) &&
                  (showPayback || label !== "Payback"),
              )
              .map(([label, value]) => (
                <span
                  key={label}
                  role="cell"
                  data-label={label}
                  data-unknown={value === "Unknown" || undefined}
                >
                  {value}
                </span>
              ))}
          </div>
        ))}
      </div>
    </AnalysisPanel>
  );
}
function shareFamily(model: string): string {
  const name = model.split("/").at(-1) ?? model;
  if (/^claude(?:[- .]|$)/i.test(name)) return "Claude";
  if (/^gpt(?:-|$)/i.test(name)) return "GPT";
  if (/^deepseek(?:[- .]|$)/i.test(name)) return "DeepSeek";
  if (/^kimi(?:[- .]|$)/i.test(name)) return "Kimi";
  return "Other";
}

export function UsageAnalysisShares({
  rows,
  incomplete,
}: {
  rows: AnalysisRow[];
  incomplete: boolean;
}) {
  const models = new Map<string, AnalysisRow[]>();
  for (const row of rows.filter((entry) => entry.contributes_usage)) {
    const family = shareFamily(row.model);
    const group = models.get(family) ?? [];
    group.push(row);
    models.set(family, group);
  }
  const total = analysisTotals(rows, incomplete).tokens;
  const shares = [...models].map(([model, group]) => ({
    model,
    tokens: analysisTotals(group, incomplete).tokens,
  }));
  return (
    <AnalysisPanel title="Token and cost shares" caption="Selected models">
      {total === null || !total ? (
        <AnalysisUnknown reason="No complete token population" />
      ) : (
        <>
          <div className="ua-share-row">
            <span>Token</span>
            <div className="ua-stack">
              {shares.map(({ model, tokens }, index) =>
                tokens === null || tokens === 0 ? null : (
                  <span key={model} style={{ flex: `${tokens} 0 0`, minWidth: 0 }}>
                    <UsageTip
                      key={model}
                      className="ua-share-piece"
                      model={{
                        title: model,
                        rows: [{ label: "Token", value: percent((tokens / total) * 100) }],
                      }}
                      tapOpens
                    >
                      <button
                        type="button"
                        aria-label={`${model} token share ${percent((tokens / total) * 100)}`}
                        style={{ width: "100%", background: colors[index % colors.length] }}
                      />
                    </UsageTip>
                  </span>
                ),
              )}
            </div>
          </div>
          <div className="ua-share-row">
            <span>Cost</span>
            <small>Not available yet · price evidence</small>
          </div>
          <div className="ua-share-legend">
            {shares.map(({ model, tokens }, index) => (
              <span key={model}>
                <i style={{ background: colors[index % colors.length] }} />
                <UsageName className="flex-1">{model || "Unknown model"}</UsageName>
                <b>{percent(tokens === null ? null : (tokens / total) * 100)} / —</b>
              </span>
            ))}
          </div>
        </>
      )}
    </AnalysisPanel>
  );
}
export function UsageAnalysisFailures({
  items,
  incomplete,
}: {
  items: Item[];
  incomplete: boolean;
}) {
  const measured = items.map((item) => ({ ...item, ...analysisTotals(item.rows, incomplete) }));
  const known = measured
    .filter((item) => item.failure !== null)
    .sort((a, b) => b.failure! - a.failure!)
    .slice(0, 5);
  const max = Math.max(1, ...known.map((item) => item.failure!));
  return (
    <AnalysisPanel
      title="Failure rate"
      caption={`${measured.filter((item) => item.failure !== null).length}/${items.length} measured items`}
    >
      {known.length ? (
        <div className="ua-bars" data-kind="failure">
          {known.map((item) => (
            <UsageTip
              key={item.key}
              model={{
                title: item.name,
                rows: [
                  { label: "Requests", value: String(item.requests) },
                  { label: "Failed", value: String(item.failed) },
                  { label: "Failure rate", value: percent(item.failure) },
                ],
              }}
              tapOpens
            >
              <button
                type="button"
                className="ua-bar"
                aria-label={`${item.name} failure rate ${percent(item.failure)}`}
              >
                <UsageName>{item.name}</UsageName>
                <span className="ua-track">
                  <i style={{ width: `${(item.failure! / max) * 100}%` }} />
                </span>
                <b>{percent(item.failure)}</b>
              </button>
            </UsageTip>
          ))}
        </div>
      ) : (
        <AnalysisUnknown reason="No complete request and failure counts" />
      )}
    </AnalysisPanel>
  );
}
