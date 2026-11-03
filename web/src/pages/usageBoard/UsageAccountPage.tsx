// One account in detail: every window the last read returned, in its group,
// with the countdown and the reset written out, and the hosts that reported.

import { Link, useNavigate, useParams } from "@/lib/routing";
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

import { UsageBoardFrame } from "./UsageBoardFrame";
import { UsageRing } from "./UsageRing";
import { UsageTip } from "./UsageTip";
import { useUsageOverview } from "./usageBoardData";
import {
  formatAge,
  formatFullDate,
  formatTimeLeft,
  isPercentWindow,
  TONE_TEXT,
  paceChip,
  stateTone,
  windowName,
} from "./usageBoardFormat";
import { groupWindows, isStale, neverRead, pickWindow } from "./usageBoardSelect";
import { readingTip, windowTip } from "./usageBoardTips";

export function UsageAccountPage() {
  const { accountId = "" } = useParams();
  const info = useServerInfo();
  const board = useUsageContextPreferences().board;
  const overview = useUsageOverview(info !== "loading" && info.usage_board_enabled === true);
  const navigate = useNavigate();
  const account = overview.data?.accounts.find((item) => item.id === accountId);

  if (!overview.isPending && !account) {
    return (
      <UsageBoardFrame title="Account" back={{ to: "/usage/board", label: "Usage board" }}>
        <div className="rounded-[11px] border border-border px-4 py-8 text-center text-sm text-muted-foreground">
          This account is not on the board any more.
          <div className="mt-3">
            <Button variant="outline" size="sm" asChild>
              <Link to="/usage/board">Back to the board</Link>
            </Button>
          </div>
        </div>
      </UsageBoardFrame>
    );
  }

  const stale = account ? isStale(account) : false;
  const empty = account ? neverRead(account) : true;
  const groups = account ? groupWindows(account.windows) : [];
  const pinnedId = board.windows[accountId];

  return (
    <UsageBoardFrame
      title={account?.display_name ?? "…"}
      back={{ to: "/usage/board", label: "Usage board" }}
      loading={overview.isPending}
      error={overview.error}
      actions={
        <Button variant="ghost" size="icon" asChild aria-label="Account settings">
          <Link to={`/usage/board/${encodeURIComponent(accountId)}/settings`}>
            <SettingsIcon className="size-4" aria-hidden="true" />
          </Link>
        </Button>
      }
    >
      {account ? (
        <>
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
            {account.plan ? <span>{account.plan}</span> : null}
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

          <div className="grid gap-2.5 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.25fr)] lg:items-start">
            <section className="rounded-[11px] border border-border bg-card px-3.5 py-3">
              <div className="mb-2 flex min-h-5 items-center gap-1.5 text-[13px] font-semibold">
                Windows
                <span className="flex-1" />
                {stale && account.age_s !== null ? (
                  <span className="text-[11px] font-normal text-muted-foreground">
                    Read {formatAge(account.age_s)} ago
                  </span>
                ) : null}
              </div>
              {groups.length === 0 ? (
                <div className="flex flex-col items-center gap-2 py-6">
                  <UsageRing window={null} stale size={96} />
                  <p className="text-xs text-muted-foreground">
                    {empty ? "No reading yet." : "The last read returned no window."}
                  </p>
                </div>
              ) : (
                groups.map((group) => {
                  const picked = pickWindow(group, account.deciding_window, pinnedId);
                  return (
                    <div key={group.key || "main"}>
                      {group.tag ? (
                        <div className="mb-1 mt-2 text-[11px] text-muted-foreground">
                          {group.tag}
                        </div>
                      ) : null}
                      <div className="grid grid-cols-[repeat(auto-fill,minmax(104px,1fr))] gap-x-2 gap-y-3">
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
                                  size={96}
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
            </section>

            <section className="rounded-[11px] border border-border bg-card px-3.5 py-3">
              <div className="mb-2 min-h-5 text-[13px] font-semibold">Hosts</div>
              {account.hosts.length === 0 ? (
                <p className="py-2 text-xs text-muted-foreground">
                  This source reports no hosts. A host's own reading appears here once one sends it.
                </p>
              ) : (
                <ul className="flex flex-col">
                  {account.hosts.map((host) => (
                    <li
                      key={host.host_id || host.name}
                      className="flex min-h-9 items-center justify-between gap-2 border-t border-border/60 py-1.5 first:border-t-0 text-xs"
                    >
                      <span className="min-w-0 truncate font-medium">{host.name}</span>
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
            </section>
          </div>
          <div className="mt-3">
            <Button
              variant="ghost"
              size="sm"
              className="-ml-2"
              onClick={() => navigate("/usage/board")}
            >
              Board
            </Button>
          </div>
        </>
      ) : null}
    </UsageBoardFrame>
  );
}
