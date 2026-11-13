import { expect, it } from "vitest";
import { boardFiles, boardSourceColors } from "./taskBoardVisual";
import type { BoardTask } from "./taskBoardData";

it("ranks sources consistently, assigns twelve distinct colors, and gives the remainder a neutral color", () => {
  const tasks = Array.from(
    { length: 13 },
    (_, i) => ({ uid: String(i), "omnigent.ai:source": `source-${i}` }) as BoardTask,
  );
  tasks.push({ ...tasks[5], uid: "extra" });
  const colors = boardSourceColors(tasks);
  expect([...colors.keys()][0]).toBe("source-5");
  expect(new Set([...colors.values()].slice(0, 12)).size).toBe(12);
  expect([...colors.values()][12]).toBe("var(--muted-foreground)");
  expect([...boardSourceColors([...tasks].reverse())]).toEqual([...colors]);
});
it("removes date, task and type affixes and distinguishes otherwise equal file extensions", () => {
  const rows = ["html", "md"].map((ext, i) => ({
    id: `path-${i}`,
    label: "Design",
    value: `/opt/work/261024_T260101-EX-AG-001_task-placement_design.${ext}`,
  }));
  const files = boardFiles([...rows, { ...rows[0], id: "path-2" }]);
  expect(files).toHaveLength(2);
  expect(files.map((f) => [f.name, f.kind, f.showExt])).toEqual([
    ["task placement", "design", true],
    ["task placement", "design", true],
  ]);
});

it.each(["", " ", "—", "-", "none", "n/a"])(
  "does not turn a blank artifact placeholder %j into a file",
  (value) => {
    expect(boardFiles([{ id: "path-0", label: "Design", value }])).toEqual([]);
  },
);
