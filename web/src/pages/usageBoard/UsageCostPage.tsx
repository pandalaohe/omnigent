import { useEffect } from "react";
import { Link, useLocation, useSearchParams } from "@/lib/routing";
import { boardExitProps, boardPath } from "@/lib/boardFrame";
import { useUsageContextPreferences } from "@/hooks/useUsageContextPreferences";
import { UsageBoardFrame } from "./UsageBoardFrame";
import { useUsageOverview } from "./usageBoardData";
import {
  AnalysisFilterMismatch,
  analysisValueText,
  useUsageAnalysisV2,
  type AnalysisValue,
  type UsageAnalysisV2,
} from "./usageAnalysisData";
import {
  AnalysisV2Items,
  AnalysisV2Shares,
  exactCost,
  paymentRestricted,
  paymentRestriction,
  v2Failure,
  v2Requests,
  v2Tokens,
} from "./UsageAnalysisAmounts";
import {
  V2Composition,
  V2Cumulative,
  V2Heatmap,
  V2Hosts,
  V2Payback,
  V2QuotaClosings,
} from "./UsageAnalysisDetails";
import { AnalysisSection, AnalysisSectionState, AnalysisUnknown } from "./UsageAnalysisPanel";
import { analysisSections, UsageAnalysisRail } from "./UsageAnalysisRail";
import {
  analysisDimensions,
  filterFromQuery,
  toQuery,
  toggleAnalysisValue,
  type FilterDimension,
  type PageFilter,
} from "./usageAnalysisFilters";
import { formatRingPercent, formatUsageValue } from "./usageBoardFormat";
import "./usageAnalysis.css";

