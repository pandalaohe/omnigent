import { describe, expect, it } from "vitest";
import { groupIdentity, taskIdentity, type BoardTask } from "./taskBoardData";
import {
  changeWindow,
  DAY,
  HOUR,
  initialWindow,
  MIN_SPAN,
  routeLayout,
  routeRows,
  routeTicks,
  segmentHitsRect,
  taskRelations,
  taskLocalTime,
  taskTime,
  detailLevel,
} from "./taskBoardRoutes";
import { sessionTree } from "./taskBoardNative";
import { taskBoardFixture } from "./taskBoardFixtures";

describe("route viewport", () => {
  it("uses the accepted two-day and fourteen-day detail boundaries", () => {
    expect(detailLevel(2 * DAY)).toBe("station");
    expect(detailLevel(2 * DAY + 1)).toBe("phase");
    expect(detailLevel(14 * DAY + 1)).toBe("segment");
  });
  it("reads JSCalendar wall-clock times in the supplied IANA zone", () => {
    expect(taskLocalTime("2026-11-06T09:30:00", "America/New_York")).toBe(
      Date.parse("2026-11-06T14:30:00Z"),
    );
    expect(taskLocalTime("2026-07-06T09:30:00", "America/New_York")).toBe(
      Date.parse("2026-07-06T13:30:00Z"),
    );
  });
  it("zooms against a fixed right edge and stops at twelve hours", () => {
    const view = initialWindow(1_800_000_000_000);
    const zoom = changeWindow(view, -5000, true);
    expect(zoom.to).toBe(view.to);
    expect(zoom.to - zoom.from).toBeGreaterThanOrEqual(MIN_SPAN);
    expect(changeWindow({ from: 0, to: MIN_SPAN }, -1000, true)).toEqual({ from: 0, to: MIN_SPAN });
    const pan = changeWindow(view, 200, false);
    expect(pan.to - pan.from).toBe(view.to - view.from);
    expect(pan.from).toBeGreaterThan(view.from);
  });
  it("places calendar ticks in the browser zone", () => {
    const from = +new Date(2026, 9, 20),
      to = +new Date(2026, 10, 20);
    const ticks = routeTicks({ from, to }, 600);
    expect(ticks.length).toBeGreaterThan(1);
    expect(ticks.every((date) => date.getHours() === 0 && date.getMinutes() === 0)).toBe(true);
  });
});
describe("generic task relationships and layout", () => {
  it("keeps chronological clusters forward and running stops independent", () => {
    const now = 1_800_000_000_000;
    const make = (uid: string, progress: BoardTask["progress"], offset: number): BoardTask => ({
      uid,
      title: uid,
      progress,
      "omnigent.ai:source": "one",
      created: new Date(now + offset).toISOString(),
      progressUpdated: progress === "completed" ? new Date(now + offset).toISOString() : undefined,
    });
    const tasks = [
      make("closed", "completed", -HOUR),
      make("open", "needs-action", -2 * HOUR),
      make("live", "in-process", -DAY),
      make("future", "needs-action", HOUR),
    ];
    const layout = routeLayout(
      routeRows(tasks, [{ id: "one", name: null, source: null }]),
      "time",
      initialWindow(now),
      900,
      now,
      (node) => node.id,
    );
    expect(layout.nodes.every((node) => node.x1 >= node.x)).toBe(true);
    expect(
      layout.nodes
        .find((node) => node.tasks.some((task) => task.uid === "live"))
        ?.tasks.map((task) => task.uid),
    ).toEqual(["live"]);
    expect(
      taskTime(
        {
          ...tasks[0],
          created: "2020-01-01T00:00:00Z",
          progressUpdated: undefined,
          "omnigent.ai:closedSeen": new Date(now).toISOString(),
        },
        now,
      ),
    ).toBe(now);
  });
  it("keeps equal uids in separate sources and resolves only an unambiguous cross-source target", () => {
    const make = (source: string, uid: string, relatedTo?: BoardTask["relatedTo"]): BoardTask => ({
      uid,
      "omnigent.ai:source": source,
      relatedTo,
    });
    const a = make("one", "same", {
      unique: { relation: { "omnigent.ai:depends-on": true } },
      missing: { relation: { next: true } },
    });
    const b = make("two", "same");
    const unique = make("two", "unique", { same: { relation: { parent: true } } });
    const rows = routeRows(
      [a, b, unique],
      [
        { id: "one", name: null, source: null },
        { id: "two", name: null, source: null },
      ],
    );
    expect(new Set(rows.map((row) => row.id)).size).toBe(2);
    expect(taskIdentity(a)).not.toBe(taskIdentity(b));
    const edges = taskRelations([a, b, unique]);
    expect(
      edges.some((edge) => edge.kind === "dependency" && edge.from === unique && edge.to === a),
    ).toBe(true);
    expect(
      edges.some((edge) => edge.kind === "parent" && edge.from === b && edge.to === unique),
    ).toBe(true);
    expect(edges.find((edge) => edge.uid === "missing")?.to).toBeUndefined();
  });
  it("keeps dropped labels available on their station and places shown labels clear of stations and rails", () => {
    const fixture = taskBoardFixture(1_800_000_000_000);
    const rows = routeRows(fixture.tasks, fixture.sources);
    const layout = routeLayout(
      rows,
      "order",
      initialWindow(1_800_000_000_000),
      1000,
      1_800_000_000_000,
      (node) => node.tasks[0].title ?? node.id,
    );
    expect(layout.nodes.flatMap((node) => node.tasks).length).toBe(fixture.tasks.length);
    const shown = layout.nodes.filter((node) => node.label);
    expect(shown.length).toBeGreaterThan(0);
    for (const node of shown) {
      const label = node.label!;
      expect(label.x).toBeGreaterThan(190);
      expect(label.x + label.w).toBeLessThanOrEqual(1000);
      for (const rail of layout.rails)
        expect(segmentHitsRect({ x: rail.x, y: rail.y }, { x: rail.x1, y: rail.y }, label, 4)).toBe(
          false,
        );
    }
    expect(layout.edges.some((edge) => edge.long)).toBe(true);
    expect(layout.edges.some((edge) => edge.hub)).toBe(true);
    const hub = layout.edges.find((edge) => edge.hub && edge.relations.length > 1)!;
    expect(hub).toBeDefined();
    const originals = taskRelations(fixture.tasks).filter(
      (relation) =>
        relation.kind === "parent" &&
        taskIdentity(relation.from) === taskIdentity(hub.relations[0].from) &&
        relation.to &&
        groupIdentity(relation.to) === hub.to.row.id,
    );
    expect(hub.relations).toEqual(originals);
    expect(hub.relations.length).toBeGreaterThan(1);
  });
  it("clusters crowded historical stations without losing task identity", () => {
    const fixture = taskBoardFixture(1_800_000_000_000);
    const tasks = Array.from({ length: 40 }, (_, i): BoardTask => ({
      ...fixture.tasks[2],
      uid: `task-${i}`,
      created: new Date(1_800_000_000_000 - DAY + i * 1000).toISOString(),
      progressUpdated: new Date(1_800_000_000_000 - DAY + i * 1000).toISOString(),
    }));
    const rows = routeRows(tasks, fixture.sources);
    const layout = routeLayout(
      rows,
      "time",
      { from: 1_800_000_000_000 - 2 * DAY, to: 1_800_000_000_000 + HOUR },
      390,
      1_800_000_000_000,
      (node) => String(node.tasks.length),
    );
    expect(layout.nodes.length).toBeLessThan(tasks.length);
    expect(new Set(layout.nodes.flatMap((node) => node.tasks.map(taskIdentity))).size).toBe(
      tasks.length,
    );
    expect(rows[0].id).toBe(groupIdentity(tasks[0]));
  });
});
it("shows orphaned and cyclic native sessions exactly once", () => {
  const tree = sessionTree([
    { id: "a", status: "idle", parent_session_id: "b" },
    { id: "b", status: "idle", parent_session_id: "a" },
    { id: "orphan", status: "waiting", parent_session_id: "missing" },
  ]);
  expect(new Set(tree.map((row) => row.session.id)).size).toBe(3);
  expect(tree.find((row) => row.session.id === "orphan")?.depth).toBe(0);
});
