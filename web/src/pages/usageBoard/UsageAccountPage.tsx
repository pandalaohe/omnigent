import { boardPath } from "@/lib/boardFrame";
// One account in detail: every window the last read returned, in its group,
// with the countdown and the reset written out, and the hosts that reported.

import { Link, useParams, useSearchParams } from "@/lib/routing";
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
import { UsageAccountCost } from "./UsageAccountCost";
import { UsagePricingSummary } from "./UsagePricingPanel";
import { UsageModelPoolsLink } from "./UsageModelPoolsLink";

import { UsageBoardFrame } from "./UsageBoardFrame";
import { UsageRing } from "./UsageRing";
import { UsageTip } from "./UsageTip";
import { useUsageOverview } from "./usageBoardData";
import {
  formatAge,
  formatFullDate,
  formatTimeLeft,
  formatUsageValue,
  isPercentWindow,
  TONE_TEXT,
  paceChip,
  stateTone,
  windowName,
} from "./usageBoardFormat";
import { groupWindows, isStale, neverRead, pickWindow } from "./usageBoardSelect";
import { readingTip, windowTip } from "./usageBoardTips";
import { useUsageAccountActivity } from "./usageActivityData";

import { renewalOf } from "./UsageBoardPage";
import { UsageBalances } from "./UsageBalances";

