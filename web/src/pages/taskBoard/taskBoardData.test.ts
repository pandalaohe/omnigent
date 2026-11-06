import { beforeEach, expect, it, vi } from "vitest";
import { readTaskBoard, type BoardData, type BoardTask, type TaskSource } from "./taskBoardData";

const fetch = vi.hoisted(() => vi.fn());
vi.mock("@/lib/identity", () => ({ authenticatedFetch: fetch }));
const task = (source: string, uid: string, extra: Partial<BoardTask> = {}): BoardTask => ({
  uid,
  progress: "in-process",
  "omnigent.ai:source": source,
  ...extra,
});
const source = (id: string, created = "2026-11-01T00:00:00Z"): TaskSource => ({
  id,
  name: id,
  source: null,
  created,
});
const before: BoardData = {
  tasks: [task("one", "keep"), task("one", "gone"), task("two", "old"), task("deleted", "drop")],
  sources: [source("one"), source("two"), source("deleted")],
  changedThrough: "2026-11-06T05:00:00Z",
};
beforeEach(() => {
  fetch.mockReset();
});
it("drains change pages, removes tombstones and refreshes a recreated source before advancing the watermark", async () => {
  fetch.mockImplementation(async (path: string) => {
    const url = new URL(path, "https://example.com");
    if (url.pathname.endsWith("/sources"))
      return Response.json({ sources: [source("one"), source("two", "2026-11-06T05:01:00Z")] });
    if (url.searchParams.has("source"))
      return Response.json({
        entries: url.searchParams.get("state") === "open" ? [task("two", "new")] : [],
      });
    if (url.searchParams.has("cursor"))
      return Response.json({
        entries: [task("one", "later")],
        "omnigent.ai:changedThrough": "2026-11-06T05:05:00Z",
      });
    expect(url.searchParams.get("changed_after")).toBe("2026-11-06T04:59:00.000Z");
    return Response.json({
      entries: [task("one", "gone", { "omnigent.ai:gone": { at: "2026-11-06T05:01:00Z" } })],
      "omnigent.ai:cursor": "page-two",
      "omnigent.ai:changedThrough": "2026-11-06T05:02:00Z",
    });
  });
  const result = await readTaskBoard(before, Date.parse("2026-11-06T05:03:00Z"));
  expect(result.tasks.map((item) => item.uid).sort()).toEqual(["keep", "later", "new"]);
  expect(result.changedThrough).toBe("2026-11-06T05:02:00Z");
  expect(before.tasks.map((item) => item.uid)).toEqual(["keep", "gone", "old", "drop"]);
  expect(fetch.mock.calls).toHaveLength(5);
});
it("preserves the published cache when a later page fails", async () => {
  const snapshot = structuredClone(before);
  fetch.mockImplementation(async (path: string) => {
    if (path.endsWith("/sources")) return Response.json({ sources: before.sources });
    if (new URL(path, "https://example.com").searchParams.has("cursor"))
      return Response.json({}, { status: 503 });
    return Response.json({
      entries: [task("one", "keep", { "omnigent.ai:gone": true })],
      "omnigent.ai:cursor": "next",
      "omnigent.ai:changedThrough": "2026-11-06T05:01:00Z",
    });
  });
  await expect(readTaskBoard(before)).rejects.toThrow("503");
  expect(before).toEqual(snapshot);
});
it("holds all open tasks and only the past year of closed tasks on the initial read", async () => {
  fetch.mockImplementation(async (path: string) => {
    const url = new URL(path, "https://example.com");
    if (url.pathname.endsWith("/sources")) return Response.json({ sources: [source("one")] });
    if (url.searchParams.get("state") === "closed") {
      expect(url.searchParams.get("closed_after")).toBe("2025-11-06T00:00:00.000Z");
      return Response.json({
        entries: [
          task("one", "recent", {
            progress: "completed",
            "omnigent.ai:closedSeen": "2026-11-05T00:00:00Z",
          }),
        ],
      });
    }
    return Response.json({
      entries: [task("one", "old-open", { created: "2020-01-01T00:00:00Z" })],
      updated: "2026-11-06T00:00:00Z",
    });
  });
  const result = await readTaskBoard(undefined, Date.parse("2026-11-06T00:00:00Z"));
  expect(result.tasks.map((item) => item.uid)).toEqual(["old-open", "recent"]);
  expect(result.changedThrough).toBe("2026-11-06T00:00:00Z");
});
it("refuses a repeated page cursor instead of polling forever", async () => {
  fetch.mockImplementation(async (path: string) =>
    Response.json(
      path.endsWith("/sources") ? { sources: [] } : { entries: [], "omnigent.ai:cursor": "same" },
    ),
  );
  await expect(readTaskBoard()).rejects.toThrow("Repeated task cursor");
});
