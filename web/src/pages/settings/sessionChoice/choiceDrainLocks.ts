import { sha256 } from "@noble/hashes/sha2.js";
import { bytesToHex } from "@noble/hashes/utils.js";
import type { ChoiceEnvelope, ChoiceLink, ChoiceRow } from "./sessionChoiceData";

export interface ChoiceDrainTag {
  linkIndex: number;
  since: number;
}

const utf8 = new TextEncoder();

// Match the server's sorted-key JSON for saved links, omitting nulls and fallbackMode.
function linkIdentity({ agent, effort, maxRunning, meter, model }: ChoiceLink) {
  return JSON.stringify({
    agent,
    effort: effort ?? undefined,
    maxRunning: maxRunning ?? undefined,
    meter: meter ?? undefined,
    model: model ?? undefined,
  });
}

function digest(json: string) {
  return bytesToHex(sha256(utf8.encode(json)));
}

function rowIdentity(row: ChoiceRow) {
  return digest(JSON.stringify(row.links.map((link) => digest(linkIdentity(link))).sort()));
}

function drainLinkIndex(row: ChoiceRow, savedLink: ChoiceLink) {
  const identity = linkIdentity(savedLink);
  const matches = row.links.filter((link) => linkIdentity(link) === identity);
  if (matches.length !== 1 || matches[0].fallbackMode !== "drain") return -1;
  return row.links.indexOf(matches[0]);
}

export function projectChoiceDrainTags(
  saved: ChoiceEnvelope | undefined,
  host: string,
  workType: string | undefined,
  draftTiers: ChoiceRow[][],
): Map<ChoiceRow, ChoiceDrainTag> {
  const tags = new Map<ChoiceRow, ChoiceDrainTag>();
  if (!workType || !saved?.runtime || saved.runtime.settingsRevision !== saved.revision)
    return tags;
  const savedTiers = saved.settings?.hosts[host]?.[workType];
  const locks = saved.runtime.drainLocks.filter(
    (lock) =>
      lock.hostId === host &&
      lock.workType === workType &&
      lock.settingsRevision === saved.revision,
  );
  if (!savedTiers || !locks.length) return tags;

  const savedRows = savedTiers.flat().map((row) => ({ row, identity: rowIdentity(row) }));
  const draftRows = draftTiers.flat().map((row) => ({ row, identity: rowIdentity(row) }));
  for (const lock of locks) {
    const savedMatches = savedRows.filter(({ identity }) => identity === lock.rowIdentity);
    const draftMatches = draftRows.filter(({ identity }) => identity === lock.rowIdentity);
    if (savedMatches.length !== 1 || draftMatches.length !== 1) continue;
    const savedRow = savedMatches[0].row;
    if (savedRow !== savedTiers[lock.tierIndex]?.[lock.rowPosition]) continue;
    if (drainLinkIndex(savedRow, lock.savedLink) < 1) continue;
    const row = draftMatches[0].row;
    const linkIndex = drainLinkIndex(row, lock.savedLink);
    if (linkIndex > 0) tags.set(row, { linkIndex, since: lock.since });
  }
  return tags;
}
