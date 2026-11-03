import { describe, expect, it } from "vitest";

import fixture from "./__fixtures__/keepWarmLayersCases.json";
import {
  clearKeepWarmBelow,
  clearKeepWarmLevel,
  dropKeepWarmCellEquals,
  keepWarmLayeredOptions,
  keepWarmLayeredStore,
  normalizeKeepWarmLayers,
  resolveKeepWarmRow,
  setKeepWarmLayer,
  type KeepWarmFamily,
  type KeepWarmLayers,
} from "./keepWarmPreferences";
import { crossingClashes } from "./layeredValues";

interface FixtureCase {
  scenario: number;
  name: string;
  layers: KeepWarmLayers;
  row: string;
  agentId?: string | null;
  harness: string;
  family: KeepWarmFamily;
  host: string;
  expected: {
    main: boolean;
    child: boolean;
    intervalSeconds: number;
    maxSeconds: number;
    coldAfterSeconds: number | null;
  };
  clashes: { field: string; winner: "row" | "col" }[];
}

const cases = (fixture as unknown as { cases: FixtureCase[] }).cases;

const CELL_FIELDS = ["main", "child", "intervalSeconds", "maxSeconds", "coldAfterSeconds"] as const;

describe("shared keep-warm layer cases", () => {
  it.each(cases)("$scenario: $name", (testCase) => {
    const layers = normalizeKeepWarmLayers(testCase.layers)!;
    const resolved = resolveKeepWarmRow(layers, testCase.row, testCase.host, testCase.family);
    expect({
      main: resolved.main,
      child: resolved.child,
      intervalSeconds: resolved.intervalSeconds,
      maxSeconds: resolved.maxSeconds,
      coldAfterSeconds: resolved.coldAfterSeconds,
    }).toEqual(testCase.expected);
    const store = keepWarmLayeredStore(layers);
    const options = keepWarmLayeredOptions(testCase.family);
    expect(crossingClashes(store, testCase.row, testCase.host, CELL_FIELDS, options)).toEqual(
      testCase.clashes,
    );
  });
});

describe("keep-warm layer editing", () => {
  it("drops a cell field equal to what it inherits", () => {
    const layers: KeepWarmLayers = {
      corner: { main: false },
      hosts: {},
      agents: {},
      cells: {},
      hostWins: {},
    };
    expect(
      dropKeepWarmCellEquals({ main: false, child: true }, layers, "codex", "", "codex"),
    ).toEqual({ child: true });
  });

  it("keeps a cell value that differs from its headers", () => {
    const layers: KeepWarmLayers = {
      corner: { main: false },
      hosts: { desktop: { main: false } },
      agents: {},
      cells: {},
      hostWins: {},
    };
    expect(dropKeepWarmCellEquals({ main: true }, layers, "codex", "desktop", "codex")).toEqual({
      main: true,
    });
  });

  it("clears a header with its column and leaves the corner", () => {
    const layers: KeepWarmLayers = {
      corner: { main: true },
      hosts: { desktop: { main: false } },
      agents: {},
      cells: { desktop: { codex: { maxSeconds: 7200 } } },
      hostWins: { desktop: true },
    };
    const cleared = clearKeepWarmLevel(layers, { level: "col", col: "desktop" });
    expect(cleared.corner).toEqual({ main: true });
    expect(cleared.hosts).toEqual({});
    expect(cleared.hostWins).toEqual({});
    expect(cleared.cells).toEqual(layers.cells);
  });

  it("clears every value below the corner", () => {
    const layers: KeepWarmLayers = {
      corner: { main: true },
      hosts: { desktop: { main: false } },
      agents: { codex: { child: true } },
      cells: { desktop: { codex: { maxSeconds: 7200 } } },
      hostWins: { desktop: true },
    };
    const cleared = clearKeepWarmBelow(layers, { level: "corner" });
    expect(cleared).toEqual({
      corner: { main: true },
      hosts: {},
      agents: {},
      cells: {},
      hostWins: {},
    });
  });

  it("sets a cell and removes an empty record", () => {
    const layers: KeepWarmLayers = {
      corner: {},
      hosts: {},
      agents: {},
      cells: {},
      hostWins: {},
    };
    const set = setKeepWarmLayer(
      layers,
      { level: "cell", row: "codex", col: "desktop" },
      {
        main: true,
      },
    );
    expect(set.cells.desktop.codex).toEqual({ main: true });
    const cleared = setKeepWarmLayer(set, { level: "cell", row: "codex", col: "desktop" }, {});
    expect(cleared.cells).toEqual({});
  });

  it("drops an interval a host column cannot hold at parse time", () => {
    const layers = normalizeKeepWarmLayers({
      corner: {},
      hosts: { desktop: { intervalSeconds: 120, main: false } },
      agents: {},
      cells: {},
      hostWins: {},
    })!;
    expect(layers.hosts.desktop).toEqual({ main: false });
  });
});
