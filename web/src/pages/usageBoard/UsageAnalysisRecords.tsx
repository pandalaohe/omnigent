import { useEffect, useMemo, useState } from "react";
import { ChevronDownIcon, ChevronRightIcon } from "lucide-react";
import { AnalysisSection } from "./UsageAnalysisPanel";
import { analysisSections } from "./UsageAnalysisRail";
import {
  analysisValueText,
  fetchUsageRequests,
  useUsageAnalysisHours,
  UsageRequestsHttpError,
  type AnalysisClasses,
  type AnalysisHour,
  type UsageAnalysisV2,
  type UsageRequestRow,
  type UsageRequestsPage,
} from "./usageAnalysisData";
import { v2Failure, v2Requests, v2Tokens } from "./UsageAnalysisAmounts";
import type { FilterDimension, PageFilter } from "./usageAnalysisFilters";
import { formatMoney, formatOverviewTokens, formatRingPercent } from "./usageBoardFormat";
import { UsageName } from "./UsageName";
import { UsageTip } from "./UsageTip";

const DAYS_PER_PAGE = 10;
const REQUESTS_PER_PAGE = 8;

const CLASS_COLORS: Record<string, string> = {
  input: "var(--usage-series)",
  cache_read: "var(--success)",
  cache_write: "var(--warning)",
  output: "var(--chart-4)",
};

function formatDayWeekday(day: string, tz: string, nowSec: number): string {
  const parts = day.split("-").map(Number);
  if (parts.length !== 3 || parts.some((part) => !Number.isFinite(part))) return "";
  const probe = new Date(Date.UTC(parts[0], parts[1] - 1, parts[2], 12, 0, 0));
  const weekday = new Intl.DateTimeFormat("en-US", { weekday: "short", timeZone: "UTC" }).format(
    probe,
  );
  const today = new Intl.DateTimeFormat("sv-SE", {
    timeZone: tz,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date(nowSec * 1000));
  return day === today ? `${weekday} · Today` : weekday;
}

function formatRequestTime(at: string, tz: string): string {
  const date = new Date(at);
  if (Number.isNaN(date.getTime())) return at;
  return new Intl.DateTimeFormat("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
    timeZone: tz,
  }).format(date);
}

