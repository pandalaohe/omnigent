import type { BoardTask } from "./taskBoardData";
import type { TaskDisplay } from "./taskBoardDisplay";

export const sourcePalette = [
  "#ee352e",
  "#0091c8",
  "#00933c",
  "#b933ad",
  "#ff6319",
  "#d4972a",
  "#00a1a7",
  "#7c5cdb",
  "#df3c85",
  "#6cbe45",
  "#996633",
  "#2f7fd4",
];
export function boardSourceColors(tasks: BoardTask[]) {
  const counts = new Map<string, number>();
  for (const task of tasks)
    counts.set(task["omnigent.ai:source"], (counts.get(task["omnigent.ai:source"]) ?? 0) + 1);
  const order = [...counts]
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .map(([id]) => id);
  return new Map(order.map((id, index) => [id, sourcePalette[index] ?? "var(--muted-foreground)"]));
}

export function boardFiles(rows: NonNullable<TaskDisplay["rows"]>) {
  const seen = new Set<string>();
  const files = rows
    .filter(
      (row) => /^path(?:[._-]|$)/.test(row.id) && !seen.has(row.value) && !!seen.add(row.value),
    )
    .map((row) => {
      const path = row.value,
        parts = path.split(/[\\/]/),
        base = parts.at(-1) ?? path;
      const ext = base.match(/\.([a-z0-9]+)$/i)?.[1] ?? "";
      const stem = ext ? base.slice(0, -ext.length - 1) : base;
      const kind = stem.match(/_(pretask|design|report|evidence|spec|research)$/)?.[1] ?? row.label;
      const name =
        stem
          .replace(/^\d{6}_/, "")
          .replace(/^T\d{6}-[A-Z]+-[A-Z]+-\d+_/, "")
          .replace(/_(pretask|design|report|evidence|spec|research)$/, "")
          .replace(/[-_]+/g, " ")
          .trim() || base;
      return {
        path,
        name,
        kind,
        ext,
        old: parts.includes("generation-history"),
        version: parts.at(-2) ?? "",
      };
    });
  return files.map((file) => ({
    ...file,
    showExt: files.some(
      (other) =>
        other.path !== file.path &&
        other.old === file.old &&
        other.name === file.name &&
        other.kind === file.kind &&
        other.ext !== file.ext,
    ),
  }));
}
