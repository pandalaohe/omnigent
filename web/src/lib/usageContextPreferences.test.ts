import { beforeEach, describe, expect, it, vi } from "vitest";

it("retains shown model labels as display preferences alongside the board", () => {
  const next = normalizeUsageContextPreferences({
    shownModels: { plan: "My model", bad: 2 },
    board: { density: "compact" },
  });
  expect(next.shownModels).toEqual({ plan: "My model" });
  writeUsageContextPreferences(next);
  expect(readUsageContextPreferences().shownModels).toEqual({ plan: "My model" });
});

import { queueUserPreferencePatch } from "./userPreferencesSync";

vi.mock("./userPreferencesSync", () => ({ queueUserPreferencePatch: vi.fn() }));

import {
  DEFAULT_USAGE_CONTEXT_PREFERENCES,
  readUsageContextPreferences,
  providerUsageLimitsForSource,
  resolveUsageContext,
  usageContextLevelKey,
  usageContextLevelLabel,
  usageContextRecordFromKey,
  usageContextSettingsHref,
  usageContextSourceFromKey,
  usageContextSourceKey,
  addUsageContextEntry,
  reportedUsageContext,
  seedUsageContextList,
  setUsageContextLevel,
  usageContextKeysOfEntry,
  usageContextRecordedEntries,
  usageContextRows,
  writeReportedContext,
  writeUsageContextPreferences,
  writeLastProviderUsageLimits,
  normalizeUsageContextPreferences,
  isDefaultUsageBoardPreferences,
  patchUsageBoardPreferences,
  type UsageContextPreferences,
} from "./usageContextPreferences";

function resolveUsageContextLimits(
  preferences: UsageContextPreferences,
  sourceKey: string,
  contextWindow: number | null,
  autoCompactTokenLimit: number | null,
) {
  const result = resolveUsageContext(preferences, sourceKey, {
    contextWindow,
    autoCompactTokenLimit,
  });
  return {
    contextWindow: result.contextWindow.value,
    autoCompactTokenLimit: result.autoCompactTokenLimit,
  };
}

beforeEach(() => {
  localStorage.clear();
  vi.mocked(queueUserPreferencePatch).mockClear();
});

