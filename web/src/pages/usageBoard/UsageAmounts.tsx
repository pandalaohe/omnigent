import { boardPath } from "@/lib/boardFrame";
import { Link } from "@/lib/routing";
import { ArrowDownRightIcon, ArrowUpRightIcon, HistoryIcon, TriangleAlertIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { patchUsageBoardPreferences } from "@/lib/usageContextPreferences";
import { useUsageContextPreferences } from "@/hooks/useUsageContextPreferences";
import { cn } from "@/lib/utils";
import type { UsageAccountActivity, UsageAmount } from "./usageActivityData";
import type { UsageAccountView } from "./usageBoardData";
import { formatFullDate, formatMoney } from "./usageBoardFormat";
import { amountTip, boardAmount, sumUsage, usageFigure } from "./usageAmountData";
import { UsageTip, UsageTipButton } from "./UsageTip";
import { UsageRangeControl } from "./UsageRangeControl";
import { UsageCostChart } from "./UsageCostChart";

export function UsageUnitControl() {
  const tokens = useUsageContextPreferences().board.unit === "tokens";
  return (
    <div
      className="inline-flex items-center rounded-md bg-muted/40 p-0.5"
      role="group"
      aria-label="Usage measure"
    >
      {(["money", "tokens"] as const).map((unit) => (
        <Button
          key={unit}
          size="sm"
          variant={tokens === (unit === "tokens") ? "secondary" : "ghost"}
          className="min-h-11 px-2 text-xs md:min-h-[30px] md:h-[30px]"
          aria-pressed={tokens === (unit === "tokens")}
          onClick={() => patchUsageBoardPreferences({ unit })}
        >
          {unit === "money" ? "Money" : "Tokens"}
        </Button>
      ))}
    </div>
  );
}

export function UsageDates({
  amount,
  account,
}: {
  amount: UsageAmount | null;
  account?: UsageAccountView;
}) {
  const range = useUsageContextPreferences().board.range?.key ?? "d7";
  return (
    <span className="inline-flex min-w-0 flex-wrap items-center gap-1 text-xs text-muted-foreground">
      {range === "period" && !account
        ? "Each account's own"
        : amount
          ? `${formatFullDate(amount.span.requested_from)} – ${formatFullDate(amount.span.until)}`
          : "No usage in this range"}
      {range === "period" && account?.period?.moved_from != null ? (
        <UsageTipButton
          label="Reset period moved"
          model={{
            title: "Reset period moved",
            rows: [
              { label: "Previous start", value: formatFullDate(account.period.moved_from) },
              { label: "New start", value: formatFullDate(account.period.start) },
            ],
          }}
        >
          <HistoryIcon className="size-3.5" aria-hidden="true" />
        </UsageTipButton>
      ) : null}
    </span>
  );
}

export function UsageAmountFigure({
  amount,
  tokens,
  total = false,
  tapOpens = false,
}: {
  amount: UsageAmount;
  tokens: boolean;
  total?: boolean;
  tapOpens?: boolean;
}) {
  const figure = (
    <>
      {!tokens ? amount.mark : null}
      {usageFigure(amount.totals, tokens)}
    </>
  );
  return (
    <UsageTip model={amountTip(amount, total)} tapOpens={tapOpens}>
      {tapOpens ? (
        <button
          type="button"
          className="min-h-11 rounded whitespace-nowrap tabular-nums focus-visible:outline-2 focus-visible:outline-ring"
        >
          {figure}
        </button>
      ) : (
        <span className="whitespace-nowrap tabular-nums">{figure}</span>
      )}
    </UsageTip>
  );
}

export function UsageSummary({ accounts }: { accounts: UsageAccountActivity[] }) {
  const tokens = useUsageContextPreferences().board.unit === "tokens";
  const amount = boardAmount(accounts, false, tokens);
  const buckets = amount?.series.slice(-24) ?? [];
  const max = Math.max(1, ...buckets.map((point) => (tokens ? point.tokens : point.cost_usd) ?? 0));
  return (
    <section
      aria-label="Usage summary"
      className="mb-3 flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 rounded-[11px] border border-border bg-card px-3 py-2"
    >
      <Link
        to={boardPath("/usage/board/cost")}
        className="inline-flex min-h-11 items-center gap-2 text-xl font-semibold md:min-h-[30px]"
      >
        {amount ? <UsageAmountFigure amount={amount} tokens={tokens} total /> : "—"}
        <span aria-hidden="true" className="text-xs text-muted-foreground">
          ›
        </span>
      </Link>
      <div className="flex h-7 w-24 max-w-24 items-end gap-0.5 overflow-hidden" aria-hidden="true">
        {buckets.map((point) => (
          <span
            key={point.at}
            className="min-w-0 flex-1 rounded-t-sm bg-info/25"
            style={{ height: `${(100 * ((tokens ? point.tokens : point.cost_usd) ?? 0)) / max}%` }}
          />
        ))}
      </div>
      <UsageRangeControl />
      <UsageDates amount={amount} />
      <div className="ml-auto">
        <UsageUnitControl />
      </div>
    </section>
  );
}

export function UsageCostCard({
  accounts,
  account,
  now,
  fee,
}: {
  accounts: UsageAccountActivity[];
  account?: UsageAccountView;
  now: number;
  fee?: number;
}) {
  const board = useUsageContextPreferences().board;
  const tokens = board.unit === "tokens";
  const amount = account ? (accounts[0] ?? null) : boardAmount(accounts, false, tokens);
  const previous = account ? accounts[0]?.previous : boardAmount(accounts, true, tokens);
  const failures = sumUsage(accounts.map((item) => item.totals));
  const issuesPath = account
    ? `/usage/board/${encodeURIComponent(account.id)}/issues`
    : "/usage/board/issues";
  const efficiency = sumUsage(accounts.map((item) => item.totals.metered ?? item.totals));
  const previousEfficiency = sumUsage(
    accounts.flatMap((item) =>
      item.previous ? [item.previous.totals.metered ?? item.previous.totals] : [],
    ),
  );
  const perMillion = efficiency.per_million;
  const oldPerMillion = previousEfficiency.per_million;
  return (
    <section
      aria-label="Cost"
      className="min-w-0 rounded-[11px] border border-border bg-card px-3.5 py-3"
    >
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-sm font-semibold">{tokens ? "Tokens" : "Cost"}</h2>
        <UsageRangeControl />
        <UsageUnitControl />
      </div>
      <UsageDates amount={amount} account={account} />
      <div className="my-2 flex flex-wrap items-baseline gap-x-4 gap-y-1">
        <span className="text-3xl font-semibold tracking-tight">
          {amount ? (
            <UsageAmountFigure amount={amount} tokens={tokens} total={!account} tapOpens />
          ) : (
            "—"
          )}
        </span>
        {previous &&
        (tokens ? previous.totals.tokens !== null : previous.totals.cost_usd !== null) ? (
          <span className="text-xs text-muted-foreground">
            Previous {usageFigure(previous.totals, tokens)}
          </span>
        ) : null}
      </div>
      {perMillion != null && amount ? (
        <UsageTip
          model={{
            title: "Per 1M tokens",
            rows: [
              { label: "Now", value: formatMoney(perMillion) },
              ...(oldPerMillion == null
                ? []
                : [{ label: "Previous", value: formatMoney(oldPerMillion) }]),
              ...(efficiency.cost_coverage_pct !== null && efficiency.cost_coverage_pct < 100
                ? [
                    {
                      label: "Covers",
                      value: `${Math.round(efficiency.cost_coverage_pct)}% of cost`,
                    },
                  ]
                : []),
            ],
          }}
          tapOpens
        >
          <button
            type="button"
            className="mb-2 inline-flex min-h-11 items-center gap-1 text-xs text-muted-foreground md:min-h-6"
          >
            Per 1M tokens <b className="font-medium text-foreground">{formatMoney(perMillion)}</b>
            {oldPerMillion != null && perMillion !== oldPerMillion ? (
              perMillion > oldPerMillion ? (
                <ArrowUpRightIcon
                  className="size-3.5 text-warning"
                  aria-label="Higher than previous"
                />
              ) : (
                <ArrowDownRightIcon
                  className="size-3.5 text-success"
                  aria-label="Lower than previous"
                />
              )
            ) : null}
          </button>
        </UsageTip>
      ) : null}
      {!tokens &&
      fee !== undefined &&
      amount?.totals.tokens_complete &&
      amount.totals.tokens &&
      board.range?.key === "d30" ? (
        <p className="mb-2 text-xs text-muted-foreground">
          Your fee works out at {formatMoney((fee / amount.totals.tokens) * 1e6)} per 1M tokens
        </p>
      ) : null}
      <UsageCostChart
        accounts={accounts}
        tokens={tokens}
        total={!account}
        period={board.range?.key === "period"}
        now={now}
        fee={fee}
      />
      {failures.requests !== null || failures.unpriced > 0 ? (
        <Link
          to={boardPath(issuesPath)}
          className="mt-2 flex min-h-11 flex-wrap items-center gap-x-3 gap-y-1 border-t border-border pt-2 text-xs text-muted-foreground"
        >
          <TriangleAlertIcon className="size-3.5" aria-hidden="true" />
          {failures.requests !== null ? (
            <>
              <span>{failures.requests.toLocaleString("en-US")} requests</span>
              <span>
                {failures.failed} failed · {(failures.failure_pct ?? 0).toFixed(1)}%
              </span>
            </>
          ) : null}
          {failures.unpriced > 0 ? <span>{failures.unpriced} unpriced</span> : null}
          <span className="ml-auto">Issues ›</span>
        </Link>
      ) : null}
    </section>
  );
}

export function UsageByModel({
  accounts,
  total = false,
}: {
  accounts: UsageAccountActivity[];
  total?: boolean;
}) {
  const tokens = useUsageContextPreferences().board.unit === "tokens";
  const models = new Map<string, NonNullable<UsageAccountActivity["models"]>>();
  for (const account of accounts)
    for (const model of account.models ?? []) {
      const group = models.get(model.model) ?? [];
      group.push(model);
      models.set(model.model, group);
    }
  return (
    <details className="mt-3 rounded-[11px] border border-border bg-card px-3.5 text-sm">
      <summary className="cursor-pointer py-3 font-semibold">By model</summary>
      {!models.size ? (
        <p className="pb-3 text-xs text-muted-foreground">No model usage in this range.</p>
      ) : (
        [...models].map(([model, values]) => {
          const totals = sumUsage(
            values.map((value) => (total && !tokens ? (value.metered ?? value) : value)),
          );
          return (
            <div
              key={model}
              className="flex min-w-0 items-center gap-3 border-t border-border py-2 text-xs"
            >
              <span className="min-w-0 flex-1 break-all">{model}</span>
              <span className="shrink-0 tabular-nums">{usageFigure(totals, tokens)}</span>
            </div>
          );
        })
      )}
    </details>
  );
}

export function UsageErrorMark({ account }: { account: UsageAccountView }) {
  const mark = account.mark;
  if (!mark || account.freshness !== "fresh") return null;
  return (
    <UsageTipButton
      label={`${account.display_name} errors in the last 24 hours`}
      model={{
        title: "Errors · last 24 hours",
        chip: { label: mark.level ?? "notice", tone: mark.level === "notice" ? "warn" : "crit" },
        rows: [
          { label: "Requests", value: String(mark.requests ?? "—") },
          { label: "Failed", value: String(mark.failed ?? "—") },
          {
            label: "Failure rate",
            value: mark.rate === undefined ? "—" : `${mark.rate.toFixed(1)}%`,
          },
        ],
      }}
      className={cn("shrink-0", mark.level === "notice" ? "text-warning" : "text-destructive")}
    >
      <TriangleAlertIcon
        className={cn("size-4", mark.level === "down" && "fill-destructive stroke-background")}
        aria-hidden="true"
      />
    </UsageTipButton>
  );
}
