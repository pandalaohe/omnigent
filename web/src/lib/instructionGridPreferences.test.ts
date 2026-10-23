import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import {
  normalizeInstructionGridPreferences,
  readInstructionGridPreferences,
  useInstructionGridPreferences,
  writeInstructionGridPreferences,
} from "./instructionGridPreferences";

const { queuePatch } = vi.hoisted(() => ({ queuePatch: vi.fn() }));
vi.mock("./userPreferencesSync", () => ({ queueUserPreferencePatch: queuePatch }));

beforeEach(() => {
  localStorage.clear();
  queuePatch.mockReset();
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

it("normalizes junk, duplicate ids and malformed lists without changing order", () => {
  for (const value of [null, false, [], "junk", 42]) {
    expect(normalizeInstructionGridPreferences(value)).toEqual({ hosts: [], agents: [] });
  }
  expect(
    normalizeInstructionGridPreferences({ hosts: ["h2", "", 3, null, "h1", "h2"], agents: {} }),
  ).toEqual({ hosts: ["h2", "h1"], agents: [] });
});

it("caps each list and rejects ids over the character limit", () => {
  const ids = Array.from({ length: 300 }, (_, index) => `host-${index}`);
  const layout = normalizeInstructionGridPreferences({
    hosts: ids,
    agents: ["a".repeat(513), "b".repeat(512), ...ids],
  });
  expect(layout.hosts).toEqual(ids.slice(0, 256));
  expect(layout.agents).toEqual(["b".repeat(512), ...ids.slice(0, 255)]);
});

it("round trips and always queues both lists, including empty lists", () => {
  writeInstructionGridPreferences({ hosts: ["h1"], agents: ["codex"] });
  expect(readInstructionGridPreferences()).toEqual({ hosts: ["h1"], agents: ["codex"] });
  writeInstructionGridPreferences({ hosts: [], agents: ["codex", "claude"] });
  expect(queuePatch).toHaveBeenLastCalledWith("instruction_grid", {
    hosts: [],
    agents: ["codex", "claude"],
  });
  expect(readInstructionGridPreferences()).toEqual({ hosts: [], agents: ["codex", "claude"] });
});

it("rereads the local mirror on writes, hydration and storage events", () => {
  const { result } = renderHook(useInstructionGridPreferences);
  act(() => result.current[1]({ hosts: ["h1"], agents: [] }));
  expect(result.current[0]).toEqual({ hosts: ["h1"], agents: [] });
  localStorage.setItem(
    "omnigent:instruction-grid",
    JSON.stringify({ hosts: [], agents: ["codex"] }),
  );
  act(() => window.dispatchEvent(new Event("omnigent:instruction-grid-changed")));
  expect(result.current[0]).toEqual({ hosts: [], agents: ["codex"] });
  localStorage.setItem("omnigent:instruction-grid", "invalid");
  act(() => window.dispatchEvent(new Event("storage")));
  expect(result.current[0]).toEqual({ hosts: [], agents: [] });
});

it("never throws on storage access errors, including sync storage access", () => {
  vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
    throw new Error("Denied");
  });
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw new Error("Denied");
  });
  queuePatch.mockImplementation(() => {
    throw new Error("Denied");
  });
  expect(readInstructionGridPreferences()).toEqual({ hosts: [], agents: [] });
  expect(() => writeInstructionGridPreferences({ hosts: ["h1"], agents: [] })).not.toThrow();
});
