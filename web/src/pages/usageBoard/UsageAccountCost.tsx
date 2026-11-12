import { useState } from "react";
import { useSearchParams } from "@/lib/routing";
import { useUsageAccountActivity } from "./usageActivityData";
import type { UsageAccountView } from "./usageBoardData";
import { UsageCostChart } from "./UsageCostChart";
import { usageFigure } from "./usageAmountData";
import { formatFullDate } from "./usageBoardFormat";
import { UsageName } from "./UsageName";

export function UsageAccountCost({ account }: { account: UsageAccountView }) {
  const [selected, setSelected] = useState(1);
  const [search] = useSearchParams();
  const focus = search.get("focus");
  const today = useUsageAccountActivity(account.id, focus, true, { key: "today" });
  const week = useUsageAccountActivity(account.id, focus, true, { key: "d7" });
  const period = useUsageAccountActivity(account.id, focus, true, { key: "period" });
  const reads = [today, week, period];
  const amounts = reads.map((read) =>
    read.data?.accounts.find((item) => item.account === account.id),
  );
  const queryState = reads[selected];
  const own = amounts[selected];
  const error = queryState.error;
  return (
    <>
      <section
        aria-label="Cost"
        className="usage-detail-cost rounded-[11px] border border-border bg-card px-3.5 py-3"
      >
        <div className="flex items-center justify-between gap-2">
          <h2 className="text-sm font-semibold">Cost</h2>
          <span className="text-xs text-muted-foreground">Reported cost</span>
        </div>
        {focus ? (
          <UsageName className="text-xs text-muted-foreground">{`${focus} · Pool usage only`}</UsageName>
        ) : null}
        <div className="usage-detail-cells" role="group" aria-label="Cost time range">
          {["Today", "Last 7 days", "This period"].map((label, index) => {
            const read = reads[index];
            const amount = amounts[index];
            return (
              <button
                key={label}
                type="button"
                aria-pressed={selected === index}
                onClick={() => setSelected(index)}
              >
                <span>{label}</span>
                <b>
                  {read.error
                    ? "Unavailable"
                    : amount
                      ? `${amount.mark ?? ""}${usageFigure(amount.totals, false)}`
                      : "—"}
                </b>
              </button>
            );
          })}
        </div>
        <p className="text-xs text-muted-foreground">API price · Not available yet</p>
        {error ? (
          <p role="alert" className="text-xs text-destructive">
            Cost could not be read
          </p>
        ) : queryState.isPending ? (
          <p className="text-xs text-muted-foreground">Reading cost…</p>
        ) : own ? (
          <>
            <p className="text-xs text-muted-foreground">
              {formatFullDate(own.span.counted_from)} – {formatFullDate(own.span.until)}
            </p>
            <UsageCostChart
              accounts={[own]}
              tokens={false}
              now={queryState.data?.generated_at ?? Date.now() / 1000}
              period={selected === 2}
            />
          </>
        ) : (
          <p className="text-xs text-muted-foreground">
            {focus ? "Not reported · No proven usage for this pool." : "No usage in this range."}
          </p>
        )}
      </section>
      <details className="rounded-[11px] border border-border bg-card px-3.5 text-sm">
        <summary className="cursor-pointer py-3 font-semibold">Cost breakdown</summary>
        {own?.models?.length ? (
          own.models.map((model) => (
            <div
              key={model.model}
              className="flex min-w-0 items-center gap-3 border-t border-border py-2 text-xs"
            >
              <UsageName className="flex-1">{model.model}</UsageName>
              <span className="shrink-0 tabular-nums">{usageFigure(model, false)}</span>
            </div>
          ))
        ) : (
          <p className="pb-3 text-xs text-muted-foreground">No model usage in this range.</p>
        )}
      </details>
    </>
  );
}
