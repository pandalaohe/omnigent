// One account's settings. Reached only from its tile: the windows it shows, the
// renewal countdown, and removing a reported source. Every change applies at
// once — no save button — and the tile beside the rows is the preview.

import { useState } from "react";
import { Link, useNavigate, useParams } from "@/lib/routing";

import { HelpTip } from "@/components/HelpTip";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { cn } from "@/lib/utils";
import {
  patchUsageBoardPreferences,
  readUsageContextPreferences,
  writeUsageContextPreferences,
  type UsageBoardPreferences,
} from "@/lib/usageContextPreferences";
import { useUsageContextPreferences } from "@/hooks/useUsageContextPreferences";

import { UsageBoardFrame } from "./UsageBoardFrame";
import { UsageTile } from "./UsageTile";
import { deleteUsageAccount, useUsageOverview, type UsageAccountView } from "./usageBoardData";
import { isPercentWindow, windowName } from "./usageBoardFormat";
import { AUTO_WINDOW, fromDateInput, pickWindow, toDateInput } from "./usageBoardSelect";
import { renewalOf } from "./UsageBoardPage";

export function UsageAccountSettingsPage() {
  const { accountId = "" } = useParams();
  const info = useServerInfo();
  const preferences = useUsageContextPreferences();
  const board = preferences.board;
  const overview = useUsageOverview(info !== "loading" && info.usage_board_enabled === true);
  const navigate = useNavigate();
  const account = overview.data?.accounts.find((item) => item.id === accountId);
  const [confirmingRemove, setConfirmingRemove] = useState(false);
  const [removing, setRemoving] = useState(false);
  const [removeError, setRemoveError] = useState<string | null>(null);

  function update(patch: Partial<UsageBoardPreferences>) {
    patchUsageBoardPreferences(patch);
  }

  if (!overview.isPending && !account) {
    return (
      <UsageBoardFrame title="Account settings" back={{ to: "/usage/board", label: "Usage board" }}>
        <p className="text-sm text-muted-foreground">This account is not on the board any more.</p>
      </UsageBoardFrame>
    );
  }

  return (
    <UsageBoardFrame
      title="Account settings"
      back={{
        to: `/usage/board/${encodeURIComponent(accountId)}`,
        label: account?.display_name ?? "Account",
      }}
      loading={overview.isPending}
      error={overview.error}
    >
      {account ? (
        <div className="grid max-w-[760px] gap-3.5 sm:grid-cols-[172px_minmax(0,1fr)] sm:items-start">
          <div className="sm:sticky sm:top-0">
            <UsageTile
              account={account}
              pinnedWindowId={board.windows[account.id]}
              renewal={renewalOf(account, board.renewals[account.id])}
              density={board.density}
              shownModel={preferences.shownModels?.[account.id]}
              onOpen={() => navigate(`/usage/board/${encodeURIComponent(account.id)}`)}
            />
          </div>
          <div className="rounded-[11px] border border-border bg-card px-3.5 py-1">
            <Link
              to={`/usage/board/${encodeURIComponent(account.id)}/models`}
              className="flex min-h-12 items-center justify-between border-b border-border py-2 text-sm"
            >
              Models <span aria-hidden="true">›</span>
            </Link>
            <label className="flex min-h-12 flex-wrap items-center gap-2 py-2 text-sm">
              Shown model
              <Input
                aria-label="Shown model"
                className="h-8 w-52"
                maxLength={128}
                value={preferences.shownModels?.[account.id] ?? ""}
                list="shown-model-options"
                placeholder="None"
                onChange={(event) => {
                  const current = readUsageContextPreferences();
                  const shownModels = { ...current.shownModels };
                  if (event.target.value) shownModels[account.id] = event.target.value;
                  else Reflect.deleteProperty(shownModels, account.id);
                  writeUsageContextPreferences({ ...current, shownModels });
                }}
              />
              <datalist id="shown-model-options">
                {account.models?.map((model) => (
                  <option key={model.id} value={model.id} />
                ))}
              </datalist>
            </label>
            <WindowRow
              account={account}
              board={board}
              onPick={(id) => {
                update({ windows: { ...board.windows, [account.id]: id } });
              }}
            />
            <RenewalRow
              account={account}
              stored={board.renewals[account.id]}
              onChange={(epoch) => {
                const renewals: Record<string, number> = Object.fromEntries(
                  Object.entries(board.renewals).filter(([key]) => key !== account.id),
                );
                if (epoch !== null) renewals[account.id] = epoch;
                update({ renewals });
              }}
            />
            {account.kind === "account" ? (
              <div className="flex min-h-[46px] flex-wrap items-center justify-between gap-2 border-t border-border py-2">
                <span />
                <div className="flex items-center gap-2">
                  <Button
                    variant="destructive"
                    size="sm"
                    disabled={removing}
                    onClick={async () => {
                      if (!confirmingRemove) {
                        setConfirmingRemove(true);
                        return;
                      }
                      setRemoving(true);
                      setRemoveError(null);
                      try {
                        await deleteUsageAccount(account.id);
                        await overview.refetch();
                        navigate("/usage/board");
                      } catch (error) {
                        setRemoveError(
                          error instanceof Error ? error.message : "Could not remove the account.",
                        );
                        setRemoving(false);
                      }
                    }}
                  >
                    {confirmingRemove ? "Confirm remove" : "Remove"}
                  </Button>
                  {confirmingRemove && !removing ? (
                    <Button variant="ghost" size="sm" onClick={() => setConfirmingRemove(false)}>
                      Keep
                    </Button>
                  ) : null}
                </div>
              </div>
            ) : null}
            {confirmingRemove && account.kind === "account" ? (
              <p className="pb-2 text-[11px] text-muted-foreground">
                Removing deletes the stored reading. A source that is still sending brings it back
                with its next report.
              </p>
            ) : null}
            {removeError ? (
              <p role="alert" className="pb-2 text-[11px] text-destructive">
                {removeError}
              </p>
            ) : null}
          </div>
        </div>
      ) : null}
    </UsageBoardFrame>
  );
}

