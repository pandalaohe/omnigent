import { usageAccountSubpagePath, useBoardPath } from "@/lib/boardFrame";
// One account in detail: every window the last read returned, in its group,
// with the countdown and the reset written out, and the hosts that reported.

import { Link, useSearchParams } from "@/lib/routing";
import {
  LaptopIcon,
  PinIcon,
  PlugIcon,
  SettingsIcon,
  TriangleAlertIcon,
  ZapIcon,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { cn } from "@/lib/utils";
import { useUsageContextPreferences } from "@/hooks/useUsageContextPreferences";
import { UsageCostCard, UsageByModel } from "./UsageAmounts";
import { LegacyUsageAccountCost } from "./UsageAccountCost";
import { UsagePricingSummary } from "./UsagePricingPanel";
import { UsageModelPoolsLink } from "./UsageModelPoolsLink";

import { UsageBoardFrame } from "./UsageBoardFrame";
import { UsageRing } from "./UsageRing";
import { UsageTip } from "./UsageTip";
import { useUsageOverview, type UsageAccountView } from "./usageBoardData";
import { t } from "./usageText";
import {
  formatAge,
  formatFullDate,
  formatPercentReading,
  formatTimeLeft,
  formatUsageValue,
  isPercentWindow,
  TONE_TEXT,
  paceChip,
  stateTone,
  windowName,
} from "./usageBoardFormat";
import { groupWindows, isStale, neverRead, pickWindow, renewalOf } from "./usageBoardSelect";
import { readingTip, windowTip } from "./usageBoardTips";
import { useUsageAccountActivity } from "./usageActivityData";

import { UsageBalances } from "./UsageBalances";
import { UsageName } from "./UsageName";
import { analysisLink, filterFromQuery } from "./usageAnalysisFilters";

export function UsageAccountPage({ accountId }: { accountId: string }) {
  const boardPath = useBoardPath();

  const info = useServerInfo();
  const board = useUsageContextPreferences().board;
  const overview = useUsageOverview(info !== "loading" && info.usage_board_enabled === true);
  const [search] = useSearchParams();
  const focus = search.get("focus");
  const activity = useUsageAccountActivity(
    accountId,
    focus,
    info !== "loading" && info.usage_board_enabled === true,
  );
  const account = overview.data?.accounts.find((item) => item.id === accountId);

  if (!overview.isPending && !overview.error && !account) {
    return (
      <UsageBoardFrame title="Account" back={{ to: "/usage/board", label: "Usage board" }}>
        <div className="rounded-[11px] border border-border px-4 py-8 text-center text-sm text-muted-foreground">
          This account is not on the board any more.
          <div className="mt-3">
            <Button variant="outline" size="sm" asChild>
              <Link to={boardPath("/usage/board")}>Back to the board</Link>
            </Button>
          </div>
        </div>
      </UsageBoardFrame>
    );
  }

  const stale = account ? isStale(account) : false;
  const renewal = account ? renewalOf(account, board.renewals[account.id]) : null;
  const drill = filterFromQuery(
    new URLSearchParams({
      range: board.range?.key ?? "d7",
      ...(board.range?.from ? { from: board.range.from } : {}),
      ...(board.range?.to ? { to: board.range.to } : {}),
    }),
  );

  return (
    <UsageBoardFrame
      title="Usage board"
      back={{ to: "/usage/board", label: "Usage board" }}
      loading={overview.isPending}
      error={overview.error ?? activity.error}
      actions={
        <Button variant="ghost" size="icon" asChild aria-label="Account settings">
          <Link to={boardPath(usageAccountSubpagePath(accountId, "settings", focus))}>
            <SettingsIcon className="size-4" aria-hidden="true" />
          </Link>
        </Button>
      }
    >
      {account ? (
        <>
          <div className="usage-account-heading">
            <h2 className="usage-account-name">{account.display_name}</h2>
            <p className="usage-caption usage-wrap">
              {`${account.plan ?? ""}${renewal != null ? ` · Renews ${formatFullDate(renewal.at / 1000)}` : ""}`}
            </p>
          </div>
          <div className="usage-account-meta">
            <UsageTip model={readingTip(account)} tapOpens>
              <span className="inline-flex items-center gap-1">
                {account.kind === "native" ? (
                  <LaptopIcon className="size-3.5" aria-hidden="true" />
                ) : (
                  <PlugIcon className="size-3.5" aria-hidden="true" />
                )}
                {account.kind === "native" ? "Built in, read on your hosts" : "Reported"}
              </span>
            </UsageTip>
            {account.band ? <UsageName>{account.band}</UsageName> : null}
            {stale && account.age_s !== null ? (
              <span className="inline-flex items-center gap-1">
                <TriangleAlertIcon className="size-3.5" aria-hidden="true" />
                Read {formatAge(account.age_s)} ago
              </span>
            ) : null}
            {account.details_url ? (
              <a
                className="underline underline-offset-2"
                href={account.details_url}
                target="_blank"
                rel="noreferrer noopener"
              >
                Open the provider
              </a>
            ) : null}
          </div>

          <div className="usage-account-detail min-w-0">
            <div className="usage-detail-grid">
              <UsageAccountQuota account={account} focus={focus} />

              <div className="grid grid-cols-1 gap-2">
                <LegacyUsageAccountCost account={account} />
                <UsagePricingSummary accountId={account.id} />
                <section className="usage-card">
                  <h2 className="usage-card-title">Used in</h2>
                  <UsageModelPoolsLink />
                </section>
              </div>
            </div>
            <details className="usage-card usage-fold mt-2">
              <summary>Usage tools</summary>
              {focus && !activity.data?.accounts.length ? (
                <p className="usage-caption">
                  {activity.error
                    ? "Cost could not be read"
                    : activity.isPending
                      ? "Reading cost…"
                      : "Not reported · No proven usage for this pool."}
                </p>
              ) : (
                <>
                  <UsageCostCard
                    accounts={activity.data?.accounts ?? []}
                    account={account}
                    focus={focus}
                    now={activity.data?.generated_at ?? Date.now() / 1000}
                    fee={focus ? undefined : board.fees?.[account.id]}
                  />
                  <UsageByModel accounts={activity.data?.accounts ?? []} />
                </>
              )}
            </details>
            <details className="usage-card usage-fold mt-2">
              <summary>Records</summary>
              <p className="usage-caption">Not available yet</p>
            </details>
            <div className="mt-2 min-w-0">
              <details className="usage-card usage-fold min-w-0">
                <summary>Hosts</summary>
                {account.hosts.length === 0 ? (
                  <p className="usage-caption">
                    This source reports no hosts. A host's own reading appears here once one sends
                    it.
                  </p>
                ) : (
                  <ul className="flex min-w-0 flex-col">
                    {account.hosts.map((host) => (
                      <li
                        key={host.host_id || host.name}
                        className="flex min-h-9 min-w-0 items-center justify-between gap-2 border-t border-border/60 py-1.5 first:border-t-0 text-xs"
                      >
                        {!focus && drill.range === (board.range?.key ?? "d7") ? (
                          <Link
                            className="min-w-0 underline underline-offset-2"
                            title={host.name}
                            to={boardPath(
                              analysisLink(search, {
                                ...drill,
                                account: [account.id],
                                host: [host.host_id],
                              }),
                            )}
                          >
                            <UsageName className="font-medium">{host.name}</UsageName>
                          </Link>
                        ) : (
                          <UsageName className="font-medium">{host.name}</UsageName>
                        )}
                        {(() => {
                          const own = activity.data?.accounts[0];
                          const part = own?.hosts?.find((item) => item.host_id === host.host_id);
                          const all = own?.totals.tokens ?? own?.totals.cost_usd;
                          const value = own?.totals.tokens != null ? part?.tokens : part?.cost_usd;
                          const percent = all && value != null ? (value / all) * 100 : null;
                          return percent === null ? null : (
                            <span className="ml-auto flex w-28 items-center gap-2 text-xs text-muted-foreground">
                              <span className="h-1 flex-1 overflow-hidden rounded bg-muted">
                                <span
                                  className="block h-full bg-info/60"
                                  style={{ width: `${Math.min(100, percent)}%` }}
                                />
                              </span>
                              {Math.round(percent)}%
                            </span>
                          );
                        })()}
                        <span className="shrink-0 text-muted-foreground">
                          {host.read_at === null
                            ? "No reading"
                            : host.windows.length === 0
                              ? "No window"
                              : `${host.windows.length} window${host.windows.length === 1 ? "" : "s"}`}
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
              </details>
            </div>
          </div>
        </>
      ) : null}
    </UsageBoardFrame>
  );
}

export function UsageAccountQuota({
  account,
  focus = null,
}: {
  account: UsageAccountView;
  focus?: string | null;
}) {
  const pinnedId = useUsageContextPreferences().board.windows[account.id];
  const stale = isStale(account);
  const empty = neverRead(account);
  const groups = groupWindows(account.windows);
  groups.sort((a, b) => Number(b.key === focus) - Number(a.key === focus));
  return (
    <section className="usage-card">
      <div className="usage-card-head">
        <h2 className="usage-card-title">{t("overviewQuota")}</h2>
        {stale && account.age_s !== null ? (
          <span className="usage-caption">Read {formatAge(account.age_s)} ago</span>
        ) : null}
      </div>
      {groups.length === 0 ? (
        <div className="flex flex-col items-center gap-2 py-6">
          <UsageRing window={null} stale size={100} />
          <p className="usage-caption">
            {empty ? "No reading yet." : "The last read returned no window."}
          </p>
        </div>
      ) : (
        groups.map((group) => {
          const picked = pickWindow(group, account.deciding_window, pinnedId);
          return (
            <div
              key={group.key || "main"}
              data-pool={group.key}
              data-focused={group.key === focus || undefined}
            >
              {group.tag ? <p className="usage-group-tag">{group.tag}</p> : null}
              <div className="usage-window-grid">
                {group.windows.map((window) => {
                  const isOn = picked.window === window && isPercentWindow(window);
                  const tone = stale || empty ? "mute" : stateTone(window.state);
                  return (
                    <div key={window.id} data-usage-window className="usage-window">
                      <UsageTip
                        className="usage-ring-trigger"
                        triggerLabel={`${windowName(window)} details`}
                        model={windowTip(account, window, group.tag, {
                          pinned: picked.pinned,
                          stale: stale || empty,
                          auto: !pinnedId || pinnedId === "auto",
                        })}
                        tapOpens
                      >
                        <UsageRing window={window} stale={stale || empty} size={100} showName />
                      </UsageTip>
                      {window.percent_used !== null ? (
                        <span className={cn("usage-countdown", stale && "text-muted-foreground")}>
                          {formatTimeLeft(window.time_left_s) || "—"}
                        </span>
                      ) : null}
                      <span className="usage-caption max-w-full">
                        {window.resets_at !== null ? (
                          `Resets ${formatFullDate(window.resets_at)}`
                        ) : (
                          <UsageName>{windowName(window)}</UsageName>
                        )}
                      </span>
                      {window.used_value !== null ? (
                        <span className="usage-caption">
                          Used {formatUsageValue(window.used_value, window.unit)}
                          {window.limit_value !== null
                            ? ` / ${formatUsageValue(window.limit_value, window.unit)}`
                            : ""}
                        </span>
                      ) : null}
                      {window.remaining_pct !== null ? (
                        <span className="usage-caption">
                          Remaining {formatPercentReading(window.remaining_pct)}%
                        </span>
                      ) : null}
                      {window.ahead_pts !== null && window.ahead_pts > 0 && !stale ? (
                        <span className="usage-caption text-warning">
                          Ahead of time by {formatPercentReading(window.ahead_pts)} points
                        </span>
                      ) : null}
                      {isOn ? (
                        <span
                          className={cn(
                            "usage-caption inline-flex items-center gap-1",
                            TONE_TEXT[tone],
                          )}
                          title={
                            picked.pinned
                              ? "You pinned this window for the board."
                              : "Auto picked the most urgent window."
                          }
                        >
                          {picked.pinned ? (
                            <PinIcon className="size-3" aria-hidden="true" />
                          ) : (
                            <ZapIcon className="size-3" aria-hidden="true" />
                          )}
                          {paceChip(window, stale || empty).label}
                        </span>
                      ) : null}
                    </div>
                  );
                })}
              </div>
            </div>
          );
        })
      )}
      {account.balances?.length ? (
        <div className="usage-card-section">
          <UsageBalances account={account} />
        </div>
      ) : null}
    </section>
  );
}
