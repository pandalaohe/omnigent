import { Link } from "@/lib/routing";
import { boardPath } from "@/lib/boardFrame";
import { useUsageContextPreferences } from "@/hooks/useUsageContextPreferences";
import { analysisValueText, useUsageAnalysis, type AnalysisValue } from "./usageAnalysisData";
import { decimalDisplay } from "./usagePricingData";
import type { UsageRange } from "./UsageRangeControl";

export function moneyText(value?: AnalysisValue, ratio = false) {
  if (!value || value.state === "unknown" || value.low === null) return "Unknown";
  const low = value.formatted?.low ?? decimalDisplay(value.low, ratio ? 3 : 2, ratio);
  const high =
    value.high !== null
      ? (value.formatted?.high ?? decimalDisplay(value.high, ratio ? 3 : 2, ratio))
      : null;
  return analysisValueText({ ...value, formatted: { low, high } }, ratio ? "ratio" : "money");
}

export function UsageAccountMoney({
  accountId,
  range,
  compact = false,
}: {
  accountId: string;
  range?: UsageRange;
  compact?: boolean;
}) {
  const board = useUsageContextPreferences().board;
  const query = useUsageAnalysis(range ?? board.range ?? { key: "d7" });
  const money = query.data?.pricing?.account_money?.[accountId];
  if (money?.show_money === false) return null;
  const title = compact ? "usage-caption px-2 pb-2" : "usage-card usage-detail-money";
  if (!money?.cost_class)
    return (
      <div className={title}>
        Money · Unknown ·{" "}
        <Link
          onClick={(event) => event.stopPropagation()}
          to={boardPath(`/usage/board/${encodeURIComponent(accountId)}/settings#usage-pricing`)}
        >
          Set pricing
        </Link>
      </div>
    );
  const figures =
    money.cost_class === "metered"
      ? [[money.estimated ? "Spend · estimated" : "Spend", moneyText(money.spend)]]
      : [
          ["At API price", moneyText(money.api_value)],
          ["Paid", moneyText(money.paid)],
          ["Payback", moneyText(money.payback, true)],
        ];
  return (
    <dl className={title} aria-label="Account money">
      {figures.map(([label, value]) => (
        <div className="flex flex-wrap justify-between gap-x-2" key={label}>
          <dt>{label}</dt>
          <dd className="tabular-nums">{value}</dd>
        </div>
      ))}
    </dl>
  );
}
