import {
  type AnalysisAmounts,
  type AnalysisCounts,
  type UsageAnalysisV2,
  type AnalysisRow,
  type AnalysisProjection,
  analysisTotals,
  analysisValueText,
} from "./usageAnalysisData";
import { AnalysisPanel, AnalysisSection, AnalysisUnknown } from "./UsageAnalysisPanel";
import { UsageTip } from "./UsageTip";
import { AnalysisTrend, compareAnalysisDecimals } from "./UsageAnalysisDetails";
import { UsageName } from "./UsageName";
import { moneyText } from "./UsageAccountMoney";
import { formatOverviewTokens, formatRingPercent } from "./usageBoardFormat";
import { useState, type CSSProperties } from "react";
import type { FilterDimension, PageFilter } from "./usageAnalysisFilters";
import { analysisSections } from "./UsageAnalysisRail";

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
const percent = (value: number | null) =>
  value === null ? "Unknown" : `${formatRingPercent(value)}%`;
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

export const paymentRestriction = "实付不分 Host / Agent / 模型 / 时段";
export const paymentRestricted = (filter: PageFilter) =>
  !!(filter.host.length || filter.agent.length || filter.model.length || filter.slot);
export const v2Tokens = (tokens: number | null | undefined) =>
  tokens == null ? "Unknown" : formatOverviewTokens(tokens);
export const v2Requests = (requests: number | null | undefined) =>
  requests == null ? "Unknown" : requests.toLocaleString();
export const v2Failure = (counts: Partial<AnalysisCounts>) =>
  counts.requests && counts.failed != null ? (100 * counts.failed) / counts.requests : null;
type V2Row = UsageAnalysisV2["groups"][number];

function amountSort(value: AnalysisAmounts["paid"] | undefined) {
  return value?.low ?? null;
}

