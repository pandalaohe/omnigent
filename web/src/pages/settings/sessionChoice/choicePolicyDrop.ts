import type { SessionChoiceDocument } from "./sessionChoiceData";
import { insertAtAnchor, type DropIntent } from "./choiceDrop";

export function applyPolicyDrop(
  document: SessionChoiceDocument,
  host: string,
  work: string | undefined,
  intent: DropIntent,
) {
  const from = intent.source.data;
  const to = intent.target.data;
  if (!intent.valid || intent.source.id === intent.target.id) return document;
  if (from.kind === "work" && to.kind === "work") {
    const source = document.workTypes.find((item) => item.id === from.work);
    const anchor = document.workTypes.find((item) => item.id === to.work);
    if (!source || !anchor) return document;
    return {
      ...document,
      workTypes: insertAtAnchor(document.workTypes, source, anchor, intent.edge),
    };
  }
  if (
    (from.kind === "preset" || from.kind === "ability") &&
    (to.kind === "ability" || to.kind === "ability-level")
  ) {
    const levels = document.abilityOrder.map((level) => [...level]);
    const anchor = to.kind === "ability" ? to.preset : undefined;
    if (from.kind === "preset" && levels.some((level) => level.includes(from.preset)))
      return document;
    if (from.kind === "ability")
      levels[from.level] = levels[from.level].filter((id) => id !== from.preset);
    while (levels.length <= to.level) levels.push([]);
    levels[to.level] =
      to.kind === "ability-level" && intent.edge === "before"
        ? [from.preset, ...levels[to.level]]
        : insertAtAnchor(levels[to.level], from.preset, anchor, intent.edge);
    return { ...document, abilityOrder: levels };
  }
  if (
    !work ||
    (from.kind !== "preset" && from.kind !== "row") ||
    (to.kind !== "row" && to.kind !== "tier")
  )
    return document;
  const tiers = document.hosts[host]?.[work];
  if (!tiers?.[to.tier]) return document;
  const next = tiers.map((rows) => [...rows]);
  const anchor = to.kind === "row" ? tiers[to.tier][to.row] : undefined;
  const preset =
    from.kind === "preset" ? document.presets.find((item) => item.id === from.preset) : undefined;
  const row =
    from.kind === "row"
      ? tiers[from.tier]?.[from.row]
      : preset && { preset: preset.id, name: preset.name, links: structuredClone(preset.links) };
  if (!row || row === anchor) return document;
  if (from.kind === "row") next[from.tier].splice(from.row, 1);
  next[to.tier] =
    to.kind === "tier" && intent.edge === "before"
      ? [row, ...next[to.tier]]
      : insertAtAnchor(next[to.tier], row, anchor, intent.edge);
  return {
    ...document,
    hosts: { ...document.hosts, [host]: { ...document.hosts[host], [work]: next } },
  };
}