export function UsageCostPage() {
  const [search, setSearch] = useSearchParams();
  const location = useLocation();
  const raw = location.search.slice(1);
  const filter = filterFromQuery(search);
  const canonical = toQuery(filter, search).toString();
  // A legacy bookmark retains its complete query until the user changes a filter.
  const keepLegacyQuery = location.state?.keepLegacyQuery === true;
  useEffect(() => {
    if (!keepLegacyQuery && raw !== canonical) {
      setSearch(canonical, { replace: true });
    }
  }, [canonical, keepLegacyQuery, raw, setSearch]);
  const overview = useUsageOverview(true, false);
  const hidden = useUsageContextPreferences().board.hidden;
  const visibleAccounts = overview.data?.accounts.filter(({ id }) => !hidden.includes(id)) ?? [];
  const accountKeys = visibleAccounts.map(({ id }) => id);
  const accounts = new Map(visibleAccounts.map(({ id, display_name }) => [id, display_name]));
  const scopedAccounts = filter.account.length
    ? filter.account.filter((id) => !hidden.includes(id))
    : accountKeys;
  const awaitScope = !!hidden.length && !filter.account.length && !overview.data;
  const invalidScope =
    !!hidden.length && !awaitScope && (scopedAccounts.length === 0 || scopedAccounts.length > 64);
  const effectiveFilter = hidden.length ? { ...filter, account: scopedAccounts } : filter;
  const reading = useUsageAnalysisV2(effectiveFilter, {
    enabled: !awaitScope && !invalidScope,
    keepResults: true,
  });
  const outdated = reading.error instanceof AnalysisFilterMismatch;
  const previousAccounts = reading.data?.filter.account;
  const placeholderMayIncludeHidden =
    reading.isPlaceholderData &&
    hidden.length > 0 &&
    (!previousAccounts?.length || previousAccounts.some((key) => hidden.includes(key)));
  const result =
    outdated || awaitScope || invalidScope || placeholderMayIncludeHidden
      ? undefined
      : reading.data;
  const updating = !!result && reading.isPlaceholderData;
  const resultGroup =
    analysisDimensions.find((group) => group === result?.filter.group) ?? filter.group;
  const groupFilter = { ...filter, group: resultGroup };
  const showAccount = (item: UsageAnalysisV2["groups"][number]) =>
    typeof item.key === "string" && !hidden.includes(item.key);
  const data =
    result && hidden.length
      ? {
          ...result,
          facets: { ...result.facets, account: result.facets.account.filter(showAccount) },
          groups: resultGroup === "account" ? result.groups.filter(showAccount) : result.groups,
        }
      : result;
  const hosts = new Map(
    visibleAccounts.flatMap((account) =>
      (account.hosts ?? []).map((host) => [host.host_id, host] as const),
    ),
  );
  function name(dimension: FilterDimension, key: string) {
    if (dimension === "account") return accounts.get(key) ?? (key || "No account");
    if (dimension === "host") return hosts.get(key)?.name ?? (key || "No host");
    return key || (dimension === "agent" ? "Untagged" : "No model");
  }
  function change(next: PageFilter) {
    const query = toQuery(next, search).toString();
    if (query !== raw) setSearch(query, { preventScrollReset: true });
  }
  function toggle(dimension: FilterDimension, value: string) {
    change(toggleAnalysisValue(filter, dimension, value));
  }

  return (
    <UsageBoardFrame
      title="Cost"
      generatedAt={data?.generated_at}
      actions={
        <details>
          <summary>Analysis tools</summary>
          <div className="flex flex-wrap gap-3">
            <Link to={boardPath("/usage/board/issues")}>Issues</Link>
            <Link to="/usage" {...boardExitProps()}>
              Sessions
            </Link>
          </div>
        </details>
      }
    >
      <UsageAnalysisRail
        filter={filter}
        facets={data?.facets}
        accounts={accountKeys}
        label={name}
        onChange={change}
        showSections={!!data}
      />
      {outdated ? (
        <p className="ua-v2-status" role="alert">
          服务器版本不支持这个筛选
        </p>
      ) : null}
      {reading.error && !outdated ? (
        <p className="ua-v2-status" role="alert">
          Usage could not be read. {reading.error.message}
        </p>
      ) : null}
      {awaitScope && overview.error ? (
        <p className="ua-v2-status" role="alert">
          The accounts on your board could not be read.
        </p>
      ) : null}
      {invalidScope ? (
        <p className="ua-v2-status" role="status">
          {scopedAccounts.length
            ? "Choose up to 64 accounts to analyse."
            : "No selected accounts are on your board."}
        </p>
      ) : null}
      {(reading.isPending || placeholderMayIncludeHidden) &&
      !invalidScope &&
      !(awaitScope && overview.error) ? (
        <p className="ua-v2-status" role="status">
          Reading your usage…
        </p>
      ) : null}
      {updating ? (
        <p className="sr-only" role="status">
          Updating usage. Showing previous results.
        </p>
      ) : null}
      <AnalysisSectionState>
        {data ? (
          <div className="ua-v2-results" data-testid="analysis-results" aria-busy={updating}>
            {!data.complete ? (
              <p className="ua-v2-status" role="status">
                Only part of this range was returned.
              </p>
            ) : null}
            {!updating && data.complete && filter.day && filter.slot && !data.totals.last_at ? (
              <p className="ua-v2-status" role="status">
                这一天没有这个时段的用量
              </p>
            ) : null}
            <AnalysisTotals data={data} filter={filter} />
            <V2Cumulative data={data} />
            <AnalysisComparison data={data} restricted={paymentRestricted(filter)} />
            <AnalysisV2Items data={data} filter={groupFilter} label={name} toggle={toggle} />
            <V2Hosts
              data={data}
              filter={effectiveFilter}
              hosts={hosts}
              onHost={(key) => toggle("host", key)}
            />
            <AnalysisV2Shares data={data} filter={filter} />
            <V2Composition data={data} />
            <AnalysisSection id="failures" title={analysisSections.failures} folded>
              <p>
                {percent(v2Failure(data.totals))}{" "}
                <span className="usage-caption">
                  {v2Requests(data.totals.failed)} failed / {v2Requests(data.totals.requests)}{" "}
                  requests
                </span>
              </p>
              <FailureBreakdown data={data} filter={groupFilter} label={name} />
            </AnalysisSection>
            <V2Heatmap
              data={data}
              selected={filter.slot}
              onSlot={(slot) => change({ ...filter, slot: filter.slot === slot ? null : slot })}
            />
            <AnalysisSection id="payback" title={analysisSections.payback} folded>
              {paymentRestricted(filter) ? (
                <PaymentUnknown />
              ) : (
                <V2Payback
                  data={data}
                  filter={{ ...effectiveFilter, group: resultGroup }}
                  names={accounts}
                />
              )}
            </AnalysisSection>
            <AnalysisSection id="remaining" title={analysisSections.remaining} folded>
              {paymentRestricted(filter) ? (
                <AnalysisUnknown reason="Closing observations are only available by account and date." />
              ) : (
                <V2QuotaClosings filter={effectiveFilter} names={accounts} />
              )}
            </AnalysisSection>
            <AnalysisSection id="savings" title={analysisSections.savings} folded>
              <AnalysisUnknown reason="No cache savings projection is available." />
            </AnalysisSection>
          </div>
        ) : null}
      </AnalysisSectionState>
    </UsageBoardFrame>
  );
}

function PaymentUnknown() {
  return (
    <div className="ua-unknown">
      <strong>未知</strong>
      <small>{paymentRestriction}</small>
    </div>
  );
}

