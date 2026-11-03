// One account on the board: its rings, the window name inside each, the
// countdown under it, and the renewal in the footer. The whole tile is the way
// into the account's detail page — on a phone a tap anywhere on it goes
// straight there, which is why the data cards live in the detail page and only
// the reading's state has a button of its own.

import {
  CalendarClockIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  GripVerticalIcon,
} from "lucide-react";

import { cn } from "@/lib/utils";

import type { UsageAccountView } from "./usageBoardData";
import {
  accountTone,
  daysUntil,
  isStale,
  neverRead,
  pickGroups,
  type UsageWindowGroup,
} from "./usageBoardSelect";
import { formatAge, formatTimeLeft, windowName } from "./usageBoardFormat";
import { UsageRing } from "./UsageRing";
import { UsageTip, UsageTipButton } from "./UsageTip";
import { readingTip, renewalTip, windowTip } from "./usageBoardTips";

const TONE_TILE: Record<string, string> = {
  warn: "border-warning/60 bg-warning/13",
  crit: "border-destructive/60 bg-destructive/13",
};

interface UsageTileProps {
  account: UsageAccountView;
  pinnedWindowId: string | undefined;
  /** The renewal the footer counts down to; the provider's wins. */
  renewal: { at: number; official: boolean } | null;
  density: "std" | "mini";
  /** Arrange mode: the tile shows move handles instead of the footer. */
  arrange?: boolean;
  onOpen: () => void;
  onMove?: (direction: -1 | 1) => void;
}

