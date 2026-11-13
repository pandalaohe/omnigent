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
  routeState,
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
  ...(state === "running" && {
    "omnigent.ai:workedBy": [{ session: "s", from: new Date(now - HOUR).toISOString() }],
  }),
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
  it("allocates historical colours in row order without reserving old closed board groups first", () => {
    const live = task("live", "running", "a"),
      old = task("old", "done", "m"),
      historical = task("historical", "done", "y");
    old.progressUpdated = new Date(now - 100 * DAY).toISOString();
    old["omnigent.ai:sourceDisplay"]!.historyState = {
      first: old.progressUpdated,
      last: old.progressUpdated,
      end: old.progressUpdated,
      outcome: "completed",
      active: true,
      pinned: false,
      future: false,
    };
    historical["omnigent.ai:sourceDisplay"]!.historyState = {
      ...old["omnigent.ai:sourceDisplay"]!.historyState!,
      last: new Date(now - HOUR).toISOString(),
      end: new Date(now - HOUR).toISOString(),
      active: false,
    };
    const source = [{ id: "example", name: "Example", source: null }],
      snapshot = cache([]);
    snapshot.display.groups = [
      { id: "a", label: "Current", badge: "A", rank: 0 },
      { id: "m", label: "Old", badge: "M", rank: 1 },
    ];
    const before = mapGroups([live, historical], source, { example: snapshot }, now),
      after = mapGroups([live, old, historical], source, { example: snapshot }, now);
    expect(after.find((group) => group.tasks.includes(historical))!.color).toBe(
      before.find((group) => group.tasks.includes(historical))!.color,
    );
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
  it("splits collapsed time clusters at the raw gap before widening their drawn extent", () => {
    const tasks = [task("first"), task("same-moment"), task("separate")],
      view = { from: now - DAY, to: now + DAY },
      width = 368,
      gap = (34 / (width - 28)) * (view.to - view.from);
    tasks.forEach((item, index) => {
      item["omnigent.ai:sourceDisplay"]!.lastActivity = new Date(
        view.from + (index === 2 ? gap : 0),
      ).toISOString();
    });
    const group = mapGroups(tasks, [{ id: "example", name: "Example", source: null }], {})[0];
    const scene = mapScene(
      [{ ...group, top: 0, height: 74, nodes: [], header: true, collapsed: true }],
      "time",
      view,
      width,
      now,
      "phase",
      [],
      new Map(),
      {},
      () => "",
    );
    expect(scene.nodes.map((node) => node.tasks.map((item) => item.uid))).toEqual([
      ["first", "same-moment"],
      ["separate"],
    ]);
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

describe("route view on real-sized rows", () => {
  const open = (uid: string, extra: Partial<BoardTask> = {}): BoardTask => ({
    uid,
    title: uid,
    progress: "in-process",
    "omnigent.ai:source": "example",
    ...extra,
  });
  it("runs only cards a session works on; other open cards are open and idle", () => {
    expect(routeState(open("idle"))).toBe("held");
    expect(
      routeState(open("span", { "omnigent.ai:workedBy": [{ session: "s1", from: "2026-11-06" }] })),
    ).toBe("running");
    expect(
      routeState(
        open("ended", {
          "omnigent.ai:workedBy": [{ session: "s1", from: "2026-11-05", to: "2026-11-06" }],
        }),
      ),
    ).toBe("held");
  });
  it("fits a crowded row into its room, keeping urgent stops and the next queued card", () => {
    const tasks = [
      ...Array.from({ length: 30 }, (_, index) => task(`done-${index}`)),
      task("run-a", "running"),
      task("user-a", "user"),
      ...Array.from({ length: 10 }, (_, index) => task(`next-${index}`, "queued")),
    ];
    const stops = orderStops(tasks, 9);
    const singles = stops
      .filter((stop) => stop.tasks.length === 1)
      .map((stop) => stop.tasks[0].uid);
    expect(stops.length).toBeLessThanOrEqual(9);
    expect(singles).toEqual(expect.arrayContaining(["run-a", "user-a", "next-0", "done-29"]));
    expect(stops.flatMap((stop) => stop.tasks)).toEqual(tasks);
  });
  it("runs a card the source calls running only while live work backs it", () => {
    const shown = (extra: Partial<BoardTask>) =>
      routeState(
        open("supplied", {
          "omnigent.ai:sourceDisplay": { version: 1, state: "running", sessions: [] },
          ...extra,
        }),
      );
    expect(shown({})).toBe("held");
    expect(
      shown({ "omnigent.ai:workedBy": [{ session: "s1", from: "2026-11-05", to: "2026-11-06" }] }),
    ).toBe("held");
    expect(shown({ "omnigent.ai:workedBy": [{ session: "s1", from: "2026-11-06" }] })).toBe(
      "running",
    );
    expect(routeState(task("waiting", "user"))).toBe("user");
    expect(routeState(task("broken", "failed"))).toBe("failed");
  });
  it("never folds a card that needs the user into a capsule", () => {
    const live = { "omnigent.ai:workedBy": [{ session: "s1", from: "2026-11-06" }] },
      waiting = { "omnigent.ai:waiting": { on: "user" } };
    const row = [
      open("held0"),
      open("running1", live),
      open("held2"),
      open("user3", waiting),
      open("held4"),
      open("held5"),
      open("held6"),
    ];
    const single = (stops: ReturnType<typeof orderStops>, uid: string) =>
      stops.some((stop) => stop.tasks.length === 1 && stop.tasks[0].uid === uid);
    expect(single(orderStops(row, 3), "user3")).toBe(true);
    const crowded = [
      ...Array.from({ length: 4 }, (_, index) => open(`run${index}`, live)),
      open("user", waiting),
      open("held"),
    ];
    expect(single(orderStops(crowded, 5, true), "user")).toBe(true);
    const busy = [
      ...Array.from({ length: 12 }, (_, index) => open(`run${index}`, live)),
      open("user-a", waiting),
      open("user-b", waiting),
    ].map((item) => ({ ...item, "omnigent.ai:group": { id: "alpha" } }));
    const group = mapGroups(busy, [{ id: "example", name: "Example", source: null }], {}, now)[0];
    const scene = mapScene(
      [{ ...group, top: 0, height: 136, nodes: [] }],
      "time",
      initialWindow(now),
      700,
      now,
      "station",
      [],
      new Map(),
      {},
      () => "",
    );
    for (const uid of ["user-a", "user-b"])
      expect(scene.nodes.some((node) => node.tasks.length === 1 && node.tasks[0].uid === uid)).toBe(
        true,
      );
    expect(scene.nodes.some((node) => node.tasks.length > 1)).toBe(true);
  });
  it("wraps crowded waiting-on-you stations onto more rows, all on the map and apart", () => {
    const waiting = { "omnigent.ai:waiting": { on: "user" } };
    const users = (count: number) =>
      Array.from({ length: count }, (_, index) => ({
        ...open(`user-${index}`, waiting),
        "omnigent.ai:group": { id: "alpha" },
      }));
    const below = { ...open("below"), "omnigent.ai:group": { id: "beta" } };
    const cases: [number, "order" | "time", number, boolean][] = [
      [6, "time", 390, false],
      [14, "time", 700, false],
      [9, "time", 700, true],
      [40, "order", 700, false],
      [12, "order", 700, true],
    ];
    for (const [count, mode, width, collapsed] of cases) {
      const groups = mapGroups(
          [...users(count), below],
          [{ id: "example", name: "Example", source: null }],
          {},
          now,
        ),
        alpha = groups.find((group) => group.tasks.some((item) => item.uid === "user-0"))!,
        beta = groups.find((group) => group !== alpha)!;
      const lanes: MapLane[] = [
        { ...alpha, top: 0, height: 80, nodes: [], collapsed },
        { ...beta, top: 80, height: 80, nodes: [] },
      ];
      const scene = mapScene(
        lanes,
        mode,
        { from: now - DAY, to: now + DAY },
        width,
        now,
        "station",
        [],
        new Map(),
        {},
        () => "",
      );
      const shown = scene.nodes.filter((node) => node.lane === alpha.id);
      const label = `${count} ${mode} ${width}${collapsed ? " collapsed" : ""}`;
      expect(
        shown.filter((node) => node.tasks.length === 1),
        label,
      ).toHaveLength(count);
      for (const node of shown) {
        expect(node.x, label).toBeGreaterThanOrEqual(width <= 620 ? 14 : 208);
        expect(node.x, label).toBeLessThanOrEqual(width - 14);
        expect(node.y, label).toBeGreaterThan(lanes[0].top);
        expect(node.y, label).toBeLessThan(lanes[0].top + lanes[0].height);
        for (const other of shown)
          if (other !== node)
            expect(Math.hypot(node.x - other.x, node.y - other.y), label).toBeGreaterThanOrEqual(
              36,
            );
      }
      expect(lanes[1].top, label).toBe(lanes[0].top + lanes[0].height);
    }
  });
  it("folds urgent cards that do not fit instead of overlapping them", () => {
    const tasks = Array.from({ length: 20 }, (_, index) => task(`run-${index}`, "running"));
    expect(orderStops(tasks, 6).length).toBeLessThanOrEqual(6);
    expect(orderStops(tasks, 6, true).length).toBeLessThanOrEqual(6);
  });
});
