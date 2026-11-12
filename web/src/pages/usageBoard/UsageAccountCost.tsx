import { useState } from "react";
import { useSearchParams } from "@/lib/routing";
import { useUsageAccountActivity } from "./usageActivityData";
import type { UsageAccountView } from "./usageBoardData";
import { UsageCostChart } from "./UsageCostChart";
import { usageFigure } from "./usageAmountData";
import { formatFullDate } from "./usageBoardFormat";
import { UsageName } from "./UsageName";
import { FigureText } from "./UsageFigure";

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
      <section aria-label="Cost" className="usage-card usage-detail-cost">
        <div className="usage-card-head">
          <h2 className="usage-card-title">Cost</h2>
          <span className="usage-caption">Reported cost</span>
        </div>
        {focus ? (
          <UsageName className="usage-caption">{`${focus} · Pool usage only`}</UsageName>
        ) : null}
        <div className="usage-detail-cells" role="group" aria-label="Cost time range">
          {["Today", "Last 7 days", "This period"].map((label, index) => {
            const read = reads[index];
            const amount = amounts[index];
            return (
              <button
                key={label}
                type="button"
                aria-label={
                  amount && !read.error
                    ? `${label} ${amount.mark ?? ""}${usageFigure(amount.totals, false)}`
                    : undefined
                }
                aria-pressed={selected === index}
                onClick={() => setSelected(index)}
              >
                <span>{label}</span>
                <b>
                  {read.error ? (
                    "Unavailable"
                  ) : amount ? (
                    <>
                      {amount.mark ? <span className="usage-approx">{amount.mark}</span> : null}
                      <FigureText text={usageFigure(amount.totals, false, true)} />
                    </>
                  ) : (
                    "—"
                  )}
                </b>
              </button>
            );
          })}
        </div>
        {error ? (
          <p role="alert" className="usage-caption text-destructive">
            Cost could not be read
          </p>
        ) : queryState.isPending ? (
          <p className="usage-caption">Reading cost…</p>
        ) : own ? (
          <>
            <p className="usage-caption">
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
          <p className="usage-caption">
            {focus ? "Not reported · No proven usage for this pool." : "No usage in this range."}
          </p>
        )}
        <p className="usage-caption usage-card-note">API price · Not available yet</p>
      </section>
      <details className="usage-card usage-fold">
        <summary>Cost breakdown</summary>
        {own?.models?.length ? (
          own.models.map((model) => (
            <div key={model.model} className="usage-row">
              <UsageName className="flex-1">{model.model}</UsageName>
              <FigureText className="usage-row-figure" text={usageFigure(model, false)} />
            </div>
          ))
        ) : (
          <p className="usage-caption">No model usage in this range.</p>
        )}
      </details>
    </>
  );
}
