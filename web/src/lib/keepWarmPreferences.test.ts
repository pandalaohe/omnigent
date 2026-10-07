import { afterEach, describe, expect, it, vi } from "vitest";

const { queuePatchMock } = vi.hoisted(() => ({ queuePatchMock: vi.fn() }));
vi.mock("./userPreferencesSync", () => ({ queueUserPreferencePatch: queuePatchMock }));

import {
  clampHostOfflineArchiveSeconds,
  clampKeepWarmColdAfterSeconds,
  clampKeepWarmIntervalSeconds,
  clampKeepWarmMaxSeconds,
  KEEP_WARM_DEFAULTS,
  KEEP_WARM_STORAGE_KEY,
  readKeepWarmPreferences,
  resolveKeepWarmAgent,
  setKeepWarmAgent,
  writeKeepWarmPreferences,
} from "./keepWarmPreferences";
import { SESSION_COLLAB_STORAGE_KEY } from "./sessionCollabPreferences";

afterEach(() => {
  localStorage.clear();
  queuePatchMock.mockReset();
});

const NATIVE_CLAUDE = [{ id: "claude-native-ui", harness: "claude-native" }];

describe("keep-warm preferences", () => {
  it("defaults to no agent rows with a four-hour host archive delay", () => {
    expect(readKeepWarmPreferences()).toEqual(KEEP_WARM_DEFAULTS);
    expect(readKeepWarmPreferences()).toEqual({ agents: {}, hostOfflineArchiveSeconds: 14400 });
    expect(localStorage.getItem(KEEP_WARM_STORAGE_KEY)).toBeNull();
  });

  it("clamps intervals per family and the shared cap and archive bounds", () => {
    expect(clampKeepWarmIntervalSeconds("claude", 100)).toBe(300);
    expect(clampKeepWarmIntervalSeconds("claude", 99999)).toBe(3540);
    expect(clampKeepWarmIntervalSeconds("codex", 100)).toBe(300);
    expect(clampKeepWarmIntervalSeconds("codex", 99999)).toBe(1740);
    expect(clampKeepWarmMaxSeconds(1)).toBe(3600);
    expect(clampKeepWarmMaxSeconds(999999)).toBe(172800);
    expect(clampHostOfflineArchiveSeconds(0)).toBe(0);
    expect(clampHostOfflineArchiveSeconds(60)).toBe(3600);
    expect(clampHostOfflineArchiveSeconds(999999)).toBe(172800);
  });

  it("treats an absent agent row as off with the platform cold rule", () => {
    expect(resolveKeepWarmAgent(KEEP_WARM_DEFAULTS, "agent-x", "claude")).toEqual({
      main: false,
      child: false,
      intervalSeconds: 3300,
      maxSeconds: 14400,
      coldAfterSeconds: null,
    });
    expect(resolveKeepWarmAgent(KEEP_WARM_DEFAULTS, "agent-x", "codex").intervalSeconds).toBe(1500);
  });

  it("clamps cold-after bounds while 0 stays never cold", () => {
    expect(clampKeepWarmColdAfterSeconds(0)).toBe(0);
    expect(clampKeepWarmColdAfterSeconds(30)).toBe(60);
    expect(clampKeepWarmColdAfterSeconds(3600)).toBe(3600);
    expect(clampKeepWarmColdAfterSeconds(999999)).toBe(172800);
  });

  it("applies the family defaults when an agent row is first switched on", () => {
    const claude = setKeepWarmAgent(KEEP_WARM_DEFAULTS, "agent-claude", "claude", { main: true });
    expect(claude.agents["agent-claude"]).toEqual({
      main: true,
      child: false,
      intervalSeconds: 3300,
      maxSeconds: 14400,
    });
    const codex = setKeepWarmAgent(KEEP_WARM_DEFAULTS, "agent-codex", "codex", { child: true });
    expect(codex.agents["agent-codex"]).toEqual({
      main: false,
      child: true,
      intervalSeconds: 1500,
      maxSeconds: 14400,
    });
  });

  it("clamps edited intervals and caps into the family bounds", () => {
    const next = setKeepWarmAgent(KEEP_WARM_DEFAULTS, "agent-x", "claude", {
      main: true,
      intervalSeconds: 60,
      maxSeconds: 10,
    });
    expect(next.agents["agent-x"]).toMatchObject({ intervalSeconds: 300, maxSeconds: 3600 });
  });

  it("sets, keeps, and clears coldAfterSeconds", () => {
    const zero = setKeepWarmAgent(KEEP_WARM_DEFAULTS, "agent-x", "claude", {
      coldAfterSeconds: 0,
    });
    expect(zero.agents["agent-x"]).toEqual({
      main: false,
      child: false,
      intervalSeconds: 3300,
      maxSeconds: 14400,
      coldAfterSeconds: 0,
    });

    const clamped = setKeepWarmAgent(zero, "agent-x", "claude", { coldAfterSeconds: 30 });
    expect(clamped.agents["agent-x"].coldAfterSeconds).toBe(60);

    const unrelated = setKeepWarmAgent(clamped, "agent-x", "claude", { main: true });
    expect(unrelated.agents["agent-x"].coldAfterSeconds).toBe(60);

    const cleared = setKeepWarmAgent(unrelated, "agent-x", "claude", { coldAfterSeconds: null });
    expect(cleared.agents["agent-x"]).toEqual({
      main: true,
      child: false,
      intervalSeconds: 3300,
      maxSeconds: 14400,
    });
  });

  it("normalizes a stored namespace and clamps the archive delay", () => {
    localStorage.setItem(
      KEEP_WARM_STORAGE_KEY,
      JSON.stringify({
        agents: {
          good: { main: true, child: "yes", intervalSeconds: 5000, maxSeconds: 10 },
          garbage: "nope",
        },
        hostOfflineArchiveSeconds: 60,
        migratedFromLegacyAt: 123,
      }),
    );

    const stored = readKeepWarmPreferences();
    expect(stored).toEqual({
      agents: { good: { main: true, child: false, intervalSeconds: 5000, maxSeconds: 10 } },
      hostOfflineArchiveSeconds: 3600,
      migratedFromLegacyAt: 123,
    });
    expect(resolveKeepWarmAgent(stored, "good", "claude")).toEqual({
      main: true,
      child: false,
      intervalSeconds: 3540,
      maxSeconds: 3600,
      coldAfterSeconds: null,
    });
  });

  it("normalizes coldAfterSeconds values from storage", () => {
    localStorage.setItem(
      KEEP_WARM_STORAGE_KEY,
      JSON.stringify({
        agents: {
          zero: { coldAfterSeconds: 0 },
          low: { coldAfterSeconds: 30 },
          high: { coldAfterSeconds: 999999 },
          negative: { coldAfterSeconds: -1 },
          string: { coldAfterSeconds: "5" },
          float: { coldAfterSeconds: 1.5 },
          bool: { coldAfterSeconds: true },
        },
        hostOfflineArchiveSeconds: 14400,
      }),
    );

    const stored = readKeepWarmPreferences();
    expect(stored.agents.zero.coldAfterSeconds).toBe(0);
    expect(stored.agents.low.coldAfterSeconds).toBe(60);
    expect(stored.agents.high.coldAfterSeconds).toBe(172800);
    expect(resolveKeepWarmAgent(stored, "zero", "claude").coldAfterSeconds).toBe(0);
    for (const id of ["negative", "string", "float", "bool"]) {
      expect(stored.agents[id]).toEqual({ main: false, child: false, maxSeconds: 14400 });
    }
  });

  it("round-trips a cold-after value through storage", () => {
    const written = setKeepWarmAgent(KEEP_WARM_DEFAULTS, "agent-x", "claude", {
      coldAfterSeconds: 7200,
    });
    writeKeepWarmPreferences(written, NATIVE_CLAUDE);

    expect(readKeepWarmPreferences()).toEqual(written);
    expect(queuePatchMock).toHaveBeenLastCalledWith("keep_warm", written);
  });

  it("treats a non-object payload as defaults", () => {
    localStorage.setItem(KEEP_WARM_STORAGE_KEY, '"nope"');
    expect(readKeepWarmPreferences()).toEqual(KEEP_WARM_DEFAULTS);
  });

  it("writes the namespace and queues the patch", () => {
    const next = setKeepWarmAgent(KEEP_WARM_DEFAULTS, "agent-x", "claude", { main: true });
    writeKeepWarmPreferences(next, NATIVE_CLAUDE);

    expect(JSON.parse(localStorage.getItem(KEEP_WARM_STORAGE_KEY) ?? "null")).toEqual(next);
    expect(queuePatchMock).toHaveBeenLastCalledWith("keep_warm", next);
  });

  it("clears storage and queues null at defaults", () => {
    writeKeepWarmPreferences(KEEP_WARM_DEFAULTS);

    expect(localStorage.getItem(KEEP_WARM_STORAGE_KEY)).toBeNull();
    expect(queuePatchMock).toHaveBeenCalledWith("keep_warm", null);
  });

  it("mirrors a native child row into the legacy session_collab switch", () => {
    writeKeepWarmPreferences(
      {
        agents: {
          "claude-native-ui": {
            main: false,
            child: true,
            intervalSeconds: 3300,
            maxSeconds: 14400,
          },
          "claude-sdk": { main: true, child: true, intervalSeconds: 3300, maxSeconds: 14400 },
        },
        hostOfflineArchiveSeconds: 14400,
      },
      NATIVE_CLAUDE,
    );

    expect(
      JSON.parse(localStorage.getItem(SESSION_COLLAB_STORAGE_KEY) ?? "{}").childKeepWarmEnabled,
    ).toBe(true);
    expect(queuePatchMock).toHaveBeenCalledWith(
      "session_collab",
      expect.objectContaining({ childKeepWarmEnabled: true }),
    );
  });

  it("mirrors the legacy switch off when only non-native children are warm", () => {
    writeKeepWarmPreferences(
      {
        agents: {
          "claude-sdk": { main: false, child: true, intervalSeconds: 3300, maxSeconds: 14400 },
        },
        hostOfflineArchiveSeconds: 14400,
      },
      NATIVE_CLAUDE,
    );

    expect(
      JSON.parse(localStorage.getItem(SESSION_COLLAB_STORAGE_KEY) ?? "{}").childKeepWarmEnabled,
    ).toBe(false);
  });

  it("keeps the legacy mirror on an archive-delay save while the only child row is unresolved", () => {
    localStorage.setItem(
      SESSION_COLLAB_STORAGE_KEY,
      JSON.stringify({ childKeepWarmEnabled: true }),
    );

    writeKeepWarmPreferences(
      {
        agents: {
          "uploaded-agent": { main: false, child: true, intervalSeconds: 3300, maxSeconds: 14400 },
        },
        hostOfflineArchiveSeconds: 7200,
      },
      [{ id: "uploaded-agent", harness: null }],
    );

    expect(
      JSON.parse(localStorage.getItem(SESSION_COLLAB_STORAGE_KEY) ?? "{}").childKeepWarmEnabled,
    ).toBe(true);
  });

  it("keeps the legacy mirror when a stored child row is absent from the agent list", () => {
    localStorage.setItem(
      SESSION_COLLAB_STORAGE_KEY,
      JSON.stringify({ childKeepWarmEnabled: true }),
    );

    writeKeepWarmPreferences(
      {
        agents: {
          "removed-agent": { main: false, child: true, intervalSeconds: 3300, maxSeconds: 14400 },
        },
        hostOfflineArchiveSeconds: 7200,
      },
      NATIVE_CLAUDE,
    );

    expect(
      JSON.parse(localStorage.getItem(SESSION_COLLAB_STORAGE_KEY) ?? "{}").childKeepWarmEnabled,
    ).toBe(true);
  });

  it("preserves every existing legacy key while mirroring", () => {
    localStorage.setItem(
      SESSION_COLLAB_STORAGE_KEY,
      JSON.stringify({
        enabled: false,
        childKeepWarmEnabled: true,
        childKeepWarmClaudeIntervalSeconds: 3000,
      }),
    );

    writeKeepWarmPreferences(KEEP_WARM_DEFAULTS, NATIVE_CLAUDE);

    expect(JSON.parse(localStorage.getItem(SESSION_COLLAB_STORAGE_KEY) ?? "{}")).toEqual({
      enabled: false,
      childKeepWarmEnabled: false,
      childKeepWarmClaudeIntervalSeconds: 3000,
    });
  });
});
