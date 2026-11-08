import type { UsageAccountView } from "./usageBoardData";
import { formatFullDate } from "./usageBoardFormat";
import { t } from "./usageText";

/** Balances have their own reading time and never borrow a window's pace. */
export function UsageBalances({
  account,
  compact = false,
}: {
  account: UsageAccountView;
  compact?: boolean;
}) {
  if (!account.balances?.length) return null;
  const stale = account.balance_freshness !== "fresh";
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
                : "flex size-[88px] flex-col items-center justify-center rounded-full border border-dashed border-muted-foreground/40 px-1"
            }
          >
            <span className="max-w-full break-words text-[10px] text-muted-foreground">
              {balance.label ?? t("balance")}
            </span>
            <span className="max-w-full break-all font-semibold tabular-nums">
              {balance.remaining.toLocaleString()} {balance.unit}
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