function percent(value: number | null) {
  return value === null ? "Unknown" : `${formatRingPercent(value)}%`;
}

function FailureBreakdown({
  data,
  filter,
  label,
}: {
  data: UsageAnalysisV2;
  filter: PageFilter;
  label: (dimension: FilterDimension, key: string) => string;
}) {
  const active = filter[filter.group];
  const rows = data.groups
    .flatMap((item) => {
      const rate = v2Failure(item);
      if (
        rate === null ||
        typeof item.key !== "string" ||
        (active.length && !active.includes(item.key))
      )
        return [];
      return [{ key: item.key, rate }];
    })
    .sort((left, right) => right.rate - left.rate)
    .slice(0, 5);
  return rows.length ? (
    <dl className="ua-v2-metrics">
      {rows.map(({ key, rate }) => (
        <div key={key}>
          <dt>{label(filter.group, key)}</dt>
          <dd>{percent(rate)}</dd>
        </div>
      ))}
    </dl>
  ) : null;
}

function delta(current: number | null, previous: number | null | undefined) {
  if (current === null || previous == null || previous <= 0) return "Previous period · —";
  const change = (current / previous - 1) * 100;
  return `Previous period · ${change > 0 ? "+" : ""}${percent(change)}`;
}

function comparable(value: AnalysisValue, previous: AnalysisValue | undefined) {
  return previous?.currency === value.currency ? exactCost(previous) : null;
}

function AnalysisComparison({ data, restricted }: { data: UsageAnalysisV2; restricted: boolean }) {
  const now = data.totals;
  const previous = data.complete ? data.previous?.totals : undefined;
  const oldApi = comparable(now.api_value, previous?.api_value);
  const newApi = exactCost(now.api_value);
  const oldPaid = comparable(now.paid, previous?.paid);
  const newPaid = exactCost(now.paid);
  const sameUsage =
    oldApi !== null && newApi !== null && previous?.tokens && now.tokens !== null
      ? newApi - now.tokens * (oldApi / previous.tokens)
      : null;
  const sameSpend =
    !restricted && oldPaid && newPaid && previous?.tokens && now.tokens !== null
      ? (now.tokens / newPaid / (previous.tokens / oldPaid) - 1) * 100
      : null;
  return (
    <AnalysisSection id="compare" title={analysisSections.compare} folded>
      <dl className="ua-v2-metrics">
        <div>
          <dt>Same usage · at API price</dt>
          <dd>
            {sameUsage === null
              ? "Unknown"
              : `${sameUsage < 0 ? "Less" : sameUsage > 0 ? "More" : "Same"} · ${formatUsageValue(Math.abs(sameUsage), now.api_value.currency)}`}
          </dd>
        </div>
        <div>
          <dt>Same spend · tokens per amount paid</dt>
          <dd>
            {restricted ? (
              <PaymentUnknown />
            ) : sameSpend === null ? (
              "Unknown"
            ) : (
              `${sameSpend > 0 ? "+" : ""}${percent(sameSpend)}`
            )}
          </dd>
        </div>
      </dl>
    </AnalysisSection>
  );
}

function AnalysisTotals({ data, filter }: { data: UsageAnalysisV2; filter: PageFilter }) {
  const totals = data.totals;
  const previous = data.complete ? data.previous?.totals : undefined;
  const restricted = paymentRestricted(filter);
  return (
    <AnalysisSection id="totals" title={analysisSections.totals}>
      <div className="ua-kpis">
        <div className="ua-kpi">
          <span>At API price</span>
          <strong>{analysisValueText(totals.api_value)}</strong>
          <small>
            {delta(exactCost(totals.api_value), comparable(totals.api_value, previous?.api_value))}
          </small>
        </div>
        <div className="ua-kpi">
          <span>Paid</span>
          {restricted ? (
            <PaymentUnknown />
          ) : (
            <>
              <strong>{analysisValueText(totals.paid)}</strong>
              <small>
                {delta(exactCost(totals.paid), comparable(totals.paid, previous?.paid))}
              </small>
            </>
          )}
        </div>
        <div className="ua-kpi">
          <span>Token · {v2Requests(totals.requests)} requests</span>
          <strong>{v2Tokens(totals.tokens)}</strong>
          <small>{delta(totals.tokens, previous?.tokens)}</small>
        </div>
        <div className="ua-kpi">
          <span>Payback</span>
          {restricted ? (
            <PaymentUnknown />
          ) : (
            <>
              <strong>{analysisValueText(totals.payback, "ratio")}</strong>
              <small>{delta(exactCost(totals.payback), exactCost(previous?.payback))}</small>
            </>
          )}
        </div>
      </div>
    </AnalysisSection>
  );
}
