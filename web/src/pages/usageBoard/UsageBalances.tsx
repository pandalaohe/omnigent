import type { UsageAccountView } from "./usageBoardData";
import { formatFullDate, formatUsageValue } from "./usageBoardFormat";
import { t } from "./usageText";

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
            <div className="usage-balance-ring">
              <span
                data-usage-amount
                style={{
                  fontSize: `min(16px, ${110 / formatUsageValue(balance.remaining, balance.unit).length}cqw)`,
                }}
              >
                {formatUsageValue(balance.remaining, balance.unit)}
              </span>
            </div>
            <span>{balance.label ?? t("balance")}</span>
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
          <span
            className={
              compact
                ? "font-medium tabular-nums"
                : "usage-balance-detail flex flex-col items-center justify-center rounded-full border border-dashed border-muted-foreground/40 px-1"
            }
          >
            <span className="max-w-full break-words text-[10px] text-muted-foreground">
              {balance.label ?? t("balance")}
            </span>
            <span data-usage-amount className="max-w-full break-all font-semibold tabular-nums">
              {formatUsageValue(balance.remaining, balance.unit)}
            </span>
          </span>
          {balance.group ? (
            <span className="max-w-full break-words text-muted-foreground">{balance.group}</span>
          ) : null}
          {balance.note ? (
            <span className="max-w-full break-words text-muted-foreground">{balance.note}</span>
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
