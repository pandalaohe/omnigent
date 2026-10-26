import { afterEach, describe, expect, it, vi } from "vitest";

const { queuePatchMock, queueAckMock } = vi.hoisted(() => ({
  queuePatchMock: vi.fn(),
  queueAckMock: vi.fn(),
}));
vi.mock("./userPreferencesSync", () => ({
  queueUserPreferencePatch: queuePatchMock,
  queueAcknowledgedUserPreferencePatch: queueAckMock,
}));

import {
  KEEP_WARM_DEFAULTS,
  KEEP_WARM_STORAGE_KEY,
  clampHostOfflineArchiveSeconds,
  clampKeepWarmIntervalSeconds,
  keepWarmFamilyForHarness,
  readKeepWarmPreferences,
  resolveKeepWarmAgent,
  setKeepWarmAgent,
  setKeepWarmDefault,
  writeKeepWarmPreferences,
  normalizeKeepWarmPreferences,
  resolveKeepWarmCell,
  saveKeepWarmPreferences,
  turnOffKeepWarmCell,
} from "./keepWarmPreferences";
import { SESSION_COLLAB_STORAGE_KEY } from "./sessionCollabPreferences";

afterEach(() => {
  localStorage.clear();
  queuePatchMock.mockReset();
  queueAckMock.mockReset();
});