function formatLatency(durationMs: number | null, ttftMs: number | null): string {
  if (durationMs === null && ttftMs === null) return "—";
  const fmt = (ms: number) => (ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${ms} ms`);
  if (durationMs !== null && ttftMs !== null) return `${fmt(durationMs)} (TTFT ${fmt(ttftMs)})`;
  return fmt((durationMs ?? ttftMs)!);
}

function formatRequestCost(costUsd: string | null): string {
  if (costUsd === null) return "—";
  const numeric = Number(costUsd);
  return Number.isFinite(numeric) ? formatMoney(numeric) : "—";
}

function totalRequestTokens(row: UsageRequestRow): number | null {
  const parts = [
    row.tokens.input,
    row.tokens.cache_read,
    row.tokens.cache_write,
    row.tokens.output,
  ];
  if (parts.every((value) => value === null)) return null;
  return parts.reduce<number>((sum, value) => sum + (value ?? 0), 0);
}

function tokenClassSummary(row: UsageRequestRow): string {
  const items: string[] = [];
  if (row.tokens.input != null) items.push(`In ${formatOverviewTokens(row.tokens.input)}`);
  if (row.tokens.cache_read != null && row.tokens.cache_read > 0)
    items.push(`CR ${formatOverviewTokens(row.tokens.cache_read)}`);
  if (row.tokens.cache_write != null && row.tokens.cache_write > 0)
    items.push(`CW ${formatOverviewTokens(row.tokens.cache_write)}`);
  if (row.tokens.output != null) items.push(`Out ${formatOverviewTokens(row.tokens.output)}`);
  if (row.tokens.reasoning != null && row.tokens.reasoning > 0)
    items.push(`Reas ${formatOverviewTokens(row.tokens.reasoning)}`);
  return items.join(" · ");
}

function ClassMiniBar({ classes }: { classes: AnalysisClasses }) {
  const segments: { key: string; tokens: number; color: string }[] = classes.items
    .filter((item) => item.tokens > 0)
    .map((item) => ({
      key: item.kind,
      tokens: item.tokens,
      color: CLASS_COLORS[item.kind] ?? "var(--usage-ink-3)",
    }));
  if (classes.unsplit_tokens > 0) {
    segments.push({
      key: "unsplit",
      tokens: classes.unsplit_tokens,
      color: "var(--usage-ink-3)",
    });
  }
  if (!segments.length) return <span className="ua-rec-bar-empty">—</span>;
  return (
    <span className="ua-rec-mix" aria-hidden="true">
      {segments.map((segment) => (
        <i key={segment.key} style={{ flex: `${segment.tokens} 0 0`, background: segment.color }} />
      ))}
    </span>
  );
}

export function V2Records({
  data,
  filter,
  label,
  onSelectDay,
}: {
  data: UsageAnalysisV2;
  filter: PageFilter;
  label: (dimension: FilterDimension, key: string) => string;
  onSelectDay: (day: string) => void;
}) {
  const filterKey = JSON.stringify({
    range: filter.range,
    from: filter.from,
    to: filter.to,
    account: filter.account,
    model: filter.model,
    host: filter.host,
    agent: filter.agent,
    slot: filter.slot,
    tz: data.tz,
  });

  const [state, setState] = useState<{
    filterKey: string;
    page: number;
    openDay: string | null;
    selectedHour: number | null;
  }>({
    filterKey,
    page: 0,
    openDay: null,
    selectedHour: null,
  });
  const [notConfiguredFromRequests, setNotConfiguredFromRequests] = useState(false);

  const effective =
    state.filterKey === filterKey
      ? state
      : { filterKey, page: 0, openDay: null, selectedHour: null };

  const days = data.days;
  const totalPages = Math.max(1, Math.ceil(days.length / DAYS_PER_PAGE));
  const page = Math.min(effective.page, totalPages - 1);
  const pageDays = days.slice(page * DAYS_PER_PAGE, page * DAYS_PER_PAGE + DAYS_PER_PAGE);
  const requestsConfigured =
    data.requests_configured === false ? false : !notConfiguredFromRequests;

  function goToPage(nextPage: number) {
    setState({
      filterKey,
      page: Math.max(0, Math.min(totalPages - 1, nextPage)),
      openDay: null,
      selectedHour: null,
    });
  }

  function toggleDay(day: string) {
    setState({
      filterKey,
      page,
      openDay: effective.openDay === day ? null : day,
      selectedHour: null,
    });
  }

  function selectHour(day: string, hourStart: number) {
    setState({
      filterKey,
      page,
      openDay: day,
      selectedHour: hourStart,
    });
  }

  return (
    <AnalysisSection id="records" title={analysisSections.records}>
      {days.length === 0 ? (
        <p className="usage-caption">
          {filter.range === "custom"
            ? "Daily records are available for ranges up to 92 days."
            : "No daily records in this range."}
        </p>
      ) : (
        <>
          <p className="usage-caption">
            {days.length} {days.length === 1 ? "day" : "days"} · Expand a day for hourly usage (
            {data.tz})
          </p>
          <div className="ua-rec-scroll">
            <div className="ua-rec-table" role="table" aria-label={analysisSections.records}>
              <div className="ua-rec-head" role="row">
                <span role="columnheader">Date</span>
                <span role="columnheader">Hours</span>
                <span role="columnheader">Requests</span>
                <span role="columnheader">Token</span>
                <span role="columnheader">At API price</span>
                <span role="columnheader">Composition</span>
                <span role="columnheader">Failed</span>
                <span role="columnheader" className="sr-only">
                  Expand
                </span>
              </div>
              {pageDays.map((day) => {
                const open = effective.openDay === day.day;
                const chronologicalHours = [...day.hours].sort((a, b) => a.start - b.start);
                const maxHourTokens = Math.max(
                  1,
                  ...chronologicalHours.map((hour) => hour.tokens ?? 0),
                );
                const failurePct = v2Failure(day);
                return (
                  <div key={day.day} className="ua-rec-day-group" data-day={day.day}>
                    <div
                      className="ua-rec-row"
                      role="row"
                      data-open={open || undefined}
                      data-selected={filter.day === day.day || undefined}
                    >
                      <span role="cell" className="ua-rec-date">
                        <strong>{day.day}</strong>
                        <small>{formatDayWeekday(day.day, data.tz, data.generated_at)}</small>
                      </span>
                      <span role="cell">
                        <div
                          className="ua-rec-strip"
                          role="group"
                          aria-label={`${day.day} hourly strip (${chronologicalHours.length} hours)`}
                          style={{
                            gridTemplateColumns: `repeat(${Math.max(1, chronologicalHours.length)}, minmax(0, 1fr))`,
                          }}
                        >
                          {chronologicalHours.map((hour) => {
                            const tokens = hour.tokens ?? 0;
                            const hasFailure =
                              hour.requests != null &&
                              hour.requests > 0 &&
                              hour.failed != null &&
                              hour.failed / hour.requests > 0.08;
                            const isSelected = open && effective.selectedHour === hour.start;
                            const heightPx = tokens
                              ? Math.max(3, Math.round((tokens / maxHourTokens) * 22))
                              : 2;
                            return (
                              <button
                                key={hour.start}
                                type="button"
                                className="ua-rec-strip-bar"
                                data-empty={!tokens || undefined}
                                data-failed={hasFailure || undefined}
                                data-selected={isSelected || undefined}
                                style={{ height: `${heightPx}px` }}
                                title={`${hour.label} · ${v2Tokens(hour.tokens)} tokens · ${v2Requests(hour.requests)} req`}
                                aria-label={`${day.day} ${hour.label} · ${v2Tokens(hour.tokens)} tokens`}
                                onClick={() => selectHour(day.day, hour.start)}
                              />
                            );
                          })}
                        </div>
                      </span>
                      <span role="cell" className="ua-rec-num">
                        {v2Requests(day.requests)}
                      </span>
                      <span role="cell" className="ua-rec-num">
                        {v2Tokens(day.tokens)}
                      </span>
                      <span role="cell" className="ua-rec-num">
                        {analysisValueText(day.api_value)}
                      </span>
                      <span role="cell">
                        <ClassMiniBar classes={day.classes} />
                      </span>
                      <span role="cell" className="ua-rec-num">
                        {v2Requests(day.failed)}
                        {failurePct !== null ? (
                          <small>{formatRingPercent(failurePct)}%</small>
                        ) : null}
                      </span>
                      <span role="cell" className="ua-rec-toggle-cell">
                        <button
                          type="button"
                          className="ua-rec-toggle"
                          aria-expanded={open}
                          aria-label={open ? `Collapse ${day.day}` : `Expand ${day.day}`}
                          onClick={() => toggleDay(day.day)}
                        >
                          {open ? (
                            <ChevronDownIcon size={15} aria-hidden="true" />
                          ) : (
                            <ChevronRightIcon size={15} aria-hidden="true" />
                          )}
                        </button>
                      </span>
                    </div>
                    {open ? (
                      <DayHoursPanel
                        day={day.day}
                        tz={data.tz}
                        filter={filter}
                        label={label}
                        selectedHour={effective.selectedHour}
                        requestsConfigured={requestsConfigured}
                        onNotConfigured={() => setNotConfiguredFromRequests(true)}
                        onSelectDay={onSelectDay}
                      />
                    ) : null}
                  </div>
                );
              })}
            </div>
          </div>
          {totalPages > 1 ? (
            <div className="ua-rec-pager" aria-label="Daily records pagination">
              <span className="usage-caption">
                Page {page + 1} / {totalPages} · {DAYS_PER_PAGE} days per page
              </span>
              <div className="ua-rec-pager-actions">
                <button type="button" disabled={page === 0} onClick={() => goToPage(page - 1)}>
                  Previous
                </button>
                <button
                  type="button"
                  disabled={page >= totalPages - 1}
                  onClick={() => goToPage(page + 1)}
                >
                  Next
                </button>
              </div>
            </div>
          ) : null}
        </>
      )}
    </AnalysisSection>
  );
}

function DayHoursPanel({
  day,
  tz,
  filter,
  label,
  selectedHour,
  requestsConfigured,
  onNotConfigured,
  onSelectDay,
}: {
  day: string;
  tz: string;
  filter: PageFilter;
  label: (dimension: FilterDimension, key: string) => string;
  selectedHour: number | null;
  requestsConfigured: boolean;
  onNotConfigured: () => void;
  onSelectDay: (day: string) => void;
}) {
  const query = useUsageAnalysisHours(filter, day, true);
  const [openRequestHour, setOpenRequestHour] = useState<number | null>(null);

  const serverDisabled = query.data?.requests_configured === false;
  const allowRequests = requestsConfigured && !serverDisabled;
  const hours = useMemo(() => {
    const raw = query.data?.hours ?? [];
    return [...raw].sort((a, b) => b.start - a.start);
  }, [query.data?.hours]);

  function topSummary(hour: AnalysisHour) {
    const host = hour.top_host === null ? "—" : label("host", hour.top_host);
    const agent = hour.top_agent === null ? "—" : label("agent", hour.top_agent);
    const model = hour.top_model === null ? "—" : label("model", hour.top_model);
    return `${host} · ${agent} · ${model}`;
  }

  return (
    <div className="ua-rec-hours" data-testid={`day-hours-${day}`}>
      {query.isPending ? (
        <p className="usage-caption" role="status">
          Reading hourly records…
        </p>
      ) : query.error ? (
        <p className="usage-caption" role="alert">
          Hourly records could not be read. {query.error.message}
        </p>
      ) : hours.length === 0 ? (
        <p className="usage-caption">No hourly usage recorded for {day}.</p>
      ) : (
        <div className="ua-rec-hours-list" role="table" aria-label={`Hourly usage for ${day}`}>
          <div className="ua-rec-hour-head" role="row">
            <span role="columnheader">Hour</span>
            <span role="columnheader">Requests</span>
            <span role="columnheader">Token</span>
            <span role="columnheader">At API price</span>
            <span role="columnheader">Top Host · Agent · Model</span>
            <span role="columnheader">Failed</span>
          </div>
          {hours.map((hour) => {
            const reqOpen = allowRequests && openRequestHour === hour.start;
            return (
              <div key={hour.start} className="ua-rec-hour-group" data-hour={hour.start}>
                <div
                  className="ua-rec-hour-row"
                  role="row"
                  data-selected={selectedHour === hour.start || undefined}
                >
                  <span role="cell" className="ua-rec-hour-label">
                    {hour.label}
                  </span>
                  <span role="cell" className="ua-rec-num">
                    {v2Requests(hour.requests)}
                  </span>
                  <span role="cell" className="ua-rec-num">
                    {v2Tokens(hour.tokens)}
                  </span>
                  <span role="cell" className="ua-rec-num">
                    {analysisValueText(hour.totals.api_value)}
                  </span>
                  <span role="cell" className="ua-rec-hour-top">
                    <UsageName>{topSummary(hour)}</UsageName>
                    {allowRequests ? (
                      <button
                        type="button"
                        className="ua-rec-req-btn"
                        aria-expanded={reqOpen}
                        onClick={() => setOpenRequestHour(reqOpen ? null : hour.start)}
                      >
                        逐条
                        {reqOpen ? (
                          <ChevronDownIcon size={13} aria-hidden="true" />
                        ) : (
                          <ChevronRightIcon size={13} aria-hidden="true" />
                        )}
                      </button>
                    ) : (
                      <UsageTip
                        model={{
                          title: "逐条",
                          rows: [{ label: "状态", value: "逐条读取没有配置" }],
                        }}
                        tapOpens
                      >
                        <span className="ua-rec-req-disabled">逐条读取没有配置</span>
                      </UsageTip>
                    )}
                  </span>
                  <span role="cell" className="ua-rec-num">
                    {v2Requests(hour.failed)}
                  </span>
                </div>
                {reqOpen ? (
                  <HourRequestsPanel
                    hour={hour.start}
                    tz={tz}
                    filter={filter}
                    label={label}
                    onNotConfigured={() => {
                      setOpenRequestHour(null);
                      onNotConfigured();
                    }}
                  />
                ) : null}
              </div>
            );
          })}
        </div>
      )}
      <div className="ua-rec-hours-foot">
        <span className="usage-caption">Hour is the finest aggregated unit</span>
        <button type="button" className="ua-rec-focus-day" onClick={() => onSelectDay(day)}>
          Filter above to {day}
        </button>
      </div>
    </div>
  );
}

function HourRequestsPanel({
  hour,
  tz,
  filter,
  label,
  onNotConfigured,
}: {
  hour: number;
  tz: string;
  filter: PageFilter;
  label: (dimension: FilterDimension, key: string) => string;
  onNotConfigured: () => void;
}) {
  const [cursorStack, setCursorStack] = useState<(string | null)[]>([null]);
  const [pageIndex, setPageIndex] = useState(0);
  const [attempt, setAttempt] = useState(0);
  const [loading, setLoading] = useState(true);
  const [pageData, setPageData] = useState<UsageRequestsPage | null>(null);
  const [error, setError] = useState<{
    kind: "unbound" | "other";
    message: string;
  } | null>(null);

  const cursor = cursorStack[pageIndex] ?? null;
  const scopeKey = JSON.stringify({
    hour,
    tz,
    account: filter.account,
    model: filter.model,
    host: filter.host,
    agent: filter.agent,
  });

  useEffect(() => {
    setCursorStack([null]);
    setPageIndex(0);
  }, [scopeKey]);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    fetchUsageRequests({
      hour,
      tz,
      filter,
      cursor,
      limit: REQUESTS_PER_PAGE,
      signal: controller.signal,
    })
      .then((result) => {
        if (controller.signal.aborted) return;
        setPageData(result);
        setLoading(false);
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted) return;
        if (err instanceof UsageRequestsHttpError) {
          if (err.status === 404 && err.detail === "not_configured") {
            onNotConfigured();
            return;
          }
          if (err.status === 504) {
            setError({ kind: "other", message: "逐条读取超时" });
            setLoading(false);
            return;
          }
          if (err.status === 404 && err.detail === "no_gateway_binding") {
            setError({
              kind: "unbound",
              message: "No gateway account is linked to this selection.",
            });
            setLoading(false);
            return;
          }
          setError({ kind: "other", message: err.detail });
          setLoading(false);
          return;
        }
        setError({
          kind: "other",
          message: err instanceof Error ? err.message : "Could not read requests",
        });
        setLoading(false);
      });
    return () => {
      controller.abort();
    };
    // filter arrays are tracked through scopeKey to avoid redundant fetches.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scopeKey, cursor, attempt]);

  function nextPage() {
    if (!pageData?.next_cursor) return;
    const nextCursor = pageData.next_cursor;
    setCursorStack((prev) => {
      const next = prev.slice(0, pageIndex + 1);
      next.push(nextCursor);
      return next;
    });
    setPageIndex((index) => index + 1);
  }

  function prevPage() {
    if (pageIndex === 0) return;
    setPageIndex((index) => index - 1);
  }

  return (
    <div className="ua-rec-reqs" data-testid={`hour-requests-${hour}`}>
      {!loading && !error && pageData?.state === "partial" ? (
        <p className="usage-caption" role="status">
          Gateway records for this hour are incomplete.
        </p>
      ) : null}
      {loading ? (
        <p className="usage-caption" role="status">
          Reading requests…
        </p>
      ) : error?.kind === "unbound" ? (
        <p className="usage-caption" role="status">
          {error.message}
        </p>
      ) : error ? (
        <div className="ua-rec-req-error" role="alert">
          <span>{error.message}</span>
          <button type="button" onClick={() => setAttempt((count) => count + 1)}>
            重试
          </button>
        </div>
      ) : pageData?.state === "expired" ? (
        <p className="usage-caption" role="status">
          Gateway records for this hour have expired.
        </p>
      ) : !pageData || pageData.rows.length === 0 ? (
        pageData?.state === "partial" ? null : (
          <p className="usage-caption">No gateway requests in this hour.</p>
        )
      ) : (
        <>
          <div className="ua-rec-req-table" role="table" aria-label="Per-request usage">
            <div className="ua-rec-req-head" role="row">
              <span role="columnheader">Time</span>
              <span role="columnheader">Host · Agent · Model</span>
              <span role="columnheader">Tokens</span>
              <span role="columnheader">Cost</span>
              <span role="columnheader">Latency</span>
              <span role="columnheader">Status</span>
            </div>
            {pageData.rows.map((row) => {
              const hostName = row.host ? label("host", row.host) : "—";
              const agentName = label("agent", row.agent ?? "");
              const modelName = row.model ? label("model", row.model) : "—";
              const total = totalRequestTokens(row);
              const split = tokenClassSummary(row);
              const statusText =
                row.status === "ok"
                  ? "OK"
                  : [row.status, row.http_status, row.error_class].filter(Boolean).join(" · ");
              return (
                <div key={row.id} className="ua-rec-req-row" role="row" data-request-id={row.id}>
                  <span role="cell">{formatRequestTime(row.at, tz)}</span>
                  <span role="cell" className="ua-rec-req-target">
                    <UsageName>{`${hostName} · ${agentName} · ${modelName}`}</UsageName>
                  </span>
                  <span role="cell" className="ua-rec-num" title={split || undefined}>
                    {v2Tokens(total)}
                    {split ? <small>{split}</small> : null}
                  </span>
                  <span role="cell" className="ua-rec-num">
                    {formatRequestCost(row.cost_usd)}
                  </span>
                  <span role="cell" className="ua-rec-num">
                    {formatLatency(row.duration_ms, row.ttft_ms)}
                  </span>
                  <span role="cell" className="ua-rec-req-status" data-status={row.status}>
                    {statusText}
                  </span>
                </div>
              );
            })}
          </div>
          <div className="ua-rec-pager" aria-label="Per-request pagination">
            <span className="usage-caption">
              Page {pageIndex + 1} · {REQUESTS_PER_PAGE} per page
            </span>
            <div className="ua-rec-pager-actions">
              <button type="button" disabled={pageIndex === 0} onClick={prevPage}>
                Previous
              </button>
              <button type="button" disabled={!pageData.next_cursor} onClick={nextPage}>
                Next
              </button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
