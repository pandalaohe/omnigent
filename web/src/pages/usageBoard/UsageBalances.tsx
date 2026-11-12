import type { UsageAccountView } from "./usageBoardData";
import { formatFullDate, formatUsageValue } from "./usageBoardFormat";
import { t } from "./usageText";
import { useUsageReadingPlacement } from "./useUsageReadingPlacement";
import { UsageName } from "./UsageName";

function BalanceCircle({ value, label }: { value: string; label?: string }) {
  const { circleRef, readingRef, outside } = useUsageReadingPlacement(value);
  const amount = (
    <span ref={readingRef} data-usage-amount className="usage-ring-reading">
      {value}
    </span>
  );
  return (
    <span
      className={`usage-reading-figure usage-balance-figure${label ? " usage-balance-detail" : ""}`}
      data-reading-outside={outside}
    >
      <span ref={circleRef} className="usage-balance-ring">
        {label ? (
          <UsageName data-usage-label className="usage-balance-label w-full">
            {label}
          </UsageName>
        ) : null}
        {!outside ? amount : null}
      </span>
      {outside ? amount : null}
    </span>
  );
}

/** Balances have their own reading time and never borrow a window's pace. */
export function UsageBalances({
  account,
  compact = false,
  tile = false,
}: {
  account: UsageAccountView;
  compact?: boolean;
  tile?: boolean;
}) {
  if (!account.balances?.length) return null;
  const stale = account.balance_freshness !== "fresh";
  if (tile)
    return (
      <div className="usage-tile-balance" data-testid="usage-balances">
        {account.balances.map((balance) => (
          <div key={balance.id} className="usage-balance-item">
            <BalanceCircle value={formatUsageValue(balance.remaining, balance.unit)} />
            <UsageName data-usage-label>{balance.label ?? t("balance")}</UsageName>
            {stale ? <span className="block text-muted-foreground">{t("balanceOld")}</span> : null}
          </div>
        ))}
      </div>
    );
  return (
    <div className="flex min-w-0 flex-wrap justify-center gap-2" data-testid="usage-balances">
      {account.balances.map((balance) => (
        <div
          key={balance.id}
          className="flex min-w-0 max-w-full flex-col items-center gap-0.5 text-center text-xs"
        >
          {compact ? (
            <span className="max-w-full break-all font-medium tabular-nums">
              <UsageName data-usage-label className="text-[10px] text-muted-foreground">
                {balance.label ?? t("balance")}
              </UsageName>{" "}
              <span data-usage-amount>{formatUsageValue(balance.remaining, balance.unit)}</span>
            </span>
          ) : (
            <BalanceCircle
              label={balance.label ?? t("balance")}
              value={formatUsageValue(balance.remaining, balance.unit)}
            />
          )}
          {balance.group ? (
            <UsageName className="text-muted-foreground">{balance.group}</UsageName>
          ) : null}
          {balance.note ? (
            <UsageName className="text-muted-foreground">{balance.note}</UsageName>
          ) : null}
          <span className="text-[10px] text-muted-foreground">
            {account.balance_read_at == null
              ? t("balanceNone")
              : t("balanceRead", { date: formatFullDate(account.balance_read_at) })}
          </span>
          {stale ? (
            <span className="text-[10px] text-muted-foreground">{t("balanceOld")}</span>
          ) : null}
          {balance.expires_at != null ? (
            <span className="text-[10px] text-muted-foreground">
              {t("expires", { date: formatFullDate(balance.expires_at) })}
            </span>
          ) : null}
        </div>
      ))}
    </div>
  );
}