describe("usage/context preferences", () => {
  const source = usageContextSourceKey({
    hostId: "host-a",
    agentName: "codex",
    harness: "codex",
    model: "gpt-5.6",
  });

  it("round-trips the four-field source used for saved profiles", () => {
    expect(usageContextSourceFromKey(source)).toEqual({
      hostId: "host-a",
      agentName: "codex",
      harness: "codex",
      model: "gpt-5.6",
    });
    expect(usageContextSourceFromKey("not-json")).toBeNull();
  });

  it("follows reported Host and model values by default", () => {
    expect(readUsageContextPreferences()).toEqual(DEFAULT_USAGE_CONTEXT_PREFERENCES);
    expect(
      resolveUsageContextLimits(DEFAULT_USAGE_CONTEXT_PREFERENCES, source, 258_400, 240_000),
    ).toEqual({ contextWindow: 258_400, autoCompactTokenLimit: 240_000 });
  });

  it("inherits the host × agent cell for another exact model", () => {
    const preferences = {
      ...DEFAULT_USAGE_CONTEXT_PREFERENCES,
      overrides: {
        '["host-a","codex","codex","","cell"]': {
          contextWindowTokens: 400_000,
          autoCompactBufferTokens: null,
        },
      },
    };
    expect(resolveUsageContextLimits(preferences, source, 258_400, 240_000).contextWindow).toBe(
      400_000,
    );
  });

  it("recomputes the compact budget from the effective cell total and reported buffer", () => {
    const preferences = {
      ...DEFAULT_USAGE_CONTEXT_PREFERENCES,
      overrides: {
        [usageContextLevelKey(usageContextSourceFromKey(source)!, "cell")]: {
          contextWindowTokens: 400_000,
          autoCompactBufferTokens: null,
        },
      },
    };
    expect(
      resolveUsageContext(preferences, source, {
        contextWindow: 258_400,
        autoCompactTokenLimit: 240_000,
      }),
    ).toEqual({
      contextWindow: { value: 400_000, level: "cell" },
      buffer: { mode: "tokens", value: 18_400, level: "reported" },
      autoCompactTokenLimit: 381_600,
      ignored: [],
    });
  });

  it.each([18_400, 10_000])(
    "rejects a reported buffer that meets or exceeds the effective context %i",
    (contextWindowTokens) => {
      const preferences = {
        ...DEFAULT_USAGE_CONTEXT_PREFERENCES,
        overrides: { [source]: { contextWindowTokens, autoCompactBufferTokens: null } },
      };
      const resolved = resolveUsageContext(preferences, source, {
        contextWindow: 258_400,
        autoCompactTokenLimit: 240_000,
      });
      expect(resolved.buffer).toEqual({ mode: "tokens", value: null, level: "reported" });
      expect(resolved.autoCompactTokenLimit).toBeNull();
    },
  );

  it("uses a manual context total and Compact buffer when supplied", () => {
    setUsageContextLevel(
      {
        version: 5,
        board: DEFAULT_USAGE_CONTEXT_PREFERENCES.board,
        showProviderUsageLimits: false,
        overrides: {},
        lastProviderUsageLimits: {},
      },
      source,
      {
        contextWindowTokens: 330_000,
        autoCompactBufferTokens: 33_000,
      },
    );
    const preferences = readUsageContextPreferences();
    expect(preferences.showProviderUsageLimits).toBe(false);
    expect(resolveUsageContextLimits(preferences, source, 258_400, 240_000)).toEqual({
      contextWindow: 330_000,
      autoCompactTokenLimit: 297_000,
    });
  });

  it("uses a saved percentage as the compact denominator and retains zero", () => {
    const preferences = {
      ...DEFAULT_USAGE_CONTEXT_PREFERENCES,
      overrides: {
        [source]: {
          contextWindowTokens: 100_000,
          autoCompactBufferTokens: 15_000,
          autoCompactBufferPercent: 20,
          compactBufferMode: "percent" as const,
        },
      },
    };
    writeUsageContextPreferences(preferences);
    const saved = readUsageContextPreferences();
    expect(saved.overrides[source]).toEqual(preferences.overrides[source]);
    expect(queueUserPreferencePatch).toHaveBeenCalledWith("usage_context", saved);
    expect(resolveUsageContextLimits(saved, source, 90_000, 72_000)).toEqual({
      contextWindow: 100_000,
      autoCompactTokenLimit: 80_000,
    });
    setUsageContextLevel(saved, source, {
      ...saved.overrides[source],
      autoCompactBufferPercent: 10,
    });
    expect(
      resolveUsageContextLimits(readUsageContextPreferences(), source, 90_000, 72_000)
        .autoCompactTokenLimit,
    ).toBe(90_000);
    setUsageContextLevel(readUsageContextPreferences(), source, {
      ...readUsageContextPreferences().overrides[source],
      autoCompactBufferPercent: 0,
      autoCompactBufferTokens: 0,
    });
    expect(readUsageContextPreferences().overrides[source]).toMatchObject({
      autoCompactBufferPercent: 0,
      autoCompactBufferTokens: 0,
    });
    expect(
      resolveUsageContextLimits(readUsageContextPreferences(), source, 90_000, 72_000)
        .autoCompactTokenLimit,
    ).toBe(100_000);
  });

  it("migrates a token-only source without changing its compact behavior", () => {
    localStorage.setItem(
      "omnigent:usage-context-preferences",
      JSON.stringify({
        version: 5,
        board: DEFAULT_USAGE_CONTEXT_PREFERENCES.board,
        showProviderUsageLimits: true,
        overrides: { [source]: { contextWindowTokens: 100_000, autoCompactBufferTokens: 20_000 } },
        lastProviderUsageLimits: {},
      }),
    );
    const preferences = readUsageContextPreferences();
    expect(preferences.overrides[source]).toEqual({
      contextWindowTokens: 100_000,
      autoCompactBufferTokens: 20_000,
    });
    expect(resolveUsageContextLimits(preferences, source, 90_000, 70_000)).toEqual({
      contextWindow: 100_000,
      autoCompactTokenLimit: 80_000,
    });
  });

  it("drops invalid percentages without erasing a valid token buffer", () => {
    localStorage.setItem(
      "omnigent:usage-context-preferences",
      JSON.stringify({
        version: 5,
        board: DEFAULT_USAGE_CONTEXT_PREFERENCES.board,
        overrides: {
          [source]: {
            contextWindowTokens: 100_000,
            autoCompactBufferTokens: 20_000,
            autoCompactBufferPercent: 100,
            compactBufferMode: "tokens",
          },
        },
      }),
    );
    expect(readUsageContextPreferences().overrides[source]).toEqual({
      contextWindowTokens: 100_000,
      autoCompactBufferTokens: 20_000,
      compactBufferMode: "tokens",
    });
  });

  it("does not leak a manual override into a different Host or model", () => {
    writeUsageContextPreferences({
      version: 5,
      board: DEFAULT_USAGE_CONTEXT_PREFERENCES.board,
      showProviderUsageLimits: false,
      overrides: {
        [source]: { contextWindowTokens: 330_000, autoCompactBufferTokens: 33_000 },
      },
      lastProviderUsageLimits: {},
    });
    const preferences = readUsageContextPreferences();
    const other = usageContextSourceKey({
      hostId: "host-b",
      agentName: "codex",
      harness: "codex",
      model: "gpt-5.6-mini",
    });
    expect(resolveUsageContextLimits(preferences, other, 128_000, 115_000)).toEqual({
      contextWindow: 128_000,
      autoCompactTokenLimit: 115_000,
    });
  });

  it("rejects invalid manual values and retains safe defaults", () => {
    localStorage.setItem(
      "omnigent:usage-context-preferences",
      JSON.stringify({
        version: 2,
        overrides: {
          [source]: { contextWindowTokens: -1, autoCompactBufferTokens: -5 },
        },
      }),
    );
    expect(readUsageContextPreferences()).toEqual(DEFAULT_USAGE_CONTEXT_PREFERENCES);
  });

  it("migrates a legacy Compact percentage into a token buffer", () => {
    localStorage.setItem(
      "omnigent:usage-context-preferences",
      JSON.stringify({
        version: 4,
        overrides: {
          [source]: { contextWindowTokens: 390_000, autoCompactThresholdPercent: 92.5 },
        },
      }),
    );
    expect(readUsageContextPreferences().overrides[source]).toEqual({
      contextWindowTokens: 390_000,
      autoCompactBufferTokens: 29_250,
    });
  });

  it("drops a legacy Compact percentage without a context total", () => {
    localStorage.setItem(
      "omnigent:usage-context-preferences",
      JSON.stringify({
        version: 4,
        overrides: {
          [source]: { contextWindowTokens: null, autoCompactThresholdPercent: 90 },
        },
      }),
    );
    expect(readUsageContextPreferences().overrides).toEqual({});
  });

  it("recomputes from the reported buffer when the saved buffer meets the context total", () => {
    writeUsageContextPreferences({
      version: 5,
      board: DEFAULT_USAGE_CONTEXT_PREFERENCES.board,
      showProviderUsageLimits: true,
      overrides: {
        [source]: { contextWindowTokens: 30_000, autoCompactBufferTokens: 33_000 },
      },
      lastProviderUsageLimits: {},
    });
    const preferences = readUsageContextPreferences();
    expect(resolveUsageContextLimits(preferences, source, 258_400, 240_000)).toEqual({
      contextWindow: 30_000,
      autoCompactTokenLimit: 11_600,
    });
  });

  it("retains provider usage only for the exact Host, agent, harness, and model", () => {
    const snapshot = {
      provider: "Codex",
      scope: "Codex",
      capturedAt: 1_900_000_000,
      windows: [{ label: "5h", ariaLabel: "5 hour", usedPercent: 11 }],
    };
    writeLastProviderUsageLimits(DEFAULT_USAGE_CONTEXT_PREFERENCES, source, snapshot);
    const preferences = readUsageContextPreferences();
    expect(providerUsageLimitsForSource(preferences, source)).toEqual(snapshot);

    const claudeSource = usageContextSourceKey({
      hostId: "host-a",
      agentName: "claude",
      harness: "claude-native",
      model: "opus",
    });
    expect(providerUsageLimitsForSource(preferences, claudeSource)).toBeNull();
  });

  it("refreshes an unchanged provider reading locally without syncing it again", () => {
    const first = {
      provider: "Codex",
      scope: "Codex",
      capturedAt: 1_900_000_000,
      windows: [{ label: "5h", ariaLabel: "5 hour", usedPercent: 11 }],
    };
    writeLastProviderUsageLimits(DEFAULT_USAGE_CONTEXT_PREFERENCES, source, first);
    expect(queueUserPreferencePatch).toHaveBeenCalledTimes(1);

    const preferences = readUsageContextPreferences();
    writeLastProviderUsageLimits(preferences, source, {
      ...first,
      capturedAt: first.capturedAt + 60_000,
    });

    expect(providerUsageLimitsForSource(readUsageContextPreferences(), source)?.capturedAt).toBe(
      first.capturedAt + 60_000,
    );
    expect(queueUserPreferencePatch).toHaveBeenCalledTimes(1);
  });

  it("does not sync provider changes that render identically", () => {
    const first = {
      provider: "Claude",
      scope: "Account",
      capturedAt: 1_900_000_000,
      windows: [
        {
          label: "5h",
          ariaLabel: "5 hour",
          usedPercent: 11.1,
          durationMinutes: 300,
          resetsAt: 1_900_010_000,
        },
      ],
    };
    writeLastProviderUsageLimits(DEFAULT_USAGE_CONTEXT_PREFERENCES, source, first);
    writeLastProviderUsageLimits(readUsageContextPreferences(), source, {
      ...first,
      capturedAt: first.capturedAt + 60_000,
      windows: [
        {
          ...first.windows[0]!,
          usedPercent: 11.4,
          resetsAt: 1_900_020_000,
        },
      ],
    });

    expect(queueUserPreferencePatch).toHaveBeenCalledTimes(1);
    expect(providerUsageLimitsForSource(readUsageContextPreferences(), source)?.capturedAt).toBe(
      first.capturedAt + 60_000,
    );
  });

  it("resolves each field independently and labels the level actually used", () => {
    const identity = usageContextSourceFromKey(source)!;
    const column = usageContextLevelKey(identity, "column");
    const cell = usageContextLevelKey(identity, "cell");
    const preferences = {
      ...DEFAULT_USAGE_CONTEXT_PREFERENCES,
      overrides: {
        [column]: { contextWindowTokens: 400_000, autoCompactBufferTokens: 50_000 },
        [cell]: { contextWindowTokens: null, autoCompactBufferTokens: 20_000 },
        [source]: { contextWindowTokens: 300_000, autoCompactBufferTokens: null },
      },
    };
    const reported = { contextWindow: 128_000, autoCompactTokenLimit: 100_000 };
    expect(resolveUsageContext(preferences, source, reported)).toEqual({
      contextWindow: { value: 300_000, level: "model" },
      buffer: { mode: "tokens", value: 20_000, level: "cell" },
      autoCompactTokenLimit: 280_000,
      ignored: [],
    });
    const other = usageContextSourceKey({ ...identity, hostId: "host-b", model: "other" });
    expect(resolveUsageContext(preferences, other, reported).contextWindow).toEqual({
      value: 400_000,
      level: "column",
    });
    expect(providerUsageLimitsForSource(preferences, other)).toBeNull();
  });

  it("ignores invalid fixed buffers at each level and reports their true fallback", () => {
    const identity = usageContextSourceFromKey(source)!;
    const cell = usageContextLevelKey(identity, "cell");
    const column = usageContextLevelKey(identity, "column");
    const preferences = {
      ...DEFAULT_USAGE_CONTEXT_PREFERENCES,
      overrides: {
        [source]: { contextWindowTokens: 100_000, autoCompactBufferTokens: 100_000 },
        [cell]: { contextWindowTokens: null, autoCompactBufferTokens: 150_000 },
        [column]: { contextWindowTokens: 400_000, autoCompactBufferTokens: 25_000 },
      },
    };
    const result = resolveUsageContext(preferences, source, {
      contextWindow: 200_000,
      autoCompactTokenLimit: 190_000,
    });
    expect(result.buffer).toEqual({ mode: "tokens", value: 25_000, level: "column" });
    expect(result.autoCompactTokenLimit).toBe(75_000);
    expect(result.ignored).toEqual([
      { level: "model", reason: "buffer ≥ context" },
      { level: "cell", reason: "buffer ≥ context" },
    ]);
    preferences.overrides[column].autoCompactBufferTokens = 150_000;
    const fallback = resolveUsageContext(preferences, source, {
      contextWindow: 200_000,
      autoCompactTokenLimit: 190_000,
    });
    expect(fallback.buffer).toEqual({ mode: "tokens", value: 10_000, level: "reported" });
    expect(fallback.autoCompactTokenLimit).toBe(90_000);
  });

  it("treats buffer mode and its value as one inherited unit, including zero percent", () => {
    const identity = usageContextSourceFromKey(source)!;
    const cell = usageContextLevelKey(identity, "cell");
    const preferences: UsageContextPreferences = {
      ...DEFAULT_USAGE_CONTEXT_PREFERENCES,
      overrides: {
        [source]: {
          contextWindowTokens: 100_000,
          autoCompactBufferTokens: 50_000,
          compactBufferMode: "percent",
        },
        [cell]: { contextWindowTokens: null, autoCompactBufferTokens: 20_000 },
      },
    };
    const reported = { contextWindow: 90_000, autoCompactTokenLimit: 70_000 };
    expect(resolveUsageContext(preferences, source, reported).buffer.level).toBe("cell");
    preferences.overrides[source]!.autoCompactBufferPercent = 0;
    expect(resolveUsageContext(preferences, source, reported).autoCompactTokenLimit).toBe(100_000);
  });

  it("keeps literal * models exact and preserves unknown levels through all writers", () => {
    const identity = { ...usageContextSourceFromKey(source)!, model: "*" };
    const exact = usageContextSourceKey(identity);
    const unknown = JSON.stringify([
      identity.hostId,
      identity.agentName,
      identity.harness,
      "",
      "future",
    ]);
    expect(usageContextLevelKey(identity, "cell")).toBe('["host-a","codex","codex","","cell"]');
    expect(usageContextSourceFromKey(usageContextLevelKey(identity, "cell"))).toBeNull();
    expect(usageContextRecordFromKey(unknown)).toBeNull();
    writeUsageContextPreferences({
      ...DEFAULT_USAGE_CONTEXT_PREFERENCES,
      overrides: {
        [exact]: { contextWindowTokens: 123_000, autoCompactBufferTokens: null },
        [unknown]: { contextWindowTokens: 999_000, autoCompactBufferTokens: null },
      },
    });
    const reported = { contextWindow: 90_000, autoCompactTokenLimit: null };
    expect(
      resolveUsageContext(readUsageContextPreferences(), exact, reported).contextWindow.value,
    ).toBe(123_000);
    expect(
      resolveUsageContext(readUsageContextPreferences(), source, reported).contextWindow.value,
    ).toBe(90_000);
    setUsageContextLevel(readUsageContextPreferences(), source, {
      contextWindowTokens: 100_000,
      autoCompactBufferTokens: null,
    });
    setUsageContextLevel(readUsageContextPreferences(), exact, null);
    expect(readUsageContextPreferences().version).toBe(6);
    expect(readUsageContextPreferences().overrides[unknown]?.contextWindowTokens).toBe(999_000);
  });

  it("preserves an unknown level's empty record and future fields while ignoring it", () => {
    const unknown = '["host-a","codex","codex","","future"]';
    const entry = { contextWindowTokens: null, autoCompactBufferTokens: null, futureBuffer: 123 };
    writeUsageContextPreferences({
      ...DEFAULT_USAGE_CONTEXT_PREFERENCES,
      overrides: { [unknown]: entry },
    });
    const saved = readUsageContextPreferences();
    expect(saved.overrides[unknown]).toEqual(entry);
    expect(
      resolveUsageContext(saved, source, { contextWindow: 100_000, autoCompactTokenLimit: 90_000 })
        .contextWindow,
    ).toEqual({ value: 100_000, level: "reported" });
    setUsageContextLevel(saved, source, {
      contextWindowTokens: 200_000,
      autoCompactBufferTokens: null,
    });
    expect(readUsageContextPreferences().overrides[unknown]).toEqual(entry);
  });

  it("keeps the Usage board switch and display fields, dropping a malformed value", () => {
    writeUsageContextPreferences({
      ...DEFAULT_USAGE_CONTEXT_PREFERENCES,
      board: {
        enabled: true,
        density: "mini",
        bands: [
          { id: "paid", name: "Paid", accounts: ["codex", "litellm"], open: false },
          { id: "paid", name: "Duplicate id", accounts: [], open: true },
        ],
        windows: { codex: "5h", broken: 7 as unknown as string },
        renewals: { codex: 1_800_000_000_000, nope: -1 },
        fees: { codex: 200, free: 0, invalid: -1, infinite: Infinity },
        hidden: ["grok"],
      },
    });
    const board = readUsageContextPreferences().board;
    expect(board.enabled).toBe(true);
    expect(board.density).toBe("mini");
    expect(board.bands).toEqual([
      { id: "paid", name: "Paid", accounts: ["codex", "litellm"], open: false },
    ]);
    expect(board.windows).toEqual({ codex: "5h" });
    expect(board.renewals).toEqual({ codex: 1_800_000_000_000 });
    expect(board.fees).toEqual({ codex: 200, free: 0 });
    expect(board.hidden).toEqual(["grok"]);
  });

  it("reads a stored envelope written before the board existed", () => {
    const legacy = {
      version: 6,
      showProviderUsageLimits: true,
      overrides: {},
      lastProviderUsageLimits: {},
    };
    expect(normalizeUsageContextPreferences(legacy).board).toEqual(
      DEFAULT_USAGE_CONTEXT_PREFERENCES.board,
    );
    // The board own defaults count as no preference, so the key is dropped
    // rather than written back.
    expect(isDefaultUsageBoardPreferences(DEFAULT_USAGE_CONTEXT_PREFERENCES.board)).toBe(true);
    expect(
      isDefaultUsageBoardPreferences({ ...DEFAULT_USAGE_CONTEXT_PREFERENCES.board, enabled: true }),
    ).toBe(false);
    expect(
      isDefaultUsageBoardPreferences({
        ...DEFAULT_USAGE_CONTEXT_PREFERENCES.board,
        fees: { codex: 200 },
      }),
    ).toBe(false);
  });

  it("patches one board field without disturbing the others", () => {
    writeUsageContextPreferences({
      ...DEFAULT_USAGE_CONTEXT_PREFERENCES,
      overrides: { [source]: { contextWindowTokens: 100_000, autoCompactBufferTokens: null } },
    });
    patchUsageBoardPreferences({ enabled: true });
    patchUsageBoardPreferences({ hidden: ["grok"] });
    const saved = readUsageContextPreferences();
    expect(saved.board.enabled).toBe(true);
    expect(saved.board.hidden).toEqual(["grok"]);
    // The context override is untouched, and the write carries the whole
    // envelope because the server stores this namespace as one value.
    expect(saved.overrides[source]).toEqual({
      contextWindowTokens: 100_000,
      autoCompactBufferTokens: null,
    });
  });
});

