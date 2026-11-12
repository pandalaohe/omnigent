import { useState } from "react";
import { PinIcon } from "lucide-react";
import type { UsageAccountView, UsageWindowView } from "@/pages/usageBoard/usageBoardData";
import type { BoardPreferences } from "./taskBoardPreferences";
import { BoardPager } from "./TaskBoardRoutes";
import { formatDate, formatNumber, t } from "./taskBoardText";
import { BoardIdentityMark } from "./BoardIdentityMark";
import type { UsageAccountActivity } from "@/pages/usageBoard/usageActivityData";

export const quotaWindows = (account: UsageAccountView) =>
  account.windows.filter(
    (window) =>
      !window.past_reset &&
      !["paused", "stale"].includes(window.state) &&
      window.percent_used != null,
  );
export const quotaReading = (account: UsageAccountView) =>
  [...quotaWindows(account)].sort((a, b) => b.percent_used! - a.percent_used!)[0];
export function quotaAccounts(accounts: UsageAccountView[], pins: string[]) {
  const live = accounts
    .filter(
      (account) =>
        account.freshness === "fresh" && account.state !== "paused" && quotaReading(account),
    )
    .sort((a, b) => quotaReading(b)!.percent_used! - quotaReading(a)!.percent_used!);
  return [
    ...pins.flatMap((id) => live.filter((account) => account.id === id)),
    ...live.filter((account) => !pins.includes(account.id)),
  ];
}
const windowLabel = (window: UsageWindowView) =>
  window.window_minutes != null
    ? window.window_minutes >= 1440
      ? `${formatNumber(window.window_minutes / 1440)}d`
      : `${formatNumber(window.window_minutes / 60)}h`
    : window.label || window.id;
const resetLabel = (window: UsageWindowView) =>
  window.resets_at == null
    ? undefined
    : formatDate(window.resets_at * 1000, {
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
      });
const percent = (window: UsageWindowView) =>
  `${formatNumber(window.percent_used!, { maximumFractionDigits: 2 })}%`;
