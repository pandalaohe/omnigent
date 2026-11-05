import { Link, useParams } from "@/lib/routing";
import { useUsageContextPreferences } from "@/hooks/useUsageContextPreferences";
import { UsageBoardFrame } from "./UsageBoardFrame";
import { UsageRangeControl } from "./UsageRangeControl";
import { useUsageOverview } from "./usageBoardData";
import { useUsageIssues } from "./usageActivityData";
import { formatFullDate } from "./usageBoardFormat";

const SOURCE_LABELS: Record<string, string> = {
  rejected: "Report rejected",
  read_failed: "Source read failed",
  silent: "No report for over an hour",
};

export function UsageIssuesPage() {
  const { accountId } = useParams();
  const board = useUsageContextPreferences().board;
  const overview = useUsageOverview(true);
  const query = useUsageIssues(accountId);
  const names = new Map(
    overview.data?.accounts.map((account) => [account.id, account.display_name]),
  );
  const visible = (id: string) => (accountId ? id === accountId : !board.hidden.includes(id));
  const failures = query.data?.failures.filter((item) => visible(item.account)) ?? [];
  const unpriced = query.data?.unpriced_models.filter((item) => visible(item.account)) ?? [];
  const reports = query.data?.source_reports.filter((item) => visible(item.account)) ?? [];
  const title = accountId ? `${names.get(accountId) ?? "Account"} · Issues` : "Issues";
  function accountLink(id: string) {
    return (
      <Link
        className="font-medium underline underline-offset-2"
        to={`/usage/board/${encodeURIComponent(id)}`}
      >
        {names.get(id) ?? id}
      </Link>
    );
  }
  return (
    <UsageBoardFrame
      title={title}
      back={{
        to: accountId ? `/usage/board/${encodeURIComponent(accountId)}` : "/usage/board/cost",
        label: accountId ? (names.get(accountId) ?? "Account") : "Cost",
      }}
      loading={query.isPending}
      error={query.error}
      actions={<UsageRangeControl />}
    >
      <div className="grid min-w-0 gap-3 lg:grid-cols-3">
        <section
          className="min-w-0 rounded-[11px] border border-border bg-card p-3.5"
          aria-label="Failures"
        >
          <h2 className="mb-2 text-sm font-semibold">Failures</h2>
          {!failures.length ? (
            <p className="text-xs text-muted-foreground">No reported failures in this range.</p>
          ) : (
            failures.map((item) => (
              <div key={item.account} className="mb-3 text-xs last:mb-0">
                {accountLink(item.account)}
                <p className="my-1 tabular-nums">
                  {item.failed} of {item.requests} requests · {(item.failure_pct ?? 0).toFixed(1)}%
                </p>
                <dl className="text-muted-foreground">
                  {Object.entries(item.reasons).map(([reason, count]) => (
                    <div key={reason} className="flex gap-3">
                      <dt className="min-w-0 flex-1 break-words">{reason}</dt>
                      <dd>{count}</dd>
                    </div>
                  ))}
                </dl>
              </div>
            ))
          )}
        </section>
        <section
          className="min-w-0 rounded-[11px] border border-border bg-card p-3.5"
          aria-label="Unpriced models"
        >
          <h2 className="mb-2 text-sm font-semibold">Unpriced models</h2>
          {!unpriced.length ? (
            <p className="text-xs text-muted-foreground">No unpriced usage in this range.</p>
          ) : (
            unpriced.map((item) => (
              <div key={`${item.account}:${item.model}`} className="mb-3 text-xs last:mb-0">
                {accountLink(item.account)}
                <p className="my-1 break-all text-muted-foreground">
                  {item.model || "Unknown model"}
                </p>
                <p>{item.unpriced} unpriced rows</p>
              </div>
            ))
          )}
        </section>
        <section
          className="min-w-0 rounded-[11px] border border-border bg-card p-3.5"
          aria-label="Source reports"
        >
          <h2 className="mb-2 text-sm font-semibold">Source reports</h2>
          {!reports.length ? (
            <p className="text-xs text-muted-foreground">No source issues in this range.</p>
          ) : (
            reports.map((item) => (
              <div
                key={`${item.account}:${item.at}:${item.kind}`}
                className="mb-3 text-xs last:mb-0"
              >
                {accountLink(item.account)}
                <p className="my-1">{SOURCE_LABELS[item.kind] ?? item.kind}</p>
                <p className="text-muted-foreground">{formatFullDate(item.at)}</p>
              </div>
            ))
          )}
          {query.data?.price_source?.map((item) => (
            <div key={`price:${item.at}:${item.error}`} className="mt-3 text-xs">
              <p className="font-medium">Price source</p>
              <p className="my-1">{item.error}</p>
              <p className="text-muted-foreground">{formatFullDate(item.at)}</p>
            </div>
          ))}
        </section>
      </div>
    </UsageBoardFrame>
  );
}
