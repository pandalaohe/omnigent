import { afterEach, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { TaskSourceDetails } from "./TaskSourceDetails";
import {
  displayCost,
  mergeDisplaySession,
  sessionObservation,
  type DisplaySession,
} from "./taskBoardDisplay";
import {
  accountDayAmounts,
  latestRelease,
  readPinnedSources,
  sourceHistory,
  type SourceCache,
  type SourcePin,
} from "./taskBoardSnapshotData";
import { readTaskBoard, type BoardData, type BoardTask } from "./taskBoardData";

const fetch = vi.hoisted(() => vi.fn());
vi.mock("@/lib/identity", () => ({ authenticatedFetch: fetch }));
afterEach(() => {
  cleanup();
  fetch.mockReset();
});
const stamp = "2026-11-12T04:00:00Z";
const pin: SourcePin = {
  sourceGeneration: "created-one",
  id: "first",
  asOf: stamp,
  phase: "published",
};
const source = { id: "example", name: "Example", source: null, snapshot: pin };
const task: BoardTask = { uid: "one", "omnigent.ai:source": "example", title: "Old" };
const cache: SourceCache = {
  timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone,
  pin,
  display: { version: 1, asOf: stamp, timeZone: "UTC" },
  tasks: [task],
  history: [],
  counts: [],
};
const previous: BoardData = { tasks: [task], sources: [source], sourceCaches: { example: cache } };

it("keeps a warm source complete during staging and gives a cold source updating state", async () => {
  const sources = [{ ...source, snapshot: { ...pin, phase: "staging" as const } }];
  const warm = await readPinnedSources(sources, previous);
  expect(warm.tasks).toEqual([task]);
  expect(warm.sourceStates).toEqual({ example: "updating" });
  expect((await readPinnedSources(sources)).tasks).toEqual([]);
  expect(fetch).not.toHaveBeenCalled();
});

it("discards partial pages and never keeps a cache across delete and recreate", async () => {
  fetch.mockImplementation(async (path: string) => {
    const url = new URL(path, "https://example.test");
    if (url.pathname.endsWith("display"))
      return Response.json({ snapshot: pin, display: cache.display });
    if (url.searchParams.has("cursor")) return Response.json({}, { status: 409 });
    return Response.json({ entries: [{ ...task, title: "New" }], "omnigent.ai:cursor": "later" });
  });
  const sources = [{ ...source, snapshot: { ...pin, id: "second" } }];
  expect((await readPinnedSources(sources, previous)).tasks).toEqual([task]);
  expect(previous.tasks[0].title).toBe("Old");
  const recreated = [
    { ...source, snapshot: { ...pin, id: "second", sourceGeneration: "created-two" } },
  ];
  expect((await readPinnedSources(recreated, previous)).tasks).toEqual([]);
});

it("pins every required part before publishing a new complete source", async () => {
  const paths: URL[] = [];
  const second = { ...pin, id: "second" };
  fetch.mockImplementation(async (path: string) => {
    const url = new URL(path, "https://example.test");
    paths.push(url);
    if (url.pathname.endsWith("sources"))
      return Response.json({ sources: [{ ...source, snapshot: second }] });
    expect(url.searchParams.get("snapshot")).toBe("second");
    expect(url.searchParams.get("source_generation")).toBe("created-one");
    if (url.pathname.endsWith("display") && !url.searchParams.has("part"))
      return Response.json({ snapshot: second, display: { ...cache.display } });
    if (url.pathname.endsWith("closed-counts")) return Response.json({ days: [] });
    if (url.pathname === "/v1/me/tasks" && url.searchParams.get("state") === "open")
      return Response.json({ entries: [{ ...task, title: "New" }] });
    return Response.json({ entries: [] });
  });
  const result = await readTaskBoard(previous);
  expect(result.tasks[0].title).toBe("New");
  expect(result.sourceCaches?.example.pin.id).toBe("second");
  expect(paths.some((url) => url.searchParams.get("part") === "accountDays")).toBe(true);
  expect(paths.filter((url) => url.pathname.endsWith("display"))).toHaveLength(6);
});

const session: DisplaySession = {
  id: "full-session",
  task: { source: "example", uid: "one" },
  role: "root",
  agentId: "agent",
  label: "Agent",
  title: "Work",
  status: "running",
  createdAt: stamp,
  lastActivityAt: "2026-11-12T03:30:00Z",
  runningSeconds: 2000,
  tool: {
    id: "tool",
    name: "Build",
    label: "Build",
    startedAt: "2026-11-12T03:59:00Z",
    limitSeconds: 120,
  },
  cost: { amount: 0.25, kind: "estimate" },
};

it("observes independent session runtime and replaces native tuples as a unit", () => {
  expect(sessionObservation(session, stamp).state).toBe("running");
  expect(
    sessionObservation({ ...session, tool: { ...session.tool!, limitSeconds: 30 } }, stamp).state,
  ).toBe("stale");
  expect(sessionObservation({ ...session, need: "Review" }, stamp).state).toBe("waiting");
  const idle = mergeDisplaySession(session, {
    id: "full-session",
    status: "idle",
    updated_at: Date.parse(stamp) / 1000,
  });
  expect(idle.runningSeconds).toBeUndefined();
  expect(idle.tool).toBeUndefined();
  expect(idle.cost).toEqual(session.cost);
  expect(sessionObservation(idle, stamp)).toMatchObject({
    state: "idle",
    workEnd: stamp,
    costEnd: stamp,
  });
  expect(mergeDisplaySession(session, { id: "full", status: "idle" })).toBe(session);
  expect(
    displayCost({
      version: 1,
      sessions: [session],
      intervals: [
        {
          id: "i",
          from: stamp,
          to: stamp,
          agentId: "a",
          origin: "event",
          cost: { amount: 9, kind: "actual" },
        },
      ],
    }),
  ).toEqual({ amount: 0.25, kind: "estimate", unpriced: 0 });
  expect(displayCost({ version: 1 })).toEqual({ amount: null, kind: "actual", unpriced: 0 });
});

it("accounts use explicit references and authorities and releases use typed dates", () => {
  const accounts = [
    {
      ref: { namespace: "example", id: "a" },
      label: "Same",
      authoritySource: "example",
      timeZone: "UTC",
    },
    {
      ref: { namespace: "example", id: "b" },
      label: "Same",
      authoritySource: "example",
      timeZone: "UTC",
    },
  ];
  const days = accounts.map((account, i) => ({
    id: String(i),
    account: account.ref,
    date: "2026-11-12",
    amount: i + 1,
    kind: "estimate" as const,
    sourceLabel: "Reported",
  }));
  const original = {
    ...cache,
    display: {
      ...cache.display,
      accounts,
      accountDays: days,
      releases: [
        { id: "b", name: "Second", publishedAt: stamp, source: "example" },
        { id: "a", name: "First", publishedAt: stamp, source: "example" },
      ],
    },
  };
  expect([...accountDayAmounts({ example: original }).values()]).toEqual([1, 2]);
  const conflict = {
    ...original,
    display: { ...original.display, accounts: [{ ...accounts[0], authoritySource: "other" }] },
  };
  expect([...accountDayAmounts({ example: original, other: conflict }).values()]).toContain(null);
  expect(latestRelease({ example: original })?.name).toBe("First");
  expect(latestRelease({})).toBeUndefined();
});

it("renders source strings literally, preserves order and restricts link schemes", () => {
  const { container, rerender } = render(
    <TaskSourceDetails
      display={{
        version: 1,
        badges: [
          { id: "first", label: "<b>literal</b>" },
          { id: "second", label: "Second" },
        ],
        rows: [{ id: "r", label: "Owner", value: "<script>text</script>" }],
        links: [
          { id: "safe", label: "Source", href: "https://example.test/task" },
          { id: "bad", label: "Unsafe", href: "javascript:alert(1)" },
        ],
      }}
    />,
  );
  expect(screen.getByText("<b>literal</b>")).toBeTruthy();
  expect(container.querySelector("script")).toBeNull();
  expect(container.querySelectorAll("a")).toHaveLength(1);
  expect(screen.getByRole("link").getAttribute("rel")).toBe("noopener noreferrer");
  expect([...container.querySelectorAll("li")].slice(0, 2).map((item) => item.textContent)).toEqual(
    ["<b>literal</b>", "Second"],
  );
  rerender(<TaskSourceDetails display={{ version: 1 }} />);
  expect(container.textContent).toBe("");
});

it("merges source history in global reverse time and opaque-id order", () => {
  const event = { id: "one", source: "a", uid: "one", kind: "state", time: stamp };
  const caches = {
    a: { ...cache, history: [event] },
    b: {
      ...cache,
      history: [
        { ...event, source: "b", time: "2026-11-12T04:00:00.0000001Z" },
        { ...event, source: "b" },
      ],
    },
  };
  expect(sourceHistory(caches).map((row) => [row.source, row.time])).toEqual([
    ["b", "2026-11-12T04:00:00.0000001Z"],
    ["b", stamp],
    ["a", stamp],
  ]);
  expect(
    latestRelease({
      a: {
        ...cache,
        display: {
          ...cache.display,
          releases: [
            { id: "old", name: "Old", publishedAt: stamp, source: "a" },
            { id: "new", name: "New", publishedAt: "2026-11-12T04:00:00.0000001Z", source: "a" },
          ],
        },
      },
    })?.name,
  ).toBe("New");
});

it("pins local closed-count reads and refreshes the cache when the zone changes", async () => {
  const zone = vi.spyOn(Date.prototype, "getTimezoneOffset").mockReturnValue(-480);
  const paths: URL[] = [];
  fetch.mockImplementation(async (path: string) => {
    const url = new URL(path, "https://example.test");
    paths.push(url);
    if (url.pathname.endsWith("display") && !url.searchParams.has("part"))
      return Response.json({ snapshot: pin, display: { ...cache.display } });
    if (url.pathname.endsWith("closed-counts")) {
      expect(url.searchParams.get("offset")).toBe("480");
      expect(url.searchParams.get("snapshot")).toBe("first");
      return Response.json({ days: [] });
    }
    return Response.json({ entries: [] });
  });
  try {
    await readPinnedSources([source], {
      ...previous,
      sourceCaches: { example: { ...cache, timeZone: "different-zone" } },
    });
    expect(paths.some((url) => url.pathname.endsWith("closed-counts"))).toBe(true);
  } finally {
    zone.mockRestore();
  }
});