export function BoardMeter({ value, warm, hot }: { value: number; warm?: boolean; hot?: boolean }) {
  return (
    <i className={`tb-meter ${hot ? "hot" : warm ? "warm" : ""}`}>
      <u style={{ width: `${Math.max(0, Math.min(100, value))}%` }} />
    </i>
  );
}
export function TaskBoardQuotaBand({
  accounts,
  preferences,
  onOpen,
  loading,
  failed,
  retry,
}: {
  accounts: UsageAccountView[];
  preferences: BoardPreferences;
  onOpen: () => void;
  loading: boolean;
  failed: boolean;
  retry: () => void;
}) {
  const shown = quotaAccounts(accounts, preferences.pinnedQuota).slice(0, 4);
  const shortName = (name: string) => name.split(/[（(]/)[0].trim();
  const names = shown.map((account) => shortName(account.display_name));
  return (
    <section className="tb-quota-band" aria-label={t("band.quota")}>
      <h2>
        {t("band.quota")}
        {accounts.length > shown.length && (
          <button
            type="button"
            onClick={onOpen}
            aria-label={t("quota.all", { count: accounts.length })}
          >
            {t("band.more", { count: accounts.length - shown.length })}
          </button>
        )}
      </h2>
      {failed ? (
        <button type="button" onClick={retry}>
          {t("band.retryFailure", { message: t("quota.failed"), retry: t("board.retry") })}
        </button>
      ) : (
        <div className="tb-quota-grid">
          {shown.map((account) => {
            const window = quotaReading(account)!,
              value = window.percent_used!;
            return (
              <button
                type="button"
                className={value >= 90 ? "hot" : value >= preferences.watch ? "warm" : ""}
                key={account.id}
                onClick={onOpen}
                title={resetLabel(window)}
              >
                <span>
                  <BoardIdentityMark
                    id={account.id}
                    label={account.display_name}
                    product={account.product}
                  />
                  <b>
                    {shortName(account.display_name)}
                    {names.filter((name) => name === shortName(account.display_name)).length >
                      1 && (
                      <small className="tb-quota-provider">
                        {account.display_name.match(/[（(]([^）)]*)[）)]/)?.[1]}
                      </small>
                    )}
                  </b>
                  <em>{percent(window)}</em>
                  <small>{windowLabel(window)}</small>
                </span>
                <BoardMeter value={value} warm={value >= preferences.watch} hot={value >= 90} />
              </button>
            );
          })}
          {!shown.length && (
            <button type="button" onClick={onOpen}>
              {t(loading ? "board.loading" : "quota.empty")}
            </button>
          )}
        </div>
      )}
    </section>
  );
}
export function TaskBoardQuotaPanel({
  accounts,
  preferences,
  update,
  costs,
}: {
  accounts: UsageAccountView[];
  preferences: BoardPreferences;
  update: (change: Partial<BoardPreferences>) => boolean | void;
  costs?: UsageAccountActivity[];
}) {
  const [page, setPage] = useState(0);
  const live = quotaAccounts(accounts, []),
    ids = new Set(live.map((account) => account.id));
  const off = accounts.filter((account) => !ids.has(account.id));
  const pages = Math.max(1, Math.ceil(live.length / 12)),
    current = Math.min(page, pages - 1);
  return (
    <div className="tb-resource-panel">
      <p>
        {t("quota.panelHint", {
          count: live.filter((account) => quotaReading(account)!.percent_used! >= preferences.watch)
            .length,
          threshold: preferences.watch,
        })}
      </p>
      <div className="tb-account-grid">
        {live.slice(current * 12, (current + 1) * 12).map((account) => {
          const deciding = quotaReading(account)!,
            pinned = preferences.pinnedQuota.includes(account.id);
          const totals = costs?.find((cost) => cost.account === account.id)?.totals;
          const amount = totals?.cost_usd;
          return (
            <article key={account.id} className="tb-account-card">
              <button
                type="button"
                className="tb-pin"
                aria-label={t(pinned ? "quota.unpin" : "quota.pin", { name: account.display_name })}
                aria-pressed={pinned}
                disabled={!pinned && preferences.pinnedQuota.length >= 4}
                title={
                  !pinned && preferences.pinnedQuota.length >= 4 ? t("display.pinsHint") : undefined
                }
                onClick={() =>
                  update({
                    pinnedQuota: pinned
                      ? preferences.pinnedQuota.filter((id) => id !== account.id)
                      : [...preferences.pinnedQuota, account.id],
                  })
                }
              >
                <PinIcon size={14} />
              </button>
              <h3>
                <BoardIdentityMark
                  id={account.id}
                  label={account.display_name}
                  product={account.product}
                  size={24}
                />
                {account.display_name}
              </h3>
              <small>
                {account.plan || account.kind}
                {amount != null &&
                  ` · ${t("quota.todayCost", { amount: formatNumber(amount, { minimumFractionDigits: 2, maximumFractionDigits: 2 }) })}`}
              </small>
              <div className="tb-quota-big">
                <b>{percent(deciding)}</b>
                <span>
                  {windowLabel(deciding)}
                  <small>{resetLabel(deciding)}</small>
                </span>
              </div>
              <BoardMeter
                value={deciding.percent_used!}
                warm={deciding.percent_used! >= preferences.watch}
                hot={deciding.percent_used! >= 90}
              />
              {quotaWindows(account)
                .filter((window) => window !== deciding)
                .map((window) => (
                  <div className="tb-quota-window" key={window.id}>
                    <span>{windowLabel(window)}</span>
                    <BoardMeter
                      value={window.percent_used!}
                      warm={window.percent_used! >= preferences.watch}
                    />
                    <b>{percent(window)}</b>
                    <small>{resetLabel(window)}</small>
                  </div>
                ))}
            </article>
          );
        })}
      </div>
      <BoardPager page={current} pages={pages} onChange={setPage} />
      {!!off.length && (
        <>
          <h3>{t("quota.excluded")}</h3>
          <p>{t("quota.excludedHint")}</p>
          <div className="tb-off-accounts">
            {off.map((account) => (
              <article key={account.id}>
                <b>{account.display_name}</b>
                <small>
                  {t(account.state === "paused" ? "quota.paused" : "quota.stale")}
                  {account.read_at != null &&
                    ` · ${formatDate(account.read_at * 1000, { month: "2-digit", day: "2-digit" })}`}
                </small>
              </article>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
