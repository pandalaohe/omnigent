import { Link } from "@/lib/routing";
import { useUsageContextPreferences } from "@/hooks/useUsageContextPreferences";
import { UsageBoardFrame } from "./UsageBoardFrame";
import { useUsageOverview } from "./usageBoardData";
import { useUsageActivity } from "./usageActivityData";
import { UsageAmountFigure, UsageByModel, UsageCostCard } from "./UsageAmounts";

export function UsageCostPage() {
  const board = useUsageContextPreferences().board;
  const overview = useUsageOverview(true);
  const activity = useUsageActivity();
  const shown =
    overview.data?.accounts.filter((account) => !board.hidden.includes(account.id)) ?? [];
  const accounts =
    activity.data?.accounts.filter((item) =>
      shown.some((account) => account.id === item.account),
    ) ?? [];
  return (
    <UsageBoardFrame
      title="Cost"
      back={{ to: "/usage/board", label: "Usage board" }}
      loading={activity.isPending || overview.isPending}
      error={activity.error ?? overview.error}
      actions={
        <Link
          className="inline-flex min-h-11 items-center text-xs text-muted-foreground"
          to="/usage"
        >
          Sessions ›
        </Link>
      }
    >
      <UsageCostCard accounts={accounts} now={activity.data?.generated_at ?? Date.now() / 1000} />
      <section
        className="mt-3 rounded-[11px] border border-border bg-card px-3.5 py-3"
        aria-label="Accounts"
      >
        <h2 className="mb-1 text-sm font-semibold">Accounts</h2>
        {!shown.length ? (
          <p className="py-3 text-sm text-muted-foreground">No accounts on the board.</p>
        ) : (
          shown.map((account) => {
            const amount = accounts.find((item) => item.account === account.id);
            return (
              <Link
                key={account.id}
                to={`/usage/board/${encodeURIComponent(account.id)}`}
                className="flex min-h-11 min-w-0 items-center gap-3 border-t border-border py-2 text-sm first:border-t-0"
              >
                <span className="min-w-0 flex-1 truncate">{account.display_name}</span>
                {amount ? (
                  <UsageAmountFigure amount={amount} tokens={board.unit === "tokens"} />
                ) : (
                  "—"
                )}
                <span aria-hidden="true" className="text-muted-foreground">
                  ›
                </span>
              </Link>
            );
          })
        )}
      </section>
      <UsageByModel accounts={accounts} total />
    </UsageBoardFrame>
  );
}