describe("keep-warm preferences", () => {
  it("uses one off default, no exception rows, and a four-hour archive delay", () => {
    expect(readKeepWarmPreferences()).toEqual(KEEP_WARM_DEFAULTS);
    expect(readKeepWarmPreferences().default).toEqual({
      main: false,
      child: false,
      maxSeconds: 14400,
    });
    expect(localStorage.getItem(KEEP_WARM_STORAGE_KEY)).toBeNull();
  });

  it("recognizes only declared keep-warm families", () => {
    const capabilities = {
      "plugin-cli": { keep_warm_family: "claude" },
      codex: { keep_warm_family: "codex" },
      pi: { keep_warm_family: "pi" },
      "claude-native": { keep_warm_family: null },
      other: { keep_warm_family: "unknown" },
    };
    expect(keepWarmFamilyForHarness("plugin-cli", capabilities)).toBe("claude");
    expect(keepWarmFamilyForHarness("codex", capabilities)).toBe("codex");
    expect(keepWarmFamilyForHarness("pi", capabilities)).toBe("pi");
    expect(keepWarmFamilyForHarness("claude-native", capabilities)).toBeNull();
    expect(keepWarmFamilyForHarness("other", capabilities)).toBeNull();
    expect(keepWarmFamilyForHarness("missing", capabilities)).toBeNull();
  });

  it("stores the shared default without an interval and resolves absent exceptions by family", () => {
    const next = setKeepWarmDefault(KEEP_WARM_DEFAULTS, {
      main: true,
      child: true,
      maxSeconds: 7200,
      coldAfterSeconds: 0,
    });
    expect(next.default).toEqual({
      main: true,
      child: true,
      maxSeconds: 7200,
      coldAfterSeconds: 0,
    });
    expect(resolveKeepWarmAgent(next, "missing", "claude")).toEqual({
      main: true,
      child: true,
      intervalSeconds: 3300,
      maxSeconds: 7200,
      coldAfterSeconds: 0,
    });
    expect(resolveKeepWarmAgent(next, "missing", "codex").intervalSeconds).toBe(1500);
    writeKeepWarmPreferences(next);
    expect(readKeepWarmPreferences()).toEqual(next);
    expect(queuePatchMock).toHaveBeenLastCalledWith("keep_warm", next);
  });

  it("keeps raw, ca_, and harness exception keys with per-family interval bounds", () => {
    let next = setKeepWarmAgent(KEEP_WARM_DEFAULTS, "raw-id", "claude", {
      main: true,
      intervalSeconds: 99999,
    });
    next = setKeepWarmAgent(next, "ca_custom", "codex", { child: true, intervalSeconds: 60 });
    next = setKeepWarmAgent(next, "harness:codex-native", "codex", { main: true });
    expect(next.agents["raw-id"].intervalSeconds).toBe(3540);
    expect(next.agents.ca_custom.intervalSeconds).toBe(300);
    expect(next.agents["harness:codex-native"].intervalSeconds).toBe(1500);
    writeKeepWarmPreferences(next);
    expect(readKeepWarmPreferences().agents).toEqual(next.agents);
    expect(clampKeepWarmIntervalSeconds("codex", 99999)).toBe(1740);
  });

  it("normalizes a legacy namespace without a default and preserves unsupported rows", () => {
    localStorage.setItem(
      KEEP_WARM_STORAGE_KEY,
      JSON.stringify({
        agents: {
          "old-product": { main: true, child: false, intervalSeconds: 5000, maxSeconds: 10 },
          "harness:unknown": { child: true, maxSeconds: 7200 },
          garbage: "invalid",
        },
        hostOfflineArchiveSeconds: 60,
        migratedFromLegacyAt: 123,
      }),
    );
    const stored = readKeepWarmPreferences();
    expect(stored.default).toEqual(KEEP_WARM_DEFAULTS.default);
    expect(Object.keys(stored.agents)).toEqual(["old-product", "harness:unknown"]);
    expect(stored.hostOfflineArchiveSeconds).toBe(3600);
    expect(stored.migratedFromLegacyAt).toBe(123);
    expect(resolveKeepWarmAgent(stored, "old-product", "claude").intervalSeconds).toBe(3540);
    expect(clampHostOfflineArchiveSeconds(0)).toBe(0);
  });

  it("clamps and clears cold-after on defaults and exceptions", () => {
    const withCold = setKeepWarmDefault(KEEP_WARM_DEFAULTS, { coldAfterSeconds: 30 });
    expect(withCold.default.coldAfterSeconds).toBe(60);
    const created = setKeepWarmAgent(withCold, "agent", "claude", {});
    expect(created.agents.agent).toEqual({
      main: false,
      child: false,
      intervalSeconds: 3300,
      maxSeconds: 14400,
      coldAfterSeconds: 60,
    });
    const exception = setKeepWarmAgent(created, "agent", "claude", { coldAfterSeconds: 0 });
    expect(resolveKeepWarmAgent(exception, "agent", "claude").coldAfterSeconds).toBe(0);
    const cleared = setKeepWarmAgent(exception, "agent", "claude", { coldAfterSeconds: null });
    expect(cleared.agents.agent.coldAfterSeconds).toBeUndefined();
    expect(resolveKeepWarmAgent(cleared, "agent", "claude").coldAfterSeconds).toBeNull();
    expect(resolveKeepWarmAgent(withCold, "missing", "claude").coldAfterSeconds).toBe(60);
    expect(
      setKeepWarmDefault(withCold, { coldAfterSeconds: null }).default.coldAfterSeconds,
    ).toBeUndefined();
  });

  it("mirrors child keep-warm for a native harness exception and the shared default", () => {
    const exception = setKeepWarmAgent(KEEP_WARM_DEFAULTS, "harness:claude-native", "claude", {
      child: true,
    });
    writeKeepWarmPreferences(exception);
    expect(
      JSON.parse(localStorage.getItem(SESSION_COLLAB_STORAGE_KEY) ?? "{}").childKeepWarmEnabled,
    ).toBe(true);
    writeKeepWarmPreferences(setKeepWarmDefault(KEEP_WARM_DEFAULTS, { child: true }));
    expect(
      JSON.parse(localStorage.getItem(SESSION_COLLAB_STORAGE_KEY) ?? "{}").childKeepWarmEnabled,
    ).toBe(true);
  });

  it("turns the legacy mirror off when both native harness exceptions override an on default", () => {
    let preferences = setKeepWarmDefault(KEEP_WARM_DEFAULTS, { child: true });
    preferences = setKeepWarmAgent(preferences, "harness:claude-native", "claude", {
      child: false,
    });
    preferences = setKeepWarmAgent(preferences, "harness:codex-native", "codex", { child: false });
    writeKeepWarmPreferences(preferences);
    expect(
      JSON.parse(localStorage.getItem(SESSION_COLLAB_STORAGE_KEY) ?? "{}").childKeepWarmEnabled,
    ).toBe(false);

    preferences = setKeepWarmAgent(preferences, "old-product", "claude", { child: true });
    writeKeepWarmPreferences(preferences, [{ id: "old-product", harness: "claude-native" }]);
    expect(
      JSON.parse(localStorage.getItem(SESSION_COLLAB_STORAGE_KEY) ?? "{}").childKeepWarmEnabled,
    ).toBe(true);
  });

  it("retains the legacy mirror when an enabled saved row cannot be classified", () => {
    localStorage.setItem(
      SESSION_COLLAB_STORAGE_KEY,
      JSON.stringify({ childKeepWarmEnabled: true, childKeepWarmClaudeIntervalSeconds: 3000 }),
    );
    writeKeepWarmPreferences(
      setKeepWarmAgent(KEEP_WARM_DEFAULTS, "removed-agent", "claude", { child: true }),
    );
    expect(JSON.parse(localStorage.getItem(SESSION_COLLAB_STORAGE_KEY) ?? "{}")).toMatchObject({
      childKeepWarmEnabled: true,
      childKeepWarmClaudeIntervalSeconds: 3000,
    });
    writeKeepWarmPreferences(KEEP_WARM_DEFAULTS);
    expect(JSON.parse(localStorage.getItem(SESSION_COLLAB_STORAGE_KEY) ?? "{}")).toMatchObject({
      childKeepWarmEnabled: false,
      childKeepWarmClaudeIntervalSeconds: 3000,
    });
    expect(queuePatchMock).toHaveBeenLastCalledWith("keep_warm", null);
  });
});