function WindowRow({
  account,
  board,
  onPick,
}: {
  account: UsageAccountView;
  board: UsageBoardPreferences;
  onPick: (windowId: string) => void;
}) {
  const timed = account.windows.filter(isPercentWindow);
  // One window means nothing to choose: the row is absent.
  if (timed.length < 2) return null;
  const current = board.windows[account.id] ?? AUTO_WINDOW;
  const picks = timed.filter((window) => window.id === current);
  const decided = pickWindow(
    { key: "", tag: null, windows: account.windows },
    account.deciding_window,
    current,
  ).window;
  const options: { id: string; label: string; detail: string }[] = [
    {
      id: AUTO_WINDOW,
      label: "Auto",
      detail: decided ? `Now: ${windowName(decided)}` : "Most urgent",
    },
    ...timed.map((window) => ({
      id: window.id,
      label: windowName(window),
      detail: window.percent_used !== null ? `${Math.round(window.percent_used)}% used` : "",
    })),
  ];
  return (
    <div className="flex min-h-[46px] flex-wrap items-center justify-between gap-2 border-t border-border py-2 first:border-t-0">
      <span className="flex items-center gap-1.5 text-sm font-normal">
        Window
        <HelpTip label="About the window">
          Auto shows the window that runs out soonest. A window the provider stops reporting leaves
          the board; your pick comes back with it.
        </HelpTip>
      </span>
      <div
        className="inline-flex max-w-full flex-wrap gap-0.5 rounded-lg bg-muted p-0.5"
        role="radiogroup"
        aria-label="Window"
      >
        {options.map((option) => (
          <button
            key={option.id}
            type="button"
            role="radio"
            aria-checked={
              current === option.id ||
              (option.id === AUTO_WINDOW && picks.length === 0 && current === AUTO_WINDOW)
            }
            onClick={() => onPick(option.id)}
            className={cn(
              "min-h-7 rounded-md px-2 text-xs",
              current === option.id
                ? "bg-background font-medium text-foreground shadow-sm"
                : "text-muted-foreground hover:text-foreground",
            )}
          >
            {option.label}
          </button>
        ))}
      </div>
    </div>
  );
}

function RenewalRow({
  account,
  stored,
  onChange,
}: {
  account: UsageAccountView;
  stored: number | undefined;
  onChange: (epochMs: number | null) => void;
}) {
  const official = account.renews_at !== null;
  if (!official && stored === undefined) {
    // Nothing to count down to yet, and the source gives no date: offer one.
    return (
      <div className="flex min-h-[46px] flex-wrap items-center justify-between gap-2 border-t border-border py-2">
        <label htmlFor="usage-renewal" className="text-sm font-normal">
          Renews
        </label>
        <Input
          id="usage-renewal"
          type="date"
          className="h-8 w-[150px]"
          onChange={(event) => onChange(fromDateInput(event.target.value))}
        />
      </div>
    );
  }
  const epoch = official ? (account.renews_at ?? 0) * 1000 : (stored ?? 0);
  return (
    <div className="flex min-h-[46px] flex-wrap items-center justify-between gap-2 border-t border-border py-2">
      <label htmlFor={official ? undefined : "usage-renewal"} className="text-sm font-normal">
        Renews
      </label>
      {official ? (
        <span className="text-sm" title="From the provider.">
          {new Date(epoch).toLocaleDateString("en-US", {
            month: "short",
            day: "numeric",
            year: "numeric",
          })}
        </span>
      ) : (
        <Input
          id="usage-renewal"
          type="date"
          className="h-8 w-[150px]"
          value={toDateInput(epoch)}
          onChange={(event) => onChange(fromDateInput(event.target.value))}
        />
      )}
    </div>
  );
}
