// The Usage board: one band of tiles per group, each tile a ring per window.
// Display only — every setting lives in Settings or the account's own settings
// page, reached from a tile. Arranging (groups and hand order) is the one edit
// on this page, and it is a mode the visitor opens and closes.

import { useState } from "react";
import { Link, useNavigate } from "@/lib/routing";
import {
  CheckIcon,
  ChevronDownIcon,
  ChevronRightIcon,
  ChevronUpIcon,
  LayoutGridIcon,
  ListIcon,
  MoveIcon,
  PlusIcon,
  SettingsIcon,
  TrashIcon,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { cn } from "@/lib/utils";
import { patchUsageBoardPreferences, type UsageBoardBand } from "@/lib/usageContextPreferences";
import { useUsageContextPreferences } from "@/hooks/useUsageContextPreferences";

import { UsageBoardFrame } from "./UsageBoardFrame";
import { boardBands, moveTile, seedBands } from "./usageBoardLayout";
import { UsageTile } from "./UsageTile";
import { useUsageOverview, type UsageAccountView } from "./usageBoardData";

/** The renewal a tile counts down to: the provider's date wins over the user's. */
export function renewalOf(
  account: Pick<UsageAccountView, "renews_at">,
  stored: number | undefined,
): { at: number; official: boolean } | null {
  if (account.renews_at !== null) return { at: account.renews_at * 1000, official: true };
  if (stored !== undefined) return { at: stored, official: false };
  return null;
}

export function UsageBoardPage() {
  const info = useServerInfo();
  const board = useUsageContextPreferences().board;
  const overview = useUsageOverview(info !== "loading" && info.usage_board_enabled === true);
  const navigate = useNavigate();
  const [arranging, setArranging] = useState(false);
  const accounts = overview.data?.accounts ?? [];
  const bands = boardBands(board, accounts);
  const total = accounts.length;

  function update(patch: Parameters<typeof patchUsageBoardPreferences>[0]) {
    patchUsageBoardPreferences(patch);
  }

  function changeBands(next: UsageBoardBand[]) {
    update({ bands: next });
  }

  function toggleBand(id: string) {
    changeBands(
      seedBands(board, accounts).map((band) =>
        band.id === id ? { ...band, open: !band.open } : band,
      ),
    );
  }

  function addBand() {
    changeBands([
      ...seedBands(board, accounts),
      { id: `g${Date.now().toString(36)}`, name: "New group", accounts: [], open: true },
    ]);
  }

  function renameBand(id: string, name: string) {
    changeBands(
      seedBands(board, accounts).map((band) => (band.id === id ? { ...band, name } : band)),
    );
  }

  function moveBand(id: string, direction: -1 | 1) {
    const existing = seedBands(board, accounts);
    const index = existing.findIndex((band) => band.id === id);
    const target = index + direction;
    if (index < 0 || target < 0 || target >= existing.length) return;
    const next = [...existing];
    const [band] = next.splice(index, 1);
    next.splice(target, 0, band);
    changeBands(next);
  }

  function removeBand(id: string) {
    changeBands(seedBands(board, accounts).filter((band) => band.id !== id));
  }

  function nudgeTile(accountId: string, direction: -1 | 1) {
    changeBands(moveTile(seedBands(board, accounts), accounts, board, accountId, direction));
  }

  return (
    <UsageBoardFrame
      title="Usage board"
      loading={overview.isPending}
      error={overview.error}
      actions={
        arranging ? (
          <>
            <Button variant="outline" size="sm" onClick={addBand} className="gap-1.5">
              <PlusIcon className="size-3.5" aria-hidden="true" />
              Group
            </Button>
            <Button size="sm" onClick={() => setArranging(false)} className="gap-1.5">
              <CheckIcon className="size-3.5" aria-hidden="true" />
              Done
            </Button>
          </>
        ) : (
          <>
            <Button
              variant="ghost"
              size="icon"
              aria-label="Arrange groups and order"
              title="Arrange"
              onClick={() => {
                // Arranging needs the ring view: a compact row has no room for
                // handles, and switching here is one line instead of a second
                // arrangement layout.
                update({ bands: seedBands(board, accounts), density: "std" });
                setArranging(true);
              }}
            >
              <MoveIcon className="size-4" aria-hidden="true" />
            </Button>
            <Button
              variant="ghost"
              size="icon"
              aria-label={board.density === "std" ? "Compact view" : "Rings view"}
              title={board.density === "std" ? "Compact view" : "Rings view"}
              onClick={() => update({ density: board.density === "std" ? "mini" : "std" })}
            >
              {board.density === "std" ? (
                <ListIcon className="size-4" aria-hidden="true" />
              ) : (
                <LayoutGridIcon className="size-4" aria-hidden="true" />
              )}
            </Button>
            <Button variant="ghost" size="icon" asChild aria-label="Usage board settings">
              <Link to="/settings/general#usage-board">
                <SettingsIcon className="size-4" aria-hidden="true" />
              </Link>
            </Button>
          </>
        )
      }
    >
      {!arranging && total === 0 ? (
        <EmptyBoard />
      ) : (
        <div className="flex flex-col gap-2.5">
          {bands.map((band, index) => {
            const open = band.open || arranging;
            // An empty group is only worth a row while it is being arranged.
            if (band.accounts.length === 0 && !arranging) return null;
            return (
              <section
                key={band.id}
                className={cn(
                  "rounded-[14px] bg-foreground/[0.035] px-2 pb-2 pt-0.5",
                  arranging && "outline-dashed outline-1 outline-foreground/30 -outline-offset-1",
                )}
                data-testid="usage-band"
                data-band={band.id}
              >
                <div className="flex min-h-[30px] min-w-0 items-center gap-1.5">
                  {arranging ? (
                    <Input
                      aria-label={`Group name for ${band.name}`}
                      value={band.name}
                      onChange={(event) => renameBand(band.id, event.target.value)}
                      className="h-7 max-w-[260px] flex-1 text-[13px] font-semibold"
                    />
                  ) : (
                    <button
                      type="button"
                      className="flex min-w-0 items-center gap-1.5 text-[13px] font-semibold text-foreground/80"
                      aria-expanded={open}
                      onClick={() => toggleBand(band.id)}
                    >
                      {open ? (
                        <ChevronDownIcon className="size-3.5 shrink-0" aria-hidden="true" />
                      ) : (
                        <ChevronRightIcon className="size-3.5 shrink-0" aria-hidden="true" />
                      )}
                      <span className="truncate">{band.name}</span>
                    </button>
                  )}
                  <span className="text-[11px] text-muted-foreground">{band.accounts.length}</span>
                  {arranging ? (
                    <span className="ml-auto flex items-center">
                      <Button
                        variant="ghost"
                        size="icon"
                        className="size-7"
                        aria-label={`Move group ${band.name} up`}
                        disabled={index === 0}
                        onClick={() => moveBand(band.id, -1)}
                      >
                        <ChevronUpIcon className="size-3.5" aria-hidden="true" />
                      </Button>
                      <Button
                        variant="ghost"
                        size="icon"
                        className="size-7"
                        aria-label={`Move group ${band.name} down`}
                        disabled={index === bands.length - 1}
                        onClick={() => moveBand(band.id, 1)}
                      >
                        <ChevronDownIcon className="size-3.5" aria-hidden="true" />
                      </Button>
                      <Button
                        variant="ghost"
                        size="icon"
                        className="size-7"
                        aria-label={`Remove group ${band.name}`}
                        disabled={band.accounts.length > 0}
                        onClick={() => removeBand(band.id)}
                      >
                        <TrashIcon className="size-3.5" aria-hidden="true" />
                      </Button>
                    </span>
                  ) : null}
                </div>
                {open ? (
                  <div
                    className={cn(
                      "grid",
                      board.density === "mini"
                        ? "grid-cols-[repeat(auto-fill,minmax(236px,1fr))] gap-1.5"
                        : "grid-cols-[repeat(auto-fill,minmax(146px,1fr))] gap-2",
                    )}
                  >
                    {band.accounts.map((account) => (
                      <UsageTile
                        key={account.id}
                        account={account}
                        pinnedWindowId={board.windows[account.id]}
                        renewal={renewalOf(account, board.renewals[account.id])}
                        density={board.density}
                        arrange={arranging}
                        onOpen={() => navigate(`/usage/board/${encodeURIComponent(account.id)}`)}
                        onMove={(direction) => nudgeTile(account.id, direction)}
                      />
                    ))}
                  </div>
                ) : null}
              </section>
            );
          })}
        </div>
      )}
    </UsageBoardFrame>
  );
}

function EmptyBoard() {
  return (
    <div className="flex flex-col items-center gap-2 rounded-[11px] border border-dashed border-border px-6 py-10 text-center">
      <p className="text-sm font-medium">Nothing on the board yet</p>
      <p className="max-w-sm text-xs text-muted-foreground">
        Turn an account on under <span className="font-medium text-foreground">On board</span> in
        Settings, or connect a source so its first report adds a tile. Claude and Codex appear once
        a host has read them.
      </p>
      <Button variant="outline" size="sm" asChild className="mt-1">
        <Link to="/settings/general#usage-board">Open settings</Link>
      </Button>
    </div>
  );
}
