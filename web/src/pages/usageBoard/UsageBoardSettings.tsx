import { boardExitProps, boardPath } from "@/lib/boardFrame";
// The Usage board's own group in Settings. One place for the module's switch,
// the ring colour line, which accounts are on the board, and how to connect an
// outside source. Display routes never render any of it.

import { Link } from "@/lib/routing";
import { PlugIcon, SunIcon } from "lucide-react";

import { HelpTip } from "@/components/HelpTip";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { usageBoardAvailable } from "@/lib/capabilities";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import {
  SESSION_COLLAB_DEFAULTS,
  writeSessionCollabPreferences,
} from "@/lib/sessionCollabPreferences";
import { useSessionCollabPreferences } from "@/hooks/useSessionCollabPreferences";
import { patchUsageBoardPreferences } from "@/lib/usageContextPreferences";
import { useUsageContextPreferences } from "@/hooks/useUsageContextPreferences";

import { ConnectDialog } from "./UsageSourceConnect";
import { t } from "./usageText";
import { UsageRing } from "./UsageRing";
import { UsagePriceSourceSettings } from "./UsagePriceSourceSettings";
import { useUsageOverview, type UsageWindowView } from "./usageBoardData";

/** A synthetic reading, so the colour line shows what each tier looks like. */
function sampleWindow(percentUsed: number): UsageWindowView {
  return {
    id: "sample",
    kind: "other",
    label: null,
    group: null,
    percent_used: percentUsed,
    remaining_pct: 100 - percentUsed,
    window_minutes: 10_080,
    resets_at: null,
    time_left_s: 10_080 * 30,
    pace_pct: null,
    state: "ok",
    ahead_pts: null,
    used_value: null,
    limit_value: null,
    unit: null,
    past_reset: false,
  };
}

/** The tone a sample reading would take under these tiers, as `quota_pace` does. */
function sampleState(percentUsed: number, tight: number, low: number, floor: number | null) {
  const remaining = 100 - percentUsed;
  const pace = remaining / 0.5; // half the window elapsed
  if (percentUsed >= 100) return "exhausted";
  if (floor !== null && remaining < floor) return "low";
  if (pace >= tight) return "ok";
  if (pace >= low) return "tight";
  return "low";
}

export function UsageBoardSettings() {
  const info = useServerInfo();
  const available = usageBoardAvailable(info);
  const board = useUsageContextPreferences().board;
  const collab = useSessionCollabPreferences();
  const overview = useUsageOverview(available);
  const accounts = overview.data?.accounts ?? [];
  const tight = collab.quotaTightBelowPct;
  const low = collab.quotaLowBelowPct;
  const floor = collab.quotaFloorPct;
  const defaults =
    tight === SESSION_COLLAB_DEFAULTS.quotaTightBelowPct &&
    low === SESSION_COLLAB_DEFAULTS.quotaLowBelowPct &&
    floor === SESSION_COLLAB_DEFAULTS.quotaFloorPct;

  function writeTiers(next: { tight: number; low: number; floor: number | null }) {
    writeSessionCollabPreferences({
      ...collab,
      quotaTightBelowPct: next.tight,
      quotaLowBelowPct: next.low,
      quotaFloorPct: next.floor,
    });
  }

  function toggleAccount(accountId: string, on: boolean) {
    const hidden = new Set(board.hidden);
    if (on) hidden.delete(accountId);
    else hidden.add(accountId);
    patchUsageBoardPreferences({ hidden: [...hidden] });
  }

  return (
    <div className="flex flex-col">
      <p className="text-xs text-muted-foreground">{t("preferencesHelp")}</p>
      <Link
        {...boardExitProps()}
        to="/settings/general#session-choice"
        className="py-2 text-sm text-primary underline underline-offset-2"
      >
        Session choice settings
      </Link>
      <div className="flex min-h-[46px] flex-wrap items-center justify-between gap-2 py-2">
        <span className="text-sm">Usage board</span>
        <span className="flex items-center gap-2">
          {!available ? (
            <span className="text-[11px] text-muted-foreground">
              Off for this server (<code>usage.enabled: false</code>)
            </span>
          ) : null}
          <Switch
            checked={available && board.enabled}
            disabled={!available}
            aria-label="Usage board"
            componentId="settings.usage_board.enabled"
            onCheckedChange={(checked) => patchUsageBoardPreferences({ enabled: checked })}
          />
        </span>
      </div>

      {available ? (
        <>
          <Link
            to={boardPath("/usage/board/issues")}
            className="inline-flex min-h-11 items-center border-t border-border py-2 text-sm"
          >
            Issues{" "}
            <span className="ml-auto" aria-hidden="true">
              ›
            </span>
          </Link>
          <UsagePriceSourceSettings />
          {board.enabled ? (
            <>
              <div className="flex flex-col gap-2 border-t border-border py-2.5">
                <div className="flex items-start justify-between gap-2">
                  <span className="flex flex-wrap items-center gap-1.5 text-sm">
                    Ring colors
                    <HelpTip label="About ring colors">
                      Compares what is left with the time left: 100 means exactly on track. Below
                      the amber handle a ring turns amber, below the red handle it turns red. A
                      remaining floor turns a ring red whatever the pace.
                    </HelpTip>
                  </span>
                  {!defaults ? (
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-6 px-2 text-[11px]"
                      onClick={() =>
                        writeTiers({
                          tight: SESSION_COLLAB_DEFAULTS.quotaTightBelowPct,
                          low: SESSION_COLLAB_DEFAULTS.quotaLowBelowPct,
                          floor: SESSION_COLLAB_DEFAULTS.quotaFloorPct,
                        })
                      }
                    >
                      Reset
                    </Button>
                  ) : null}
                </div>
                <ColourLine tight={tight} low={low} floor={floor} onChange={writeTiers} />
              </div>

              <div className="flex flex-col gap-1 border-t border-border py-2.5">
                <div className="flex items-center gap-1.5 text-sm">
                  On board
                  <HelpTip label="About accounts on the board">
                    One switch per account. Off means it is not on the board and not in any total;
                    its reading keeps arriving, so turning it back on needs no new report.
                  </HelpTip>
                </div>
                {accounts.length === 0 ? (
                  <p className="text-[11px] text-muted-foreground">
                    No account yet. Claude and Codex need a host that has read them; anything else
                    reports through the usage API.
                  </p>
                ) : (
                  <div className="grid gap-x-6 sm:grid-cols-2">
                    {accounts.map((account) => {
                      const on = !board.hidden.includes(account.id);
                      return (
                        <label
                          key={account.id}
                          className="flex min-h-9 items-center justify-between gap-2 text-sm"
                        >
                          <span className="flex min-w-0 items-center gap-1.5">
                            {account.kind === "native" ? (
                              <SunIcon
                                className="size-3.5 shrink-0 text-muted-foreground"
                                aria-hidden="true"
                              />
                            ) : (
                              <PlugIcon
                                className="size-3.5 shrink-0 text-muted-foreground"
                                aria-hidden="true"
                              />
                            )}
                            <span className="truncate">{account.display_name}</span>
                          </span>
                          <Switch
                            checked={on}
                            aria-label={`Show ${account.display_name} on the board`}
                            componentId="settings.usage_board.account"
                            onCheckedChange={(checked) => toggleAccount(account.id, checked)}
                          />
                        </label>
                      );
                    })}
                  </div>
                )}
              </div>
            </>
          ) : null}
          <div className="border-t border-border py-2.5">
            <ConnectDialog />
          </div>
        </>
      ) : null}
    </div>
  );
}