export function AnalysisV2Items({
  data,
  filter,
  label,
  toggle,
}: {
  data: UsageAnalysisV2;
  filter: PageFilter;
  label: (dimension: FilterDimension, value: string) => string;
  toggle: (dimension: FilterDimension, value: string) => void;
}) {
  const [sort, setSort] = useState<{ key: number; direction: 1 | -1 }>({ key: 0, direction: -1 });
  const restricted = paymentRestricted(filter) || filter.group !== "account";
  const metrics = [
    {
      title: "At API price",
      display: (row: V2Row) => analysisValueText(row.api_value),
      value: (row: V2Row) => amountSort(row.api_value),
    },
    {
      title: "Paid",
      display: (row: V2Row) => (restricted ? "未知" : analysisValueText(row.paid)),
      value: (row: V2Row) => (restricted ? null : amountSort(row.paid)),
    },
    {
      title: "Token",
      display: (row: V2Row) => v2Tokens(row.tokens),
      value: (row: V2Row) => row.tokens ?? null,
    },
    {
      title: "Requests",
      display: (row: V2Row) => v2Requests(row.requests),
      value: (row: V2Row) => row.requests ?? null,
    },
    {
      title: "Payback",
      display: (row: V2Row) => (restricted ? "未知" : analysisValueText(row.payback, "ratio")),
      value: (row: V2Row) => (restricted ? null : amountSort(row.payback)),
    },
    { title: "Failure rate", display: (row: V2Row) => percent(v2Failure(row)), value: v2Failure },
  ];
  const ordered = [...data.groups].sort((a, b) => {
    if (a.remainder !== b.remainder && (a.remainder || b.remainder)) return a.remainder ? 1 : -1;
    const left = metrics[sort.key].value(a);
    const right = metrics[sort.key].value(b);
    if (left === null) return right === null ? 0 : 1;
    if (right === null) return -1;
    return (
      sort.direction *
      (typeof left === "string" && typeof right === "string"
        ? compareAnalysisDecimals(left, right)
        : Number(left) - Number(right))
    );
  });
  const active = filter[filter.group];
  return (
    <AnalysisSection id="items" title={analysisSections.items}>
      {restricted ? <p className="usage-caption">{paymentRestriction}</p> : null}
      {ordered.length ? (
        <div className="ua-v2-table-scroll">
          <table className="ua-v2-table" aria-label={analysisSections.items}>
            <thead>
              <tr>
                <th scope="col">Item</th>
                <th scope="col">Trend</th>
                {metrics.map(({ title }, index) => (
                  <th
                    scope="col"
                    key={title}
                    aria-sort={
                      sort.key === index
                        ? sort.direction === 1
                          ? "ascending"
                          : "descending"
                        : undefined
                    }
                  >
                    <button
                      type="button"
                      onClick={() =>
                        setSort({
                          key: index,
                          direction: sort.key === index && sort.direction === -1 ? 1 : -1,
                        })
                      }
                    >
                      {title}
                    </button>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {ordered.map((row) => {
                const key = typeof row.key === "string" ? row.key : null;
                const selected = key !== null && active.includes(key);
                const name = key === null ? "Other items" : label(filter.group, key);
                return (
                  <tr
                    key={key === null ? "remainder" : `item:${key}`}
                    data-selected={selected || undefined}
                    data-dimmed={(active.length > 0 && !selected) || undefined}
                  >
                    <th scope="row">
                      {key === null ? (
                        name
                      ) : (
                        <button
                          type="button"
                          aria-pressed={selected}
                          disabled={active.length >= 64 && !selected}
                          onClick={() => toggle(filter.group, key)}
                        >
                          <UsageName>{name}</UsageName>
                        </button>
                      )}
                    </th>
                    <td>
                      <V2Trend trend={row.trend} />
                    </td>
                    {metrics.map(({ title, display }) => (
                      <td key={title}>{display(row)}</td>
                    ))}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="usage-caption">No items in this range.</p>
      )}
    </AnalysisSection>
  );
}

function V2Trend({ trend }: { trend: V2Row["trend"] }) {
  if (!trend?.length || trend.some((point) => point.tokens === null)) return <span>—</span>;
  const values = trend.map((point) => point.tokens ?? 0);
  const max = Math.max(1, ...values);
  return (
    <svg className="ua-spark" viewBox="0 0 75 20" role="img" aria-label="Token trend">
      <path
        d={values
          .flatMap((value, index) => {
            const y = 19 - (value / max) * 17;
            return values.length === 1
              ? [`M0,${y} L75,${y}`]
              : [`${index ? "L" : "M"}${(index * 75) / (values.length - 1)},${y}`];
          })
          .join(" ")}
      />
    </svg>
  );
}

export const exactCost = (value: AnalysisAmounts["api_value"] | undefined) =>
  value?.state === "exact" && value.low !== null ? Number(value.low) : null;
export const v2Share = (value: number | null, total: number | null) =>
  value !== null && total !== null && total > 0 ? (value / total) * 100 : null;

export function AnalysisV2Shares({ data, filter }: { data: UsageAnalysisV2; filter: PageFilter }) {
  const families = new Map<string, { tokens: number | null; cost: number | null }>();
  for (const row of data.facets.model) {
    if (filter.model.length && (typeof row.key !== "string" || !filter.model.includes(row.key)))
      continue;
    const name = typeof row.key === "string" ? shareFamily(row.key) : "Unlisted models";
    const previous = families.get(name) ?? { tokens: 0, cost: 0 };
    const cost =
      row.api_value?.currency === data.totals.api_value.currency ? exactCost(row.api_value) : null;
    families.set(name, {
      tokens: previous.tokens === null || row.tokens == null ? null : previous.tokens + row.tokens,
      cost: previous.cost === null || cost === null ? null : previous.cost + cost,
    });
  }
  const shares = [...families].map(([name, amounts], index) => ({
    name,
    color: colors[index % colors.length],
    tokens: data.complete ? v2Share(amounts.tokens, data.totals.tokens) : null,
    cost: data.complete ? v2Share(amounts.cost, exactCost(data.totals.api_value)) : null,
  }));
  return (
    <AnalysisSection id="shares" title={analysisSections.shares} folded>
      {shares.length ? (
        <>
          {(
            [
              { key: "tokens", title: "Token" },
              { key: "cost", title: "Cost · at API price" },
            ] as const
          ).map(({ key, title }) => (
            <div className="ua-share-row" key={key}>
              <span>{title}</span>
              {shares.every((item) => item[key] !== null) ? (
                <div className="ua-stack">
                  {shares.map((item) =>
                    item[key] ? (
                      <span key={item.name} style={{ width: `${item[key]}%` }}>
                        <UsageTip
                          model={{
                            title: item.name,
                            rows: [{ label: title, value: percent(item[key]) }],
                          }}
                          tapOpens
                        >
                          <button
                            type="button"
                            aria-label={`${item.name} ${title} share ${percent(item[key])}`}
                            style={{ width: "100%", background: item.color }}
                          />
                        </UsageTip>
                      </span>
                    ) : null,
                  )}
                </div>
              ) : (
                <small>Unknown for part of this range</small>
              )}
            </div>
          ))}
          <div className="ua-share-legend">
            {shares.map((item) => (
              <span key={item.name}>
                <i style={{ background: item.color }} />
                <UsageName className="flex-1">{item.name}</UsageName>
                <b>
                  {percent(item.tokens)} / {percent(item.cost)}
                </b>
              </span>
            ))}
          </div>
        </>
      ) : (
        <AnalysisUnknown reason="No matching models" />
      )}
    </AnalysisSection>
  );
}