it("keeps empty cells, display columns, timestamps and explicit OFF masks", () => {
  const preferences = normalizeKeepWarmPreferences({
    default: { child: true },
    migratedToCellsAt: 123,
    cells: { "host-1": { direct: { main: false, child: false }, ca_empty: {} } },
    columns: ["direct", "harness:pi", "direct"],
  });
  writeKeepWarmPreferences(preferences);
  expect(readKeepWarmPreferences().cells).toEqual({
    "host-1": { direct: { main: false, child: false }, ca_empty: {} },
  });
  expect(readKeepWarmPreferences().columns).toEqual(["direct", "harness:pi"]);
  expect(readKeepWarmPreferences().migratedToCellsAt).toBe(123);
  expect(queuePatchMock).toHaveBeenLastCalledWith("keep_warm", {
    cells: preferences.cells,
    columns: ["direct", "harness:pi"],
    migratedToCellsAt: 123,
    hostOfflineArchiveSeconds: 14400,
  });
  writeKeepWarmPreferences({ ...KEEP_WARM_DEFAULTS, cells: {} });
  expect(JSON.parse(localStorage.getItem(KEEP_WARM_STORAGE_KEY)!)).toMatchObject({ cells: {} });
  expect(queuePatchMock).toHaveBeenLastCalledWith("keep_warm", {
    cells: {},
    hostOfflineArchiveSeconds: 14400,
  });
});

it.each([
  [{ "harness:claude-native": { child: true } }, [], false, true],
  [{ missing: { child: true } }, [], true, true],
  [{ missing: { child: true } }, [], false, false],
  [{ sdk: { child: true } }, [{ id: "sdk", harness: "pi" }], true, false],
  [{}, [], true, false],
])(
  "mirrors cells with proven-native, unresolved and known non-native identities",
  (rows, identities, previous, expected) => {
    localStorage.setItem(
      SESSION_COLLAB_STORAGE_KEY,
      JSON.stringify({ childKeepWarmEnabled: previous, retained: "yes" }),
    );
    writeKeepWarmPreferences(
      {
        ...KEEP_WARM_DEFAULTS,
        default: { main: true, child: true, maxSeconds: 14400 },
        agents: { legacy: { main: true, child: true, maxSeconds: 14400 } },
        cells: { "host-1": rows },
      },
      identities,
    );
    expect(JSON.parse(localStorage.getItem(SESSION_COLLAB_STORAGE_KEY)!)).toMatchObject({
      childKeepWarmEnabled: expected,
      retained: "yes",
    });
  },
);

it("turns a direct cell off without exposing its warm harness or changing another host", () => {
  const preferences = normalizeKeepWarmPreferences({
    cells: {
      "host-1": { "harness:codex": { main: true, child: true }, direct: { main: true } },
      "host-2": { direct: { child: true } },
    },
  });
  const off = turnOffKeepWarmCell(preferences, "host-1", "direct", "codex");
  expect(off.cells).toEqual({
    "host-1": {
      "harness:codex": { main: true, child: true },
      direct: { main: false, child: false },
    },
    "host-2": { direct: { child: true } },
  });
  expect(resolveKeepWarmCell(off, "host-1", "direct", "codex")).toEqual({
    main: false,
    child: false,
    maxSeconds: 14400,
  });
  expect(resolveKeepWarmCell(off, "new-host", "direct", "codex").main).toBe(false);
  expect(
    turnOffKeepWarmCell(preferences, "host-1", "removed-agent").cells?.["host-1"]["removed-agent"],
  ).toEqual({
    main: false,
    child: false,
  });
});

it("waits for the namespace acknowledgement before updating the legacy mirror", async () => {
  let resolve!: () => void;
  queueAckMock.mockReturnValue(
    new Promise<void>((done) => {
      resolve = done;
    }),
  );
  const save = saveKeepWarmPreferences(
    { ...KEEP_WARM_DEFAULTS, cells: { "": { "harness:claude-native": { child: true } } } },
    [],
  );
  expect(queuePatchMock).not.toHaveBeenCalled();
  resolve();
  await save;
  expect(JSON.parse(localStorage.getItem(SESSION_COLLAB_STORAGE_KEY)!)).toMatchObject({
    childKeepWarmEnabled: true,
  });
});