/** Two handles on one bar, plus the optional remaining floor. */
function ColourLine({
  tight,
  low,
  floor,
  onChange,
}: {
  tight: number;
  low: number;
  floor: number | null;
  onChange: (next: { tight: number; low: number; floor: number | null }) => void;
}) {
  const samples = [10, 30, 50, 70, 90];
  return (
    <div className="flex flex-col gap-2">
      {/* Two handles on one reading, each on its own row: an overlay range that
          covers the other one would take the pointer for whichever sits on top,
          so a drag could never reach the handle underneath. */}
      <div className="h-2.5 w-full max-w-[420px] overflow-hidden rounded-full">
        <div className="flex h-full gap-0.5">
          <span className="flex-1 bg-destructive/45" style={{ flexGrow: low }} />
          <span className="flex-1 bg-warning/45" style={{ flexGrow: Math.max(0, tight - low) }} />
          <span className="flex-1 bg-success/45" style={{ flexGrow: Math.max(0, 100 - tight) }} />
        </div>
      </div>
      <label className="flex max-w-[420px] flex-col gap-1 text-xs text-muted-foreground">
        <span className="flex items-center justify-between gap-2">
          <span>
            Amber below <b className="text-warning">{tight}</b>
          </span>
          <span className="text-[11px]">pace</span>
        </span>
        <input
          type="range"
          min={10}
          max={100}
          step={5}
          value={tight}
          aria-label={`Amber below a pace of ${tight}`}
          onChange={(event) => {
            const value = Number(event.target.value);
            onChange({
              tight: value,
              low: Math.min(low, value - 5),
              floor,
            });
          }}
          className="h-6 w-full accent-[var(--warning)]"
        />
      </label>
      <label className="flex max-w-[420px] flex-col gap-1 text-xs text-muted-foreground">
        <span className="flex items-center justify-between gap-2">
          <span>
            Red below <b className="text-destructive">{low}</b>
          </span>
          <span className="text-[11px]">pace</span>
        </span>
        <input
          type="range"
          min={5}
          max={95}
          step={5}
          value={low}
          aria-label={`Red below a pace of ${low}`}
          onChange={(event) => {
            const value = Number(event.target.value);
            onChange({
              tight: Math.max(tight, value + 5),
              low: value,
              floor,
            });
          }}
          className="h-6 w-full accent-[var(--destructive)]"
        />
      </label>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-xs text-muted-foreground">
        <span className="flex items-center gap-1.5">
          <label htmlFor="usage-floor">Red below, percent left</label>
          <Input
            id="usage-floor"
            inputMode="numeric"
            className="h-7 w-14 text-xs"
            value={floor === null ? "" : String(floor)}
            placeholder="—"
            onChange={(event) => {
              const text = event.target.value.trim();
              if (text === "") {
                onChange({ tight, low, floor: null });
                return;
              }
              const parsed = Number(text);
              if (!Number.isFinite(parsed) || parsed < 0 || parsed > 100) return;
              onChange({ tight, low, floor: Math.round(parsed) });
            }}
          />
          <span>% left</span>
        </span>
      </div>
      <div className="flex flex-wrap items-center gap-2.5" aria-hidden="true">
        {samples.map((used) => (
          <UsageRing
            key={used}
            window={{ ...sampleWindow(used), state: sampleState(used, tight, low, floor) }}
            size={46}
            showName={false}
          />
        ))}
      </div>
    </div>
  );
}
