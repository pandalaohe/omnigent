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
import { boardPath } from "@/lib/boardFrame";
import { Link } from "@/lib/routing";
import type { ReactNode } from "react";

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
import { formatAge, formatTimeLeft, windowName, stateTone } from "./usageBoardFormat";
import { UsageRing } from "./UsageRing";
import { UsageTip, UsageTipButton } from "./UsageTip";
import { readingTip, renewalTip, windowTip } from "./usageBoardTips";
import type { UsageAmount } from "./usageActivityData";
import { UsageAmountFigure, UsageErrorMark } from "./UsageAmounts";
import { UsageBalances } from "./UsageBalances";
import { t } from "./usageText";
import { UsageName } from "./UsageName";

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
  onOpenGroup?: (group: string) => void;
  onMove?: (direction: -1 | 1) => void;
  shownModel?: string;
  floorControl?: ReactNode;
  amount?: UsageAmount;
  tokens?: boolean;
}

export function UsageTile({
  account,
  pinnedWindowId,
  renewal,
  density,
  arrange = false,
  onOpen,
  onOpenGroup,
  onMove,
  shownModel,
  floorControl,
  amount,
  tokens = false,
}: UsageTileProps) {
  const stale = isStale(account);
  const empty = neverRead(account);
  const tone = accountTone(account, stale);
  const picks = pickGroups(account, pinnedWindowId);
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
          // Only the tile itself: a nested control's Enter or Space must not be
          // swallowed into a navigation.
          if (event.target !== event.currentTarget) return;
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
        {!account.windows.length && account.balances?.length ? null : (
          <UsageRing
            window={first?.window ?? null}
            stale={stale || empty}
            size={42}
            showName={false}
          />
        )}
        <div className="flex min-w-0 flex-1 flex-col leading-tight">
          <UsageName className="font-semibold">{name}</UsageName>
          {plan ? (
            <UsageName className="text-[11px] text-muted-foreground">{plan}</UsageName>
          ) : null}
          {shownModel && (
            <UsageName className="text-xs text-muted-foreground">{shownModel}</UsageName>
          )}
          <UsageBalances account={account} compact />
          {(amount?.totals.partial_rows ?? 0) > 0 ? (
            <span className="text-[10px] text-muted-foreground">{t("incomplete")}</span>
          ) : null}
        </div>
        {floorControl}
        <UsageErrorMark account={account} />
        <div className="flex shrink-0 flex-col items-end leading-tight">
          {amount &&
          (tokens
            ? amount.totals.tokens !== null
            : amount.totals.cost_usd !== null || amount.totals.unpriced > 0) ? (
            <span className="text-xs">
              <UsageAmountFigure amount={amount} tokens={tokens} />
            </span>
          ) : null}
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

  const balanceGroups = account.balances ?? [];
  const wideBoxes = picks.length + balanceGroups.length > 1;
  const mainOf = (pick: (typeof picks)[number]) =>
    pick.pinned ? pick.window : (pick.group.windows.at(-1) ?? null);
  const firstMain = picks[0] ? mainOf(picks[0]) : null;
  const displayTone = (window: typeof firstMain) =>
    stale || empty || !window ? "mute" : stateTone(window.state);
  return (
    <article
      className={cn("usage-tile", wideBoxes && "usage-tile-wide")}
      data-tone={!wideBoxes ? displayTone(firstMain) : undefined}
      data-testid="usage-tile"
      data-account={account.id}
      onClick={(event) => {
        if (!wideBoxes && !arrange && !(event.target as HTMLElement).closest("a, button"))
          activate();
      }}
    >
      <div className="usage-tile-head">
        <button type="button" className="usage-tile-title" onClick={activate}>
          <UsageName>{name}</UsageName>
        </button>
        {plan ? <UsageName className="usage-tile-plan">{plan}</UsageName> : null}
        <ReadingButton account={account} />
        <UsageErrorMark account={account} />
      </div>
      <div className="usage-units">
        {(picks.length
          ? picks
          : balanceGroups.length
            ? []
            : [{ group: { key: "", tag: null, windows: [] }, window: null, pinned: false }]
        ).map(({ group, window, pinned }) => {
          const main = mainOf({ group, window, pinned });
          return (
            <Link
              key={group.key}
              className={cn("usage-unit", wideBoxes && "usage-box")}
              data-tone={wideBoxes ? displayTone(main) : undefined}
              aria-label={`${name}${group.tag ? ` ${group.tag}` : ""}`}
              to={boardPath(
                `/usage/board/${encodeURIComponent(account.id)}${group.key ? `?focus=${encodeURIComponent(group.key)}` : ""}`,
              )}
              onClick={(event) => {
                if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
                event.preventDefault();
                if (group.key && onOpenGroup) onOpenGroup(group.key);
                else activate();
              }}
            >
              {wideBoxes ? (
                <UsageName className="usage-unit-tag">{group.tag || "\u00a0"}</UsageName>
              ) : null}
              <UsageTip
                className="usage-ring-trigger"
                model={
                  main
                    ? windowTip(account, main, group.tag, {
                        pinned,
                        stale: stale || empty,
                        auto: !pinnedWindowId || pinnedWindowId === "auto",
                      })
                    : readingTip(account)
                }
              >
                <UsageRing window={main} stale={stale || empty} size={92} />
              </UsageTip>
              <span className={cn("usage-countdown", stale && "text-muted-foreground")}>
                {main ? formatTimeLeft(main.time_left_s) || "—" : t("notReported")}
              </span>
              {pinned ? (
                <UsageName className="usage-tile-plan">
                  {`${t("pinned")} ${main ? windowName(main) : ""}`}
                </UsageName>
              ) : null}
              {!pinned ? (
                <div className="usage-satellites">
                  {group.windows
                    .filter((item) => item !== main)
                    .map((item) => (
                      <div key={item.id} className="usage-satellite">
                        <UsageRing
                          window={item}
                          stale={stale || empty}
                          size={16}
                          showName={false}
                        />
                        <UsageName>{windowName(item)}</UsageName>
                        <b>
                          {item.percent_used?.toLocaleString("en-US", {
                            minimumFractionDigits: 2,
                            maximumFractionDigits: 2,
                          }) ?? "—"}
                          %
                        </b>
                        <span>{formatTimeLeft(item.time_left_s)}</span>
                      </div>
                    ))}
                </div>
              ) : null}
            </Link>
          );
        })}
        {balanceGroups.map((balance) => (
          <Link
            key={`balance:${balance.id}`}
            className={cn("usage-unit", wideBoxes && "usage-box")}
            aria-label={`${name} ${balance.label ?? balance.group ?? t("balance")}`}
            onClick={(event) => {
              if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
              event.preventDefault();
              if (onOpenGroup) onOpenGroup(balance.group ?? balance.id);
              else activate();
            }}
            to={boardPath(
              `/usage/board/${encodeURIComponent(account.id)}?focus=${encodeURIComponent(balance.group ?? balance.id)}`,
            )}
          >
            {wideBoxes ? (
              <UsageName className="usage-unit-tag">{balance.group || "\u00a0"}</UsageName>
            ) : null}
            <UsageBalances tile account={{ ...account, balances: [balance] }} />
          </Link>
        ))}
      </div>
      {(amount?.totals.partial_rows ?? 0) > 0 ? (
        <p className="text-center text-[11px] text-muted-foreground">{t("incomplete")}</p>
      ) : null}
      {arrange ? (
        <div className="usage-tile-footer">
          <GripVerticalIcon className="size-3.5" />
          <button
            type="button"
            aria-label={`Move ${name} back`}
            className="min-h-11 min-w-11 md:min-h-7"
            onClick={() => onMove?.(-1)}
          >
            <ChevronLeftIcon className="size-4" />
          </button>
          <button
            type="button"
            aria-label={`Move ${name} forward`}
            className="min-h-11 min-w-11 md:min-h-7"
            onClick={() => onMove?.(1)}
          >
            <ChevronRightIcon className="size-4" />
          </button>
        </div>
      ) : (
        <div className="usage-tile-footer">
          {renewal && renewDays !== null ? (
            <UsageTip model={renewalTip(renewal.at, renewal.official)}>
              <span>
                <CalendarClockIcon className="inline size-3" /> <b>{renewDays}d</b>
              </span>
            </UsageTip>
          ) : (
            <span>{t("renewalUnknown")}</span>
          )}
          <span>
            {t("periodCost")}{" "}
            <b>
              {amount && amount.totals.cost_usd !== null ? (
                <UsageAmountFigure amount={amount} tokens={false} />
              ) : (
                "—"
              )}
            </b>
          </span>
        </div>
      )}
    </article>
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