describe("the list of context rows", () => {
  const agent = { agentName: "claude", harness: "claude-native", model: "" };
  const opus = { ...agent, model: "claude-opus-5-5" };
  const codex = { agentName: "codex", harness: "codex", model: "" };
  const opusOnHost = usageContextSourceKey({ ...opus, hostId: "host-a" });

  beforeEach(() => {
    localStorage.clear();
    vi.mocked(queueUserPreferencePatch).mockClear();
  });

  it("drops malformed entries and repeats, and keeps the switch", () => {
    const saved = normalizeUsageContextPreferences({
      contextList: {
        severalOpen: true,
        entries: [agent, agent, { agentName: "", harness: "x" }, { harness: "x" }, 7, null, opus],
      },
    });
    expect(saved.contextList).toEqual({ entries: [agent, opus], severalOpen: true });
  });

  it("reads an envelope written before the list as not seeded", () => {
    expect(normalizeUsageContextPreferences({ overrides: {} }).contextList).toBeUndefined();
  });

  it("counts a seeded empty list as a stored choice", () => {
    writeUsageContextPreferences({
      ...DEFAULT_USAGE_CONTEXT_PREFERENCES,
      contextList: { entries: [], severalOpen: false },
    });
    expect(readUsageContextPreferences().contextList).toEqual({
      entries: [],
      severalOpen: false,
    });
  });

  it("seeds from what is in use and recorded, exactly once", () => {
    const preferences = {
      ...DEFAULT_USAGE_CONTEXT_PREFERENCES,
      overrides: {
        [usageContextLevelKey({ ...opus, hostId: "" }, "agent-model")]: {
          contextWindowTokens: 1,
          autoCompactBufferTokens: null,
        },
      },
    };
    seedUsageContextList(preferences, [agent, codex]);
    const seeded = readUsageContextPreferences();
    expect(seeded.contextList).toEqual({ entries: [agent, codex, opus], severalOpen: false });

    const removedByUser = {
      ...seeded,
      contextList: { entries: [agent], severalOpen: false },
    };
    writeUsageContextPreferences(removedByUser);
    seedUsageContextList(readUsageContextPreferences(), [agent, codex]);
    expect(readUsageContextPreferences().contextList?.entries).toEqual([agent]);
  });

  it("gives every saved value a row, including one the list does not name", () => {
    const preferences = {
      ...DEFAULT_USAGE_CONTEXT_PREFERENCES,
      contextList: { entries: [agent], severalOpen: false },
      overrides: {
        [opusOnHost]: { contextWindowTokens: 1, autoCompactBufferTokens: null },
        [usageContextLevelKey({ ...codex, hostId: "host-a" }, "cell")]: {
          contextWindowTokens: 2,
          autoCompactBufferTokens: null,
        },
        // A level with no agent, and a record with no value, name no row.
        [usageContextLevelKey({ ...codex, hostId: "host-a" }, "host")]: {
          contextWindowTokens: 3,
          autoCompactBufferTokens: null,
        },
        [usageContextLevelKey({ ...agent, model: "m", hostId: "" }, "agent-model")]: {
          contextWindowTokens: null,
          autoCompactBufferTokens: null,
        },
      },
    };
    expect(usageContextRows(preferences)).toEqual([agent, opus, codex]);
  });

  it("names every level an agent's row owns, and only its model's for a model row", () => {
    const keys = {
      column: usageContextLevelKey({ ...agent, hostId: "" }, "column"),
      cell: usageContextLevelKey({ ...agent, hostId: "host-a" }, "cell"),
      everywhere: usageContextLevelKey({ ...opus, hostId: "" }, "agent-model"),
      here: opusOnHost,
      other: usageContextLevelKey({ ...codex, hostId: "" }, "column"),
      computer: usageContextLevelKey({ ...agent, hostId: "host-a" }, "host"),
    };
    const preferences = {
      ...DEFAULT_USAGE_CONTEXT_PREFERENCES,
      overrides: Object.fromEntries(
        Object.values(keys).map((key) => [
          key,
          { contextWindowTokens: 1, autoCompactBufferTokens: null },
        ]),
      ),
    };
    expect(usageContextKeysOfEntry(preferences, agent).sort()).toEqual(
      [keys.column, keys.cell, keys.everywhere, keys.here].sort(),
    );
    expect(usageContextKeysOfEntry(preferences, opus).sort()).toEqual(
      [keys.everywhere, keys.here].sort(),
    );
  });

  it("shows a reported field only where every covered source agrees", () => {
    const report = (contextWindow: number, autoCompactTokenLimit: number | null) => ({
      contextWindow,
      autoCompactTokenLimit,
      capturedAt: 1,
    });
    const preferences = {
      ...DEFAULT_USAGE_CONTEXT_PREFERENCES,
      reportedContext: {
        [opusOnHost]: report(1_000_000, 967_000),
        [usageContextSourceKey({ ...opus, hostId: "host-b" })]: report(1_000_000, 500_000),
        [usageContextSourceKey({ ...agent, model: "small", hostId: "host-a" })]: report(
          200_000,
          167_000,
        ),
      },
    };
    expect(reportedUsageContext(preferences, { ...opus, hostId: "host-a" })).toEqual({
      contextWindow: 1_000_000,
      autoCompactTokenLimit: 967_000,
    });
    expect(reportedUsageContext(preferences, { ...opus, hostId: null })).toEqual({
      contextWindow: 1_000_000,
      autoCompactTokenLimit: null,
    });
    expect(reportedUsageContext(preferences, { ...agent, hostId: "host-a" })).toEqual({
      contextWindow: null,
      autoCompactTokenLimit: null,
    });
    expect(reportedUsageContext(preferences, { ...codex, hostId: null })).toEqual({
      contextWindow: null,
      autoCompactTokenLimit: null,
    });
  });

  it("writes a report only when a figure changes, and caps the record", () => {
    writeReportedContext(
      DEFAULT_USAGE_CONTEXT_PREFERENCES,
      opusOnHost,
      { contextWindow: 1_000_000, autoCompactTokenLimit: 550_000 },
      5,
    );
    expect(queueUserPreferencePatch).toHaveBeenCalledTimes(1);
    writeReportedContext(
      readUsageContextPreferences(),
      opusOnHost,
      { contextWindow: 1_000_000, autoCompactTokenLimit: 550_000 },
      6,
    );
    expect(queueUserPreferencePatch).toHaveBeenCalledTimes(1);
    expect(readUsageContextPreferences().reportedContext?.[opusOnHost]?.capturedAt).toBe(5);

    const many = Object.fromEntries(
      Array.from({ length: 70 }, (_, index) => [
        usageContextSourceKey({ ...opus, hostId: `h${index}` }),
        { contextWindow: 1, autoCompactTokenLimit: null, capturedAt: index + 1 },
      ]),
    );
    const capped = normalizeUsageContextPreferences({ reportedContext: many }).reportedContext!;
    expect(Object.keys(capped)).toHaveLength(64);
    expect(capped[usageContextSourceKey({ ...opus, hostId: "h69" })]).toBeDefined();
    expect(capped[usageContextSourceKey({ ...opus, hostId: "h0" })]).toBeUndefined();
  });

  it("offers what stored readings prove was used", () => {
    const preferences = {
      ...DEFAULT_USAGE_CONTEXT_PREFERENCES,
      reportedContext: {
        [opusOnHost]: { contextWindow: 1, autoCompactTokenLimit: null, capturedAt: 1 },
      },
    };
    expect(usageContextRecordedEntries(preferences)).toEqual([opus]);
  });

  it("adds an entry without repeating it", () => {
    addUsageContextEntry(DEFAULT_USAGE_CONTEXT_PREFERENCES, opus);
    addUsageContextEntry(readUsageContextPreferences(), opus);
    expect(readUsageContextPreferences().contextList?.entries).toEqual([opus]);
  });
});

