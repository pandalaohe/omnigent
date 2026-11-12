import { describe, expect, it } from "vitest";
import { taskIdentity, type BoardTask } from "./taskBoardData";
import { DAY, HOUR, MIN_SPAN, initialWindow } from "./taskBoardRouteData";
import {
  clampMapWindow,
  mapDensity,
  mapOverview,
  mapGroups,
  mapHubMembers,
  mapRelations,
  mapScene,
  orderStops,
  routeMoment,
  stepMapWindow,
  type MapLane,
} from "./taskBoardRouteModel";
import type { SourceCache } from "./taskBoardSnapshotData";

const now = Date.parse("2026-11-06T12:00:00Z");
const task = (
  uid: string,
  state: "done" | "running" | "queued" | "failed" | "user" = "done",
  group = "alpha",
): BoardTask => ({
  uid,
  title: uid,
  "omnigent.ai:source": "example",
  "omnigent.ai:group": { id: group, name: group },
  "omnigent.ai:sourceDisplay": {
    version: 1,
    state,
    lastActivity: new Date(now - HOUR).toISOString(),
  },
});
const cache = (days: SourceCache["display"]["closedDays"]): SourceCache => ({
  pin: {
    sourceGeneration: "one",
    id: "snapshot",
    asOf: new Date(now).toISOString(),
    phase: "published",
  },
  timeZone: "UTC",
  display: { version: 1, asOf: new Date(now).toISOString(), timeZone: "UTC", closedDays: days },
  tasks: [],
  history: [],
  counts: [],
});
describe("route source projection", () => {
  it("uses supplied identities, unique group colours and explicit cross-source relations without uid guessing", () => {
    const a = task("same"),
      b = { ...task("same", "running", "beta"), "omnigent.ai:source": "second" },
      c = task("third", "user", "gamma");
    a["omnigent.ai:sourceDisplay"] = {
      ...a["omnigent.ai:sourceDisplay"]!,
      identity: {
        key: "E-1",
        label: "First",
        groupId: "a",
        groupLabel: "First group",
        groupBadge: "A",
      },
      relations: [
        {
          id: "relation",
          from: { source: "example", uid: "same" },
          to: { source: "second", uid: "same" },
          kind: "dependency",
          origin: "manual",
          provenance: "Planning meeting",
          since: "2026-11-05T12:00:00Z",
        },
      ],
    };
    const groups = mapGroups(
      [a, b, c],
      [
        { id: "example", name: "Example", source: null },
        { id: "second", name: "Second", source: null },
      ],
      {},
    );
    expect(groups.find((group) => group.tasks.includes(a))).toMatchObject({
      name: "First group",
      badge: "A",
    });
    expect(
      new Set(groups.filter((group) => group.source === "example").map((group) => group.color))
        .size,
    ).toBe(2);
    expect(mapRelations([a, b, c])).toMatchObject([
      { from: a, to: b, kind: "dependency", provenance: "Planning meeting" },
    ]);
    expect(mapRelations([a, c])).toEqual([]);
    const before = mapGroups([a, c], [{ id: "example", name: null, source: null }], {}).map(
      (group) => [group.id, group.color],
    );
    c["omnigent.ai:sourceDisplay"]!.sessions = [];
    expect(
      mapGroups([a, c], [{ id: "example", name: null, source: null }], {}).map((group) => [
        group.id,
        group.color,
      ]),
    ).toEqual(before);
  });
  it("takes hub membership only from explicit full references and deduplicates it", () => {
    const a = task("a"),
      b = task("b"),
      c = { ...task("b"), "omnigent.ai:source": "second" };
    a["omnigent.ai:sourceDisplay"]!.hub = {
      scope: "explicit",
      members: [
        { source: "example", uid: "b" },
        { source: "example", uid: "b" },
        { source: "missing", uid: "b" },
      ],
    };
    expect(mapHubMembers(a, [a, b, c])).toEqual([b]);
    expect(mapHubMembers(b, [a, b, c])).toEqual([]);
  });
  it("uses supplied history time ahead of the snapshot delivery timestamp", () => {
    const old = task("old");
    old["omnigent.ai:sourceDisplay"]!.lastActivity = undefined;
    old.updated = new Date(now).toISOString();
    old["omnigent.ai:sourceDisplay"]!.historyState = {
      first: "2020-01-01T00:00:00Z",
      last: "2020-01-03T00:00:00Z",
      end: "2020-01-02T00:00:00Z",
      outcome: "completed",
      active: false,
      pinned: false,
      future: false,
    };
    expect(routeMoment(old, now)).toBe(Date.parse("2020-01-02T00:00:00Z"));
    expect(routeMoment(task("live", "running"), now)).toBe(now);
  });
});
describe("order folding and viewport", () => {
  it("keeps every attention stop and the first queued task while preserving all folded identities", () => {
    const tasks = Array.from({ length: 25 }, (_, i) =>
      task(
        String(i),
        i === 10 ? "running" : i === 11 ? "user" : i === 12 ? "failed" : i > 19 ? "queued" : "done",
      ),
    );
    const folded = orderStops(tasks, 8);
    expect(
      folded.filter((item) => item.tasks.length === 1).map((item) => item.tasks[0].uid),
    ).toEqual(expect.arrayContaining(["10", "11", "12", "20"]));
    expect(folded.flatMap((item) => item.tasks)).toEqual(tasks);
    expect(
      orderStops(tasks, 8, true)
        .filter((item) => item.tasks.length === 1)
        .map((item) => item.tasks[0].uid),
    ).toEqual(["10", "11", "12"]);
  });
  it("zooms by discrete rungs against the right edge and restores the original now window", () => {
    const oldest = now - 400 * DAY,
      original = initialWindow(now),
      out = stepMapWindow(original, 1, oldest, now);
    expect(out).toEqual({ from: now - 3 * DAY, to: now + DAY });
    expect(stepMapWindow(out, -1, oldest, now)).toEqual(original);
    const shifted = { from: now - 11 * DAY, to: now - 7 * DAY };
    expect(stepMapWindow(shifted, -1, oldest, now)).toEqual({
      from: now - 9 * DAY,
      to: now - 7 * DAY,
    });
    const floor = clampMapWindow({ from: now - HOUR, to: now }, oldest, now);
    expect(floor.to - floor.from).toBe(MIN_SPAN);
  });
  it("renders authoritative density even when old cards are no longer held", () => {
    const buckets = mapDensity(
      [],
      { from: now - 200 * DAY, to: now + DAY },
      900,
      now,
      cache([
        { date: "2026-07-01", count: 100 },
        { date: "2026-11-05", count: 7 },
      ]),
    );
    expect(buckets.reduce((total, bucket) => total + bucket.count, 0)).toBe(107);
    expect(buckets.every((bucket) => bucket.tasks.length === 0)).toBe(true);
  });
  it("uses source rollups in the overview without double counting held cards", () => {
    const held = task("held");
    const view = { from: now - 200 * DAY, to: now + DAY };
    const buckets = mapOverview(
      [held],
      { example: cache([{ date: "2026-11-05", count: 300 }]) },
      view,
      900,
      now,
    );
    expect(buckets.reduce((total, bucket) => total + bucket.count, 0)).toBe(300);
    expect(buckets).toHaveLength(240);
    expect(
      stepMapWindow({ from: now - 6 * HOUR, to: now + 6 * HOUR }, -1, now - 200 * DAY, now),
    ).toEqual({ from: now - 6 * HOUR, to: now + 6 * HOUR });
  });
  it("includes the first partial calendar day in historical density", () => {
    const buckets = mapDensity(
      [],
      { from: Date.parse("2026-11-05T09:00:00Z"), to: now },
      900,
      now,
      cache([
        { date: "2026-11-04", count: 20 },
        { date: "2026-11-05", count: 1 },
        { date: "2026-11-06", count: 7 },
      ]),
    );
    expect(buckets.reduce((sum, bucket) => sum + bucket.count, 0)).toBe(8);
  });
  it("keeps a time capsule's extent and member failures in the scene", () => {
    const tasks = [task("first"), task("second", "failed")];
    tasks.forEach((item, index) => {
      item["omnigent.ai:sourceDisplay"]!.lastActivity = new Date(
        now - HOUR + index * 10 * 60_000,
      ).toISOString();
    });
    const group = mapGroups(tasks, [{ id: "example", name: "Example", source: null }], {})[0];
    const scene = mapScene(
      [{ ...group, top: 0, height: 136, nodes: [] }],
      "time",
      initialWindow(now),
      1200,
      now,
      "station",
      [],
      new Map(),
      {},
      () => "Two stops",
    );
    expect(scene.nodes).toHaveLength(1);
    expect(scene.nodes[0].timeCluster).toBe(true);
    expect(scene.nodes[0].x1).toBeGreaterThan(scene.nodes[0].x);
    expect(scene.nodes[0].tasks).toEqual(tasks);
  });
  it("draws rounded nearby connectors and omits phone order connectors", () => {
    const a = task("from", "running"),
      b = task("to", "user", "beta");
    a.relatedTo = { to: { relation: { "omnigent.ai:depends-on": true } } };
    const rows = mapGroups([a, b], [{ id: "example", name: "Example", source: null }], {});
    const lanes = (): MapLane[] =>
      rows.map((group, index) => ({ ...group, top: index * 136, height: 136, nodes: [] }));
    const relations = mapRelations([a, b]),
      scene = mapScene(
        lanes(),
        "order",
        initialWindow(now),
        1200,
        now,
        "station",
        relations,
        new Map(),
        {},
        (node) => node.tasks[0].title!,
      );
    expect(scene.edges).toHaveLength(1);
    expect(scene.edges[0].relations[0].kind).toBe("dependency");
    expect(scene.edges[0].path).toContain("Q");
    expect(new Set(scene.nodes.flatMap((node) => node.tasks.map(taskIdentity))).size).toBe(2);
    expect(
      mapScene(
        lanes(),
        "order",
        initialWindow(now),
        390,
        now,
        "station",
        relations,
        new Map(),
        {},
        (node) => node.id,
      ).edges,
    ).toEqual([]);
  });
});
