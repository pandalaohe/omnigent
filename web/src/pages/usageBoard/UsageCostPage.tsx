import { useMemo, useState } from "react";
import { Link } from "@/lib/routing";
import { boardPath } from "@/lib/boardFrame";
import { patchUsageBoardPreferences } from "@/lib/usageContextPreferences";
import { useUsageContextPreferences } from "@/hooks/useUsageContextPreferences";
import { UsageBoardFrame } from "./UsageBoardFrame";
import { useUsageOverview } from "./usageBoardData";
import {
  analysisKey,
  analysisTotals,
  poolKey,
  toggleAnalysisPick,
  useUsageAnalysis,
  type AnalysisDimension,
  analysisValueText,
} from "./usageAnalysisData";
import {
  UsageAnalysisShares,
  UsageAnalysisFailures,
  UsageAnalysisTable,
} from "./UsageAnalysisAmounts";
import { UsageAnalysisChart } from "./UsageCostChart";
import { AnalysisPanel, AnalysisUnknown } from "./UsageAnalysisPanel";
import {
  AnalysisCards,
  AnalysisComparisons,
  AnalysisPayback,
  AnalysisClosings,
  AnalysisHeatmap,
} from "./UsageAnalysisDetails";
import "./usageAnalysis.css";

const compact = (value: number | null) =>
  value === null
    ? "Unknown"
    : new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 2 }).format(
        value,
      );

