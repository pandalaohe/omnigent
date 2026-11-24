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
it("uses supplied file labels and distinguishes otherwise equal file extensions", () => {
  const rows = ["html", "md"].map((ext, i) => ({
    id: `artifact-${i}`,
    label: "Design",
    value: `/opt/work/task-placement_design.${ext}`,
    file: { name: "task placement" },
  }));
  const files = boardFiles([...rows, { ...rows[0], id: "path-2" }]);
  expect(files).toHaveLength(2);
  expect(files.map((f) => [f.name, f.kind, f.showExt])).toEqual([
    ["task placement", "Design", true],
    ["task placement", "Design", true],
  ]);
});

it.each([
  "/opt/work/generation-history/v1/261024_release-notes_report.md",
  "C:\\work\\generation-history\\v1\\261024_release-notes_report.md",
])(
  "shows the unmodified basename and source label for %s without guessing its archive status",
  (value) => {
    expect(boardFiles([{ id: "path-0", label: "Notes", value }])).toMatchObject([
      { path: value, kind: "Notes", name: "261024_release-notes_report.md", old: false },
    ]);
  },
);

it.each(["", " ", "—", "-", "none", "n/a"])(
  "does not turn a blank artifact placeholder %j into a file",
  (value) => {
    expect(boardFiles([{ id: "path-0", label: "Design", value }])).toEqual([]);
  },
);

it("retains a placeholder-like relative path when the source explicitly declares a file", () => {
  expect(boardFiles([{ id: "path-0", label: "Notes", value: "n/a", file: {} }])).toMatchObject([
    { path: "n/a", name: "a", kind: "Notes" },
  ]);
});