export function UsageAccountPage() {
  const { accountId = "" } = useParams();
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
  const empty = account ? neverRead(account) : true;
  const groups = account ? groupWindows(account.windows) : [];
  groups.sort((a, b) => Number(b.key === focus) - Number(a.key === focus));
  const pinnedId = board.windows[accountId];
  const renewal = account ? renewalOf(account, board.renewals[account.id]) : null;

  return (
    <UsageBoardFrame
      title="Usage board"
      back={{ to: "/usage/board", label: "Usage board" }}
      loading={overview.isPending}
      error={overview.error ?? activity.error}
      actions={
        <Button variant="ghost" size="icon" asChild aria-label="Account settings">
          <Link
            to={boardPath(
              `/usage/board/${encodeURIComponent(accountId)}/settings${focus ? `?focus=${encodeURIComponent(focus)}` : ""}`,
            )}
          >
            <SettingsIcon className="size-4" aria-hidden="true" />
          </Link>
        </Button>
      }
    >
      {account ? (
        <>
          <div className="usage-account-heading">
            <h2 className="text-xl font-bold">{account.display_name}</h2>
            <p className="text-sm text-muted-foreground">
              {account.plan}
              {renewal != null ? ` · Renews ${formatFullDate(renewal.at / 1000)}` : ""}
            </p>
          </div>
          <div className="mb-3 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
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
            {account.band ? <span>{account.band}</span> : null}
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

          <div className="usage-account-detail">
            <div className="usage-detail-grid">
              <section className="rounded-[11px] border border-border bg-card px-3.5 py-3">
                <div className="mb-2 flex min-h-5 items-center gap-1.5 text-[13px] font-semibold">
                  Quota
                  <span className="flex-1" />
                  {stale && account.age_s !== null ? (
                    <span className="text-[11px] font-normal text-muted-foreground">
                      Read {formatAge(account.age_s)} ago
                    </span>
                  ) : null}
                </div>
                {groups.length === 0 ? (
                  <div className="flex flex-col items-center gap-2 py-6">
                    <UsageRing window={null} stale size={92} />
                    <p className="text-xs text-muted-foreground">
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
                        {group.tag ? (
                          <div className="mb-1 mt-2 text-[11px] text-muted-foreground">
                            {group.tag}
                          </div>
                        ) : null}
                        <div className="grid grid-cols-[repeat(auto-fill,minmax(112px,1fr))] gap-x-2 gap-y-3">
                          {group.windows.map((window) => {
                            const isOn = picked.window === window && isPercentWindow(window);
                            const tone = stale || empty ? "mute" : stateTone(window.state);
                            return (
                              <div
                                key={window.id}
                                className="relative flex min-w-0 flex-col items-center gap-0.5 text-center"
                              >
                                <UsageTip
                                  model={windowTip(account, window, group.tag, {
                                    pinned: picked.pinned,
                                    stale: stale || empty,
                                    auto: !pinnedId || pinnedId === "auto",
                                  })}
                                  tapOpens
                                >
                                  <UsageRing
                                    window={window}
                                    stale={stale || empty}
                                    size={92}
                                    showName
                                  />
                                </UsageTip>
                                {window.percent_used !== null ? (
                                  <span
                                    className={cn(
                                      "whitespace-nowrap text-[14px] font-semibold leading-tight",
                                      stale ? "text-muted-foreground" : "text-info",
                                    )}
                                  >
                                    {formatTimeLeft(window.time_left_s) || "—"}
                                  </span>
                                ) : null}
                                <span className="text-[11px] leading-tight text-muted-foreground">
                                  {window.resets_at !== null
                                    ? `Resets ${formatFullDate(window.resets_at)}`
                                    : windowName(window)}
                                </span>
                                {window.used_value !== null ? (
                                  <span className="text-[11px] text-muted-foreground">
                                    Used {formatUsageValue(window.used_value, window.unit)}
                                    {window.limit_value !== null
                                      ? ` / ${formatUsageValue(window.limit_value, window.unit)}`
                                      : ""}
                                  </span>
                                ) : null}
                                {window.remaining_pct !== null ? (
                                  <span className="text-[11px] text-muted-foreground">
                                    Remaining {Number(window.remaining_pct.toFixed(2))}%
                                  </span>
                                ) : null}
                                {window.ahead_pts !== null && window.ahead_pts > 0 && !stale ? (
                                  <span className="text-[11px] text-warning">
                                    Ahead of time by {Number(window.ahead_pts.toFixed(2))} points
                                  </span>
                                ) : null}
                                {isOn ? (
                                  <span
                                    className={cn(
                                      "inline-flex items-center gap-0.5 text-[11px]",
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
                  <div className="mt-3 border-t border-border pt-3 [&>div]:justify-start">
                    <UsageBalances account={account} />
                  </div>
                ) : null}
              </section>

              <div className="grid gap-2">
                <UsageAccountCost account={account} />
                <UsagePricingSummary accountId={account.id} />
                <section className="rounded-[11px] border border-border bg-card px-3.5 py-3">
                  <h2 className="text-sm font-semibold">Used in</h2>
                  <UsageModelPoolsLink />
                </section>
              </div>
            </div>
            <details className="mt-2 rounded-[11px] border border-border bg-card px-3.5 py-3 text-sm">
              <summary className="cursor-pointer font-semibold">Usage tools</summary>
              {focus && !activity.data?.accounts.length ? (
                <p className="pt-2 text-xs text-muted-foreground">
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
                    now={activity.data?.generated_at ?? Date.now() / 1000}
                    fee={focus ? undefined : board.fees?.[account.id]}
                  />
                  <UsageByModel accounts={activity.data?.accounts ?? []} />
                </>
              )}
            </details>
            <details className="mt-2 rounded-[11px] border border-border bg-card px-3.5 py-3 text-sm">
              <summary className="cursor-pointer font-semibold">Records</summary>
              <p className="pt-2 text-xs text-muted-foreground">Not available yet</p>
            </details>
            <div className="mt-2">
              <details className="rounded-[11px] border border-border bg-card px-3.5 py-3">
                <summary className="cursor-pointer text-[13px] font-semibold">Hosts</summary>
                {account.hosts.length === 0 ? (
                  <p className="py-2 text-xs text-muted-foreground">
                    This source reports no hosts. A host's own reading appears here once one sends
                    it.
                  </p>
                ) : (
                  <ul className="flex flex-col">
                    {account.hosts.map((host) => (
                      <li
                        key={host.host_id || host.name}
                        className="flex min-h-9 items-center justify-between gap-2 border-t border-border/60 py-1.5 first:border-t-0 text-xs"
                      >
                        <span className="min-w-0 truncate font-medium">{host.name}</span>
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