export function UsageCostPage() {
  const board = useUsageContextPreferences().board;
  const range = ["today", "d7", "d30", "d90"].includes(board.range?.key ?? "")
    ? board.range!
    : { key: "d30" as const };
  const overview = useUsageOverview(true);
  const analysis = useUsageAnalysis(range);
  const [dimension, setDimension] = useState<AnalysisDimension | "card">("account");
  const [picks, setPicks] = useState<Partial<Record<AnalysisDimension | "card", string[] | null>>>(
    {},
  );
  const [cardSort, setCardSort] = useState({ key: "paid", direction: -1 });
  const [sort, setSort] = useState<{
    key: "api" | "paid" | "tokens" | "payback" | "failure";
    direction: number;
  }>({ key: "api", direction: -1 });
  const visible = useMemo(
    () => overview.data?.accounts.filter((account) => !board.hidden.includes(account.id)) ?? [],
    [overview.data, board.hidden],
  );
  const names = useMemo(
    () => new Map(visible.map((account) => [account.id, account.display_name])),
    [visible],
  );
  const rows = useMemo(
    () => analysis.data?.rows.filter((row) => names.has(row.parent_account_id)) ?? [],
    [analysis.data, names],
  );
  const items = useMemo(() => {
    if (dimension === "card")
      return (analysis.data?.pricing?.cards ?? [])
        .filter((card) => !board.hidden.includes(card.parent_account_id ?? ""))
        .map((card) => ({ key: card.id, name: card.name }));
    if (dimension === "account")
      return visible.map((account) => ({ key: account.id, name: account.display_name }));
    if (dimension === "pool")
      return (analysis.data?.pool_catalog ?? [])
        .filter((pool) => names.has(pool.parent_account_id))
        .map((pool) => ({
          key: poolKey(pool.parent_account_id, pool.pool_id),
          name: pool.display_name,
        }));
    return [...new Set(rows.filter((row) => row.contributes_usage).map((row) => row.model))].map(
      (model) => ({ key: model, name: model || "Unknown model" }),
    );
  }, [dimension, visible, analysis.data, names, rows, board.hidden]);
  const live = picks[dimension]?.filter((key) => items.some((item) => item.key === key));
  const all = !live?.length || live.length === items.length;
  const keys = all ? items.map((item) => item.key) : live;
  const selected = useUsageAnalysis(range, {
    dimension,
    keys,
    hidden: board.hidden,
    enabled: !!analysis.data,
  });
  const presentation = selected.data?.presentation;
  const selectedRows = useMemo(
    () =>
      dimension === "card"
        ? []
        : (presentation?.rows ??
          rows.filter((row) => keys.includes(analysisKey(row, dimension) ?? ""))),
    [rows, keys, dimension, presentation],
  );
  const partial = presentation
    ? presentation.partial || selected.data?.partial || selected.data?.truncated
    : analysis.data?.partial || analysis.data?.truncated;
  const totals = analysisTotals(selectedRows, partial);
  const selectedItems = items
    .filter((item) => keys.includes(item.key))
    .map((item) => ({
      ...item,
      projection: presentation?.items.find((value) => value.key === item.key)?.projection,
      rows:
        dimension === "card"
          ? []
          : selectedRows.filter((row) => analysisKey(row, dimension) === item.key),
    }));
  return (
    <UsageBoardFrame
      title="Cost"
      generatedAt={analysis.data?.generated_at}
      loading={analysis.isPending || overview.isPending}
      error={selected.error ?? analysis.error ?? overview.error}
      actions={
        <details>
          <summary>Analysis tools</summary>
          <div className="flex flex-wrap gap-3">
            <Link to={boardPath("/usage/board/issues")}>Issues</Link>
            <Link to="/usage">Sessions</Link>
            <Link to={"/settings/general#usage-settings"} aria-label="Analysis settings">
              Settings
            </Link>
          </div>
        </details>
      }
    >
      <div className="ua-controls">
        <div className="ua-segment" role="group" aria-label="Group by">
          {(
            [
              ["account", "Account"],
              ["pool", "Pool"],
              ["model", "Model"],
              ["card", "Card"],
            ] as const
          ).map(([key, label]) => (
            <button
              type="button"
              key={key}
              aria-pressed={dimension === key}
              onClick={() => setDimension(key)}
            >
              {label}
            </button>
          ))}
        </div>
        <div className="ua-segment" role="group" aria-label="Time range">
          {(
            [
              ["today", "Today"],
              ["d7", "7 days"],
              ["d30", "30 days"],
              ["d90", "90 days"],
            ] as const
          ).map(([key, label]) => (
            <button
              type="button"
              key={key}
              aria-pressed={range.key === key}
              onClick={() => patchUsageBoardPreferences({ range: { key } })}
            >
              {label}
            </button>
          ))}
        </div>
      </div>
      <div className="ua-picks" role="group" aria-label="Select items">
        <button
          type="button"
          aria-pressed={all}
          onClick={() => setPicks({ ...picks, [dimension]: null })}
        >
          All
        </button>
        {items.map((item) => (
          <button
            type="button"
            key={item.key}
            aria-pressed={!all && keys.includes(item.key)}
            onClick={() =>
              setPicks({
                ...picks,
                [dimension]: toggleAnalysisPick(
                  picks[dimension] ?? null,
                  item.key,
                  items.map((value) => value.key),
                ),
              })
            }
          >
            {item.name}
          </button>
        ))}
      </div>
      {dimension === "card" ? (
        <AnalysisCards
          data={presentation}
          sort={cardSort}
          onSort={(key) =>
            setCardSort({ key, direction: cardSort.key === key ? -cardSort.direction : -1 })
          }
        />
      ) : (
        <>
          {!items.length ? <p role="status">No usage in this range.</p> : null}
          <div className="ua-kpis">
            {[
              ["At API price", "Unknown", "Not available yet · price evidence"],
              [
                "Paid",
                analysisValueText(presentation?.projection.paid),
                "Configured FX · selected payments",
              ],
              [
                "Token",
                compact(totals.tokens),
                partial ? "Partial source coverage" : "Reported tokens",
              ],
              [
                "Payback",
                analysisValueText(presentation?.projection.payback, "ratio"),
                `Known-paid ${presentation?.projection.payback.coverage?.known_paid ?? 0}/${presentation?.projection.payback.coverage?.total_items ?? 0}`,
              ],
            ].map(([label, value, caption]) => (
              <div className="ua-kpi" key={label}>
                <span>{label}</span>
                <strong>{value}</strong>
                <small>{caption}</small>
              </div>
            ))}
          </div>
          <div className="ua-main">
            <AnalysisPanel title="Cumulative" caption="Reported tokens">
              <UsageAnalysisChart
                rows={selectedRows}
                span={analysis.data?.span}
                unknown={totals.tokens === null}
                previousUnavailable={range.key !== "d90"}
                previous={range.key === "d90" ? null : presentation?.previous}
                paidSeries={presentation?.paid_series}
              />
            </AnalysisPanel>
            <AnalysisComparisons data={presentation} ninety={range.key === "d90"} />
          </div>
          <UsageAnalysisTable
            items={selectedItems}
            incomplete={!!partial}
            sort={sort}
            onSort={(key) => setSort({ key, direction: sort.key === key ? -sort.direction : -1 })}
          />
          <div className="ua-cards">
            <AnalysisPayback items={selectedItems} />
            <AnalysisClosings data={presentation} names={names} model={dimension === "model"} />
            <UsageAnalysisShares rows={selectedRows} incomplete={!!partial} />
            <UsageAnalysisFailures items={selectedItems} incomplete={!!partial} />
            <AnalysisHeatmap
              rows={selectedRows}
              partial={!!partial}
              timezone={analysis.data?.tz ?? Intl.DateTimeFormat().resolvedOptions().timeZone}
            />
            <AnalysisPanel title="Cache savings">
              <AnalysisUnknown reason="Not available yet · price evidence" />
            </AnalysisPanel>
          </div>
        </>
      )}
    </UsageBoardFrame>
  );
}
