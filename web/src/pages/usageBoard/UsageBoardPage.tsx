import { Link, useSearchParams } from "@/lib/routing";
import { boardExitProps, usageAccountSubpagePath, useBoardPath } from "@/lib/boardFrame";
import { SettingsIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { useUsageContextPreferences } from "@/hooks/useUsageContextPreferences";
import { UsageAccountQuota } from "./UsageAccountPage";
import {
  overviewRange,
  UsageAccountCost,
  UsageOverviewHosts,
  useOverviewCostReads,
} from "./UsageAccountCost";
import { UsageBoardFrame } from "./UsageBoardFrame";
import { UsageEvents } from "./UsageEvents";
import { UsageModelPoolsLink } from "./UsageModelPoolsLink";
import { UsageName } from "./UsageName";
import { UsagePricingSummary } from "./UsagePricingPanel";
import { UsageRing } from "./UsageRing";
import { useUsageOverview, type UsageAccountView } from "./usageBoardData";
import { formatPercentReading, formatTimeLeft, windowName } from "./usageBoardFormat";
import { boardBands } from "./usageBoardLayout";
import { isStale } from "./usageBoardSelect";
import { t } from "./usageText";

function tightest(account: UsageAccountView) {
  const windows = account.windows.filter((window) => !window.past_reset);
  return (
    windows.reduce<(typeof windows)[number] | null>((best, window) => {
      if (window.remaining_pct === null) return best;
      return window.remaining_pct < (best?.remaining_pct ?? Infinity) ? window : best;
    }, null) ??
    windows.find((window) => window.id === account.deciding_window) ??
    windows[0] ??
    null
  );
}

export function UsageBoardPage() {
  const boardPath = useBoardPath();

  const board = useUsageContextPreferences().board;
  const overview = useUsageOverview(true);
  const [search] = useSearchParams();
  const accounts = boardBands(board, overview.data?.accounts ?? []).flatMap(
    (band) => band.accounts,
  );
  const selected = search.get("account");
  const account = accounts.find((item) => item.id === selected);
  const settings = account
    ? boardPath(usageAccountSubpagePath(account.id, "settings"))
    : "/settings/general#usage-settings";
  const reads = useOverviewCostReads(account?.id, accounts.length > 0);
  const shown = account ? [account] : accounts;
  const selectedRange = overviewRange(search.get("range"));

  function selectAccount(id?: string) {
    const next = new URLSearchParams(search);
    if (id) next.set("account", id);
    else next.delete("account");
    return { search: next.toString() };
  }

  const details = new URLSearchParams(search);
  details.set("view", "details");

  return (
    <UsageBoardFrame
      title={t("boardTitle")}
      generatedAt={overview.data?.generated_at}
      loading={overview.isPending}
      error={overview.error}
      actions={
        <Button variant="ghost" size="icon" asChild aria-label={t("overviewSettings")}>
          <Link {...(account ? {} : boardExitProps())} to={settings}>
            <SettingsIcon className="size-4" aria-hidden="true" />
          </Link>
        </Button>
      }
    >
      <nav className="usage-overview-accounts" aria-label={t("overviewAccounts")}>
        <Link to={selectAccount()} aria-current={!account ? "true" : undefined}>
          {t("overviewAll")}
        </Link>
        {accounts.map((item) => (
          <Link
            key={item.id}
            to={selectAccount(item.id)}
            aria-current={account === item ? "true" : undefined}
          >
            <UsageRing window={tightest(item)} size={18} stale={isStale(item)} />
            <UsageName>{item.display_name}</UsageName>
          </Link>
        ))}
      </nav>
      {selected && !account && !overview.error ? (
        <p className="usage-card usage-caption">This account is not on the board any more.</p>
      ) : accounts.length ? (
        <div data-testid="usage-overview">
          <div className="usage-account-heading">
            <h2 className="usage-account-name">
              {account?.display_name ?? t("overviewAllAccounts")}
            </h2>
            {account?.plan ? <p className="usage-caption usage-wrap">{account.plan}</p> : null}
            {account ? (
              <Link
                className="inline-flex min-h-11 items-center text-sm underline"
                to={{ search: details.toString() }}
              >
                Account details
              </Link>
            ) : null}
          </div>
          <div className="usage-overview">
            <div className="usage-overview-column">
              {account ? (
                <UsageAccountQuota account={account} />
              ) : (
                <section className="usage-card">
                  <h2 className="usage-card-title">{t("overviewQuota")}</h2>
                  <ul className="usage-overview-quota">
                    {accounts.map((item) => {
                      const window = tightest(item);
                      return (
                        <li key={item.id}>
                          <Link className="usage-row" to={selectAccount(item.id)}>
                            <UsageRing
                              window={window}
                              size={54}
                              showName={false}
                              stale={isStale(item)}
                            />
                            <span className="min-w-0 flex-1">
                              <UsageName className="font-semibold">{item.display_name}</UsageName>
                              <span className="usage-caption block">
                                {window ? windowName(window) : t("overviewNoQuota")}
                              </span>
                            </span>
                            {window && !isStale(item) ? (
                              <span className="usage-caption text-right">
                                {window.remaining_pct === null
                                  ? null
                                  : t("overviewRemaining", {
                                      percent: formatPercentReading(window.remaining_pct),
                                    })}
                                {window.time_left_s !== null ? (
                                  <span className="block">
                                    {formatTimeLeft(window.time_left_s)}
                                  </span>
                                ) : null}
                              </span>
                            ) : null}
                          </Link>
                        </li>
                      );
                    })}
                  </ul>
                </section>
              )}
              <UsageOverviewHosts
                accounts={shown}
                account={account?.id}
                range={selectedRange}
                query={reads[selectedRange]}
              />
              <UsageEvents
                key={account?.id}
                accounts={accounts}
                account={account?.id}
                activity={reads.period.data}
              />
            </div>
            <div className="usage-overview-column">
              <UsageAccountCost account={account} accounts={shown} reads={reads} />
              <div className="usage-overview-meta">
                <UsagePricingSummary accounts={shown} amounts={reads.period.data?.accounts} />
                <section className="usage-card">
                  <h2 className="usage-card-title">{t("overviewUsedIn")}</h2>
                  <UsageModelPoolsLink />
                </section>
              </div>
            </div>
          </div>
        </div>
      ) : (
        <p className="usage-card usage-caption">{t("overviewEmpty")}</p>
      )}
    </UsageBoardFrame>
  );
}