export function UsageTile({
  account,
  pinnedWindowId,
  renewal,
  density,
  arrange = false,
  onOpen,
  onMove,
}: UsageTileProps) {
  const stale = isStale(account);
  const empty = neverRead(account);
  const tone = accountTone(account, stale);
  const picks = pickGroups(account, pinnedWindowId);
  // An account whose provider groups its windows needs the extra width for a
  // second ring; one group stays a normal tile.
  const wide = picks.length > 1 && density === "std";
  const renewDays = renewal ? daysUntil(renewal.at) : null;

  const name = account.display_name;
  const plan = account.plan ?? "";

  function activate() {
    onOpen();
  }

  if (density === "mini") {
    const first = picks[0];
    return (
      <div
        role="link"
        tabIndex={0}
        aria-label={tileLabel(account, picks, stale)}
        onClick={activate}
        onKeyDown={(event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            activate();
          }
        }}
        className={cn(
          "flex min-h-[54px] min-w-0 cursor-pointer items-center gap-2 rounded-[10px] border border-border bg-card py-1.5 pl-1.5 pr-2.5 transition-colors hover:border-foreground/30",
          TONE_TILE[tone],
        )}
        data-testid="usage-tile"
        data-account={account.id}
      >
        <UsageRing
          window={first?.window ?? null}
          stale={stale || empty}
          size={42}
          showName={false}
        />
        <div className="flex min-w-0 flex-1 flex-col leading-tight">
          <span className="truncate font-semibold">{name}</span>
          {plan ? <span className="truncate text-[11px] text-muted-foreground">{plan}</span> : null}
        </div>
        <div className="flex shrink-0 flex-col items-end leading-tight">
          {first?.window && first.window.percent_used !== null ? (
            <span
              className={cn(
                "text-[13px] font-semibold",
                stale ? "text-muted-foreground" : "text-info",
              )}
            >
              {formatTimeLeft(first.window.time_left_s)}
            </span>
          ) : first?.window?.used_value !== null && first?.window ? (
            <span className="text-[13px] font-semibold">
              {first.window.used_value.toLocaleString("en-US")}
            </span>
          ) : null}
        </div>
        {stale ? <ReadingButton account={account} /> : null}
      </div>
    );
  }

  return (
    <div
      role="link"
      tabIndex={0}
      aria-label={tileLabel(account, picks, stale)}
      onClick={activate}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          activate();
        }
      }}
      draggable={arrange}
      className={cn(
        "relative flex min-w-0 cursor-pointer flex-col rounded-[11px] border border-border bg-card px-2.5 pb-2 pt-2 transition-colors hover:border-foreground/30",
        wide && "col-span-2",
        TONE_TILE[tone],
        arrange && "cursor-grab outline-dashed outline-1 outline-foreground/30 -outline-offset-1",
      )}
      data-testid="usage-tile"
      data-account={account.id}
    >
      <div className="flex min-h-9 min-w-0 items-start gap-1">
        <div className="flex min-w-0 flex-1 flex-col leading-tight">
          <span className="truncate font-semibold">{name}</span>
          {plan ? <span className="truncate text-[11px] text-muted-foreground">{plan}</span> : null}
        </div>
        <ReadingButton account={account} />
      </div>

      <div className="mb-2 mt-0.5 flex min-w-0 justify-evenly gap-2.5">
        {picks.length === 0 ? (
          <div className="flex min-w-0 flex-col items-center gap-0.5">
            <UsageRing window={null} stale size={88} />
          </div>
        ) : (
          picks.map(({ group, window, pinned }) => (
            <div key={group.key || "main"} className="flex min-w-0 flex-col items-center gap-0.5">
              {picks.length > 1 && group.tag ? (
                <span className="whitespace-nowrap text-[11px] leading-tight text-muted-foreground">
                  {group.tag}
                </span>
              ) : null}
              {window ? (
                <UsageTip
                  model={windowTip(account, window, group.tag, {
                    pinned,
                    stale: stale || empty,
                    auto: !pinnedWindowId || pinnedWindowId === "auto",
                  })}
                >
                  <UsageRing window={window} stale={stale || empty} size={88} />
                </UsageTip>
              ) : (
                <UsageRing window={null} stale size={88} />
              )}
              {window && window.percent_used !== null ? (
                <span
                  className={cn(
                    "whitespace-nowrap text-[14px] font-semibold leading-tight",
                    stale ? "text-muted-foreground" : "text-info",
                  )}
                >
                  {formatTimeLeft(window.time_left_s) || "—"}
                </span>
              ) : window && window.used_value !== null ? (
                <span className="whitespace-nowrap text-[11px] leading-tight text-muted-foreground">
                  {windowName(window)}
                </span>
              ) : (
                <span className="whitespace-nowrap text-[11px] leading-tight text-muted-foreground">
                  Not read
                </span>
              )}
            </div>
          ))
        )}
      </div>

      {arrange ? (
        <div className="mt-1.5 flex items-center justify-between">
          <GripVerticalIcon className="size-3.5 text-muted-foreground" aria-hidden="true" />
          <div className="flex items-center">
            <button
              type="button"
              className="grid size-7 place-items-center rounded text-muted-foreground hover:text-foreground"
              aria-label={`Move ${name} back`}
              onClick={(event) => {
                event.stopPropagation();
                onMove?.(-1);
              }}
            >
              <ChevronLeftIcon className="size-3.5" aria-hidden="true" />
            </button>
            <button
              type="button"
              className="grid size-7 place-items-center rounded text-muted-foreground hover:text-foreground"
              aria-label={`Move ${name} forward`}
              onClick={(event) => {
                event.stopPropagation();
                onMove?.(1);
              }}
            >
              <ChevronRightIcon className="size-3.5" aria-hidden="true" />
            </button>
          </div>
        </div>
      ) : (
        <div className="mt-auto flex min-h-[26px] min-w-0 items-center justify-between gap-1.5 border-t border-foreground/12 pt-1.5 text-[11px] text-muted-foreground">
          {renewal && renewDays !== null ? (
            <UsageTip model={renewalTip(renewal.at, renewal.official)}>
              <span
                className={cn(
                  "inline-flex items-center gap-1 whitespace-nowrap",
                  renewDays <= 3 && "text-warning",
                )}
              >
                <CalendarClockIcon className="size-3" aria-hidden="true" />
                <b className="font-semibold">{renewDays}d</b>
              </span>
            </UsageTip>
          ) : (
            <span />
          )}
        </div>
      )}
    </div>
  );
}

/** The state of the reading: how old it is when it is not current. */
function ReadingButton({ account }: { account: UsageAccountView }) {
  if (account.freshness === "fresh") return null;
  const label = account.age_s === null ? "Not read" : `Last read ${formatAge(account.age_s)} ago`;
  return (
    <UsageTipButton model={readingTip(account)} label={label} className="shrink-0">
      {account.age_s === null ? "Not read" : formatAge(account.age_s)}
    </UsageTipButton>
  );
}

function tileLabel(
  account: UsageAccountView,
  picks: { group: UsageWindowGroup; window: UsageAccountView["windows"][number] | null }[],
  stale: boolean,
): string {
  const parts = picks.map(({ window }) => {
    if (!window) return "no reading";
    if (window.percent_used !== null) {
      return `${windowName(window)} ${Math.round(window.percent_used)} percent used, ${formatTimeLeft(window.time_left_s)} left`;
    }
    return `${windowName(window)} ${window.used_value ?? ""}`;
  });
  return [account.display_name, ...parts, stale ? "reading is old" : ""].filter(Boolean).join(". ");
}
