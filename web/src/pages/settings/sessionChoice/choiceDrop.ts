export type ChoiceDragData =
  | { kind: "preset"; preset: string }
  | { kind: "row"; tier: number; row: number }
  | { kind: "work"; work: string }
  | { kind: "tier"; tier: number }
  | { kind: "ability"; level: number; preset: string }
  | { kind: "ability-level"; level: number }
  | { kind: "rule"; rule: string };

export interface ChoiceDragItem {
  id: string;
  data: ChoiceDragData;
  label: string;
}
export interface DropIntent {
  source: ChoiceDragItem;
  target: ChoiceDragItem;
  edge: "before" | "after" | "empty";
  operation: "move" | "copy";
  valid: boolean;
  message: string;
}

export function resolveDropIntent(
  source: ChoiceDragItem,
  target: ChoiceDragItem,
  edge: DropIntent["edge"],
  abilityOrder: string[][] = [],
): DropIntent {
  const from = source.data;
  const to = target.data;
  const operation = from.kind === "preset" ? "copy" : "move";
  const allowed =
    (from.kind === "work" && to.kind === "work") ||
    (from.kind === "rule" && to.kind === "rule") ||
    ((from.kind === "row" || from.kind === "preset") &&
      (to.kind === "row" || to.kind === "tier")) ||
    ((from.kind === "ability" || from.kind === "preset") &&
      (to.kind === "ability" || to.kind === "ability-level"));
  const duplicate =
    from.kind === "preset" &&
    (to.kind === "ability" || to.kind === "ability-level") &&
    abilityOrder.some((level) => level.includes(from.preset));
  const valid = allowed && !duplicate;
  const container = to.kind === "tier" || to.kind === "ability-level";
  const placement =
    edge === "empty"
      ? "into"
      : container
        ? edge === "before"
          ? "at the start of"
          : "at the end of"
        : edge;
  return {
    source,
    target,
    edge,
    operation,
    valid,
    message: duplicate
      ? "Already in ability order. Move the existing entry instead."
      : !allowed
        ? "Cannot drop here."
        : `${operation === "copy" ? "Copy" : "Move"} ${source.label} ${placement} ${target.label}${edge === "empty" ? " as the first entry" : ""}`,
  };
}

/** Locate the anchor after removing the source, including downward moves. */
export function insertAtAnchor<T>(
  items: T[],
  item: T,
  anchor: T | undefined,
  edge: DropIntent["edge"],
): T[] {
  const next = items.filter((entry) => entry !== item);
  const index = anchor === undefined ? next.length : next.indexOf(anchor);
  if (anchor === item) return items;
  if (index < 0) return items;
  next.splice(index + (anchor !== undefined && edge === "after" ? 1 : 0), 0, item);
  return next;
}
