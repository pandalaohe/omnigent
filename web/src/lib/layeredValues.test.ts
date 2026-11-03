import { describe, expect, it } from "vitest";

import fixture from "./__fixtures__/layeredCases.json";
import {
  crossingClashes,
  inheritedAt,
  levelsBelow,
  missingCellText,
  resolveField,
  type LayeredLevel,
  type LayeredOptions,
  type LayeredStore,
} from "./layeredValues";

interface FixtureCase {
  scenario: number;
  name: string;
  store: LayeredStore<string>;
  row: string;
  col: string;
  field: string;
  normalize?: "clamp-interval";
  canHold?: "numbers-only";
  expected: { value: unknown; stored: unknown; source: LayeredLevel | null };
  inherited?: { value: unknown; source: LayeredLevel | null };
  clashes: { field: string; winner: "row" | "col" }[];
}

const cases = (fixture as { cases: FixtureCase[] }).cases;

function optionsFor(testCase: FixtureCase): LayeredOptions<string> {
  return {
    canHold:
      testCase.canHold === "numbers-only"
        ? (_row, _field, value) => typeof value === "number"
        : undefined,
    normalize:
      testCase.normalize === "clamp-interval"
        ? (_row, _field, value) => (typeof value === "number" ? Math.max(300, value) : undefined)
        : undefined,
  };
}

describe("shared layered resolver cases", () => {
  it.each(cases)("$scenario: $name", (testCase) => {
    const options = optionsFor(testCase);
    expect(
      resolveField(testCase.store, testCase.row, testCase.col, testCase.field, options),
    ).toEqual(testCase.expected);
    expect(
      crossingClashes(testCase.store, testCase.row, testCase.col, [testCase.field], options),
    ).toEqual(testCase.clashes);
    if (testCase.inherited) {
      expect(
        inheritedAt(testCase.store, { level: "row", row: testCase.row }, testCase.field, options),
      ).toEqual(testCase.inherited);
    }
  });

  it("uses each marked host only in its own column", () => {
    const testCase = cases.find((item) => item.scenario === 8)!;
    expect(resolveField(testCase.store, "codex", "desktop", "maxSeconds").value).toBe(7200);
    expect(resolveField(testCase.store, "codex", "server", "maxSeconds").value).toBe(3600);
  });
});

describe("layered resolver boundaries", () => {
  it("never reads an agent-only field from a marked host or the corner", () => {
    const store: LayeredStore<"intervalSeconds"> = {
      corner: { intervalSeconds: 60 },
      rows: { codex: { intervalSeconds: 1200 } },
      cols: { desktop: { intervalSeconds: 120 } },
      cells: { desktop: { codex: {} } },
      hostWins: { desktop: true },
    };
    expect(
      resolveField(store, "codex", "desktop", "intervalSeconds", {
        colFields: [],
      }),
    ).toEqual({
      value: 1200,
      stored: 1200,
      source: { level: "row", row: "codex" },
    });
  });

  it("normalizes the built-in default when no level holds a value", () => {
    const store: LayeredStore<"maxSeconds"> = {
      corner: {},
      rows: {},
      cols: {},
      cells: {},
    };
    expect(
      resolveField(store, "codex", "desktop", "maxSeconds", {
        normalize: (_row, _field, value) => value ?? 14400,
      }),
    ).toEqual({ value: 14400, stored: undefined, source: null });
  });

  it("inherits a cell as though its own record were absent", () => {
    const store: LayeredStore<"state"> = {
      corner: { state: "On" },
      rows: { codex: {} },
      cols: { desktop: { state: "Off" } },
      cells: { desktop: { codex: { state: "On" } } },
    };
    expect(inheritedAt(store, { level: "cell", row: "codex", col: "desktop" }, "state")).toEqual({
      value: "Off",
      source: { level: "col", col: "desktop" },
    });
    expect(inheritedAt(store, { level: "col", col: "desktop" }, "state")).toEqual({
      value: "On",
      source: { level: "corner" },
    });
    expect(inheritedAt(store, { level: "corner" }, "state")).toEqual({
      value: undefined,
      source: null,
    });
  });

  it("does not report a clash when normalization makes both headers equal", () => {
    const store: LayeredStore<"intervalSeconds"> = {
      corner: {},
      rows: { codex: { intervalSeconds: 60 } },
      cols: { desktop: { intervalSeconds: 30 } },
      cells: { desktop: { codex: {} } },
    };
    expect(
      crossingClashes(store, "codex", "desktop", ["intervalSeconds"], {
        normalize: (_row, _field, value) => Math.max(300, Number(value)),
      }),
    ).toEqual([]);
  });

  it("lists every lower value and a Host-wins mark for Clear, without duplicates", () => {
    const store: LayeredStore<"state"> = {
      corner: { state: "On" },
      rows: { codex: { state: "On" }, empty: {} },
      cols: { desktop: { state: "Off" }, server: {} },
      cells: {
        desktop: { codex: { state: "On" }, empty: {} },
        server: { codex: { state: "Off" } },
      },
      hostWins: { server: true },
    };
    const below = levelsBelow(store, { level: "corner" });
    expect(below).toEqual([
      { level: "row", row: "codex" },
      { level: "col", col: "desktop" },
      { level: "col", col: "server" },
      { level: "cell", row: "codex", col: "desktop" },
      { level: "cell", row: "codex", col: "server" },
    ]);
    expect(new Set(below.map((level) => JSON.stringify(level))).size).toBe(below.length);
    expect(levelsBelow(store, { level: "row", row: "codex" })).toEqual([
      { level: "cell", row: "codex", col: "desktop" },
      { level: "cell", row: "codex", col: "server" },
    ]);
    expect(levelsBelow(store, { level: "col", col: "desktop" })).toEqual([
      { level: "cell", row: "codex", col: "desktop" },
    ]);
    expect(levelsBelow(store, { level: "cell", row: "codex", col: "desktop" })).toEqual([]);
  });

  it("distinguishes Native on Windows from an unavailable ACP agent", () => {
    expect(missingCellText("claude-native", "win32")).toBe("Not on Windows");
    expect(missingCellText("acp:hermes", "win32")).toBe("Not installed here");
    expect(missingCellText("claude-native", "darwin")).toBe("Not installed here");
    expect(missingCellText(null, "win32")).toBe("Not installed here");
  });
});
