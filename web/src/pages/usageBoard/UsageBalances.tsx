import type { UsageAccountView } from "./usageBoardData";
import { formatFullDate, formatTileAmount, formatUsageValue } from "./usageBoardFormat";
import { t } from "./usageText";
import { UsageFigure } from "./UsageFigure";
import { UsageName } from "./UsageName";

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
            <UsageFigure
              label={balance.label === "" ? undefined : (balance.label ?? t("balance"))}
              value={balance.remaining}
              unit={balance.unit}
              size={88}
            />
            {stale ? <span className="usage-caption">{t("balanceOld")}</span> : null}
          </div>
        ))}
      </div>
    );
  return (
    <div className="usage-balance-list" data-testid="usage-balances">
      {account.balances.map((balance) => (
        <div key={balance.id} className="usage-balance-entry">
          {compact ? (
            <span className="usage-balance-compact">
              <UsageName data-usage-label className="usage-caption">
                {balance.label ?? t("balance")}
              </UsageName>
              <span
                data-usage-amount
                className="shrink-0"
                title={formatUsageValue(balance.remaining, balance.unit)}
              >
                {formatTileAmount(balance.remaining, balance.unit)}
              </span>
            </span>
          ) : (
            <UsageFigure
              label={balance.label ?? t("balance")}
              value={balance.remaining}
              unit={balance.unit}
              size={64}
              full
            />
          )}
          {balance.group && balance.group !== balance.label ? (
            <UsageName className="usage-caption">{balance.group}</UsageName>
          ) : null}
          {balance.note ? <UsageName className="usage-caption">{balance.note}</UsageName> : null}
          <span className="usage-caption">
            {account.balance_read_at == null
              ? t("balanceNone")
              : t("balanceRead", { date: formatFullDate(account.balance_read_at) })}
          </span>
          {stale ? <span className="usage-caption">{t("balanceOld")}</span> : null}
          {balance.expires_at != null ? (
            <span className="usage-caption">
              {t("expires", { date: formatFullDate(balance.expires_at) })}
            </span>
          ) : null}
        </div>
      ))}
    </div>
  );
}
