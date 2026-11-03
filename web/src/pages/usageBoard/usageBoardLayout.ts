// Turning the stored arrangement into what the board renders: which band each
// account sits in and in what order. A user who never arranged anything gets
// one band in the server's order, so a fresh board needs no setup.

import type { UsageAccountView } from "./usageBoardData";
import type { UsageBoardBand, UsageBoardPreferences } from "@/lib/usageContextPreferences";

export const IMPLICIT_BAND_ID = "all";
const IMPLICIT_BAND_NAME = "Accounts";

export interface RenderedBand {
  id: string;
  name: string;
  open: boolean;
  accounts: UsageAccountView[];
}

/** Accounts the user left switched on. */
export function onBoardAccounts(
  board: UsageBoardPreferences,
  accounts: UsageAccountView[],
): UsageAccountView[] {
  const hidden = new Set(board.hidden);
  return accounts.filter((account) => !hidden.has(account.id));
}

/** The stored bands, or the implicit one a fresh board renders. */
export function boardBands(
  board: UsageBoardPreferences,
  accounts: UsageAccountView[],
): RenderedBand[] {
  const live = onBoardAccounts(board, accounts);
  if (board.bands.length === 0) {
    return [{ id: IMPLICIT_BAND_ID, name: IMPLICIT_BAND_NAME, open: true, accounts: live }];
  }
  const byId = new Map(live.map((account) => [account.id, account]));
  const placed = new Set<string>();
  const bands: RenderedBand[] = board.bands.map((band) => {
    const members: UsageAccountView[] = [];
    for (const id of band.accounts) {
      const account = byId.get(id);
      if (!account || placed.has(id)) continue;
      placed.add(id);
      members.push(account);
    }
    return { id: band.id, name: band.name, open: band.open, accounts: members };
  });
  // An account a forced refresh added after the user arranged the board keeps
  // showing, at the end of the last band, rather than vanishing.
  const leftover = live.filter((account) => !placed.has(account.id));
  if (leftover.length > 0) bands[bands.length - 1].accounts.push(...leftover);
  return bands;
}

/** Materialise the current arrangement so a first edit has something to edit. */
export function seedBands(
  board: UsageBoardPreferences,
  accounts: UsageAccountView[],
): UsageBoardBand[] {
  if (board.bands.length > 0) return board.bands;
  return [
    {
      id: IMPLICIT_BAND_ID,
      name: IMPLICIT_BAND_NAME,
      accounts: onBoardAccounts(board, accounts).map((account) => account.id),
      open: true,
    },
  ];
}

/** Swap a tile with its neighbour in its own band, else hand it to the next band. */
export function moveTile(
  bands: UsageBoardBand[],
  accounts: UsageAccountView[],
  board: UsageBoardPreferences,
  accountId: string,
  direction: -1 | 1,
): UsageBoardBand[] {
  const seeded = seedBands({ ...board, bands }, accounts);
  const next = seeded.map((band) => ({ ...band, accounts: [...band.accounts] }));
  const index = next.findIndex((band) => band.accounts.includes(accountId));
  if (index === -1) return seeded;
  const band = next[index];
  const position = band.accounts.indexOf(accountId);
  const target = position + direction;
  if (target >= 0 && target < band.accounts.length) {
    const neighbour = band.accounts[target];
    band.accounts[target] = accountId;
    band.accounts[position] = neighbour;
    return next;
  }
  const other = next[index + direction];
  if (!other) return seeded;
  band.accounts.splice(position, 1);
  if (direction > 0) other.accounts.unshift(accountId);
  else other.accounts.push(accountId);
  return next;
}
