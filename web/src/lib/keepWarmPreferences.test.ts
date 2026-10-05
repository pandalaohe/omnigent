import { afterEach, describe, expect, it, vi } from "vitest";

const { queuePatchMock } = vi.hoisted(() => ({ queuePatchMock: vi.fn() }));
vi.mock("./userPreferencesSync", () => ({ queueUserPreferencePatch: queuePatchMock }));

import {
  clampHostOfflineArchiveSeconds,
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

  it("treats an absent agent row as off", () => {
    expect(resolveKeepWarmAgent(KEEP_WARM_DEFAULTS, "agent-x", "claude")).toEqual({
      main: false,
      child: false,
      intervalSeconds: 3300,
      maxSeconds: 14400,
    });
    expect(resolveKeepWarmAgent(KEEP_WARM_DEFAULTS, "agent-x", "codex").intervalSeconds).toBe(1500);
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
    });
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
