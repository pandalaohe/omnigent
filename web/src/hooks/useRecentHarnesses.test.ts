import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  RECENT_AGENTS_KEY,
  RECENT_SDK_KEY,
  useRecentHarnesses,
  useRecentIds,
} from "./useRecentHarnesses";

const KEY = "omnigent:recent-harnesses";

function stored(): unknown {
  const raw = localStorage.getItem(KEY);
  return raw === null ? null : JSON.parse(raw);
}

describe("useRecentHarnesses", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.restoreAllMocks();
  });

  it("starts empty and records a launched harness", () => {
    const { result } = renderHook(() => useRecentHarnesses());
    expect(result.current.recentHarnesses).toEqual([]);

    act(() => result.current.addRecentHarness("pi-native"));
    expect(result.current.recentHarnesses).toEqual(["pi-native"]);
    expect(stored()).toEqual(["pi-native"]);
  });

  it("updates another mounted consumer when a harness is recorded", () => {
    const writer = renderHook(() => useRecentHarnesses());
    const reader = renderHook(() => useRecentHarnesses());

    act(() => writer.result.current.addRecentHarness("pi-native"));
    expect(reader.result.current.recentHarnesses).toEqual(["pi-native"]);
  });

  it("moves a repeat launch to the front instead of duplicating it", () => {
    const { result } = renderHook(() => useRecentHarnesses());
    act(() => result.current.addRecentHarness("pi-native"));
    act(() => result.current.addRecentHarness("cursor-native"));
    act(() => result.current.addRecentHarness("pi-native"));
    expect(result.current.recentHarnesses).toEqual(["pi-native", "cursor-native"]);
  });

  it("caps the list at four entries, dropping the oldest", () => {
    const { result } = renderHook(() => useRecentHarnesses());
    for (const h of ["a", "b", "c", "d", "e"]) {
      act(() => result.current.addRecentHarness(h));
    }
    expect(result.current.recentHarnesses).toEqual(["e", "d", "c", "b"]);
  });

  it("skips the write when the harness is already newest", () => {
    const { result } = renderHook(() => useRecentHarnesses());
    act(() => result.current.addRecentHarness("pi-native"));
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    act(() => result.current.addRecentHarness("pi-native"));
    // Relaunching the same harness is the common case; re-storing an identical
    // list would only cost a wasted render.
    expect(setItem).not.toHaveBeenCalled();
    expect(result.current.recentHarnesses).toEqual(["pi-native"]);
  });

  it("ignores blank ids", () => {
    const { result } = renderHook(() => useRecentHarnesses());
    act(() => result.current.addRecentHarness("   "));
    expect(result.current.recentHarnesses).toEqual([]);
    expect(stored()).toBeNull();
  });

  it("reads through malformed stored values without throwing", () => {
    localStorage.setItem(KEY, "{not json");
    expect(renderHook(() => useRecentHarnesses()).result.current.recentHarnesses).toEqual([]);

    // Wrong shape (object, not array) and non-string members are both dropped.
    localStorage.setItem(KEY, JSON.stringify({ "pi-native": true }));
    expect(renderHook(() => useRecentHarnesses()).result.current.recentHarnesses).toEqual([]);

    localStorage.setItem(KEY, JSON.stringify(["pi-native", 7, null]));
    expect(renderHook(() => useRecentHarnesses()).result.current.recentHarnesses).toEqual([
      "pi-native",
    ]);
  });

  it("survives a storage write failure (quota / disabled)", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("QuotaExceededError");
    });
    const { result } = renderHook(() => useRecentHarnesses());
    // Non-fatal: the in-memory list still updates so the current session sees
    // the promotion; only persistence is lost.
    act(() => result.current.addRecentHarness("pi-native"));
    expect(result.current.recentHarnesses).toEqual([]);
  });
});

describe("useRecentIds (keyed)", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.restoreAllMocks();
  });

  it("keeps separate keys isolated, including their notifications", () => {
    const sdk = renderHook(() => useRecentIds(RECENT_SDK_KEY));
    const agents = renderHook(() => useRecentIds(RECENT_AGENTS_KEY));
    const native = renderHook(() => useRecentHarnesses());

    act(() => sdk.result.current.addRecentId("codex"));

    expect(sdk.result.current.recentIds).toEqual(["codex"]);
    expect(agents.result.current.recentIds).toEqual([]);
    expect(native.result.current.recentHarnesses).toEqual([]);
    expect(JSON.parse(localStorage.getItem(RECENT_SDK_KEY) ?? "[]")).toEqual(["codex"]);
    expect(localStorage.getItem(RECENT_AGENTS_KEY)).toBeNull();
    // The native key keeps its own storage spelling.
    act(() => native.result.current.addRecentHarness("codex-native"));
    expect(native.result.current.recentHarnesses).toEqual(["codex-native"]);
    expect(sdk.result.current.recentIds).toEqual(["codex"]);
  });

  it("de-duplicates, caps at four, and ignores a malformed stored value", () => {
    localStorage.setItem(RECENT_AGENTS_KEY, "{not json");
    const { result } = renderHook(() => useRecentIds(RECENT_AGENTS_KEY));
    expect(result.current.recentIds).toEqual([]);

    for (const id of ["a", "b", "c", "d", "e"]) {
      act(() => result.current.addRecentId(id));
    }
    expect(result.current.recentIds).toEqual(["e", "d", "c", "b"]);

    act(() => result.current.addRecentId("c"));
    expect(result.current.recentIds).toEqual(["c", "e", "d", "b"]);
  });

  it("reads non-string members out of a stored list instead of crashing", () => {
    localStorage.setItem(RECENT_SDK_KEY, JSON.stringify(["codex", 7, null]));
    const { result } = renderHook(() => useRecentIds(RECENT_SDK_KEY));
    expect(result.current.recentIds).toEqual(["codex"]);
  });
});
