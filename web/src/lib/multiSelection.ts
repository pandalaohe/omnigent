import type { PickerItem } from "@/components/SelectionList";

export interface MultiValue {
  mode: "only" | "except";
  ids: string[];
}

export function resolveMulti(value: MultiValue, items: PickerItem[]): string[] {
  const selected = new Set(value.ids);
  return items
    .filter(
      (item) =>
        !item.unavailableReason &&
        (value.mode === "only" ? selected.has(item.id) : !selected.has(item.id)),
    )
    .map((item) => item.id);
}

export function summarizeMulti(value: MultiValue, items: PickerItem[]) {
  const applicable = items.filter((item) => !item.unavailableReason).length;
  const skipped = items.length - applicable;
  const applies = resolveMulti(value, items).length;
  const marked = value.mode === "only" ? applies : applicable - applies;
  const summary =
    value.mode === "only"
      ? marked === 0
        ? "No projects selected"
        : `${marked} selected`
      : marked === 0
        ? `All ${applicable} applicable projects`
        : `All ${applicable}, except ${marked}`;
  return {
    applies,
    applicable,
    skipped,
    text: `${summary}${skipped ? ` · ${skipped} skipped` : ""}`,
  };
}

export function suggestMode(value: MultiValue, items: PickerItem[]): MultiValue | null {
  const applicable = items.filter((item) => !item.unavailableReason);
  const active = resolveMulti(value, items);
  const marked = value.mode === "only" ? active.length : applicable.length - active.length;
  if (marked <= applicable.length / 2) return null;
  return value.mode === "only"
    ? {
        mode: "except",
        ids: applicable.filter((item) => !active.includes(item.id)).map((item) => item.id),
      }
    : { mode: "only", ids: active };
}