describe("saved context levels", () => {
  const source = {
    hostId: "host-a",
    agentName: "claude",
    harness: "claude-native",
    model: "claude-opus-5-5",
  };
  const value = (contextWindowTokens: number) => ({
    contextWindowTokens,
    autoCompactBufferTokens: null,
  });
  const store = (overrides: Record<string, unknown>) =>
    ({
      ...DEFAULT_USAGE_CONTEXT_PREFERENCES,
      overrides,
    }) as unknown as UsageContextPreferences;

  it("round trips every level through its own key", () => {
    const levels = ["model", "agent-model", "cell", "column", "host", "all"] as const;
    const keys = levels.map((level) => usageContextLevelKey(source, level));
    expect(new Set(keys).size).toBe(levels.length);
    for (const [index, key] of keys.entries()) {
      const record = usageContextRecordFromKey(key);
      expect(record?.level).toBe(levels[index]);
      expect(record?.source.model).toBe(
        levels[index] === "model" || levels[index] === "agent-model" ? source.model : "",
      );
    }
    expect(usageContextRecordFromKey(usageContextLevelKey(source, "host"))?.source).toEqual({
      hostId: "host-a",
      agentName: "",
      harness: "",
      model: "",
    });
    expect(usageContextRecordFromKey(usageContextLevelKey(source, "all"))?.source).toEqual({
      hostId: "",
      agentName: "",
      harness: "",
      model: "",
    });
  });

  it("resolves this model, then the model everywhere, then each level below it", () => {
    const exact = usageContextSourceKey(source);
    const order = ["model", "agent-model", "cell", "column", "host", "all"] as const;
    const expected = [100_000, 200_000, 300_000, 400_000, 500_000, 600_000];
    const overrides: Record<string, unknown> = {};
    for (const [index, level] of order.entries())
      overrides[usageContextLevelKey(source, level)] = value(expected[index]!);
    for (const [index, level] of order.entries()) {
      const resolved = resolveUsageContext(store(overrides), exact, {
        contextWindow: 1_000,
        autoCompactTokenLimit: null,
      });
      expect([level, resolved.contextWindow.value, resolved.contextWindow.level]).toEqual([
        level,
        expected[index],
        level,
      ]);
      Reflect.deleteProperty(overrides, usageContextLevelKey(source, level));
    }
    expect(
      resolveUsageContext(store(overrides), exact, {
        contextWindow: 1_000,
        autoCompactTokenLimit: null,
      }).contextWindow,
    ).toEqual({ value: 1_000, level: "reported" });
  });

  it("applies the corner to a model that was never saved", () => {
    const preferences = store({
      [usageContextLevelKey({ ...source, model: "" }, "all")]: value(550_000),
    });
    const unseen = usageContextSourceKey({ ...source, model: "claude-opus-9" });
    expect(
      resolveUsageContext(preferences, unseen, {
        contextWindow: 200_000,
        autoCompactTokenLimit: null,
      }).contextWindow,
    ).toEqual({ value: 550_000, level: "all" });
  });

  it("keeps a model everywhere above the cell and below this computer's model", () => {
    const preferences = store({
      [usageContextLevelKey(source, "agent-model")]: value(200_000),
      [usageContextLevelKey(source, "cell")]: value(300_000),
    });
    expect(
      resolveUsageContext(preferences, usageContextSourceKey(source), {
        contextWindow: null,
        autoCompactTokenLimit: null,
      }).contextWindow,
    ).toEqual({ value: 200_000, level: "agent-model" });
  });

  it("builds the settings entry that carries the whole source", () => {
    const exact = usageContextSourceKey(source);
    const href = usageContextSettingsHref(exact);
    expect(href.startsWith("/settings/general?ctx=")).toBe(true);
    expect(href.endsWith("#context-usage")).toBe(true);
    const query = href.slice(href.indexOf("?"), href.indexOf("#"));
    expect(usageContextSourceFromKey(new URLSearchParams(query).get("ctx") ?? "")).toEqual(source);
  });

  it("labels each level without a dotted suffix or the word override", () => {
    const labels = { hostLabel: "Alpha", agentLabel: "Claude Code" };
    const levels = ["model", "agent-model", "cell", "column", "host", "all", "reported"] as const;
    const texts = levels.map((level) => usageContextLevelLabel(level, labels));
    expect(texts).toEqual([
      "this model",
      "Claude Code, this model on all computers",
      "Claude Code on Alpha",
      "Claude Code, all computers",
      "all agents on Alpha",
      "all agents, all computers",
      "reported",
    ]);
    for (const text of texts) {
      expect(text).not.toContain("·");
      expect(text).not.toContain("override");
    }
  });
});
