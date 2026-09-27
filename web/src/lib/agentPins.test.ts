import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { MAX_PINNED_AGENTS, resolvePinnedAgentIds, unpinAgent, useAgentPins } from "./agentPins";

const { queuePatchMock } = vi.hoisted(() => ({ queuePatchMock: vi.fn() }));
vi.mock("./userPreferencesSync", () => ({ queueUserPreferencePatch: queuePatchMock }));

const STORAGE_KEY = "omnigent:agent-pins";
const CHANGED_EVENT = "omnigent:agent-pins-changed";

const POLLY = { id: "ag_polly", name: "polly", builtin: true };
const DEBBY = { id: "ag_debby", name: "debby", builtin: true };
const REVIEWER = { id: "ca_reviewer", name: "Reviewer" };

afterEach(() => {
  cleanup();
  localStorage.clear();
  queuePatchMock.mockReset();
});

describe("resolvePinnedAgentIds", () => {
  it("defaults to the built-in Polly + Debby ids present, in that order", () => {
    expect(resolvePinnedAgentIds(null, [REVIEWER, DEBBY, POLLY])).toEqual(["ag_polly", "ag_debby"]);
    expect(resolvePinnedAgentIds(null, [POLLY])).toEqual(["ag_polly"]);
    expect(resolvePinnedAgentIds(null, [REVIEWER])).toEqual([]);
  });

  it("does not default a saved Agent that shares a built-in name", () => {
    const savedPolly = { id: "ca_polly", name: "polly", builtin: false };
    expect(resolvePinnedAgentIds(null, [savedPolly])).toEqual([]);
  });

  it("keeps stored ids that are present, in stored order", () => {
    expect(
      resolvePinnedAgentIds(["ca_reviewer", "ag_debby", "ag_gone"], [POLLY, DEBBY, REVIEWER]),
    ).toEqual(["ca_reviewer", "ag_debby"]);
  });

  it("treats an explicit empty list as no pins", () => {
    expect(resolvePinnedAgentIds([], [POLLY, DEBBY])).toEqual([]);
  });
});

describe("useAgentPins", () => {
  it.each([
    ["a non-object", '"nope"'],
    ["an empty object", "{}"],
    ["a non-array ids", '{"ids":"ag_polly"}'],
    ["invalid JSON", "not json"],
  ])("treats %s as never set", (_label, raw) => {
    localStorage.setItem(STORAGE_KEY, raw);
    const { result } = renderHook(() => useAgentPins());
    expect(result.current.storedIds).toBeNull();
  });

  it("keeps only strings, de-duplicates, and caps the stored list", () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ ids: ["a", 2, "a", "b", "c", "d"] }));
    const { result } = renderHook(() => useAgentPins());
    expect(result.current.storedIds).toEqual(["a", "b", "c"]);
    expect(result.current.storedIds).toHaveLength(MAX_PINNED_AGENTS);
  });

  it("stores, dispatches, and queues the sync patch on setPinnedIds", () => {
    const { result } = renderHook(() => useAgentPins());
    const changed = vi.fn();
    window.addEventListener(CHANGED_EVENT, changed);

    act(() => result.current.setPinnedIds(["ag_polly", "ca_reviewer"]));

    expect(JSON.parse(localStorage.getItem(STORAGE_KEY)!)).toEqual({
      ids: ["ag_polly", "ca_reviewer"],
    });
    expect(changed).toHaveBeenCalledTimes(1);
    expect(queuePatchMock).toHaveBeenCalledWith("agent_pins", {
      ids: ["ag_polly", "ca_reviewer"],
    });
    expect(result.current.storedIds).toEqual(["ag_polly", "ca_reviewer"]);
    window.removeEventListener(CHANGED_EVENT, changed);
  });

  it("never stores more than the cap", () => {
    const { result } = renderHook(() => useAgentPins());
    act(() => result.current.setPinnedIds(["a", "b", "c", "d"]));
    expect(JSON.parse(localStorage.getItem(STORAGE_KEY)!)).toEqual({ ids: ["a", "b", "c"] });
    expect(queuePatchMock).toHaveBeenCalledWith("agent_pins", { ids: ["a", "b", "c"] });
  });

  it("re-reads the list on the category event and the storage event", () => {
    const { result } = renderHook(() => useAgentPins());
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ ids: ["ag_debby"] }));
    act(() => window.dispatchEvent(new Event(CHANGED_EVENT)));
    expect(result.current.storedIds).toEqual(["ag_debby"]);

    localStorage.setItem(STORAGE_KEY, JSON.stringify({ ids: ["ag_polly"] }));
    act(() => window.dispatchEvent(new StorageEvent("storage", { key: STORAGE_KEY })));
    expect(result.current.storedIds).toEqual(["ag_polly"]);
  });
});

describe("unpinAgent", () => {
  it("drops the id from the latest stored list, not the one read at render", () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ ids: ["ag_polly", "ca_reviewer"] }));
    const { result } = renderHook(() => useAgentPins());

    localStorage.setItem(STORAGE_KEY, JSON.stringify({ ids: ["ag_debby", "ca_reviewer"] }));
    act(() => unpinAgent("ca_reviewer"));

    expect(JSON.parse(localStorage.getItem(STORAGE_KEY)!)).toEqual({ ids: ["ag_debby"] });
    expect(result.current.storedIds).toEqual(["ag_debby"]);
    expect(queuePatchMock).toHaveBeenCalledWith("agent_pins", { ids: ["ag_debby"] });
  });

  it("does not write when no list was ever stored", () => {
    renderHook(() => useAgentPins());
    act(() => unpinAgent("ca_reviewer"));
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
    expect(queuePatchMock).not.toHaveBeenCalled();
  });

  it("does not write when the id is not stored", () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ ids: ["ag_polly"] }));
    act(() => unpinAgent("ca_reviewer"));
    expect(JSON.parse(localStorage.getItem(STORAGE_KEY)!)).toEqual({ ids: ["ag_polly"] });
    expect(queuePatchMock).not.toHaveBeenCalled();
  });
});
